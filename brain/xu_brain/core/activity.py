"""Process-wide activity log — the LOGS surface (contract/methods.md ``logs.list``).

Records everything the brain does — RPC calls, agent turns, tool runs, plugin
adapter events — into one bounded in-memory buffer, newest first. Credentials
and message payloads are excluded by the callers. Errors are recorded
explicitly so the shell's Logs view doubles as the error trail.

The buffer doubles as the Cordis-style "buffer exporter". A thin named-logger
facade (``get_logger``) gives ``logger('loop').info(...)`` ergonomics without
touching the shared surface: every emit fanned to the one global buffer.
"""
from __future__ import annotations

import re
import sys
import time
from collections import deque
from types import FrameType
from typing import Any

LOG_CAP = 1000
_LEVELS = frozenset({"debug", "info", "warn", "error"})
# printf-style specifiers we honour.
_SPEC = re.compile(r"%[sidfoO]")

_MAX_MSG = 160
_MAX_DETAIL = 240


def trunc(s: str, limit: int) -> str:
    return s if len(s) <= limit else s[: limit - 3] + "..."


def fmt(message: Any, *args: Any) -> str:
    """printf-style format with stray-arg coercion; never raises.

    ``%s %d %i %f %o %O`` are substituted positionally; an unconsumed specifier
    or a bare argument degrades to a plain string join.
    """
    msg = message if isinstance(message, str) else str(message)
    if not args:
        return msg
    it = iter(args)

    def _sub(m: re.Match[str]) -> str:
        try:
            v = next(it)
        except StopIteration:
            return m.group(0)
        kind = m.group(0)[-1]
        try:
            if kind in "di":
                return str(int(v))
            if kind == "f":
                return f"{float(v):g}"
            if kind in "oO":
                return repr(v)
            return str(v)
        except (TypeError, ValueError):
            return str(v)

    return _SPEC.sub(_sub, msg)


def _aggregate_children(e: BaseException) -> list[BaseException]:
    """Sub-exceptions of an aggregate, or ``[]``.

    ``.errors`` is a *method* on some libraries (pydantic's ``ValidationError``),
    so the attribute is only trusted when it is a real sequence of exceptions --
    calling or iterating it blindly crashed the error logger itself.
    """
    raw = getattr(e, "exceptions", None)  # stdlib ExceptionGroup
    if raw is None:
        raw = getattr(e, "errors", None)  # AggregateError-style
    if not isinstance(raw, list | tuple):
        return []
    return [c for c in raw if isinstance(c, BaseException)]


def _cause_chain(err: BaseException) -> list[str]:
    """Flatten an exception into display lines, following __cause__ and
    expanding AggregateError-style groups (so a batch of parallel failures
    shows every entry, not just the first)."""
    lines: list[str] = []
    seen: set[int] = set()
    e: BaseException | None = err
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        children = _aggregate_children(e)
        if children:
            for c in children:
                lines.extend(_cause_chain(c))
        else:
            text = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            lines.append(text)
        # prefer the explicit cause; fall back to the context chain.
        e = e.__cause__ if e.__cause__ else e.__context__
    return lines


def unpack(err: Any) -> tuple[str, str | None]:
    """Turn an error into a ``(message, detail)`` pair: headline plus the
    expanded cause chain as detail."""
    if not isinstance(err, BaseException):
        return str(err)[:_MAX_MSG], None
    lines = _cause_chain(err)
    detail = "\n".join(lines[1:]) if len(lines) > 1 else None
    return trunc(lines[0], _MAX_MSG), trunc(detail, _MAX_DETAIL) if detail else None


def _derive_name() -> str:
    """Best-effort logger name from the caller's module (falls back to 'brain')."""
    try:
        frame: FrameType | None = sys._getframe(2)
    except ValueError:  # pragma: no cover
        return "brain"
    if frame is None:
        return "brain"
    return (frame.f_globals.get("__name__", "") or "brain").rsplit(".", 1)[-1]


class ActivityLog:
    """Bounded in-memory ring buffer, newest first (the LOGS surface)."""

    def __init__(self, cap: int = LOG_CAP) -> None:
        self.cap = cap
        self._entries: deque[dict[str, Any]] = deque(maxlen=cap)
        self._seq = 0

    def record(
        self,
        level: str,
        source: str,
        message: str,
        detail: str | None = None,
        *,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one entry. ``payload`` (when given) is stored at full
        fidelity for the details modal — not truncated like ``message`` /
        ``detail``. Callers must sanitize credentials before passing it in.
        """
        entry: dict[str, Any] = {
            "id": self._seq,
            "ts": time.time(),
            "level": level if level in _LEVELS else "info",
            "source": source,
            "message": trunc(message, _MAX_MSG),
        }
        self._seq += 1
        if detail:
            entry["detail"] = trunc(detail, _MAX_DETAIL)
        if payload is not None:
            entry["payload"] = payload
        self._entries.append(entry)

    def entries(
        self,
        limit: int = 100,
        source: str | None = None,
        level: str | None = None,
    ) -> list[dict[str, Any]]:
        items = self._entries
        if source:
            items = deque(e for e in items if e["source"] == source)
        if level and level in _LEVELS:
            items = deque(e for e in items if e["level"] == level)
        return list(items)[-max(1, min(int(limit), self.cap)):][::-1]

    def clear(self) -> None:
        self._entries.clear()


class Named:
    """Cordis-style named logger: ``logger('loop').info('msg %s', x)``.

    Thin facade over the shared :data:`activity` buffer — no own storage.
    """

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def _emit(self, level: str, message: Any, args: tuple[Any, ...], detail: str | None) -> None:
        activity.record(level, self.name, fmt(message, *args), detail)

    def debug(self, message: Any, *args: Any, detail: str | None = None) -> None:
        self._emit("debug", message, args, detail)

    def info(self, message: Any, *args: Any, detail: str | None = None) -> None:
        self._emit("info", message, args, detail)

    def warn(self, message: Any, *args: Any, detail: str | None = None) -> None:
        self._emit("warn", message, args, detail)

    def error(self, message: Any, *args: Any, detail: str | None = None) -> None:
        # A bare exception argument (and no explicit detail) is unwrapped into
        # the detail field (Cordis-style), so the cause chain survives in the
        # error trail.
        if not detail and len(args) == 1 and isinstance(args[0], BaseException):
            _, inner = unpack(args[0])
            activity.record("error", self.name, fmt(message), inner)
            return
        if isinstance(detail, BaseException):
            _, detail = unpack(detail)
        self._emit("error", message, args, detail)

    def __call__(self, child: str) -> "Named":
        return Named(f"{self.name}/{child}")


def get_logger(name: str | None = None) -> Named:
    """Return a named logger; name is auto-derived from the caller module when
    omitted. ``logger('a')('b')`` yields the scoped ``a/b``. Additional
    comma-separated children append scopes."""
    return Named(name or _derive_name())


activity = ActivityLog()
# The shared global logger facade for module-level ``log.debug(...)`` style use.
log = get_logger()
