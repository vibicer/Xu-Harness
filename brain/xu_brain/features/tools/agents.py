"""agents toolset — delegate / subagent management + orchestration presets.

The one delegation primitive is ``delegate``: dispatch a sub-task to a sub-agent
and return its result. Under an orchestration preset it targets a named squad
member (their own persona/model/tools scope); for a normal agent it runs a
default agent that knows it is a sub-agent. ``background: true`` spawns a
durable ``s…`` id immediately so the parent can fan out and collect later.

- ``delegate``          — the single summon primitive (blocking or background).
- ``subagent_list`` / ``subagent_message`` / ``subagent_interrupt``
                        — manage spawned subagents.
- ``preset_create``     — author an orchestration preset.
- ``ask``               — block the parent turn until the user replies.
"""
from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult
from ..agent.scheduler import run_bounded
from ..presets import count_nodes



def _find_descendant(node: dict[str, Any], ref: str) -> dict[str, Any] | None:
    """Depth-first lookup of a child by id or name in an orchestration node."""
    for c in node.get("children", []):
        if c.get("id") == ref or c.get("name") == ref:
            return c
    for c in node.get("children", []):
        got = _find_descendant(c, ref)
        if got is not None:
            return got
    return None


# A leaf sub-agent has no channel to the user: nothing can answer a question it
# asks, and `subagent_message` cannot reach a single-turn `delegate` child. When
# a child ends its turn asking for permission instead of working, that is a
# failed delegation, not a result — surface it so the orchestrator re-delegates
# with an explicit mandate rather than "approving" into a void.
_APPROVAL_ASK = re.compile(
    r"\b(may|shall|should|can|could)\s+i\b|\bwant me to\b|\bwould you like me to\b"
    r"|\bproceed\?|\bboleh (saya|aku)\b|\bapakah saya\b|\blanjut\?",
    re.IGNORECASE,
)


def _is_approval_stall(text: str) -> bool:
    """True when a child's final text is only a request for permission."""
    body = (text or "").strip()
    if not body:
        return False
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    if not lines:
        return False
    # Only judge the closing thought: a real report that happens to contain a
    # rhetorical question mid-body is still a result.
    tail = lines[-1]
    return tail.endswith("?") and bool(_APPROVAL_ASK.search(tail))


class DelegateTool(Tool):
    name = "delegate"
    toolset = "orchestration"
    description = (
        "Dispatch a sub-task to a sub-agent and return its result. With an "
        "orchestration preset, 'label' (or 'child') names a squad member from "
        "the roster, run in that member's own persona/model/tools scope. With "
        "no preset (a normal agent), 'label' is a free-form display name and "
        "the sub-agent runs as a default agent (same global persona, skills, "
        "tools, and memory as any agent) that knows it is a sub-agent. Set "
        "'background': true to spawn it and return a durable id immediately "
        "(track with subagent_list; interrupt with subagent_interrupt)."
    )
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "name/id of a preset squad member, or a free-form display label"},
            "prompt": {"type": "string"},
            "background": {"type": "boolean", "description": "if true, spawn and return a durable id immediately"},
            "tasks": {"type": "array", "description": "optional fan-out batch; each item has prompt and optional label", "items": {"type": "object", "properties": {"label": {"type": "string"}, "prompt": {"type": "string"}}, "required": ["prompt"]}},
        },
        "anyOf": [{"required": ["prompt"]}, {"required": ["tasks"]}],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tasks = args.get("tasks")
        if tasks is not None:
            if not isinstance(tasks, list) or not tasks:
                return ToolResult.err("tasks must be a non-empty array")
            if args.get("prompt"):
                return ToolResult.err("use prompt or tasks, not both")
            if any(not isinstance(task, dict) or not str(task.get("prompt", "")).strip() for task in tasks):
                return ToolResult.err("each task must include a non-empty prompt")
            if ctx.config.get("subtask"):
                return ToolResult.err("delegate is not available inside subagent turns")
            depth = int(ctx.config.get("preset_depth", 0))
            if depth >= 3:
                return ToolResult.err("delegate recursion cap (3) reached — do the work directly")
            node = ctx.config.get("preset_node")
            if node and node.get("children"):
                for task in tasks:
                    label = str(task.get("label") or task.get("child") or "").strip()
                    if not label or _find_descendant(node, label) is None:
                        return ToolResult.err(f"no squad member '{label}'")
            base = {key: value for key, value in args.items() if key != "tasks"}

            async def run_one(task: dict[str, Any]) -> ToolResult:
                return await self.run({**base, **task}, ctx)
            limit = int(ctx.config.get("max_parallel_subagents", len(tasks)) or len(tasks))
            settled = await run_bounded(tasks, run_one, limit)
            results: list[dict[str, Any]] = []
            failed = 0
            for index, result in enumerate(settled, 1):
                if isinstance(result, BaseException):
                    failed += 1
                    results.append({"index": index, "status": "error", "error": str(result)})
                elif result.error:
                    failed += 1
                    results.append({"index": index, "status": "error", "error": result.error})
                else:
                    results.append({"index": index, "status": "ok", "output": result.output})
            return ToolResult.ok(
                f"fan-out completed: {len(tasks) - failed}/{len(tasks)} succeeded",
                raw={"results": results, "total": len(tasks), "failed": failed},
            )
        label = str(args.get("label") or args.get("child") or "").strip()
        prompt = str(args.get("prompt", "")).strip()
        if not prompt:
            return ToolResult.err("prompt required")
        if ctx.config.get("subtask"):
            return ToolResult.err(
                "delegate is not available inside subagent turns — "
                "complete the assigned work directly"
            )
        max_depth = 3
        depth = int(ctx.config.get("preset_depth", 0))
        if depth >= max_depth:
            return ToolResult.err(
                f"delegate recursion cap ({max_depth}) reached — do the work directly"
            )
        agent = ctx.agent
        node = ctx.config.get("preset_node")
        child_node = None
        if node and node.get("children"):
            # Orchestrator: resolve the named squad member.
            if not label:
                return ToolResult.err("a squad member name is required under an orchestration preset")
            target = _find_descendant(node, label)
            if target is None:
                names = ", ".join(c.get("name") for c in node.get("children", []))
                return ToolResult.err(f"no squad member '{label}'; available: {names}")
            from xu_brain.features.presets import AgentNode
            child_node = AgentNode.from_dict(target)
        # else: normal agent — no preset. Fall through with child_node=None so
        # the sub-agent runs as a default agent (global persona/skills/tools/
        # memory), and ``label`` is only a free-form display name.
        display = (child_node.name if child_node is not None else (label or "subagent"))

        if args.get("background"):
            if not hasattr(agent, "spawn_managed_subagent"):
                return ToolResult.err("subagent execution unavailable")
            sid = await agent.spawn_managed_subagent(prompt, ctx, node=child_node, depth=depth + 1, label=display)
            return ToolResult.ok(
                f"subagent {sid} spawned in background. Track with subagent_list; "
                f"interrupt with subagent_interrupt({sid}).",
                raw={"id": sid},
            )

        if not hasattr(agent, "run_subtask"):
            return ToolResult.err("subagent execution unavailable")
        # Reserve the activity record here — after every validation above —
        # so a rejected or malformed call can never leak a "running" run.
        # (A loop-side pre-reservation used to leak one on every failure path.)
        run_id = ctx.config.pop("_delegation_run_id", None)
        if run_id is None and hasattr(agent, "begin_delegation"):
            run_id = agent.begin_delegation(display, prompt, ctx.session_id)
        try:
            text = await agent.run_subtask(
                prompt, ctx, node=child_node, depth=depth + 1, run_id=run_id
            )
        except asyncio.TimeoutError:
            if run_id and hasattr(agent, "finish_delegation"):
                agent.finish_delegation(run_id, "", "error")
            return ToolResult.err("delegated sub-task timed out", subagent_run=run_id)
        if _is_approval_stall(text):
            if run_id and hasattr(agent, "finish_delegation"):
                agent.finish_delegation(run_id, text, "error")
            return ToolResult.err(
                f"'{display}' ended its turn asking for permission "
                "instead of doing the work, and a delegate child cannot be "
                "answered. Re-delegate with an explicit mandate (state that "
                "approval is already granted and it must execute now). Its "
                f"reply: {text.strip()[:300]}",
                subagent_run=run_id,
                subagent=display,
            )
        return ToolResult.ok(text, raw=text, subagent_run=run_id, subagent=display)


class SubagentListTool(Tool):
    name = "subagent_list"
    toolset = "orchestration"
    description = (
        "List background subagents and their status (running/done/error/interrupted), "
        "or fetch one subagent's full result by passing its id."
    )
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "optional subagent id; returns that subagent's full result"},
        },
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        agent = ctx.agent
        orch = getattr(agent, "orchestrator", None)
        sub_id = str(args.get("id") or "").strip()
        managed = []
        if orch is not None:
            if sub_id:
                sub = orch.owned_subagent(sub_id, ctx.session_id)
                if sub is not None:
                    managed = [sub.to_dict()]
            else:
                managed = orch.list_subagents(ctx.session_id)
        runs = agent.delegation_activity(run_id=sub_id or None, session_id=ctx.session_id) \
            if hasattr(agent, "delegation_activity") else []
        if sub_id and not managed and not runs:
            return ToolResult.err(f"no subagent '{sub_id}' owned by this session")
        if sub_id:
            records = managed or runs
            record = records[0]
            result = record.get("result") or f"subagent {sub_id} {record.get('status', 'unknown')} (no result)"
            return ToolResult.ok(result, raw=record)
        records = managed + [r for r in runs if r.get("id") not in {s.get("id") for s in managed}]
        if not records:
            return ToolResult.ok("no subagents")
        lines = [
            f"- {s.get('id')} [{s.get('status')}] {str(s.get('prompt', ''))[:60]}"
            + (f" → {s['result'][:80]}" if s.get("result") else "")
            for s in records
        ]
        return ToolResult.ok("\n".join(lines), raw=records)


class SubagentMessageTool(Tool):
    name = "subagent_message"
    toolset = "orchestration"
    description = (
        "Send a follow-up prompt to a subagent. Returns whether the message "
        "was accepted. (Multi-message child turns — a no-op for finished agents.)"
    )
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["id", "message"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        sub_id = str(args.get("id") or "").strip()
        message = str(args.get("message") or "").strip()
        if not sub_id or not message:
            return ToolResult.err("id and message required")
        try:
            ok = await ctx.agent.subagent_message(sub_id, message, parent_session=ctx.session_id)
        except (LookupError, RuntimeError) as exc:
            # A silent False reads as "delivered but ignored" and invites the
            # caller to retry against a channel that cannot exist; say why.
            return ToolResult.err(str(exc))
        return ToolResult.ok("accepted" if ok else "not accepted (finished or not running)", raw={"accepted": ok})


class SubagentInterruptTool(Tool):
    name = "subagent_interrupt"
    toolset = "orchestration"
    description = "Stop a running background subagent."
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {"id": {"type": "string"}},
        "required": ["id"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        sub_id = str(args.get("id") or "").strip()
        if not sub_id:
            return ToolResult.err("id required")
        ok = await ctx.agent.subagent_interrupt(sub_id, parent_session=ctx.session_id)
        if not ok:
            return ToolResult.err("subagent not found or not running")
        return ToolResult.ok(f"subagent {sub_id} interrupted", raw={"id": sub_id})


class AskTool(Tool):
    name = "ask"
    toolset = "agents"
    description = (
        "Ask the user a clarification question and block until they reply. "
        "Use only when tools/context cannot resolve an ambiguity."
    )
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "question": {"type": "string"},
            "options": {"type": "array", "items": {"type": "string"}, "description": "optional choices"},
        },
        "required": ["question"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        question = str(args.get("question", "")).strip()
        if not question:
            return ToolResult.err("question required")
        request_id = "q" + secrets.token_hex(4)
        agent = ctx.agent
        if not hasattr(agent, "await_user_reply"):
            return ToolResult.err("user-reply channel unavailable")
        try:
            answer = await asyncio.wait_for(
                agent.await_user_reply(
                    request_id, question, args.get("options"), session_id=ctx.session_id
                ),
                timeout=600.0,
            )
        except asyncio.TimeoutError:
            return ToolResult.err("user did not respond in time")
        if answer is None:
            return ToolResult.err("no reply")
        return ToolResult.ok(str(answer), raw=answer)


class PresetTool(Tool):
    name = "preset_create"
    toolset = "orchestration"
    description = (
        "Create or update an orchestration preset (an agent composition: a "
        "Main-orchestrator root plus sub-agents), stored so it can be bound to "
        "a session via set_session_preset. Pass a JSON `tree` for the root node "
        "and an optional existing preset `id` to overwrite it. Returns the saved "
        "preset id and agent count."
    )
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "preset name"},
            "tree": {
                "type": "object",
                "description": (
                    "root AgentNode: {role:'orchestrator', persona, model?, "
                    "fallbacks?, skills?, tools?, memory?, children:[{name, "
                    "role:'agent', persona, model?, fallbacks?, skills?, tools?, "
                    "memory?}]}. null = inherit. `fallbacks` = ordered backup "
                    "model ids tried when `model` fails."
                ),
            },
            "id": {"type": "string", "description": "existing preset id to overwrite (omit to create new)"},
        },
        "required": ["name", "tree"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name") or "").strip()
        tree = args.get("tree")
        pid = args.get("id") or None
        if not name:
            return ToolResult.err("name is required")
        if not isinstance(tree, dict):
            return ToolResult.err("tree (root node) must be an object")
        store = getattr(ctx, "presets", None)
        if store is None:
            return ToolResult.err("preset store unavailable in this context")
        try:
            preset = store.upsert(pid, name, tree)
        except Exception as exc:  # noqa: BLE001 — surface a clean tool error
            return ToolResult.err(f"preset create failed: {exc}")
        return ToolResult.ok(
            f"Preset '{preset.name}' ({preset.id}) saved — {count_nodes(preset.root)} agents. "
            f"Bind it to a session with set_session_preset({preset.id}).",
            raw=preset.to_dict(),
        )


ask = AskTool()
delegate = DelegateTool()
preset_create = PresetTool()
subagent_list = SubagentListTool()
subagent_message = SubagentMessageTool()
subagent_interrupt = SubagentInterruptTool()

tools = [ask, delegate, preset_create, subagent_list, subagent_message, subagent_interrupt]
