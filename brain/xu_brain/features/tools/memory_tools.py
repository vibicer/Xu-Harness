"""Memory tools — MEMORY.md-backed writes and reads.

Four tools registered under the ``memory`` toolset:

- ``memory_edit`` (ALWAYS) — replace an entry's text; approval-gated even in
  YOLO mode because memory writes are durable.
- ``retain``      (ALWAYS) — durable-retain an entry; approval-gated.
- ``recall``     (NEVER)  — case-insensitive substring search, read-only.
- ``reflect``    (NEVER)  — compact full-memory digest for the system prompt.

Tools do NOT emit events themselves — the registry wraps every call and emits
``turn.tool`` chips. The state-panel ``memory.updated`` event is emitted by
the RPC layer (``server.py``) after an approved write.
"""
from __future__ import annotations

from typing import Any

from ...core.governance import ApprovalLevel
from ..tools.base import Tool, ToolResult

__all__ = ["memory_edit", "retain_tool", "recall", "reflect"]


class _MemoryEditTool:
    """``memory_edit`` — replace an existing memory entry's text (durable write)."""

    name = "memory_edit"
    toolset = "memory"
    description = (
        "Edit an existing memory entry in MEMORY.md. Durable write — "
        "requires approval even in YOLO mode. Use a recall first to find the id."
    )
    approval = ApprovalLevel.ALWAYS
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "The memory entry id (e.g. 'm1a2b3').",
            },
            "text": {
                "type": "string",
                "description": "The new entry text (replaces the existing body).",
            },
        },
        "required": ["id", "text"],
    }

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        id = args.get("id")
        text = args.get("text")
        if not id or not isinstance(id, str):
            return ToolResult.err("memory_edit requires a non-empty 'id' string")
        if not isinstance(text, str):
            return ToolResult.err("memory_edit requires a 'text' string")
        if ctx.memory is None:
            return ToolResult.err("memory store unavailable in this context")
        if not ctx.memory.update(id, text):
            return ToolResult.err(f"no memory entry with id '{id}'")
        return ToolResult.ok(
            f"memory entry {id} updated.", raw={"id": id, "text": text}
        )


class _RetainTool:
    """``retain`` — mark an entry durable (keep it across turns)."""

    name = "retain"
    toolset = "memory"
    description = (
        "Mark a memory entry as durable-retained so it persists. "
        "Approval-gated durable write. Returns the retained id."
    )
    approval = ApprovalLevel.ALWAYS
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "The memory entry id to retain.",
            },
        },
        "required": ["id"],
    }

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        id = args.get("id")
        if not id or not isinstance(id, str):
            return ToolResult.err("retain requires a non-empty 'id' string")
        if ctx.memory is None:
            return ToolResult.err("memory store unavailable in this context")
        if not ctx.memory.retain(id):
            return ToolResult.err(f"no memory entry with id '{id}' to retain")
        return ToolResult.ok(f"memory entry {id} retained.", raw={"id": id})


class _RecallTool:
    """``recall`` — case-insensitive substring search over memory text."""

    name = "recall"
    toolset = "memory"
    description = (
        "Search memory entries by case-insensitive substring over their text. "
        "Returns matching entries as text. Read-only."
    )
    approval = ApprovalLevel.NEVER
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Substring to search for in memory entry text.",
            },
        },
        "required": ["query"],
    }

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        query = args.get("query")
        if not isinstance(query, str) or not query:
            return ToolResult.err("recall requires a non-empty 'query' string")
        if ctx.memory is None:
            return ToolResult.err("memory store unavailable in this context")
        matches = ctx.memory.recall(query)
        if not matches:
            return ToolResult.ok(
                f"no memory entries match '{query}'.", raw={"matches": []}
            )
        lines = [f"[{m['id']}] {m['text']}" for m in matches]
        return ToolResult.ok(
            "\n".join(lines),
            raw={"matches": matches, "count": len(matches)},
        )


class _ReflectTool:
    """``reflect`` — compact full-memory digest for the system prompt."""

    name = "reflect"
    toolset = "memory"
    description = (
        "Return a compact digest of all memory entries (each prefixed with its "
        "id), suitable for injecting into the system prompt. Read-only."
    )
    approval = ApprovalLevel.NEVER
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
    }

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        if ctx.memory is None:
            return ToolResult.err("memory store unavailable in this context")
        digest = ctx.memory.reflect()
        if not digest:
            return ToolResult.ok("(memory empty)", raw={"entries": []})
        return ToolResult.ok(digest, raw={"entries": ctx.memory.list()})


# Module-level singletons — match the names the registry/loop imports.
memory_edit = _MemoryEditTool()
retain_tool = _RetainTool()
recall = _RecallTool()
reflect = _ReflectTool()


def all_tools() -> list[Tool]:
    """All tools in the ``memory`` toolset, for registry bulk-register."""
    return [memory_edit, retain_tool, recall, reflect]
