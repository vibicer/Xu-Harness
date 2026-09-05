"""plan toolset — todo / yield / goal turn semantics.

`todo`: task tracking (init/start/done/drop/append/view). `yield`/`goal` are
hidden turn semantics the loop interprets (not surfaced to the model as
user-visible tools) — exposed here only so the loop can dispatch them.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult


def _todo_file(ctx: ToolContext) -> Path:
    f = ctx.data_home / "sessions" / f"{ctx.session_id}.todo.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    return f


def _load(ctx: ToolContext) -> dict[str, Any]:
    f = _todo_file(ctx)
    if not f.exists():
        return {"phases": []}
    try:
        return json.loads(f.read_text("utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"phases": []}


async def _save(ctx: ToolContext, data: dict[str, Any]) -> None:
    _todo_file(ctx).write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    if ctx.events is not None:
        await ctx.events.emit("todo.updated", session_id=ctx.session_id)


class TodoTool(Tool):
    name = "todo"
    toolset = "plan"
    description = (
        "Track tasks across a turn. ops: init (phases+items), start, done, drop, "
        "append, view. Tasks referenced by content string. Returns current list."
    )
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "op": {"type": "string", "enum": ["init", "start", "done", "drop", "append", "view"]},
            "task": {"type": "string"},
            "phase": {"type": "string"},
            "items": {"type": "array", "items": {"type": "string"}},
            "list": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["op"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        op = str(args.get("op", "view"))
        data = _load(ctx)
        phases: list[dict[str, Any]] = data.get("phases", [])
        if op == "init":
            # Rebuild from scratch; items become {content, status} dicts so the
            # start/done/drop ops (which match dict items) can address them.
            phases = []
            for entry in args.get("list", []):
                phases.append({
                    "phase": entry.get("phase", "Tasks"),
                    "items": [
                        it if isinstance(it, dict) else {"content": str(it), "status": "pending"}
                        for it in entry.get("items", [])
                    ],
                })
            if not phases and args.get("items"):
                phases.append({
                    "phase": args.get("phase", "Tasks"),
                    "items": [
                        {"content": str(it), "status": "pending"} for it in args["items"]
                    ],
                })
            data = {"phases": phases}
            await _save(ctx, data)
            return ToolResult.ok(_render(phases), raw=data)
        elif op == "view":
            pass
        else:
            task = str(args.get("task", ""))
            phase = str(args.get("phase", ""))
            for ph in phases:
                if phase and ph.get("phase") != phase:
                    continue
                for it in ph.get("items", []):
                    if isinstance(it, dict) and it.get("content") == task:
                        it["status"] = _status_for(op)
                        await _save(ctx, data)
                        return ToolResult.ok(_render(phases), raw=data)
            if op == "append":
                ph = phases[-1] if phases else {"phase": phase or "Tasks", "items": []}
                if ph not in phases:
                    phases.append(ph)
                ph.setdefault("items", []).append({"content": task, "status": "pending"})
                await _save(ctx, data)
            else:
                return ToolResult.err(f"task not found: {task}")
        await _save(ctx, data)
        return ToolResult.ok(_render(phases), raw=data)


def _status_for(op: str) -> str:
    if op == "done":
        return "done"
    if op == "drop":
        return "dropped"
    if op == "start":
        return "in_progress"
    return "pending"


def _render(phases: list[dict[str, Any]]) -> str:
    if not phases:
        return "(no tasks)"
    out: list[str] = []
    for ph in phases:
        out.append(f"[{ph.get('phase', '?')}]")
        for it in ph.get("items", []):
            if isinstance(it, str):
                out.append(f"  - {it}")
            else:
                st = it.get("status", "pending")
                mark = {"done": "x", "in_progress": ">", "dropped": "-"}.get(st, " ")
                out.append(f"  [{mark}] {it.get('content', '?')}")
    return "\n".join(out)


todo = TodoTool()
