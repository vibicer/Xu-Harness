"""``tool_create`` — the agent authors its own drop-in tool.

This is the *meta-tool* that turns the toolbox into a closed loop: instead of
hardcoding every new capability, the agent (or user) calls ``tool_create`` to
author a new drop-in tool on the fly. It:

1. validates the request (safe name, unique, well-formed schema),
2. builds a drop-in module using the :func:`~xu_brain.features.tools.loader.tool` helper,
3. compiles + imports it *in memory* to prove the body yields a usable tool,
4. writes it to ``data_home/tools/{name}.py`` (atomic),
5. registers the resulting :class:`Tool` into the live
   :class:`~xu_brain.features.tools.registry.ToolRegistry` — so it is callable in the
   *same session* (the loop rebuilds ``schemas_for_model()`` each turn).

The written module is then re-discovered by the loader on the next boot, so the
tool persists across restarts.

Policy:
- ``name`` must be a valid, non-keyword Python identifier, not already taken by
  a built-in tool or an existing drop-in (built-ins are unshadowable).
- ``schema`` must be a JSON-Schema ``object`` (may be empty ``{}``-ish).
- Gates at ``ApprovalLevel.ALWAYS``: writing executable code to disk warrants an
  explicit prompt, mirroring durable memory writes.
"""
from __future__ import annotations

import keyword
from pathlib import Path
from typing import Any

from ...core.governance import ApprovalLevel
from ..tools.base import Tool, ToolContext, ToolResult
from .loader import active_dropins

__all__ = ["tool_create", "tool_list", "tool_remove"]

_TOOL_HDR = (
    "from xu_brain.features.tools.loader import tool as _make_tool\n"
    "from xu_brain.features.tools.base import ToolResult\n\n"
    "async def _impl(args, ctx):\n"
)
_TRAILER_TMPL = (
    "\n\ntool = _make_tool({name!r}, {description!r}, {schema!r}, "
    "toolset={toolset!r})(_impl)\n"
)


class _ToolCreate:
    """``tool_create`` — write + live-register a new drop-in tool."""

    name = "tool_create"
    toolset = "toolbuilder"
    description = (
        "Author a new drop-in tool and register it so it is callable from the "
        "next turn on. Provide a unique `name` (a Python identifier, must not "
        "shadow a built-in tool), a `description`, an optional JSON-Schema "
        "`schema` for args, and an `async def` `body` that receives (args, ctx) "
        "and returns a ToolResult. The tool is written to data_home/tools/ and "
        "persists across restarts."
    )
    approval = ApprovalLevel.ALWAYS
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Unique tool name; valid Python identifier, no built-in shadows.",
            },
            "description": {"type": "string"},
            "schema": {
                "type": "object",
                "description": "JSON Schema for the new tool's args (object).",
            },
            "body": {
                "type": "string",
                "description": "Python statements for the tool body; async fn (args, ctx) -> ToolResult.",
            },
            "toolset": {
                "type": "string",
                "description": "Optional toolset grouping (default 'dropin').",
            },
        },
        "required": ["name", "body"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name", "")).strip()
        body = args.get("body", "")
        description = str(args.get("description", "")).strip() or f"Drop-in tool {name}"
        schema = args.get("schema") or {}
        toolset = str(args.get("toolset", "")).strip() or "dropin"

        # 1. Name safety.
        name_err = _validate_name(name)
        if name_err:
            return ToolResult.err(name_err)
        if not isinstance(body, str) or not body.strip():
            return ToolResult.err("tool_create: `body` must be non-empty Python statements")

        registry = getattr(ctx.agent, "registry", None)
        if registry is None:
            return ToolResult.err("tool_create: agent registry unavailable")
        if name in registry.names():
            return ToolResult.err(
                f"tool name {name!r} is already registered (built-in or existing drop-in); cannot shadow"
            )

        # 2. Schema shape.
        if not isinstance(schema, dict):
            return ToolResult.err("tool_create: `schema` must be a JSON-Schema object")
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})

        # 3. Build + compile + import in memory to validate before writing.
        module_src = _TOOL_HDR + _indent(body) + _TRAILER_TMPL.format(
            name=name, description=description, schema=schema, toolset=toolset
        )
        ns: dict[str, Any] = {}
        try:
            compiled = compile(module_src, f"<tool_create:{name}>", "exec")
            exec(compiled, ns)  # noqa: S102 - runs the user-authored body by design
        except Exception as exc:  # noqa: BLE001 - surface any authoring error
            return ToolResult.err(
                f"tool_create: body failed to compile/import: {type(exc).__name__}: {exc}"
            )
        new_tool = ns.get("tool")
        if not hasattr(new_tool, "run"):
            return ToolResult.err(
                "tool_create: body did not produce a usable tool (no run method)"
            )
        actual = getattr(new_tool, "name", None)
        if actual != name:
            return ToolResult.err(
                f"tool_create: internal name mismatch ({actual!r} != {name!r})"
            )

        # 4. Write atomically (mirror memory-store write policy).
        tools_dir = Path(ctx.data_home) / "tools"
        tools_dir.mkdir(parents=True, exist_ok=True)
        target = tools_dir / f"{name}.py"
        if target.exists():
            return ToolResult.err(
                f"tool_create: {target.name} already exists; refuse to overwrite"
            )
        tmp = target.with_suffix(".py.tmp")
        tmp.write_text(module_src, encoding="utf-8")
        tmp.replace(target)

        # 5. Live-register so it is callable this session (next model turn).
        registry.register(new_tool)
        active_dropins().add(name)

        return ToolResult.ok(
            f"Tool `{name}` created at {target} and registered (callable next turn). "
            f"toolset={toolset!r}, approval={new_tool.approval.value!r}.",
            raw={"name": name, "path": str(target), "toolset": toolset},
        )


def _validate_name(name: str) -> str | None:
    """Return an error string if ``name`` is not a safe drop-in tool name."""
    if not name or not name.isidentifier() or keyword.iskeyword(name):
        return (
            f"invalid tool name {name!r}: must be a valid, non-keyword Python identifier"
        )
    return None


def _indent(stmts: str) -> str:
    """Indent a multi-line body to sit under ``async def _impl``."""
    return "\n".join(("    " + line) if line.strip() else "" for line in stmts.splitlines()) + "\n"


class _ToolList:
    """``tool_list`` — list the currently active drop-in tools."""

    name = "tool_list"
    toolset = "toolbuilder"
    description = (
        "List the user-authored drop-in tools currently loaded. Result gives "
        "each tool's name, toolset, description and approval level."
    )
    approval = ApprovalLevel.NEVER
    schema: dict[str, Any] = {"type": "object", "properties": {}}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        registry = getattr(ctx.agent, "registry", None)
        names = sorted(active_dropins())
        if not names:
            return ToolResult.ok("(no drop-in tools)", raw={"tools": []})

        by_name = registry._tools if registry is not None else {}  # noqa: SLF001
        rows: list[dict[str, Any]] = []
        items: list[str] = []
        for nm in names:
            tool = by_name.get(nm)
            rows.append(
                {
                    "name": nm,
                    "toolset": getattr(tool, "toolset", "dropin"),
                    "approval": getattr(tool, "approval", ApprovalLevel.RISKY).value,
                    "description": getattr(tool, "description", ""),
                    "registered": bool(tool),
                }
            )
            items.append(
                f"- {nm} [{rows[-1]['toolset']}, {rows[-1]['approval']}]"
            )
        return ToolResult.ok(
            "Drop-in tools:\n" + "\n".join(items), raw={"tools": rows}
        )


class _ToolRemove:
    """``tool_remove`` — delete a drop-in tool (file + live registration)."""

    name = "tool_remove"
    toolset = "toolbuilder"
    description = (
        "Delete a user-authored drop-in tool by name: removes it from the live "
        "registry and deletes its file from data_home/tools/ so it won't reload. "
        "Built-in tools cannot be removed."
    )
    approval = ApprovalLevel.ALWAYS
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of an existing drop-in tool to delete.",
            }
        },
        "required": ["name"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        name = str(args.get("name", "")).strip()
        registry = getattr(ctx.agent, "registry", None)
        if name not in active_dropins():
            is_builtin = bool(registry is not None and name in registry.names())
            reason = "is a built-in; cannot remove" if is_builtin else "is not an active drop-in"
            return ToolResult.err(f"tool_remove: {name!r} {reason}")
        if registry is not None:
            registry.unregister(name)
        active_dropins().discard(name)
        target = Path(ctx.data_home) / "tools" / f"{name}.py"
        removed = target.exists()
        if removed:
            target.unlink(missing_ok=True)
        (target.with_suffix(".py.tmp")).unlink(missing_ok=True)
        return ToolResult.ok(
            f"Removed tool `{name}` (file deleted: {removed}).",
            raw={"name": name, "removed_file": removed},
        )


tool_create = _ToolCreate()
tool_list = _ToolList()
tool_remove = _ToolRemove()
