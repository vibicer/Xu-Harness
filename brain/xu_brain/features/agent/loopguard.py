"""Loop guard: notice when one turn keeps repeating itself, and say so.

An agent that is stuck calls the same tool with the same arguments — or
re-derives the same reasoning — round after round, each time hoping for a
different result. The guard watches the tool rounds of a single turn for that
pattern and, once a consecutive run of identical rounds reaches a threshold,
hands the turn a short reminder so the model changes its approach instead of
looping on. Advisory by design, never a block: what looks like a loop can be a
retry worth one more attempt, and whether it is stays the model's call.

What counts as a repeat:

* **tools** — a round's *set* of ``(tool, args)`` calls matches the previous
  round's exactly. Order and duplicates inside one round don't matter
  (parallel siblings arrive either way); reading a different file is progress,
  not a loop. A round made only of bookkeeping calls (``BOOKKEEPING_TOOLS``)
  neither advances nor starts a chain.
* **reasoning** — the round's thinking is near-identical to the *chain's first*
  round, where "near" is a token-overlap fingerprint (``_similar``). Comparing
  against the anchor, not just the previous round, is what keeps a healthy
  turn from drifting into a claimed "chain" one near-match at a time.

The reminder is a model-visible system note that rides the live turn's
message list until the turn ends — seen on every later request — is refreshed
rather than stacked (``LoopGuard.inject``), hoisted by compaction like any
other system row, and never persisted (``Agent._commit_history`` drops
non-checkpoint system rows). The same warning is also a ``guard`` timeline
chip so a reader of the transcript sees that the guard fired, not just the
model.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

# A single round is never a loop, and the escalation threshold must be bigger
# than the first warning or nothing would ever remind harder.
_MIN_THRESHOLD = 2
_MAX_THRESHOLD = 50
DEFAULT_THRESHOLDS = (3, 5)

# Reasoning-fingerprint exactness: normalization differences (casing,
# whitespace) never count, and a retelling of the same argument only counts
# as the same thought when the token overlap is at least this high.
_SIMILAR = 0.9
# Below this many tokens a fingerprint is too small for overlap math — only
# exact equality counts, so one-word turns never read as each other.
_MIN_TOKENS = 8

# Rounds made only of these calls are state bookkeeping, not work: they don't
# mean progress (a ``todo`` rewrite says nothing about moving the task) and
# they don't mean a loop either — so they neither advance nor start a chain.
BOOKKEEPING_TOOLS = frozenset({"todo"})

_PREFIX = "[loop guard] "


def _setting(config: Any, name: str, default: Any) -> Any:
    """Read one brain setting off a Config and tolerate stand-ins.

    A real :class:`~xu_brain.core.config.Config` exposes settings as methods;
    mocks carry plain values or nothing at all. ``default`` covers a mock with
    no such attribute — the feature stays on at the shipped trip points.
    """
    raw = getattr(config, name, None)
    if callable(raw):
        try:
            raw = raw()
        except Exception:  # noqa: BLE001 — a broken mock must not break a turn
            raw = None
    if raw is not None:
        return raw
    getter = getattr(config, "get", None)
    if callable(getter):
        try:
            raw = getter(name)
        except Exception:  # noqa: BLE001
            raw = None
    return default if raw is None else raw


def _clean_thresholds(raw: Any) -> list[int]:
    """Normalize thresholds into a sorted, deduped int list."""
    if isinstance(raw, str):
        raw = [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]
    if not isinstance(raw, (list, tuple)):
        raw = []
    out: list[int] = []
    for item in raw:
        try:
            n = int(item)
        except (TypeError, ValueError):
            continue
        if _MIN_THRESHOLD <= n <= _MAX_THRESHOLD and n not in out:
            out.append(n)
    out.sort()
    return out or list(DEFAULT_THRESHOLDS)


def _canonical(args: Any) -> str:
    """A stable string for arguments: dict key order must not matter."""
    if isinstance(args, dict):
        try:
            return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # exotic args degrade to repr
            return repr(args)
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return repr(args)


def _calls_key(calls: list[tuple[str, Any]]):
    """The identity of one tool round: distinct ``(tool, args)`` pairs, sorted.

    ``None`` when the round carried nothing comparable — a chain resets rather
    than counting a bookkeeping-only round as either progress or a repeat."""
    pairs = {(name, _canonical(args)) for name, args in calls if name not in BOOKKEEPING_TOOLS}
    return tuple(sorted(pairs)) or None


def _tokens(text: str) -> frozenset[str]:
    """Word fingerprint of one round's reasoning."""
    return frozenset(re.findall(r"[^\W_]+", text.lower()))


def _similar(a: frozenset[str], b: frozenset[str]) -> bool:
    """Whether two reasoning fingerprints are the same thought.

    Equality is always a match; anything shorter than ``_MIN_TOKENS`` must be
    equal, because on a few tokens the overlap metric is too coarse to trust.
    """
    if a == b:
        return True
    if not a or not b or len(a) < _MIN_TOKENS or len(b) < _MIN_TOKENS:
        return False
    return len(a & b) / max(1, len(a | b)) >= _SIMILAR


@dataclass(frozen=True)
class Signal:
    """One threshold the current turn's run reached."""

    kind: str  # "tools" | "reasoning"
    count: int  # consecutive identical rounds when this fired
    tier: int  # 1 = the first reminder for this run, higher = escalation


class LoopGuard:
    """Round-level repetition counters for one turn (both tool and reasoning)."""

    def __init__(self, thresholds: Any = None, watch_reasoning: bool = True) -> None:
        self.thresholds = _clean_thresholds(thresholds)
        self.watch_reasoning = bool(watch_reasoning)
        self._tool_key: tuple[tuple[str, str], ...] | None = None
        self._tool_count = 0
        self._tool_fired: set[int] = set()
        self._reason_anchor: frozenset[str] | None = None
        self._reason_count = 0
        self._reason_fired: set[int] = set()

    @classmethod
    def for_config(cls, config: Any) -> LoopGuard | None:
        """Build from the brain's config; ``None`` when the guard is off."""
        if not bool(_setting(config, "loop_guard", True)):
            return None
        return cls(
            thresholds=_setting(config, "loop_guard_thresholds", list(DEFAULT_THRESHOLDS)),
            watch_reasoning=bool(_setting(config, "loop_guard_reasoning", True)),
        )

    def reset(self) -> None:
        """A queued user message rode in mid-turn: the new instruction changes
        what the model is trying to do, so the round count starts over."""
        self._tool_key = None
        self._tool_count = 0
        self._tool_fired = set()
        self._reason_anchor = None
        self._reason_count = 0
        self._reason_fired = set()

    # -- observation ------------------------------------------------------

    def observe_tools(self, calls: list[tuple[str, Any]]) -> Signal | None:
        """One tool round's identity against the previous round's."""
        key = _calls_key(calls)
        if key is None:
            self._tool_key = None
            self._tool_count = 0
            self._tool_fired = set()
            return None
        if key == self._tool_key:
            self._tool_count += 1
        else:
            self._tool_key = key
            self._tool_count = 1
            self._tool_fired = set()
        return self._crossed(self._tool_count, self._tool_fired, "tools")

    def observe_reasoning(self, text: str) -> Signal | None:
        """One round's reasoning against the current chain's anchor thought."""
        if not self.watch_reasoning:
            return None
        tokens = _tokens(text) if text else None
        if not tokens:
            # No thinking on this round is not a repeat of anything.
            self._reason_anchor = None
            self._reason_count = 0
            self._reason_fired = set()
            return None
        anchor = self._reason_anchor
        if anchor is not None and _similar(anchor, tokens):
            self._reason_count += 1
        else:
            self._reason_anchor = tokens
            self._reason_count = 1
            self._reason_fired = set()
        return self._crossed(self._reason_count, self._reason_fired, "reasoning")

    def _crossed(self, count: int, fired: set[int], kind: str) -> Signal | None:
        """The lowest not-yet-fired threshold the run has now reached, if any."""
        for tier, threshold in enumerate(self.thresholds, start=1):
            if count >= threshold and threshold not in fired:
                fired.add(threshold)
                return Signal(kind=kind, count=count, tier=tier)
        return None

    # -- surfaces ---------------------------------------------------------

    def note(self, signals: list[Signal]) -> str:
        """The model-facing reminder for the run's newly crossed thresholds."""
        lines: list[str] = []
        for s in signals:
            if s.kind == "tools":
                if s.tier <= 1:
                    lines.append(
                        f"the same tool call (or set of calls) has now repeated {s.count} "
                        "rounds in a row — repeating it again will not change the result. "
                        "Change the approach or the arguments, or summarise what you have "
                        "and say what is blocking you."
                    )
                else:
                    lines.append(
                        f"{s.count} identical tool rounds in a row and still no progress. "
                        "Do NOT repeat the same calls again — change the approach entirely, "
                        "or stop and report what is blocking you."
                    )
            else:
                if s.tier <= 1:
                    lines.append(
                        f"your reasoning has re-derived the same thought for {s.count} rounds "
                        "in a row with no new conclusion. Stop looping in it — pick one "
                        "concrete next action and take it, or answer from what you have."
                    )
                else:
                    lines.append(
                        f"{s.count} rounds of near-identical reasoning and still no new "
                        "conclusion. Whatever you keep re-deriving is stable enough to act "
                        "on — commit to it and move."
                    )
        return " — ".join(lines)

    def chip(self, signals: list[Signal]) -> str:
        """The short timeline line for the shell (a ``kind: "guard"`` step)."""
        parts = [
            f"the same {'tool round' if s.kind == 'tools' else 'reasoning'} repeated {s.count}×"
            for s in signals
        ]
        return " · ".join(parts)

    def inject(self, messages: list[dict[str, Any]], note: str) -> None:
        """Put the reminder where the model reads it on the next request.

        The turn carries at most one guard note: a later threshold refreshes it
        in place, so the message list never grows a stack of reminders."""
        content = f"{_PREFIX}{note}"
        for i in range(len(messages) - 1, -1, -1):
            own = messages[i]
            if own.get("role") == "system" and str(own.get("content") or "").startswith(_PREFIX):
                own["content"] = content
                return
        messages.append({"role": "system", "content": content})
