"""Memory CRUD RPC handlers."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.contract import RpcError
from ...core.notify import notify

if TYPE_CHECKING:
    from ...core.runtime import App

def register(app: App) -> None:
    @app.register("memory.list")
    async def memory_list(params: dict[str, Any]) -> dict[str, Any]:
        return {"entries": app.memory.list()}

    @app.register("memory.update")
    async def memory_update(params: dict[str, Any]) -> dict[str, Any]:
        app.memory.update(params["id"], params["text"])
        await notify.emit("memory.updated", entries=app.memory.list())
        return {}

    @app.register("memory.add")
    async def memory_add(params: dict[str, Any]) -> dict[str, Any]:
        text = str(params.get("text") or "").strip()
        if not text:
            raise RpcError(-32602, "memory text must be non-empty")
        app.memory.append(text, badge="user-edited")
        await notify.emit("memory.updated", entries=app.memory.list())
        return {"entries": app.memory.list()}

    @app.register("memory.delete")
    async def memory_delete(params: dict[str, Any]) -> dict[str, Any]:
        if not app.memory.delete(params["id"]):
            raise RpcError(-32002, "memory entry not found", {"id": params["id"]})
        await notify.emit("memory.updated", entries=app.memory.list())
        return {}
