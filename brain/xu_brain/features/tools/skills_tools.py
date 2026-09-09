"""Skills toolset — catalog, load, and manage skill packs.

Thin async wrappers over :class:`SkillsEngine` exposed to the agent loop.

- ``skill_list``  (NEVER)  — catalog rows: name + description + state.
- ``skill_load``  (NEVER)  — fetch a skill's full body (progressive disclosure).
- ``manage_skill`` (RISKY) — install / update / remove / enable / disable packs.
"""
from __future__ import annotations

from typing import Any

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult

__all__ = ["skill_list", "skill_load", "manage_skill", "tools"]


class SkillListTool(Tool):
    name = "skill_list"
    toolset = "skills"
    description = "List the skill catalog: name, description, and state (LOADED/DEMAND)."
    approval = ApprovalLevel.NEVER
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rows = ctx.skills.list(session_id=ctx.session_id or None)
        if not rows:
            return ToolResult.ok("No skills available.")
        lines = [f"- {r['id']}  [{r['state']}]  {r['name']} — {r['desc']}" for r in rows]
        out = "\n".join(lines)
        return ToolResult.ok(out, raw=rows)


class SkillLoadTool(Tool):
    name = "skill_load"
    toolset = "skills"
    description = "Load a skill's full body by id (progressive disclosure)."
    approval = ApprovalLevel.NEVER
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "Skill id from the catalog, e.g. 'general/code-review'.",
            },
        },
        "required": ["id"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        sid = args.get("id")
        if not sid or not isinstance(sid, str):
            return ToolResult.err("skill_load requires an 'id' string.")
        if ctx.session_id and ctx.skills.session_overrides(ctx.session_id).get(sid) is False:
            return ToolResult.err(f"skill disabled for this session: {sid}")
        try:
            body = ctx.skills.load(sid)
        except (KeyError, FileNotFoundError, ValueError) as exc:
            return ToolResult.err(str(exc))
        return ToolResult.ok(body, raw=body, id=sid)


class ManageSkillTool(Tool):
    name = "manage_skill"
    toolset = "skills"
    description = (
        "Manage the complete skill filesystem lifecycle. Create a SKILL.md in "
        "data_home/skills/<pack>/<id>, install a validated pack from a directory "
        "or zip, update/remove a skill, or enable/disable ambient loading."
    )
    approval = ApprovalLevel.RISKY
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["create", "install", "update", "remove", "enable", "disable"],
                "description": "Lifecycle operation.",
            },
            "id": {"type": "string", "description": "Skill slug for create, or catalog id for other operations."},
            "pack": {"type": "string", "description": "Safe pack name for create (default: custom)."},
            "name": {"type": "string", "description": "Display name for create."},
            "description": {"type": "string", "description": "Skill description for create."},
            "body": {"type": "string", "description": "Markdown instructions for create."},
            "keywords": {"type": "array", "items": {"type": "string"}},
            "source": {"type": "string", "description": "Directory or .zip pack for install/update."},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = args.get("action")
        if not isinstance(action, str) or not action:
            return ToolResult.err("manage_skill requires an 'action' string.")
        try:
            if action == "create":
                required = ("id", "name", "body")
                if any(not isinstance(args.get(key), str) or not args[key].strip() for key in required):
                    return ToolResult.err("create requires non-empty id, name, and body.")
                result = ctx.skills.create(
                    args["id"], args["name"], args.get("description", ""), args["body"],
                    args.get("keywords"), args.get("pack", "custom"),
                )
            else:
                result = ctx.skills.manage(action, id=args.get("id"), source=args.get("source"))
        except (KeyError, FileNotFoundError, FileExistsError, ValueError, OSError) as exc:
            return ToolResult.err(str(exc))
        if isinstance(result, str):
            return ToolResult.ok(f"Skill operation complete: {result}", raw={"result": result})
        return ToolResult.ok(f"Skill operation complete: {result}", raw=result)


skill_list = SkillListTool()
skill_load = SkillLoadTool()
manage_skill = ManageSkillTool()

tools = [skill_list, skill_load, manage_skill]
