"""Frozen in-process hook bus.

This module owns the **unified event bus**: one bus, two subscription kinds:

- :meth:`HookBus.on` — fire-and-forget, return ignored. Absorbs the legacy
  7-event ``HookBus`` (`turn.start`, `tool.pre_execute`, `request.pre`, …).
- :meth:`HookBus.transform` — return-replacing, chainable. Absorbs the legacy
  4-hook ``PluginBus`` (`before_llm`, `after_tool`, `on_message_out`, …).

Both share fail-soft behavior: a raising handler is logged and skipped — never
crashes the turn loop. ``PluginBus`` (:mod:`xu_brain.plugins`) registers its
loaded plugins onto a shared :class:`HookBus` instance.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HookHandle:
    """A reversible registration returned by :meth:`HookBus.on` / ``transform``."""

    _remove: Callable[[], None]

    def dispose(self) -> None:
        self._remove()


async def _maybe_await(result: Any) -> Any:
    if inspect.isawaitable(result):
        return await result
    return result


def _run_soon(result: Any) -> Any:
    """Schedule an awaitable returned by a sync-called plugin hook.

    Plugin ``activate`` / ``deactivate`` may be ``async def`` even though the
    host calls them from sync code (``__init__``, ``reload``). When a
    loop is running we hand the coroutine to it and return the task, so the
    caller can await it later (see ``PluginHost.ready``); with no loop we run it
    to completion immediately.
    """
    if not inspect.isawaitable(result):
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_maybe_await(result))
    return loop.create_task(_maybe_await(result))


class HookBus:
    """Unified in-process hook bus.

    ``on(event, handler)`` subscribes a fire-and-forget handler (return
    ignored); ``transform(point, handler)`` subscribes a return-replacing
    handler that receives ``apply``'s positional args and whose non-``None``
    return replaces the last arg. Both are fail-soft and reversible via the
    returned :class:`HookHandle`.

    Dispatch:

    - ``emit(event, *args)``  — fire the ``on`` handlers with ``*args``.
    - ``waterfall(event, payload)`` — legacy dict chain: each ``on`` handler
      receives the payload dict, a dict return replaces it, ``handled=True``
      short-circuits (kept for the governance ``tool.pre_execute`` hooks).
    - ``apply(point, *args)`` — chain the ``transform`` handlers; the final
      (possibly replaced) last arg is returned.
    """

    def __init__(self) -> None:
        self._listeners: dict[str, list[Callable[..., Any]]] = {}
        self._transforms: dict[str, list[Callable[..., Any]]] = {}

    # ------------------------------------------------------------------ #
    # registration
    # ------------------------------------------------------------------ #

    def on(self, event: str, handler: Callable[..., Any]) -> HookHandle:
        """Fire-and-forget subscription. Unknown event names are allowed (a
        forward-compatible plugin must never take the loop down)."""
        bucket = self._listeners.setdefault(event, [])
        bucket.append(handler)
        return HookHandle(self._remover(bucket, handler))

    def transform(self, point: str, handler: Callable[..., Any]) -> HookHandle:
        """Return-replacing subscription (chainable)."""
        bucket = self._transforms.setdefault(point, [])
        bucket.append(handler)
        return HookHandle(self._remover(bucket, handler))

    @staticmethod
    def _remover(
        bucket: list[Callable[..., Any]], handler: Callable[..., Any]
    ) -> Callable[[], None]:
        removed = False

        def remove() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            try:
                bucket.remove(handler)
            except ValueError:
                pass

        return remove

    # ------------------------------------------------------------------ #
    # dispatch
    # ------------------------------------------------------------------ #

    async def emit(self, event: str, *args: Any) -> None:
        for handler in tuple(self._listeners.get(event, ())):
            try:
                await _maybe_await(handler(*args))
            except Exception:  # noqa: BLE001 — fail-soft plugin isolation
                log.exception("hook %s listener failed", event)

    async def waterfall(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Legacy dict-chain: listeners receive ``payload``, may return a dict
        replacement, and ``handled=True`` short-circuits. Matches the old
        ``HookBus.waterfall`` exactly (governance hooks depend on it)."""
        current = payload
        for handler in tuple(self._listeners.get(event, ())):
            try:
                result = await _maybe_await(handler(current))
            except Exception:  # noqa: BLE001 — fail-soft plugin isolation
                log.exception("hook %s listener failed", event)
                continue
            if isinstance(result, dict):
                current = result
                if result.get("handled"):
                    break
        return current

    async def apply(self, point: str, *args: Any) -> Any:
        """Chain ``transform`` handlers over ``*args``. A non-``None`` return
        replaces the last arg; returns the final last arg (passthrough when the
        chain is empty or every handler returned ``None``)."""
        if not args:
            return None
        current = list(args)
        for handler in tuple(self._transforms.get(point, ())):
            try:
                out = await _maybe_await(handler(*current))
            except Exception:  # noqa: BLE001 — fail-soft plugin isolation
                log.exception("transform %s handler failed", point)
                continue
            if out is not None:
                current[-1] = out
        return current[-1]

    def subscribed(self, point: str) -> int:
        """Count of transform handlers on ``point`` (introspection aid)."""
        return len(self._transforms.get(point, ()))