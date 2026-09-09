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

    @app.register("memory.replace_all")
    async def memory_replace_all(params: dict[str, Any]) -> dict[str, Any]:
        """Whole-file editor: paragraphs map positionally onto entries.

        Changed paragraph -> update, removed/blanked paragraph -> delete,
        new paragraph -> append. Unchanged paragraphs keep their badge.
        """
        paras = params.get("entries")
        if not isinstance(paras, list) or not all(isinstance(p, str) for p in paras):
            raise RpcError(-32602, "entries must be an array of strings")
        texts = [p.strip() for p in paras]
        current = app.memory.list()
        for i, entry in enumerate(current):
            if i >= len(texts) or not texts[i]:
                app.memory.delete(entry["id"])  # paragraph removed or blanked
            elif texts[i] != entry["text"]:
                app.memory.update(entry["id"], texts[i])
        for text in texts[len(current):]:
            if text:
                app.memory.append(text, badge="user-edited")
        await notify.emit("memory.updated", entries=app.memory.list())
        return {"entries": app.memory.list()}

    @app.register("memory.mnemo")
    async def memory_mnemo(params: dict[str, Any]) -> dict[str, Any]:
        """Built-in Mnemosyne status: package availability + store location."""
        import os

        from .mnemo import available, embeddings_enabled, embeddings_installed

        return {"available": available(),
                "data_dir": os.environ.get("MNEMOSYNE_DATA_DIR", ""),
                "embeddings_enabled": embeddings_enabled(),
                "embeddings_installed": embeddings_installed()}
