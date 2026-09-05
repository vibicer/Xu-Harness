"""Drop-in tool management RPC handlers."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.contract import RpcError
from .loader import active_dropins

if TYPE_CHECKING:
    from ...core.runtime import App

def register(app: App) -> None:
    @app.register("tool.list")
    async def tool_list(params: dict[str, Any]) -> dict[str, Any]:
        drops = active_dropins()
        toolsets = app.registry.toolsets()
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
                            "enabled": app.registry.tool_enabled(tool["name"]),
                        }
                    )
        return {"toolsets": toolsets, "dropins": dropins}

    @app.register("tool.set_dropin_enabled")
    async def tool_set_dropin_enabled(params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name", ""))
        enabled = bool(params.get("enabled", False))
        if name not in active_dropins():
            raise RpcError(-32002, "not a drop-in tool", {"name": name})
        app.registry.enable_tool(name, enabled)
        app.config.set_dropin(name, enabled)
        return {}

    @app.register("tool.set_enabled")
    async def tool_set_enabled(params: dict[str, Any]) -> dict[str, Any]:
        app.registry.enable(params["toolset"], bool(params["enabled"]))
        app.config.set_toolset(params["toolset"], bool(params["enabled"]))
        return {}
