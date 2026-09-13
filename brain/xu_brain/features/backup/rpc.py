"""RPC handlers for filesystem undo, redo, diff, and history."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.contract import RpcError

if TYPE_CHECKING:
    from ...core.runtime import App


def register(app: App) -> None:
    @app.register("fs.undo")
    async def fs_undo(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "")
        if not sid or app.sessions.get(sid) is None:
            raise RpcError(-32002, "session not found", {"session_id": sid})
        session = app.sessions.get(sid)
        cwd = session.cwd if session else ""
        steps = int(params.get("steps", 1))
        path = params.get("path")
        res = app.backups.undo(
            session_id=sid,
            steps=steps,
            path=str(path) if path else None,
            cwd=cwd,
        )
        return res.to_dict()

    @app.register("fs.redo")
    async def fs_redo(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "")
        if not sid or app.sessions.get(sid) is None:
            raise RpcError(-32002, "session not found", {"session_id": sid})
        session = app.sessions.get(sid)
        cwd = session.cwd if session else ""
        steps = int(params.get("steps", 1))
        res = app.backups.redo(session_id=sid, steps=steps, cwd=cwd)
        return res.to_dict()

    @app.register("fs.history")
    async def fs_history(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "")
        if not sid or app.sessions.get(sid) is None:
            raise RpcError(-32002, "session not found", {"session_id": sid})
        limit = int(params.get("limit", 50))
        history = app.backups.history(session_id=sid, limit=limit)
        return {"history": history}

    @app.register("fs.diff")
    async def fs_diff(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "")
        if not sid or app.sessions.get(sid) is None:
            raise RpcError(-32002, "session not found", {"session_id": sid})
        turn_id = params.get("turn_id")
        backup_id = params.get("backup_id")
        diff_text = app.backups.diff(session_id=sid, turn_id=turn_id, backup_id=backup_id)
        return {"diff": diff_text}
