"""Preset CRUD RPC handlers."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.contract import RpcError

if TYPE_CHECKING:
    from ...core.runtime import App

def register(app: App) -> None:
    @app.register("preset.list")
    async def preset_list(params: dict[str, Any]) -> dict[str, Any]:
        return {"presets": app.presets.list()}

    @app.register("preset.get")
    async def preset_get(params: dict[str, Any]) -> dict[str, Any]:
        preset = app.presets.get(str(params.get("id") or ""))
        if preset is None:
            raise RpcError(-32002, "preset not found", {"id": params.get("id")})
        return {"preset": preset.to_dict()}

    @app.register("preset.upsert")
    async def preset_upsert(params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        tree = params.get("tree") or params.get("root")
        pid = params.get("id") or None
        if not name:
            raise RpcError(-32602, "name is required")
        if not isinstance(tree, dict):
            raise RpcError(-32602, "tree (root node) is required")
        preset = app.presets.upsert(pid, name, tree)
        return {"preset": preset.to_dict()}

    @app.register("preset.delete")
    async def preset_delete(params: dict[str, Any]) -> dict[str, Any]:
        pid = str(params.get("id") or "")
        if not app.presets.delete(pid):
            raise RpcError(-32002, "preset not found", {"id": params.get("id")})
        # Drop any session binding to the deleted preset.
        return {}
