"""Extract the few durable facts worth carrying across sessions.

The extractor is deliberately conservative: a failed model or memory backend
must never affect a completed turn.  Storage is imported lazily so this module
also remains usable in minimal test and runtime environments.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from .mnemo import available as _mnemo_available, recall, remember

_PROMPT = """Extract only durable, cross-session facts from the transcript: user preferences, corrections of agent behavior, and stable project facts. Never extract task-in-progress state, one-off commands, code snippets, secrets/credentials, or anything resembling an instruction to do something now. Return at most 3 entries, or [] if nothing qualifies. Each entry must be {\"content\": string, \"importance\": number from 0 to 1, \"kind\": \"preference\"|\"correction\"|\"project-fact\"}. Output ONLY the JSON array, with no prose."""

_LAST_CAPTURE: dict[str, float] = {}
_WORDS = re.compile(r"\b[\w']+\b", re.UNICODE)
_KINDS = {"preference", "correction", "project-fact"}


def _truncate(text: str, limit: int) -> str:
    """Trim text without cutting through a word where practical."""
    if len(text) <= limit:
        return text
    room = limit - 2
    prefix = text[:room].rstrip()
    if " " in prefix:
        prefix = prefix[: prefix.rfind(" ")].rstrip()
    return prefix + " …"


def _parse(text: str) -> list[dict[str, Any]]:
    """Parse and validate the extractor's JSON response."""
    try:
        start, end = text.find("["), text.rfind("]")
        if start < 0 or end < start:
            return []
        raw = json.loads(text[start : end + 1])
        if not isinstance(raw, list):
            return []
    except Exception:
        return []

    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            continue
        content = item["content"].strip()
        if not content:
            continue
        kind = item.get("kind")
        if kind not in _KINDS:
            continue
        value = item.get("importance", 0.5)
        try:
            importance = float(value)
        except (TypeError, ValueError):
            importance = 0.5
        if importance != importance:  # NaN
            importance = 0.5
        importance = max(0.0, min(1.0, importance))
        result.append({"content": _truncate(content, 600), "importance": importance, "kind": kind})
        if len(result) == 3:
            break
    return result


def _overlap(a: str, b: str) -> float:
    """Return Jaccard overlap of lowercase word sets."""
    left = {word.lower() for word in _WORDS.findall(a)}
    right = {word.lower() for word in _WORDS.findall(b)}
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _resolve_model(agent: Any, session_id: str) -> tuple[Any, Any] | None:
    config = agent.config
    override = config.get("memory_capture_model", None)
    model = override
    if not model:
        try:
            model = config.session_model(session_id)
        except (AttributeError, TypeError):
            try:
                model = agent._session_model(session_id)
            except (AttributeError, TypeError):
                model = None
    if not model:
        return None
    try:
        return agent.providers.resolve(model)
    except Exception:
        return None


async def capture_turn(
    agent: Any,
    session_id: str,
    user_text: str,
    assistant_text: str,
    *,
    turn_id: str = "",
) -> list[dict]:
    """Extract, deduplicate, and persist durable facts, fail-soft."""
    config = agent.config
    if not config.get("memory_autocapture", True):
        return []
    now = time.monotonic()
    try:
        interval = float(config.get("memory_capture_min_interval", 300))
    except (TypeError, ValueError):
        interval = 300.0
    if now - _LAST_CAPTURE.get(session_id, float("-inf")) < interval:
        return []

    if not _mnemo_available():
        return []

    resolved = _resolve_model(agent, session_id)
    if resolved is None:
        return []
    provider, model = resolved
    messages = [
        {"role": "system", "content": _PROMPT},
        {"role": "user", "content": f"User:\n{_truncate(user_text, 2000)}\n\nAssistant:\n{_truncate(assistant_text, 2000)}"},
    ]
    parts: list[str] = []
    try:
        async for event in agent.providers.chat_stream(provider, model, messages, max_tokens=800):
            delta = getattr(event, "delta", None)
            if delta:
                parts.append(delta)
    except Exception:
        return []
    _LAST_CAPTURE[session_id] = time.monotonic()
    candidates = _parse("".join(parts))
    accepted: list[dict] = []
    for candidate in candidates:
        try:
            hits = recall(candidate["content"], top_k=3)
            if any(_overlap(candidate["content"], hit.get("content", "")) >= 0.8 for hit in hits):
                continue
            remember(
                content=candidate["content"],
                importance=candidate["importance"],
                metadata={"kind": candidate["kind"], "session_id": session_id},
            )
        except Exception:
            continue
        accepted.append(candidate)
    return accepted


__all__ = ["capture_turn"]
