"""Reserved metadata used to make tool activity understandable in the shell."""

from __future__ import annotations

import json
from typing import Any

NOTE_ARG = "_xu_note"
_MAX_NOTE_LENGTH = 160


def clean_note(value: Any) -> str | None:
    """Return a compact one-line note suitable for the collapsed activity row."""
    if not isinstance(value, str):
        return None
    note = " ".join(value.split()).strip()
    if not note:
        return None
    return note[:_MAX_NOTE_LENGTH]


def tool_args(args: dict[str, Any]) -> dict[str, Any]:
    """Strip shell-only metadata before validation, approval, and execution."""
    return {key: value for key, value in args.items() if key != NOTE_ARG}


def strip_note_arguments(call: dict[str, Any]) -> dict[str, Any]:
    """Copy one provider tool call with reserved display metadata removed."""
    copied = {**call}
    function = copied.get("function")
    if not isinstance(function, dict):
        return copied
    copied["function"] = {**function}
    raw = function.get("arguments")
    if not isinstance(raw, str):
        return copied
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return copied
    if not isinstance(parsed, dict) or NOTE_ARG not in parsed:
        return copied
    copied["function"]["arguments"] = json.dumps(tool_args(parsed), ensure_ascii=False)
    return copied
