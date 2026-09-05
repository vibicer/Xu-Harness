"""Skill management RPC handlers."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.contract import RpcError

if TYPE_CHECKING:
    from ...core.runtime import App

def register(app: App) -> None:
    @app.register("skill.list")
    async def skill_list(params: dict[str, Any]) -> dict[str, Any]:
        # all=True keeps OFF skills visible so the UI can render toggles
        return {"skills": app.skills.list(all=True)}

    @app.register("skill.set")
    async def skill_set(params: dict[str, Any]) -> dict[str, Any]:
        sid = params["id"]
        if params.get("enabled", True):
            app.skills.enable(sid)
        else:
            app.skills.disable(sid)
        return {"skills": app.skills.list(all=True)}

    @app.register("skill.load")
    async def skill_load(params: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"id": params["id"], "body": app.skills.load(params["id"])}
        except (KeyError, ValueError, FileNotFoundError) as exc:
            raise RpcError(-32602, f"skill load failed: {exc}") from exc

    @app.register("skill.body")
    async def skill_body(params: dict[str, Any]) -> dict[str, Any]:
        try:
            return {"id": params["id"], "body": app.skills.read_body(params["id"])}
        except (KeyError, ValueError, FileNotFoundError) as exc:
            raise RpcError(-32602, f"skill body read failed: {exc}") from exc

    @app.register("skill.add")
    async def skill_add(params: dict[str, Any]) -> dict[str, Any]:
        try:
            app.skills.create(params["id"], params["name"], params["desc"], params["body"], params.get("keywords"))
        except (KeyError, ValueError, FileNotFoundError) as exc:
            raise RpcError(-32602, f"skill add failed: {exc}") from exc
        return {"skills": app.skills.list(all=True)}

    @app.register("skill.update")
    async def skill_update(params: dict[str, Any]) -> dict[str, Any]:
        patch = {key: params[key] for key in ("name", "desc", "body", "keywords") if key in params}
        try:
            app.skills.save(params["id"], **patch)
        except (KeyError, ValueError, FileNotFoundError) as exc:
            raise RpcError(-32602, f"skill update failed: {exc}") from exc
        return {"skills": app.skills.list(all=True)}

    @app.register("skill.remove")
    async def skill_remove(params: dict[str, Any]) -> dict[str, Any]:
        try:
            app.skills.remove(params["id"])
        except (KeyError, ValueError, FileNotFoundError) as exc:
            raise RpcError(-32602, f"skill remove failed: {exc}") from exc
        return {"skills": app.skills.list(all=True)}
