"""MCP client plugin — every configured server's tools, in the agent's registry.

Config lives in ``data_home/mcp.json`` and uses the shape every other client
uses, so an existing config pastes in unchanged::

    {
      "mcpServers": {
        "files": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv/data"],
          "env": {"API_KEY": "${MY_KEY}"},
          "cwd": ".",
          "enabled": true,
          "timeout": 30
        }
      }
    }

Only the stdio transport is implemented; an ``http``/``sse`` entry is reported
and skipped rather than silently ignored.

Tools land as ``mcp__<server>_<tool>`` in the ``mcp`` toolset and are **RISKY**
by default — an MCP server is third-party code running on this machine, so its
tools prompt in manual mode exactly like ``bash`` does. List a server in the
``trusted_servers`` setting to drop its tools to NEVER.

A server that fails to start is logged and skipped: the plugin still loads, and
the remaining servers still work.

Servers are managed live — no restart to add one. ``Config → MCP`` drives six
RPCs (``mcp.list``, ``mcp.add``, ``mcp.remove``, ``mcp.connect``,
``mcp.disconnect``, ``mcp.set``), all of them thin skins over :class:`Hub` so
the panel and the agent see one state. The agent gets one tool, ``mcp``, with
``list`` / ``tools`` / ``connect`` / ``disconnect``: adding a server means
running a command nobody vetted, so that stays human-only.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any

from xu_brain.core.contract import RpcError
from xu_brain.core.governance import ApprovalLevel
from xu_brain.features.tools.base import ToolResult

# The host execs activate.py directly (no package, no sys.path entry), so a
# sibling module has to be loaded by path.
_HERE = Path(__file__).resolve().parent


def _sibling(name: str) -> Any:
    spec = importlib.util.spec_from_file_location(f"xu_mcp_{name}", _HERE / f"{name}.py")
    if spec is None or spec.loader is None:  # pragma: no cover — packaging bug
        raise ImportError(f"cannot load {name}.py next to {__file__}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_client = _sibling("client")
StdioClient = _client.StdioClient
MCPError = _client.MCPError

DEFAULTS: dict[str, Any] = {"timeout_ms": 30000, "trusted_servers": []}
CONFIG_FILE = "mcp.json"
TOOLSET = "mcp"
# Providers reject function names past 64 chars, and a long server name plus a
# long tool name blows through that easily.
_MAX_NAME = 64
# Anthropic caps a tool description at 1024; longer is silently rejected.
_MAX_DESC = 1024

# A server name is a config key and part of every tool name it contributes.
_NAME_OK = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")

# The live hub, so deactivate() can reap every child process and the RPCs can
# reach the same state the agent tool sees.
_HUB: Hub | None = None


def tool_name(server: str, tool: str) -> str:
    """``mcp__<server>_<tool>``, sanitized and length-capped.

    A tool already prefixed with its own server name loses the stutter
    (``caido`` + ``caido_list_projects`` → ``mcp__caido_list_projects``): the
    duplicate buys nothing and every character is prompt budget.

    The cap keeps a stable 8-char hash of the full name so two long tools on
    the same server cannot collapse onto one truncated name.
    """
    bare = tool[len(server) + 1:] if tool.lower().startswith(f"{server.lower()}_") else tool
    raw = f"mcp__{server}_{bare}".lower()
    safe = re.sub(r"[^a-z0-9_]+", "_", raw).strip("_") or "mcp_tool"
    if len(safe) > _MAX_NAME:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[: _MAX_NAME - 9]}_{digest}"
    return safe


def _duplicates(text: str, structured: dict[str, Any]) -> bool:
    """Whether ``text`` already carries exactly ``structured``.

    Servers routinely send the same payload twice — once as a text block, once
    as ``structuredContent`` — and the text form is usually compact JSON, so a
    substring test against a pretty dump misses it and the model pays for the
    payload twice.
    """
    stripped = text.strip()
    if not stripped:
        return False
    try:
        return json.loads(stripped) == structured
    except ValueError:
        return json.dumps(structured, ensure_ascii=False, indent=2) in text


def render(result: dict[str, Any]) -> tuple[str, bool]:
    """MCP tool result → (model-facing text, is_error)."""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text", "")))
        elif kind == "resource_link":
            parts.append(f"[resource] {item.get('uri', '')}")
        elif kind == "resource":
            resource = item.get("resource")
            text = resource.get("text") if isinstance(resource, dict) else None
            parts.append(str(text) if text else f"[resource] {item.get('uri', '')}")
        else:
            # Images and audio are bytes the loop cannot forward as tool output.
            parts.append(f"[{kind or 'unknown'} content omitted]")
    text = "\n".join(p for p in parts if p)
    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured and not _duplicates(text, structured):
        blob = json.dumps(structured, ensure_ascii=False, indent=2)
        text = f"{text}\n```json\n{blob}\n```".strip()
    is_error = bool(result.get("isError"))
    if not text:
        text = "Tool failed with no message." if is_error else "(no output)"
    return text, is_error


class MCPTool:
    """One remote MCP tool, wearing the local :class:`Tool` protocol."""

    toolset = TOOLSET
    output_schema = None

    def __init__(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        client: Any,
        remote: str,
        approval: ApprovalLevel,
    ) -> None:
        self.name = name
        self.description = description[:_MAX_DESC]
        self.schema = schema
        self.approval = approval
        self._client = client
        self._remote = remote

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        try:
            result = await self._client.call_tool(self._remote, args or {})
        except MCPError as exc:
            # A dead or slow server is an expected failure, not a crash: report
            # it as a tool error so the model can react instead of the turn
            # blowing up.
            return ToolResult.err(str(exc), server=self._client.name)
        text, is_error = render(result)
        if is_error:
            return ToolResult.err(text, raw=result, server=self._client.name)
        return ToolResult.ok(text, raw=result, server=self._client.name)


def _load_raw(data_home: Path) -> dict[str, Any]:
    """``mcp.json`` as a dict; ``{}`` when absent, ``ValueError`` when broken."""
    path = Path(data_home) / CONFIG_FILE
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: {exc}") from exc
    return data if isinstance(data, dict) else {}


def read_config(data_home: Path) -> dict[str, dict[str, Any]]:
    """``mcpServers`` from ``data_home/mcp.json``; empty when absent or bad."""
    servers = _load_raw(data_home).get("mcpServers")
    if not isinstance(servers, dict):
        return {}
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)}


def write_config(data_home: Path, servers: dict[str, dict[str, Any]]) -> None:
    """Persist ``mcpServers``, keeping any other top-level key untouched.

    Atomic via a same-directory temp file: this file is what the plugin reads
    on every boot, so a half-written one would cost the user every server.
    """
    path = Path(data_home) / CONFIG_FILE
    data = _load_raw(data_home)
    data["mcpServers"] = servers
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, path)


def check_spec(spec: dict[str, Any]) -> str | None:
    """Why this server cannot be started, or ``None`` when it can."""
    transport = str(spec.get("type") or "stdio").lower()
    if transport != "stdio" or spec.get("url"):
        return f"{transport} transport is not supported yet"
    if not str(spec.get("command") or "").strip():
        return "no 'command'"
    return None


def _schema_of(tool: dict[str, Any]) -> dict[str, Any]:
    schema = tool.get("inputSchema")
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    return schema


async def _connect(name: str, spec: dict[str, Any], timeout: float, log: Any) -> Any:
    client = StdioClient(
        name=name,
        command=str(spec.get("command", "")),
        args=[str(a) for a in (spec.get("args") or [])],
        env=spec.get("env") if isinstance(spec.get("env"), dict) else None,
        cwd=str(spec["cwd"]) if spec.get("cwd") else None,
        # A per-server `timeout` (seconds) beats the plugin-wide setting.
        timeout=float(spec["timeout"]) if spec.get("timeout") is not None else timeout,
        log=log,
    )
    await client.start()
    return client


class Session:
    """One configured server: its spec, its live client, the tools it owns."""

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        self.name = name
        self.spec = spec
        self.client: Any | None = None
        self.tools: list[str] = []
        self.error: str | None = None
        self.info: dict[str, Any] = {}
        # False once the server is gone from mcp.json but still running.
        self.configured = True
        # A double-clicked Connect must not spawn a second child process.
        self.lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self.spec.get("enabled") is not False

    @property
    def state(self) -> str:
        if self.client is not None:
            return "on"
        return "error" if self.error else "off"


class Hub:
    """Every configured server, its connection, and the tools it contributed.

    The RPCs and the ``mcp`` tool are thin skins over these methods, so the
    panel and the agent can never disagree about what is running.
    """

    def __init__(self, ctx: Any) -> None:
        self.ctx = ctx
        conf = {**DEFAULTS, **ctx.settings()}
        ms = conf.get("timeout_ms")
        ok = isinstance(ms, (int, float)) and not isinstance(ms, bool)
        self.timeout = float(ms) / 1000.0 if ok else 30.0
        self.trusted = {str(s) for s in (conf.get("trusted_servers") or [])}
        self.data_home = Path(getattr(ctx.app, "data_home", "."))
        self.registry = getattr(ctx.app, "registry", None)
        self.sessions: dict[str, Session] = {}
        self.config_error: str | None = None

    # ---- config ------------------------------------------------------ #

    def sync(self) -> None:
        """Fold ``mcp.json`` into the session map — it can be hand-edited."""
        try:
            specs = read_config(self.data_home)
        except ValueError as exc:
            self.config_error = str(exc)
            return
        self.config_error = None
        for name, spec in specs.items():
            live = self.sessions.get(name)
            if live is None:
                self.sessions[name] = Session(name, spec)
                continue
            live.configured = True
            if live.client is None:
                live.spec = spec  # a disconnected session re-reads its spec
        for name in [n for n in self.sessions if n not in specs]:
            session = self.sessions[name]
            if session.client is None:
                del self.sessions[name]
            else:
                session.configured = False

    def _need(self, name: str) -> Session:
        session = self.sessions.get(name)
        if session is None:
            raise ValueError(f"unknown server: {name}")
        return session

    # ---- connection --------------------------------------------------- #

    def _register(self, session: Session, tools: list[dict[str, Any]]) -> list[str]:
        approval = ApprovalLevel.NEVER if session.name in self.trusted else ApprovalLevel.RISKY
        names: list[str] = []
        for tool in tools:
            remote = str(tool.get("name") or "")
            if not remote:
                continue
            local = tool_name(session.name, remote)
            if local in names:
                # Two remote names sanitized onto one local name; keeping the
                # first is arbitrary but at least deterministic.
                self.ctx.log(
                    f"{session.name}: {remote} collides with an earlier tool; skipped",
                    level="warning",
                )
                continue
            self.ctx.register_tool(MCPTool(
                name=local,
                description=f"[{session.name}] {tool.get('description') or remote}",
                schema=_schema_of(tool),
                client=session.client,
                remote=remote,
                approval=approval,
            ))
            names.append(local)
        return names

    async def connect(self, name: str) -> Session:
        session = self._need(name)
        async with session.lock:
            if session.client is not None:
                return session
            why = check_spec(session.spec)
            if why:
                session.error = why
                return session
            client = None
            try:
                client = await _connect(name, session.spec, self.timeout, self.ctx.log)
                tools = await client.list_tools()
            except Exception as exc:  # noqa: BLE001 — a third-party process: any failure is data
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.close()
                session.error = str(exc) or exc.__class__.__name__
                self.ctx.log(f"{name}: {session.error}", level="warning")
                return session
            session.client = client
            session.error = None
            session.info = dict(client.server_info or {})
            session.tools = self._register(session, tools)
            self.ctx.log(f"{name}: {len(session.tools)} tools ({session.info.get('name', '?')})")
        return session

    async def disconnect(self, name: str) -> Session:
        session = self._need(name)
        async with session.lock:
            for local in session.tools:
                if self.registry is not None:
                    self.registry.unregister(local)
            session.tools = []
            client, session.client = session.client, None
            if client is not None:
                with contextlib.suppress(Exception):
                    await client.close()
        return session

    async def start_all(self) -> None:
        self.sync()
        names = sorted(n for n, s in self.sessions.items() if s.enabled)
        if not names:
            self.ctx.log(f"no MCP servers configured ({self.data_home / CONFIG_FILE})")
            return
        await asyncio.gather(*(self.connect(n) for n in names), return_exceptions=True)
        tools = sum(len(s.tools) for s in self.sessions.values())
        live = sum(1 for s in self.sessions.values() if s.client is not None)
        if tools:
            self.ctx.log(f"{tools} MCP tools from {live} server(s)")

    async def stop_all(self) -> None:
        await asyncio.gather(
            *(self.disconnect(n) for n in list(self.sessions)), return_exceptions=True,
        )

    # ---- mutation ----------------------------------------------------- #

    def _entry(self, spec: dict[str, Any]) -> dict[str, Any]:
        """A config entry built from known keys only.

        Whitelisting is the point: this dict is written to ``mcp.json`` from an
        unauthenticated local RPC, so anything the caller invented gets dropped
        rather than persisted.
        """
        entry: dict[str, Any] = {"command": str(spec.get("command") or "").strip()}
        # `type`/`url` ride along even though only stdio works: the whitelist
        # must not drop a field the validator rejects on, or an http entry
        # would pass the check and then be spawned as a local command.
        transport = str(spec.get("type") or "").strip().lower()
        if transport and transport != "stdio":
            entry["type"] = transport
        if spec.get("url"):
            entry["url"] = str(spec["url"])
        args = spec.get("args")
        # A one-line command from the panel is friendlier than a JSON array.
        if isinstance(args, str):
            args = shlex.split(args)
        if args:
            entry["args"] = [str(a) for a in args]
        env = spec.get("env")
        if isinstance(env, dict) and env:
            entry["env"] = {str(k): str(v) for k, v in env.items()}
        if spec.get("cwd"):
            entry["cwd"] = str(spec["cwd"])
        timeout = spec.get("timeout")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            entry["timeout"] = float(timeout)
        if spec.get("enabled") is False:
            entry["enabled"] = False
        return entry

    async def add(self, spec: dict[str, Any], *, connect: bool = True) -> Session:
        name = str(spec.get("name") or "").strip()
        if not _NAME_OK.match(name):
            raise ValueError("name: 1-48 chars of letters, digits, '_', '-' or '.'")
        entry = self._entry(spec)
        why = check_spec(entry)
        if why:
            raise ValueError(why)
        self.sync()
        if name in self.sessions:
            raise ValueError(f"{name} already exists")
        servers = read_config(self.data_home)
        servers[name] = entry
        write_config(self.data_home, servers)
        self.sessions[name] = Session(name, entry)
        if connect and entry.get("enabled") is not False:
            return await self.connect(name)
        return self.sessions[name]

    async def remove(self, name: str) -> None:
        self.sync()
        servers = read_config(self.data_home)
        if name not in servers and name not in self.sessions:
            raise ValueError(f"unknown server: {name}")
        if name in self.sessions:
            await self.disconnect(name)
            del self.sessions[name]
        if servers.pop(name, None) is not None:
            write_config(self.data_home, servers)

    async def set_enabled(self, name: str, enabled: bool) -> Session:
        """Autostart on/off. Disabling also stops it now — a switch that
        only takes effect next boot is a switch nobody trusts."""
        self.sync()
        servers = read_config(self.data_home)
        if name not in servers:
            raise ValueError(f"unknown server: {name}")
        if enabled:
            servers[name].pop("enabled", None)
        else:
            servers[name]["enabled"] = False
        write_config(self.data_home, servers)
        session = self._need(name)
        session.spec = servers[name]
        if enabled:
            return await self.connect(name)
        await self.disconnect(name)
        session.error = None
        return session

    async def set_trusted(self, name: str, trusted: bool) -> Session:
        """Trusted = this server's tools stop asking for approval.

        The level is baked into each tool at registration, so a live server is
        reconnected to re-register at the new level.
        """
        self._need(name)
        if trusted:
            self.trusted.add(name)
        else:
            self.trusted.discard(name)
        self.ctx.set_setting("trusted_servers", sorted(self.trusted))
        if self.sessions[name].client is not None:
            await self.disconnect(name)
            return await self.connect(name)
        return self.sessions[name]

    # ---- reporting ---------------------------------------------------- #

    def describe(self, session: Session) -> dict[str, Any]:
        spec = session.spec
        return {
            "name": session.name,
            "command": str(spec.get("command") or ""),
            "args": [str(a) for a in (spec.get("args") or [])],
            "cwd": str(spec["cwd"]) if spec.get("cwd") else "",
            # Keys only: a value is either a literal token or a ${VAR} the
            # brain expands, and neither belongs in a UI payload.
            "env_keys": sorted(str(k) for k in (spec.get("env") or {})),
            "transport": str(spec.get("type") or "stdio").lower(),
            "enabled": session.enabled,
            "configured": session.configured,
            "state": session.state,
            "trusted": session.name in self.trusted,
            "error": session.error,
            "tools": list(session.tools),
            "server": session.info,
        }

    def snapshot(self) -> dict[str, Any]:
        self.sync()
        return {
            "servers": [self.describe(s) for _, s in sorted(self.sessions.items())],
            "toolset": TOOLSET,
            "config": str(self.data_home / CONFIG_FILE),
            "config_error": self.config_error,
        }


class MCPAdminTool:
    """The agent's view of the MCP hub: look, and turn servers on or off.

    Deliberately no add/remove: those spawn a command the user never vetted,
    so they stay in Config → MCP where a human types them.
    """

    name = "mcp"
    toolset = TOOLSET
    output_schema = None
    # Connecting starts a third-party process, so this prompts like `bash`.
    approval = ApprovalLevel.RISKY
    description = (
        "Inspect and control MCP server connections. Actions: 'list' (servers, "
        "state, tool counts), 'tools' (the tools one server contributed), "
        "'connect', 'disconnect'. Connecting registers that server's tools for "
        "the next turn. Adding or removing a server is human-only."
    )
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "tools", "connect", "disconnect"]},
            "server": {"type": "string", "description": "Server name; required except for 'list'."},
        },
        "required": ["action"],
        "additionalProperties": False,
    }

    def __init__(self, hub: Hub) -> None:
        self._hub = hub

    def _line(self, entry: dict[str, Any]) -> str:
        bits = [f"{entry['name']}: {entry['state']}", f"{len(entry['tools'])} tools"]
        if not entry["enabled"]:
            bits.append("autostart off")
        if entry["trusted"]:
            bits.append("trusted")
        if not entry["configured"]:
            bits.append("not in mcp.json")
        if entry["error"]:
            bits.append(f"error: {entry['error']}")
        return " · ".join(bits)

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        hub = self._hub
        action = str((args or {}).get("action") or "").lower()
        name = str((args or {}).get("server") or "").strip()
        if action != "list" and not name:
            return ToolResult.err(f"{action} needs a 'server' name")
        try:
            if action == "list":
                snap = hub.snapshot()
                if not snap["servers"]:
                    return ToolResult.ok(f"No MCP servers configured ({snap['config']}).")
                text = "\n".join(self._line(e) for e in snap["servers"])
                if snap["config_error"]:
                    text = f"{text}\nconfig error: {snap['config_error']}"
                return ToolResult.ok(text, raw=snap)
            if action == "tools":
                hub.sync()
                session = hub._need(name)  # noqa: SLF001 — same module, one owner
                if not session.tools:
                    return ToolResult.ok(f"{name} is {session.state} and contributes no tools.")
                registry = hub.registry
                lines = []
                for local in session.tools:
                    tool = registry.get(local) if registry is not None else None
                    desc = (getattr(tool, "description", "") or "").split("\n")[0]
                    lines.append(f"{local} — {desc}" if desc else local)
                return ToolResult.ok("\n".join(lines))
            if action == "connect":
                session = await hub.connect(name)
                if session.error:
                    return ToolResult.err(f"{name}: {session.error}")
                return ToolResult.ok(self._line(hub.describe(session)))
            if action == "disconnect":
                session = await hub.disconnect(name)
                return ToolResult.ok(f"{name}: disconnected, its tools are retired")
        except ValueError as exc:
            return ToolResult.err(str(exc))
        return ToolResult.err(f"unknown action: {action!r}")


def _register_rpcs(ctx: Any, hub: Hub) -> None:
    """The panel's five verbs. Every one is a skin over a Hub method."""

    def named(params: dict[str, Any]) -> str:
        name = str((params or {}).get("name") or "").strip()
        if not name:
            raise RpcError(-32602, "name is required")
        return name

    def flag(params: dict[str, Any], key: str, default: bool) -> bool:
        value = (params or {}).get(key, default)
        return value if isinstance(value, bool) else str(value).lower() == "true"

    async def mcp_list(params: dict[str, Any]) -> dict[str, Any]:
        return hub.snapshot()

    async def mcp_add(params: dict[str, Any]) -> dict[str, Any]:
        try:
            session = await hub.add(params or {}, connect=flag(params, "connect", True))
        except ValueError as exc:
            raise RpcError(-32602, str(exc)) from exc
        except OSError as exc:
            raise RpcError(-32603, f"cannot write config: {exc}") from exc
        return hub.describe(session)

    async def mcp_remove(params: dict[str, Any]) -> dict[str, Any]:
        try:
            await hub.remove(named(params))
        except ValueError as exc:
            raise RpcError(-32602, str(exc)) from exc
        return {"ok": True}

    async def mcp_connect(params: dict[str, Any]) -> dict[str, Any]:
        # A failed connect is reported in the row, not as an RPC error: the
        # panel needs the message next to the server it belongs to.
        try:
            return hub.describe(await hub.connect(named(params)))
        except ValueError as exc:
            raise RpcError(-32602, str(exc)) from exc

    async def mcp_disconnect(params: dict[str, Any]) -> dict[str, Any]:
        try:
            return hub.describe(await hub.disconnect(named(params)))
        except ValueError as exc:
            raise RpcError(-32602, str(exc)) from exc

    async def mcp_set(params: dict[str, Any]) -> dict[str, Any]:
        """Toggle autostart (``enabled``) or approval bypass (``trusted``)."""
        name = named(params)
        try:
            if "enabled" in (params or {}):
                session = await hub.set_enabled(name, flag(params, "enabled", True))
            elif "trusted" in (params or {}):
                session = await hub.set_trusted(name, flag(params, "trusted", False))
            else:
                raise RpcError(-32602, "pass 'enabled' or 'trusted'")
        except ValueError as exc:
            raise RpcError(-32602, str(exc)) from exc
        return hub.describe(session)

    ctx.register_rpc("mcp.list", mcp_list)
    ctx.register_rpc("mcp.add", mcp_add)
    ctx.register_rpc("mcp.remove", mcp_remove)
    ctx.register_rpc("mcp.connect", mcp_connect)
    ctx.register_rpc("mcp.disconnect", mcp_disconnect)
    ctx.register_rpc("mcp.set", mcp_set)


def live_clients() -> dict[str, Any]:
    """Connected servers, for tests and introspection."""
    hub = _HUB
    if hub is None:
        return {}
    return {n: s.client for n, s in hub.sessions.items() if s.client is not None}


async def activate(ctx: Any) -> None:
    global _HUB
    hub = Hub(ctx)
    _HUB = hub
    # Registered before any server starts, so Config → MCP works on a fresh
    # install where mcp.json does not exist yet.
    _register_rpcs(ctx, hub)
    ctx.register_tool(MCPAdminTool(hub))
    await hub.start_all()
    if hub.config_error:
        ctx.log(f"unreadable config: {hub.config_error}", level="error")


async def deactivate(ctx: Any) -> None:
    global _HUB
    hub, _HUB = _HUB, None
    if hub is not None:
        await hub.stop_all()
