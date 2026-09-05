"""Agent-scoped JSON-RPC handlers: model providers and subagent orchestration."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ...core.contract import RpcError
from ...core.notify import notify
from .provider import Provider

if TYPE_CHECKING:
    from ...core.runtime import App


def _sessionless_ctx(app: App):
    """Build a minimal ToolContext for spawning a subagent from an RPC call
    (no session yet). Session-less spawns default to the repo root cwd."""
    from ..session import DEFAULT_CWD
    from ..tools.base import ToolContext

    return ToolContext(
        data_home=app.data_home,
        session_id="",
        turn_id="",
        cwd=DEFAULT_CWD,
        agent=app.agent,
        events=notify,
        approvals=app.approvals,
        providers=app.providers,
        memory=app.memory,
        skills=app.skills,
        flat_plugins=app.flat_plugins,
        config={**app.config.all()},
    )


def register(app: App) -> None:
    @app.register("provider.list")
    async def provider_list(params: dict[str, Any]) -> dict[str, Any]:
        return {"providers": [p.to_dict() for p in app.providers.list()]}

    @app.register("provider.upsert")
    async def provider_upsert(params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        base_url = str(params.get("base_url") or "").strip()
        if not base_url:
            raise RpcError(-32602, "base_url required")
        ptype = str(params.get("type") or "")
        if ptype and ptype not in ("openai-compatible", "anthropic-compatible"):
            raise RpcError(-32602, "type must be openai-compatible or anthropic-compatible")
        existing_id = str(params.get("id") or "").strip()
        if existing_id:
            pid = existing_id
        elif name:
            pid = name.lower().replace(" ", "-")
        else:
            pid = f"p{abs(hash(base_url)) % 100000}"
        models = params.get("models") or []
        if not isinstance(models, list):
            raise RpcError(-32602, "models must be a list")
        existing = app.providers.get(pid)
        # An update that omits `type` (the edit form) must keep the provider's
        # current routing — defaulting it to openai-compatible would silently
        # convert an anthropic-compatible provider and break its requests.
        if not ptype and existing is not None:
            ptype = existing.type
        ptype = ptype or "openai-compatible"
        provider = Provider(
            id=pid,
            type=ptype,
            base_url=base_url,
            models=[str(m) for m in models if str(m).strip()],
            key_set=bool(params.get("api_key")) or (existing.key_set if existing else False),
            name=name or (existing.name if existing else ""),
        )
        app.providers.upsert(provider, params.get("api_key") or None)
        return {"id": provider.id}

    @app.register("provider.delete")
    async def provider_delete(params: dict[str, Any]) -> dict[str, Any]:
        app.providers.delete(params["id"])
        return {}

    @app.register("provider.set_enabled")
    async def provider_set_enabled(params: dict[str, Any]) -> dict[str, Any]:
        pid = params.get("id")
        provider = app.providers.get(pid)
        if provider is None:
            raise RpcError(-32002, "provider not found", {"provider_id": pid})
        provider.enabled = bool(params.get("enabled", True))
        app.providers.save()
        return {"id": provider.id, "enabled": provider.enabled}

    @app.register("provider.test")
    async def provider_test(params: dict[str, Any]) -> dict[str, Any]:
        provider = app.providers.get(params["id"])
        if provider is None:
            raise RpcError(-32002, "provider not found", {"provider_id": params["id"]})
        ok, latency, models, error = await app.providers.fetch_models(provider)
        if ok and models:
            provider.models = models
            app.providers.save()
        return {"ok": ok, "latency_ms": latency, "models": models, "error": error}

    @app.register("keychain.get")
    async def keychain_get(params: dict[str, Any]) -> dict[str, Any]:
        """Resolve a key for a provider id. Shell should override with the OS
        keyring; the brain's dev path (env XU_KEY_<ID> or in-memory) is the fallback."""
        key = await app.providers.get_key(params["id"])
        return {"key": key}

    @app.register("reply.resolve")
    async def reply_resolve(params: dict[str, Any]) -> dict[str, Any]:
        request_id = params["request_id"]
        answer = str(params.get("answer", ""))
        ok = app.agent.resolve_reply(request_id, answer)
        if ok:
            sid = app.agent.reply_session(request_id)
            await notify.emit(
                "turn.approval_resolved", request_id=request_id, approved=True,
                answer=answer, session_id=sid,
            )
        return {"resolved": ok}

    @app.register("subagent.list")
    async def subagent_list(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "") or None
        return {"subagents": app.orchestrator.list_subagents(sid)}

    @app.register("subagent.send")
    async def subagent_send(params: dict[str, Any]) -> dict[str, Any]:
        """Spawn a managed background subagent; returns its durable ``s…`` id."""
        prompt = str(params.get("prompt") or "").strip()
        if not prompt:
            raise RpcError(-32602, "prompt required")
        session = app.sessions.get(str(params.get("session_id") or "")) if params.get("session_id") else None
        # Fall back to a sessionless context so spawning still works from the UI.
        ctx = app.agent._ctx(session, "", None) if session else _sessionless_ctx(app)
        sid = await app.agent.spawn_managed_subagent(prompt, ctx, tools=params.get("tools"))
        return {"id": sid}

    @app.register("subagent.message")
    async def subagent_message(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "") or None
        try:
            ok = await app.agent.subagent_message(
                str(params.get("id") or ""), str(params.get("message") or ""),
                parent_session=sid,
            )
        except (LookupError, RuntimeError) as exc:
            return {"accepted": False, "error": str(exc)}
        return {"accepted": ok}

    @app.register("subagent.interrupt")
    async def subagent_interrupt(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "") or None
        ok = await app.agent.subagent_interrupt(str(params.get("id") or ""), parent_session=sid)
        if not ok:
            raise RpcError(-32002, "subagent not found or not running", {"id": params.get("id")})
        return {"ok": True}

    @app.register("subagent.activity")
    async def subagent_activity(params: dict[str, Any]) -> dict[str, Any]:
        """What a delegated sub-agent is doing: one run by ``run_id`` (as
        carried on the delegate tool chip), every sibling of one fan-out by
        ``group``, or every run a session delegated."""
        runs = app.agent.delegation_activity(
            run_id=str(params.get("run_id") or "") or None,
            session_id=str(params.get("session_id") or "") or None,
            group=str(params.get("group") or "") or None,
        )
        return {"runs": runs}
