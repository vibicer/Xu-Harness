"""Drop-in tool management RPC handlers."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.contract import RpcError
from ...core.notify import notify
from .loader import active_dropins

if TYPE_CHECKING:
    from ...core.runtime import App

def register(app: App) -> None:
    @app.register("tool.list")
    async def tool_list(params: dict[str, Any]) -> dict[str, Any]:
        # A session_id overlay returns that session's effective enable state
        # (Agent State panel); without it the rows are the global defaults
        # (Config → Tools).
        session_id = params.get("session_id") or None
        ov = app.config.session_tool_overrides(session_id) if session_id else {}
        drops = active_dropins()
        toolsets = app.registry.toolsets()
        if session_id:
            for ts in toolsets:
                ts["enabled"] = ov.get(ts["toolset"], ts["enabled"])
        dropins: list[dict[str, Any]] = []
        for ts in toolsets:
            for tool in ts["tools"]:
                if tool["name"] in drops:
                    tool["is_dropin"] = True
                    dropins.append(
                        {
                            "name": tool["name"],
                            "description": tool.get("description", ""),
                            "toolset": ts["toolset"],
                            "approval": app.registry.approval_of(tool["name"]),
                            "enabled": app.registry.tool_enabled_for(tool["name"], session_id),
                        }
                    )
        return {"toolsets": toolsets, "dropins": dropins}

    @app.register("tool.set_dropin_enabled")
    async def tool_set_dropin_enabled(params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        enabled = bool(params.get("enabled", False))
        if name not in active_dropins():
            raise RpcError(-32002, "not a drop-in tool", {"name": name})
        session_id = params.get("session_id") or None
        if session_id:
            if app.sessions.get(session_id) is None:
                raise RpcError(-32002, "session not found", {"session_id": session_id})
            app.config.set_session_tool(session_id, name, enabled)
            await notify.emit("state.updated", session_id=session_id)
            return {}
        app.registry.enable_tool(name, enabled)
        app.config.set_dropin(name, enabled)
        return {}

    @app.register("tool.set_enabled")
    async def tool_set_enabled(params: dict[str, Any]) -> dict[str, Any]:
        session_id = params.get("session_id") or None
        if session_id:
            if app.sessions.get(session_id) is None:
                raise RpcError(-32002, "session not found", {"session_id": session_id})
            app.config.set_session_tool(session_id, params["toolset"], bool(params["enabled"]))
            await notify.emit("state.updated", session_id=session_id)
            return {}
        app.registry.enable(params["toolset"], bool(params["enabled"]))
        app.config.set_toolset(params["toolset"], bool(params["enabled"]))
        return {}
