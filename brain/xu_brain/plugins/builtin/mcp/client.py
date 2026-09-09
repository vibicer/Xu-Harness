"""Minimal MCP stdio client — NDJSON JSON-RPC over a child process.

Only what the tool bridge needs: the ``initialize`` handshake, ``tools/list``,
``tools/call``, and a clean shutdown. No HTTP/SSE transport, no OAuth, no
resources or prompts — those arrive when a server actually demands them.

Deliberate details, each one a bug that bites otherwise:

* **stdin writes are never awaited.** A server that stops draining its stdin
  fills the pipe, and ``await drain()`` would hang the agent turn behind it.
* **stdout gets a 4 MiB line limit.** A tool catalog is one JSON line and
  routinely passes asyncio's default 64 KiB, which would abort the read.
* **stderr is drained into a ring buffer.** An undrained pipe eventually blocks
  the child, and "server exited" is useless without its last words.
* **Integer request ids.** Some servers reject string ids outright.
* **A server→client request gets a ``-32601`` reply**, so a server asking for
  ``roots/list`` or sampling fails fast instead of waiting forever.
* **The child env is an allowlist** plus whatever the config names: an MCP
  server is third-party code, and handing it the brain's whole environment
  would hand it every API key in it.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
from collections import deque
from collections.abc import Callable
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "xu", "version": "1.0.0"}

# 4 MiB per JSON line — a catalog of a few hundred tools with full schemas.
_STREAM_LIMIT = 4 * 1024 * 1024
# Keep the tail of stderr for error messages; a chatty server can log forever.
_STDERR_TAIL = 20
# tools/list is paginated; a cursor loop needs a stop so a buggy server that
# always returns a cursor cannot spin the boot forever.
_MAX_PAGES = 20

# Handed to every server. Anything else must be named explicitly in the config
# (``env: {"API_KEY": "${MY_KEY}"}``) — an opt-in, one variable at a time.
_ENV_PASSTHROUGH = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TZ",
    "TMPDIR", "TEMP", "TMP",
    # Windows: a child cannot start without these.
    "SystemRoot", "SystemDrive", "COMSPEC", "PATHEXT", "WINDIR", "APPDATA",
    "LOCALAPPDATA", "USERPROFILE", "PROGRAMFILES", "PROGRAMFILES(X86)",
    "PROGRAMDATA", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
)

_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class MCPError(RuntimeError):
    """A server failed, timed out, or answered with a JSON-RPC error."""


def expand(value: str, environ: dict[str, str] | None = None) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` against the environment."""
    env = os.environ if environ is None else environ
    return _VAR.sub(lambda m: env.get(m.group(1)) or (m.group(2) or ""), value)


def child_env(declared: dict[str, Any] | None) -> dict[str, str]:
    """Allowlisted parent env + the server's own ``env`` block, expanded."""
    env = {k: v for k in _ENV_PASSTHROUGH if (v := os.environ.get(k)) is not None}
    for key, value in (declared or {}).items():
        env[str(key)] = expand(str(value))
    return env


class StdioClient:
    """One MCP server, spoken to over its stdin/stdout.

    ``start`` → ``list_tools`` → ``call_tool`` → ``close``. Every method is
    safe to call after ``close``: it raises :class:`MCPError` rather than
    touching a dead process.
    """

    def __init__(
        self,
        name: str,
        command: str,
        args: list[str] | None = None,
        env: dict[str, Any] | None = None,
        cwd: str | None = None,
        timeout: float = 30.0,
        log: Callable[..., None] | None = None,
    ) -> None:
        self.name = name
        self.command = command
        self.args = [expand(str(a)) for a in (args or [])]
        self.env = env
        self.cwd = cwd
        self.timeout = timeout if timeout and timeout > 0 else None
        self._log = log or (lambda *a, **k: None)
        self.server_info: dict[str, Any] = {}
        self._proc: asyncio.subprocess.Process | None = None
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self._stderr: deque[str] = deque(maxlen=_STDERR_TAIL)

    # ---- lifecycle ---------------------------------------------------- #

    async def start(self) -> dict[str, Any]:
        """Spawn the server and complete the MCP handshake."""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                expand(self.command), *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd or None,
                env=child_env(self.env),
                limit=_STREAM_LIMIT,
                # Own process group: `npx`-style launchers fork children, and
                # signalling only the parent leaves those orphaned.
                start_new_session=os.name == "posix",
            )
        except OSError as exc:
            raise MCPError(f"{self.name}: cannot start {self.command!r}: {exc}") from exc
        self._tasks = [
            asyncio.create_task(self._read_loop()),
            asyncio.create_task(self._drain_stderr()),
        ]
        result = await self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            # Only tools are consumed, so nothing else is advertised.
            "capabilities": {},
            "clientInfo": CLIENT_INFO,
        })
        self.server_info = result.get("serverInfo") or {}
        self._notify("notifications/initialized")
        return result

    async def close(self) -> None:
        """Terminate the child and drop every pending request. Idempotent."""
        proc, self._proc = self._proc, None
        tasks, self._tasks = self._tasks, []
        for task in tasks:
            task.cancel()
        self._fail_pending(MCPError(f"{self.name}: client closed"))
        if proc is None or proc.returncode is not None:
            return
        self._signal(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=1.0)
        except TimeoutError:
            self._signal(proc, signal.SIGKILL)
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except TimeoutError:
                self._log(f"{self.name}: server ignored SIGKILL", level="warning")

    def _signal(self, proc: asyncio.subprocess.Process, sig: int) -> None:
        """Signal the whole process group where the platform has one."""
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(proc.pid), sig)
            elif sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
        except (ProcessLookupError, PermissionError, OSError):
            pass  # already gone, or never ours — either way nothing to kill

    # ---- protocol verbs ----------------------------------------------- #

    async def list_tools(self) -> list[dict[str, Any]]:
        """Every tool the server advertises, following pagination cursors."""
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(_MAX_PAGES):
            params = {"cursor": cursor} if cursor else None
            page = await self._request("tools/list", params)
            found = page.get("tools")
            if isinstance(found, list):
                tools.extend(t for t in found if isinstance(t, dict))
            cursor = page.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                break
        return tools

    async def call_tool(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Invoke a tool. The raw MCP result dict comes back untouched."""
        return await self._request(
            "tools/call", {"name": tool, "arguments": arguments or {}}
        )

    # ---- json-rpc ----------------------------------------------------- #

    async def _request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        proc = self._proc
        if proc is None:
            raise MCPError(f"{self.name}: not connected")
        self._next_id += 1
        rid = self._next_id
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)
        try:
            result = await asyncio.wait_for(future, timeout=self.timeout)
        except TimeoutError as exc:
            self._pending.pop(rid, None)
            raise MCPError(
                f"{self.name}: {method} timed out after {self.timeout:g}s{self._tail()}"
            ) from exc
        finally:
            self._pending.pop(rid, None)
        return result if isinstance(result, dict) else {}

    def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def _write(self, message: dict[str, Any]) -> None:
        """Queue one NDJSON frame. Never awaits — see the module docstring."""
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError(f"{self.name}: not connected")
        try:
            proc.stdin.write(json.dumps(message).encode("utf-8") + b"\n")
        except (BrokenPipeError, ConnectionResetError, RuntimeError) as exc:
            raise MCPError(f"{self.name}: server closed its input{self._tail()}") from exc

    async def _read_loop(self) -> None:
        """Dispatch server frames until EOF, then fail everything pending."""
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    # A frame past the 4 MiB limit: the stream position is now
                    # unknown, so the connection is unusable.
                    self._fail_pending(MCPError(f"{self.name}: response exceeded {_STREAM_LIMIT}B"))
                    return
                if not line:
                    break
                text = line.strip()
                if not text:
                    continue
                try:
                    message = json.loads(text)
                except ValueError:
                    # Servers that print to stdout are common; ignore the noise
                    # rather than tearing down a working connection.
                    self._log(f"{self.name}: non-JSON on stdout: {text[:120]!r}", level="debug")
                    continue
                if isinstance(message, dict):
                    self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a reader crash must not vanish
            self._fail_pending(MCPError(f"{self.name}: reader failed: {exc}"))
            return
        self._fail_pending(MCPError(f"{self.name}: server exited{self._tail()}"))

    def _dispatch(self, message: dict[str, Any]) -> None:
        rid = message.get("id")
        if "method" in message:
            # Server→client traffic. Nothing is advertised in `capabilities`,
            # so answer requests with "no such method" and drop notifications.
            if rid is not None:
                try:
                    self._write({
                        "jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601, "message": f"unsupported: {message['method']}"},
                    })
                except MCPError:
                    pass
            return
        future = self._pending.pop(rid, None) if isinstance(rid, int) else None
        if future is None or future.done():
            return
        error = message.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            detail = error.get("message") or "error"
            future.set_exception(MCPError(f"{self.name}: {detail} ({code})"))
        else:
            future.set_result(message.get("result") or {})

    def _fail_pending(self, exc: MCPError) -> None:
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_exception(exc)

    # ---- stderr ------------------------------------------------------- #

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        try:
            while line := await proc.stderr.readline():
                text = line.decode("utf-8", "replace").rstrip()
                if text:
                    self._stderr.append(text)
                    self._log(f"{self.name}: {text}", level="debug")
        except (asyncio.CancelledError, ValueError):
            return
        except Exception:  # noqa: BLE001 — draining is best-effort
            return

    def _tail(self) -> str:
        """Last stderr lines, formatted for an error message."""
        if not self._stderr:
            return ""
        return " — stderr: " + " | ".join(self._stderr)

