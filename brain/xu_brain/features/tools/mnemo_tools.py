"""Built-in Mnemosyne tools (``mnemosyne`` toolset).

``mnemosyne_remember`` / ``mnemosyne_recall`` / ``mnemosyne_forget`` — the
write path and explicit read path for long-term memory. Fail-soft: with the
``mnemosyne-memory`` package absent each call returns a clear error message
instead of raising, so the toolset stays registered and harmless.
"""
from __future__ import annotations

from typing import Any

from ...core.governance import ApprovalLevel
from ..memory import mnemo
from .base import ToolResult

__all__ = ["mnemosyne_remember", "mnemosyne_recall", "mnemosyne_forget"]


class _RememberTool:
    name = "mnemosyne_remember"
    toolset = "mnemosyne"
    description = (
        "Store a fact in long-term memory (Mnemosyne). Use for durable user "
        "preferences, decisions, project facts — not transient chatter."
    )
    approval = ApprovalLevel.RISKY
    output_schema = None
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact to remember, self-contained.",
            },
            "importance": {"type": "number", "description": "0.0-1.0, default 0.5."},
        },
        "required": ["content"],
    }

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        content = args.get("content")
        if not content or not isinstance(content, str):
            return ToolResult.err("mnemosyne_remember requires a 'content' string")
        try:
            importance = float(args.get("importance", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        try:
            out = mnemo.remember(content, importance=importance)
        except Exception as exc:  # noqa: BLE001 — tool errors are model-facing
            return ToolResult.err(f"mnemosyne failed: {exc}")
        return ToolResult.ok(str(out) if out else "stored (filtered as noise)")


class _RecallTool:
    name = "mnemosyne_recall"
    toolset = "mnemosyne"
    description = "Search long-term memory (Mnemosyne) by query. Returns ranked memories."
    approval = ApprovalLevel.NEVER
    output_schema = None
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "top_k": {"type": "integer", "description": "Default 5."},
        },
        "required": ["query"],
    }

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        query = args.get("query")
        if not query or not isinstance(query, str):
            return ToolResult.err("mnemosyne_recall requires a 'query' string")
        try:
            out = mnemo.recall(query, top_k=int(args.get("top_k", 5)))
        except Exception as exc:  # noqa: BLE001
            return ToolResult.err(f"mnemosyne failed: {exc}")
        return ToolResult.ok(str(out), raw=out)


class _ForgetTool:
    name = "mnemosyne_forget"
    toolset = "mnemosyne"
    description = "Delete one memory by its id (get ids from mnemosyne_recall)."
    approval = ApprovalLevel.ALWAYS
    output_schema = None
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"memory_id": {"type": "string"}},
        "required": ["memory_id"],
    }

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        memory_id = args.get("memory_id")
        if not memory_id or not isinstance(memory_id, str):
            return ToolResult.err("mnemosyne_forget requires a 'memory_id' string")
        try:
            mnemo.forget(memory_id)
        except Exception as exc:  # noqa: BLE001
            return ToolResult.err(f"mnemosyne failed: {exc}")
        return ToolResult.ok(f"forgot {memory_id}")


mnemosyne_remember = _RememberTool()
mnemosyne_recall = _RecallTool()
mnemosyne_forget = _ForgetTool()