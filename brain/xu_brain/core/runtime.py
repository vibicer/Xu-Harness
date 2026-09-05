"""Frozen runtime.

The spine: boot → load plugins → serve WS → run the turn loop. This module owns
:class:`App` (the live brain wiring), ``build_app``, ``_register_methods`` (the
~110 RPC methods as slot registrations), ``handle_message`` (the JSON-RPC
envelope) and ``serve`` (the WebSocket + static-web entry point).

Everything here was moved verbatim out of the old ``server.py``; that module is
now a thin re-export shim (see the compatibility note in ``__init__``). The
relative imports below are intra-package (``..``) because this module lives one
level deeper than the old server did.
"""
from __future__ import annotations

import inspect
import json
import logging
import os
from pathlib import Path
from typing import Any, Awaitable, Callable

from websockets.asyncio.server import ServerConnection, serve as ws_serve
from websockets.http11 import Request, Response
from websockets.datastructures import Headers

from .. import __version__
from .activity import activity
from ..features.agent.loop import Agent
from ..features.agent.provider import ProviderManager
from ..features.agent import rpc as agent_rpc
from .governance import ApprovalManager
from .config import Config, default_data_home, ensure_data_home
from .notify import notify
from ..features.memory import MemoryStore
from ..features.memory import rpc as memory_rpc
from ..features.agent.orchestrator import Orchestrator
from ..features import presets as _presets_mod
from ..features.presets import PresetStore
from ..features.presets import rpc as presets_rpc
from ..plugins import PluginBus
from ..plugins import rpc as plugins_rpc
from ..plugins.seed import seed_builtin_plugins
from ..features.tools.registry import ToolRegistry
from ..features.session import SessionStore
from ..features.session import rpc as session_rpc
from ..features.skills import SkillsEngine
from ..features.skills import rpc as skills_rpc
from ..features.tools import all_tools
from ..features.tools import rpc as tools_rpc
from ..features.tools.loader import load_dropin_tools
from .web import serve_web

from .contract import RpcError, _build_payload, _log_rpc_ok
from .host import PluginHost
from .bus import HookBus

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[Any]]


def _rss_mb_self() -> float | None:
    """Resident memory of the Xu brain process itself in MB.

    Linux: ``VmRSS`` from /proc/self/status. Windows: ``WorkingSetSize`` via
    psapi ``GetProcessMemoryInfo`` (stdlib ctypes, no dependency). Deliberately
    does *not* walk the process tree — spawned children (e.g. the browser
    harness) are separate workloads and would inflate the readout with
    double-counted shared pages.

    Returns None where neither source is available so the bar shows —.
    """
    try:  # Linux
        with open("/proc/self/status", "r", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except (OSError, ValueError, IndexError):
        pass

    try:  # Windows
        import ctypes
        from ctypes import wintypes

        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32.dll")
        psapi = ctypes.WinDLL("psapi.dll")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PROCESS_MEMORY_COUNTERS),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if psapi.GetProcessMemoryInfo(kernel32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
            return round(pmc.WorkingSetSize / (1024 * 1024), 1)
    except (OSError, AttributeError):
        pass
    return None


class _SlotStore:
    """Records which plugins filled which slots, for introspection (slots.list).

    The fixed slot set is owned by the Core; this is just
    a registry of what's currently filling each slot, keyed by slot name.
    """

    def __init__(self) -> None:
        self._slots: dict[str, list[dict[str, Any]]] = {}

    def register(self, kind: str, plugin: str, value: Any) -> Callable[[], None]:
        """Record a slot filling; returns a disposer that unrecords it."""
        entries = self._slots.setdefault(kind, [])
        entry = {"kind": kind, "plugin": plugin, "value": value}
        entries.append(entry)

        def dispose() -> None:
            try:
                entries.remove(entry)
            except ValueError:
                pass

        return dispose

    def list(self) -> dict[str, list[dict[str, Any]]]:
        return {
            kind: [{"plugin": e["plugin"], "repr": repr(e["value"])} for e in entries]
            for kind, entries in self._slots.items()
        }


class App:
    def __init__(self, data_home: Path) -> None:
        data_home = ensure_data_home(data_home)
        self.data_home = data_home
        self.config = Config(data_home)
        self.sessions = SessionStore(data_home)
        self.providers = ProviderManager(data_home)
        self.memory = MemoryStore(data_home)
        self.skills = SkillsEngine(data_home)
        # One shared hook bus: PluginBus registers its
        # loaded plugins onto it, ToolRegistry + Agent + PluginHost all use it.
        self.bus = HookBus()
        self.flat_plugins = PluginBus(data_home, bus=self.bus)
        self.flat_plugins.load_dir(data_home / "plugins")
        self.orchestrator = Orchestrator(data_home)
        self.presets = PresetStore(data_home)
        _presets_mod.presets = self.presets
        self.approvals = ApprovalManager(self.config.get("approval_mode", "manual"))
        self.approvals.set_modes(self.config.get("approval_modes", {}))
        self.approvals.set_emitter(notify.emit)
        self.registry = ToolRegistry(self.approvals, hooks=self.bus)
        for tool in all_tools():
            self.registry.register(tool)
        # drop-in custom tools (data_home/tools/*.py): built-ins win on name
        # collision, so a drop-in can never shadow a product tool.
        for dtool in load_dropin_tools(self.data_home):
            if dtool.name not in self.registry.names():
                self.registry.register(dtool)
                # restore persisted per-drop-in enable state
                self.registry.enable_tool(
                    dtool.name, self.config.dropin_enabled(dtool.name)
                )
            else:
                log.warning("drop-in tool %r shadows a built-in; ignored", dtool.name)
        # apply persisted toolset overrides
        for ts in self.registry.toolsets():
            enabled = self.config.toolset_enabled(ts["toolset"], True)
            self.registry.enable(ts["toolset"], enabled)
        self.agent = Agent(
            self.sessions, data_home, self.providers, self.registry,
            self.approvals, self.memory, self.skills, self.flat_plugins,
            self.config, hooks=self.bus,
        )
        self.agent.orchestrator = self.orchestrator
        self.agent.presets = self.presets
        # Load manifest-based plugins (data_home/plugins/<name>/manifest.json)
        # after the live subsystems exist so activate(ctx) can register slots.
        self.slots = _SlotStore()
        # Built-in product defaults for the layout/panel slots: each slot ships with a default so out-of-the-box Xu behaves
        # identically, and plugins can replace/extend them via register_*.
        for lid in ("default", "simple"):
            self.slots.register("layout", "builtin", {"name": lid, "builtin": True})
        for pid in ("chat", "todo", "activity", "logs", "config", "terminal", "agent", "sessions", "onboarding"):
            self.slots.register("panel", "builtin", {"name": pid, "builtin": True})
        # RPC table must exist *before* plugins load: activate(ctx) may call
        # ctx.register_rpc, and _register_methods runs after __init__ anyway.
        self._methods: dict[str, Handler] = {}
        # Shipped packages (data_home/plugins/<name>) before the scan: a fresh
        # install must find the built-ins already on disk, and the host only
        # ever scans this one root.
        seed_builtin_plugins(data_home)
        self.plugins = PluginHost(self.bus)
        self.plugins.load_dir(data_home / "plugins", self)
        # Forge tools reach the plugin host / slot store through the agent.
        self.agent.plugins = self.plugins
        self.agent.slots = self.slots

    def register(self, name: str) -> Callable[[Handler], Handler]:
        def wrap(fn: Handler) -> Handler:
            self._methods[name] = fn
            return fn

        return wrap

    def register_handler(self, name: str, handler: Handler) -> Callable[[], None]:
        """Imperative form of :meth:`register`, for plugins (the `rpc` slot).

        Returns a disposer that restores whatever the method pointed at before,
        so an plugin reload can't permanently shadow a Core method.
        """
        previous = self._methods.get(name)
        self._methods[name] = handler

        def dispose() -> None:
            if self._methods.get(name) is not handler:
                return
            if previous is None:
                self._methods.pop(name, None)
            else:
                self._methods[name] = previous

        return dispose

    async def dispatch(self, name: str | None, params: dict[str, Any]) -> Any:
        if not name:
            raise RpcError(-32600, "invalid request: missing method")
        handler = self._methods.get(name)
        if handler is None:
            raise RpcError(-32601, f"method not found: {name}")
        result = handler(params)
        # Core handlers are async; a plugin's `rpc` slot handler may be sync.
        return await result if inspect.isawaitable(result) else result

    async def startup(self) -> None:
        # Plugins with ``async def activate`` register on the loop; wait for
        # them before we serve, so the first request sees a complete registry.
        await self.plugins.ready()

    async def shutdown(self) -> None:
        # Let plugins release their own resources before the Core tears down.
        await self.plugins.shutdown()
        # Kill the spawned browser (Chrome + throwaway profile cleanup).
        from ..features.tools.browser import BrowserManager
        await BrowserManager.shutdown()
        # Close the shared provider HTTP connection pool.
        if not self.providers._client.is_closed:
            await self.providers._client.aclose()


def build_app(data_home: Path | None = None) -> App:
    app = App(data_home or default_data_home())
    _register_methods(app)
    return app


def _register_methods(app: App) -> None:
    """The Core's own RPC surface, plus each concern's ``register(app)``.

    A handler lives with the code it drives; what stays here is what no
    feature owns: the runtime's own identity/health/diagnostics, its activity
    buffer, and the config + persona surface (``core/config.py`` is Core).
    Registration order is irrelevant — names are unique and ``dispatch`` is a
    per-request dict lookup — but the whole set must still run at the same
    point in ``build_app``, i.e. after plugins load, so a plugin's
    ``ctx.register_rpc`` of a Core name is overridden rather than the reverse.
    """
    agent_rpc.register(app)
    memory_rpc.register(app)
    presets_rpc.register(app)
    plugins_rpc.register(app)
    session_rpc.register(app)
    skills_rpc.register(app)
    tools_rpc.register(app)

    @app.register("app.info")
    async def app_info(params: dict[str, Any]) -> dict[str, Any]:
        return {"version": __version__, "data_home": str(app.data_home), "brain_pid": os.getpid()}

    @app.register("app.status")
    async def app_status(params: dict[str, Any]) -> dict[str, Any]:
        return {
            "brain": "ok",
            "providers": [p.id for p in app.providers.list()],
            "rss_mb": _rss_mb_self(),
        }

    @app.register("config.get")
    async def config_get(params: dict[str, Any]) -> dict[str, Any]:
        raw = app.config.all()
        # Present a stable UI surface while keeping internal keys.
        thr = float(raw.get("compress_threshold", 0.6))
        thr_pct = int(round(thr * 100)) if thr <= 1 else int(thr)
        soul_path = app.data_home / "SOUL.md"
        soul_md = soul_path.read_text("utf-8") if soul_path.exists() else ""
        return {
            "context_length": int(raw.get("context_length", 128_000)),
            "compress_threshold": thr_pct,
            "approval_mode": str(raw.get("approval_mode", "manual")),
            "approval_modes": dict(raw.get("approval_modes", {})),
            "job_timeout": raw.get("job_timeout"),
            "max_parallel_subagents": max(1, int(raw.get("max_parallel_subagents", 100))),
            "retry_max": int(raw.get("retry_max", 10)),
            "retry_interval": int(raw.get("retry_interval", 3)),
            "retain_ratio": float(raw.get("retain_ratio", 0.16)),
            "compaction_retries": int(raw.get("compaction_retries", 1)),
            "context_skill_budget": int(raw.get("context_skill_budget", 6000)),
            "vision_model": raw.get("vision_model"),
            "model_fallbacks": [str(m) for m in (raw.get("model_fallbacks") or []) if isinstance(m, str)],
            "firecrawl_enabled": bool(raw.get("firecrawl_enabled", False)),
            "firecrawl_key": raw.get("firecrawl_key"),
            "prune_keep": int(raw.get("prune_keep", 30)),  # cap on saved sessions (count, not age)
            "soul_md": soul_md,
            "data_home": str(app.data_home),
        }

    @app.register("config.set")
    async def config_set(params: dict[str, Any]) -> dict[str, Any]:
        key = params.get("key")
        value = params.get("value")
        if key is None:
            raise RpcError(-32602, "key required")
        if key == "approval_mode":
            try:
                app.approvals.set_mode(str(value))
            except ValueError as exc:
                raise RpcError(-32005, str(exc)) from exc
            app.config.set("approval_mode", str(value))
        elif key == "approval_modes":
            if not isinstance(value, dict):
                raise RpcError(-32005, "approval_modes must be an object")
            app.approvals.set_modes(value)
            app.config.set("approval_modes", app.approvals._modes)
        elif key == "compress_threshold":
            # UI sends percent 0–100; agent loop expects 0–1 fraction.
            try:
                n = float(value)
            except (TypeError, ValueError) as exc:
                raise RpcError(-32005, f"invalid compress_threshold: {value!r}") from exc
            if not 0 <= n <= 100:
                raise RpcError(-32005, "compress_threshold must be 0–100")
            # n >= 1 is a percent (1 → 0.01); below 1 it's already a fraction.
            app.config.set("compress_threshold", n / 100.0 if n >= 1 else n)
        elif key == "soul_md":
            soul = app.data_home / "SOUL.md"
            soul.write_text(str(value), "utf-8")
        elif key == "retain_ratio":
            try:
                n = float(value)
            except (TypeError, ValueError) as exc:
                raise RpcError(-32005, f"invalid retain_ratio: {value!r}") from exc
            if not 0 < n < 1:
                raise RpcError(-32005, "retain_ratio must be between 0 and 1")
            app.config.set(key, n)
        elif key == "context_skill_budget":
            try:
                n = int(value)
            except (TypeError, ValueError) as exc:
                raise RpcError(-32005, f"invalid context_skill_budget: {value!r}") from exc
            if n < 0:
                raise RpcError(-32005, "context_skill_budget must be >= 0")
            app.config.set(key, n)
        elif key == "compaction_retries":
            app.config.set(key, int(value))
        elif key == "job_timeout":
            if value is None or value == "":
                app.config.set(key, None)
            else:
                try:
                    n = int(value)
                except (TypeError, ValueError) as exc:
                    raise RpcError(-32005, f"invalid job_timeout: {value!r}") from exc
                if n < 1:
                    raise RpcError(-32005, "job_timeout must be >= 1 or never")
                app.config.set(key, n)
        elif key == "max_parallel_subagents":
            try:
                n = int(value)
            except (TypeError, ValueError) as exc:
                raise RpcError(-32005, f"invalid max_parallel_subagents: {value!r}") from exc
            if n < 1:
                raise RpcError(-32005, "max_parallel_subagents must be >= 1")
            app.config.set(key, n)
        elif key == "prune_keep":
            try:
                n = int(value)
            except (TypeError, ValueError) as exc:
                raise RpcError(-32005, f"invalid prune_keep: {value!r}") from exc
            if n < 1:
                raise RpcError(-32005, "prune_keep must be >= 1")
            app.config.set("prune_keep", n)
            # Enforce the new cap immediately so lowering the limit trims old sessions.
            try:
                app.sessions.prune(keep=n)
            except Exception:  # noqa: BLE001 — pruning is best-effort
                pass
        elif key == "model_fallbacks":
            if not isinstance(value, list):
                raise RpcError(-32005, "model_fallbacks must be an array of model ids")
            # Ordered + deduped; blanks dropped so the chain never stalls on "".
            chain: list[str] = []
            for m in value:
                if not isinstance(m, str) or not m.strip():
                    continue
                if m not in chain:
                    chain.append(m)
            app.config.set(key, chain)
        else:
            app.config.set(key, value)
        return {}

    @app.register("approval.resolve")
    async def approval_resolve(params: dict[str, Any]) -> dict[str, Any]:
        request_id = params["request_id"]
        approved = bool(params.get("approved", False))
        remember = bool(params.get("remember", False))
        ok = app.approvals.resolve(request_id, approved, remember=remember)
        if ok:
            sid = app.approvals.session_for(request_id)
            await notify.emit(
                "turn.approval_resolved", request_id=request_id, approved=approved,
                session_id=sid,
            )
        return {"resolved": ok}

    @app.register("persona.list")
    async def persona_list(params: dict[str, Any]) -> dict[str, Any]:
        return {"personas": app.config.list_personas(), "active": app.config.active_persona()}

    @app.register("persona.get")
    async def persona_get(params: dict[str, Any]) -> dict[str, Any]:
        pid = params["id"]
        text = app.config.get_persona(pid)
        if text is None:
            raise RpcError(-32002, "persona not found", {"id": pid})
        return {"id": pid, "text": text}

    @app.register("persona.upsert")
    async def persona_upsert(params: dict[str, Any]) -> dict[str, Any]:
        pid = app.config.save_persona(params["id"], params.get("text", ""))
        return {"id": pid, "personas": app.config.list_personas()}

    @app.register("persona.delete")
    async def persona_delete(params: dict[str, Any]) -> dict[str, Any]:
        app.config.delete_persona(params["id"])
        return {"personas": app.config.list_personas()}

    @app.register("persona.set_active")
    async def persona_set_active(params: dict[str, Any]) -> dict[str, Any]:
        pid = params.get("id") or None
        if pid is not None and app.config.get_persona(pid) is None:
            raise RpcError(-32002, "persona not found", {"id": pid})
        app.config.set_active_persona(pid)
        return {"active": pid}

    @app.register("logs.list")
    async def logs_list(params: dict[str, Any]) -> dict[str, Any]:
        return {
            "logs": activity.entries(
                int(params.get("limit", 200)),
                source=params.get("source"),
                level=params.get("level"),
            )
        }

    @app.register("app.doctor")
    async def app_doctor(params: dict[str, Any]) -> dict[str, Any]:
        checks: list[dict[str, Any]] = []
        checks.append({"name": "data_home", "ok": app.data_home.is_dir(),
                       "detail": str(app.data_home)})
        providers = app.providers.list()
        checks.append({"name": "providers", "ok": len(providers) > 0,
                       "detail": f"{len(providers)} configured"})
        for p in providers[:3]:
            ok, latency, models, error = await app.providers.fetch_models(p)
            checks.append({"name": f"provider:{p.id}", "ok": ok, "detail": error or f"{len(models)} models"})
        return {"checks": checks}




async def handle_message(app: App, ws: ServerConnection, raw: str | bytes) -> None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", "replace")
    try:
        message = json.loads(raw)
    except json.JSONDecodeError:
        await ws.send(json.dumps({"jsonrpc": "2.0", "error": {"code": -32700, "message": "parse error"}}))
        return

    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        await ws.send(json.dumps({"jsonrpc": "2.0", "error": {"code": -32600, "message": "invalid request"}}))
        return

    method = message.get("method")
    params = message.get("params") or {}
    msg_id = message.get("id")

    if not isinstance(params, dict):
        if msg_id is not None:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": msg_id,
                "error": {"code": -32602, "message": "invalid params: expected an object"},
            }))
        return

    if msg_id is None:
        # Notification — no reply expected.
        try:
            await app.dispatch(method, params)
        except Exception as exc:  # noqa: BLE001
            if method:
                activity.record(
                    "error", "brain", f"rpc {method} failed", str(exc),
                    payload=_build_payload(method, params, error=exc),
                )
        return

    try:
        result = await app.dispatch(method, params)
        reply = {"jsonrpc": "2.0", "id": msg_id, "result": result}
        if _log_rpc_ok(method):
            activity.record(
                "info", "brain", f"rpc {method} → ok",
                payload=_build_payload(method, params, result=result),
            )
    except RpcError as exc:
        reply = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": exc.code, "message": str(exc), "data": exc.data}}
        if method and not method.startswith("logs."):
            activity.record(
                "warn", "brain", f"rpc {method} rejected", str(exc),
                payload=_build_payload(method, params, error=exc),
            )
    except Exception as exc:  # noqa: BLE001 — last-resort envelope
        reply = {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32603, "message": str(exc)}}
        if method:
            activity.record(
                "error", "brain", f"rpc {method} failed", str(exc),
                payload=_build_payload(method, params, error=exc),
            )

    await ws.send(json.dumps(reply))


async def serve(
    host: str = "127.0.0.1",
    port: int = 9876,
    data_home: Path | None = None,
    web_dir: str | Path | None = None,
) -> None:
    app = build_app(data_home)
    await app.startup()
    activity.record("info", "brain", f"brain started {__version__}", f"data={app.data_home}")

    def _reject_cross_origin(connection: ServerConnection, request: Request) -> Response | None:
        """Deny browser cross-origin WS handshakes (drive-by attack vector).

        Browsers always send Origin on WS; non-browser clients may omit it.
        Only same-machine loopback origins are allowed — a webpage can point
        JS at ws://127.0.0.1:9876 but cannot spoof its Origin header.
        """
        origin = request.headers.get("Origin")
        if origin is None:
            return None  # non-browser client (xu shell, scripts) — allow
        from urllib.parse import urlparse
        try:
            o = urlparse(origin)
        except ValueError:
            o = None
        host = (o.hostname or "") if o else ""
        if o and o.scheme in ("http", "https") and host in ("127.0.0.1", "localhost", "::1"):
            return None  # our own webui
        return Response(403, "cross-origin websocket connections are not allowed", Headers())

    async def handler(ws: ServerConnection) -> None:
        notify.attach(ws)
        try:
            async for raw in ws:
                await handle_message(app, ws, raw)
        except Exception:  # noqa: BLE001
            pass
        finally:
            notify.detach(ws)

    # Bind the WS port FIRST — it is the contract. Only announce readiness
    # after the socket is actually bound, so launchers/logs never lie.
    try:
        server = await ws_serve(handler, host, port, process_request=_reject_cross_origin)
    except OSError as exc:
        activity.record("error", "brain", "brain failed to start", f"{host}:{port} already in use ({exc})")
        print(f"[xu-brain] FAILED to start: {host}:{port} already in use ({exc})", flush=True)
        print("[xu-brain] is another xu-brain already running? try `xu status` / `xu restart`", flush=True)
        return
    print(f"[xu-brain] {__version__} data={app.data_home} listening ws://{host}:{port}", flush=True)

    if web_dir:
        served = False
        base = int(os.environ.get("XU_WEB_PORT", "1421"))
        for p in (base, base + 1, base + 2):
            try:
                # serve_web returns None when the dist dir is missing — that's
                # not "served", keep polling ports only for real binds.
                if serve_web(Path(web_dir), p, app.plugins.ui_asset) is not None:
                    served = True
                    break
            except OSError:
                continue
        if not served:
            print(f"[xu-brain] webui skipped: ports {base}-{base + 2} busy (WS-only mode)", flush=True)

    try:
        async with server:
            await server.serve_forever()
    finally:
        activity.record("info", "brain", "brain stopping")
        await app.shutdown()
