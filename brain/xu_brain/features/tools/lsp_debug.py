"""lsp + debug toolsets.

lsp: a JSON-RPC 2.0 client over stdio to a language server; exposes
references / definition / hover / rename / code actions via a single `lsp`
tool that dispatches on `action`. Servers are spawned per (lang, project) and
kept alive in a pool.

debug: a DAP client (debugpy-first); exposes attach/continue/breakpoint/eval.
A single `debug` tool dispatches on `action`. Both import their server binaries
lazily so startup stays fast when unused.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult

# Pool lifecycle bounds: idle-eviction plus a hard server cap.
_LSP_IDLE_TTL = 300.0     # seconds idle before a pooled language server is closed
_LSP_MAX_SERVERS = 4      # hard cap on pooled servers; least-recently-used evicted
_SHUTDOWN_TIMEOUT = 5.0   # graceful LSP shutdown/exit budget (s)
_KILL_GRACE = 2.0         # SIGTERM -> SIGKILL grace (s)


# ---------------------------------------------------------------------------
# shared JSON-RPC stdio framing
# ---------------------------------------------------------------------------


class _JsonRpcStdio:
    def __init__(self, cmd: list[str], cwd: str | None = None) -> None:
        self._cmd = cmd
        self._cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._next = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()
        self.initialized = False
        # open-document bookkeeping: uri -> version (didOpen once, didChange after)
        self.open_docs: dict[str, int] = {}
        # latest publishDiagnostics per uri + waiters blocked on the next push
        self.diagnostics: dict[str, list[dict[str, Any]]] = {}
        self._diag_waiters: dict[str, list[asyncio.Future[list[dict[str, Any]]]]] = {}
        # pool lifecycle: monotonic last-use timestamp + disposed latch
        self.last_used = 0.0
        self._disposed = False
    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        # Fresh (or respawned) process: reset handshake + document state so a
        # respawn doesn't inherit a stale "initialized" flag or didOpen set.
        self.initialized = False
        self.open_docs.clear()
        self.diagnostics.clear()
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
        )
        asyncio.create_task(self._read_loop())
        return self._proc

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        buf = b""
        while True:
            chunk = await self._proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\r\n\r\n" in buf:
                header, buf = buf.split(b"\r\n\r\n", 1)
                length = 0
                for line in header.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                if len(buf) < length:
                    break
                payload = buf[:length]
                buf = buf[length:]
                try:
                    msg = json.loads(payload.decode("utf-8"))
                except json.JSONDecodeError:
                    continue
                if "id" in msg:
                    fut = self._pending.pop(msg["id"], None)
                    if fut and not fut.done():
                        fut.set_result(msg.get("result", msg.get("error", {})))
                elif msg.get("method") == "textDocument/publishDiagnostics":
                    params = msg.get("params") or {}
                    uri = str(params.get("uri") or "")
                    diags = list(params.get("diagnostics") or [])
                    if uri:
                        self.diagnostics[uri] = diags
                        for w in self._diag_waiters.pop(uri, []):
                            if not w.done():
                                w.set_result(diags)
                # other notifications ignored
        # Process exited: fail any in-flight requests so they don't hang until
        # their 20s timeout, and wake pending diagnostic waiters with no push.
        for fut in self._pending.values():
            if not fut.done():
                fut.set_result({"error": "language server exited"})
        self._pending.clear()
        for waiters in self._diag_waiters.values():
            for w in waiters:
                if not w.done():
                    w.set_result([])
        self._diag_waiters.clear()

    async def wait_diagnostics(self, uri: str, timeout: float = 3.0) -> list[dict[str, Any]]:
        """Wait for the next publishDiagnostics push for `uri`; on timeout,
        fall back to the last known set (possibly empty)."""
        fut: asyncio.Future[list[dict[str, Any]]] = asyncio.get_event_loop().create_future()
        self._diag_waiters.setdefault(uri, []).append(fut)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return self.diagnostics.get(uri, [])

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self._lock:
            proc = await self._ensure()
            assert proc.stdin
            self.last_used = time.monotonic()
            self._next += 1
            req_id = self._next
            msg: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
            if params is not None:
                msg["params"] = params
            fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
            self._pending[req_id] = fut
            try:
                await self._send(proc, msg)
            except (BrokenPipeError, ConnectionResetError):
                self._pending.pop(req_id, None)
                self._proc = None  # force respawn on the next call
                return {"error": "language server connection lost"}
            try:
                return await asyncio.wait_for(fut, timeout=20)
            except asyncio.TimeoutError:
                self._pending.pop(req_id, None)
                return {"error": "timeout"}

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        proc = await self._ensure()
        assert proc.stdin
        self.last_used = time.monotonic()
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            await self._send(proc, msg)
        except (BrokenPipeError, ConnectionResetError):
            self._proc = None  # force respawn on the next call

    async def _send(self, proc: asyncio.subprocess.Process, msg: dict[str, Any]) -> None:
        assert proc.stdin
        body = json.dumps(msg).encode("utf-8")
        frame = b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        proc.stdin.write(frame)
        await proc.stdin.drain()

    async def close(self) -> None:
        """Bounded teardown: graceful LSP shutdown/exit -> SIGTERM -> SIGKILL,
        awaiting process exit so callers never leak orphans. Idempotent."""
        if self._disposed:
            return
        self._disposed = True
        proc = self._proc
        if proc is None or proc.returncode is not None:
            self._proc = None
            return
        # Graceful LSP shutdown: `shutdown` request, then `exit` notification.
        try:
            self._next += 1
            await asyncio.wait_for(
                self._send(proc, {"jsonrpc": "2.0", "id": self._next, "method": "shutdown"}),
                timeout=_SHUTDOWN_TIMEOUT,
            )
            await self._send(proc, {"jsonrpc": "2.0", "method": "exit"})
        except (BrokenPipeError, ConnectionResetError):
            return  # already gone
        except asyncio.TimeoutError:
            pass
        if proc.returncode is None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_TIMEOUT)
            except asyncio.TimeoutError:
                pass
        if proc.returncode is not None:
            return
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


_LSP_SERVERS: dict[str, _JsonRpcStdio] = {}


def _lsp_server(lang: str, ctx: ToolContext) -> _JsonRpcStdio | None:
    cmd = _lsp_cmd(lang)
    if cmd is None:
        return None
    key = f"{lang}:{ctx.cwd}"
    srv = _LSP_SERVERS.get(key)
    if srv is None:
        srv = _JsonRpcStdio(cmd, cwd=ctx.cwd)
        _LSP_SERVERS[key] = srv
    srv.last_used = time.monotonic()
    _schedule_reap()
    return srv


_reap_task: asyncio.Task | None = None


async def _reap_idle_lsp() -> None:
    """Close servers idle beyond ``_LSP_IDLE_TTL`` and enforce the LRU cap."""
    now = time.monotonic()
    for key in [k for k, s in _LSP_SERVERS.items() if now - s.last_used > _LSP_IDLE_TTL]:
        srv = _LSP_SERVERS.pop(key, None)
        if srv is not None:
            await srv.close()
    while len(_LSP_SERVERS) > _LSP_MAX_SERVERS:
        key = min(_LSP_SERVERS, key=lambda k: _LSP_SERVERS[k].last_used)
        srv = _LSP_SERVERS.pop(key)
        await srv.close()


async def _reap_loop() -> None:
    """Reap idle servers periodically until the pool becomes empty."""
    global _reap_task
    try:
        while _LSP_SERVERS:
            await asyncio.sleep(_LSP_IDLE_TTL)
            await _reap_idle_lsp()
    except asyncio.CancelledError:
        raise
    finally:
        _reap_task = None


def _schedule_reap() -> None:
    """Schedule delayed idle reaping, reusing an existing watcher."""
    global _reap_task
    if _reap_task is None or _reap_task.done():
        _reap_task = asyncio.get_running_loop().create_task(_reap_loop())

def _lsp_cmd(lang: str) -> list[str] | None:
    py = shutil.which("pylsp") or shutil.which("basedpyright")
    if lang in ("python", "py") and py:
        return [py]
    ts = shutil.which("typescript-language-server")
    if lang in ("typescript", "ts", "javascript") and ts:
        return [ts, "--stdio"]
    rls = shutil.which("rust-analyzer")
    if lang == "rust" and rls:
        return [rls]
    clangd = shutil.which("clangd")
    if lang in ("c", "cpp", "c++") and clangd:
        return [clangd]
    return None


class LspTool(Tool):
    name = "lsp"
    toolset = "lsp"
    description = (
        "Language-server operations: hover, definition, references, rename, "
        "code_actions, diagnostics. Dispatches on `action`. Servers are spawned "
        "per language+project. Falls back to a clear error if no server binary."
    )
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["hover", "definition", "references", "rename", "code_actions", "diagnostics"]},
            "file": {"type": "string"},
            "line": {"type": "integer", "description": "1-indexed"},
            "symbol": {"type": "string"},
            "lang": {"type": "string"},
            "new_name": {"type": "string"},
        },
        "required": ["action", "file"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = str(args.get("action", "hover"))
        file = str(args.get("file", ""))
        lang = str(args.get("lang", _guess_lang(file)))
        if not file:
            return ToolResult.err("file required")
        fp = Path(file)
        if not fp.is_absolute():
            fp = Path(ctx.cwd) / fp
        srv = _lsp_server(lang, ctx)
        if srv is None:
            return ToolResult.err(f"no language server found for {lang}")
        # initialize on first use per server — re-initializing is a protocol
        # error most servers reject
        if not srv.initialized:
            await srv.call("initialize", {
                "processId": os.getpid(),
                "rootUri": Path(ctx.cwd).as_uri(),
                "capabilities": {},
            })
            await srv.notify("initialized", {})
            srv.initialized = True
        uri = fp.as_uri()
        text = fp.read_text("utf-8") if fp.exists() else ""
        await _sync_doc(srv, uri, lang, text)
        line = int(args.get("line", 1)) - 1
        pos = {"line": line, "character": _character_for(text, line + 1, str(args.get("symbol", "")))}
        try:
            if action == "hover":
                r = await srv.call("textDocument/hover", {"textDocument": {"uri": uri}, "position": pos})
                return ToolResult.ok(json.dumps(r, indent=2), raw=r)
            if action == "definition":
                r = await srv.call("textDocument/definition", {"textDocument": {"uri": uri}, "position": pos})
                return ToolResult.ok(json.dumps(r, indent=2), raw=r)
            if action == "references":
                r = await srv.call("textDocument/references", {"textDocument": {"uri": uri}, "position": pos, "context": {"includeDeclaration": True}})
                return ToolResult.ok(json.dumps(r, indent=2), raw=r)
            if action == "rename" and args.get("new_name"):
                r = await srv.call("textDocument/rename", {"textDocument": {"uri": uri}, "position": pos, "newName": str(args["new_name"])})
                return ToolResult.ok(json.dumps(r, indent=2), raw=r)
            if action == "code_actions":
                r = await srv.call("textDocument/codeAction", {"textDocument": {"uri": uri}, "range": {"start": pos, "end": pos}, "context": {"diagnostics": []}})
                return ToolResult.ok(json.dumps(r, indent=2), raw=r)
            if action == "diagnostics":
                diags = await srv.wait_diagnostics(uri, timeout=3.0)
                return ToolResult.ok(
                    _render_diags(diags) if diags else "(no diagnostics)", raw=diags
                )
        except Exception as e:  # noqa: BLE001
            return ToolResult.err(f"lsp error: {e}")
        return ToolResult.err(f"unsupported action: {action}")


def _guess_lang(file: str) -> str:
    return {
        ".py": "python", ".ts": "typescript", ".tsx": "typescript",
        ".js": "javascript", ".rs": "rust", ".c": "c", ".cpp": "cpp",
    }.get(Path(file).suffix, "python")


async def _sync_doc(srv: _JsonRpcStdio, uri: str, lang: str, text: str) -> None:
    """didOpen once per document, didChange (full text) afterwards — sending
    didOpen for an already-open doc is a protocol order violation."""
    if uri in srv.open_docs:
        version = srv.open_docs[uri] + 1
        srv.open_docs[uri] = version
        await srv.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": version},
            "contentChanges": [{"text": text}],
        })
    else:
        srv.open_docs[uri] = 1
        await srv.notify("textDocument/didOpen", {
            "textDocument": {"uri": uri, "languageId": lang, "version": 1, "text": text}
        })


_SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}


def _render_diags(diags: list[dict[str, Any]]) -> str:
    lines = []
    for d in diags:
        sev = _SEVERITY.get(d.get("severity"), "info")
        ln = (d.get("range", {}).get("start", {}).get("line", 0) or 0) + 1
        src = d.get("source") or ""
        msg = str(d.get("message", "")).splitlines()[0] if d.get("message") else ""
        lines.append(f"{sev} line {ln}{f' ({src})' if src else ''}: {msg}")
    return "\n".join(lines)


async def diagnostics_for(path: Path, ctx: ToolContext, timeout: float = 3.0) -> str:
    """Diagnostics note for a just-written file; '' when clean or unavailable.

    The edit/write feedback loop: LSP push-diagnostics when a server binary
    exists for the language, else an ast syntax check for .py (zero deps).
    Best-effort — never raises, never blocks the edit result long.
    """
    lang = _guess_lang(str(path))
    srv = _lsp_server(lang, ctx)
    if srv is not None:
        try:
            if not srv.initialized:
                await srv.call("initialize", {
                    "processId": os.getpid(),
                    "rootUri": Path(ctx.cwd).as_uri(),
                    "capabilities": {},
                })
                await srv.notify("initialized", {})
                srv.initialized = True
            text = path.read_text("utf-8")
            await _sync_doc(srv, path.as_uri(), lang, text)
            diags = await srv.wait_diagnostics(path.as_uri(), timeout=timeout)
            if diags:
                return "⚠ diagnostics:\n" + _render_diags(diags)
            return ""
        except Exception:  # noqa: BLE001 — diagnostics must never fail the edit
            return ""
    if path.suffix == ".py":
        try:
            import ast

            ast.parse(path.read_text("utf-8"))
        except SyntaxError as e:
            return f"⚠ syntax error: {e.msg} (line {e.lineno})"
        except OSError:
            return ""
    return ""


def _character_for(text: str, line_no: int, symbol: str) -> int:
    """Column of `symbol` on the line, in UTF-16 code units (LSP positions).
    Falls back to 0 when the symbol isn't given or isn't on the line."""
    if not symbol:
        return 0
    lines = text.splitlines()
    if not (1 <= line_no <= len(lines)):
        return 0
    col = lines[line_no - 1].find(symbol)
    if col < 0:
        return 0
    return len(lines[line_no - 1][:col].encode("utf-16-le")) // 2


lsp = LspTool()


# ---------------------------------------------------------------------------
# debug (DAP) — debugpy adapter over stdio
# ---------------------------------------------------------------------------


class _DapStdio:
    """A minimal Debug Adapter Protocol client over stdio.

    DAP borrows LSP's ``Content-Length`` framing but **not** its envelope, so
    it cannot reuse :class:`_JsonRpcStdio`. A request is
    ``{seq, type:"request", command, arguments}`` and a reply is
    ``{type:"response", request_seq, success, body}`` — no ``id``, no
    ``method``, and asynchronous ``{type:"event"}`` messages in between.
    """

    def __init__(self, cmd: list[str], cwd: str | None = None) -> None:
        self._cmd = cmd
        self._cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._seq = 0
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._events: dict[str, list[asyncio.Future[dict[str, Any]]]] = {}
        # Thread the adapter last reported as stopped; None while running.
        # DAP does not promise the main thread is id 1, so it is read from the
        # event rather than assumed.
        self.stopped_thread: int | None = None
        self.exited = False

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
        )
        asyncio.create_task(self._read_loop())

    async def _read_loop(self) -> None:
        assert self._proc and self._proc.stdout
        buf = b""
        while True:
            chunk = await self._proc.stdout.read(4096)
            if not chunk:
                break
            buf += chunk
            while b"\r\n\r\n" in buf:
                header, rest = buf.split(b"\r\n\r\n", 1)
                length = 0
                for line in header.split(b"\r\n"):
                    if line.lower().startswith(b"content-length:"):
                        length = int(line.split(b":", 1)[1].strip())
                if len(rest) < length:
                    break          # partial body — wait for the next chunk
                payload, buf = rest[:length], rest[length:]
                try:
                    msg = json.loads(payload.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                self._dispatch(msg)
        # Adapter gone: fail every waiter now rather than let each burn its
        # own timeout, so a finished program reports as such immediately.
        self.exited = True
        self.stopped_thread = None
        self._abort("debug adapter exited")

    def _dispatch(self, msg: dict[str, Any]) -> None:
        kind = msg.get("type")
        if kind == "response":
            fut = self._pending.pop(int(msg.get("request_seq", -1)), None)
            if fut and not fut.done():
                fut.set_result(msg)
            return
        if kind != "event":
            return
        name = str(msg.get("event", ""))
        if name == "stopped":
            self.stopped_thread = (msg.get("body") or {}).get("threadId")
        elif name in ("continued", "terminated", "exited"):
            self.stopped_thread = None
        for waiter in self._events.pop(name, []):
            if not waiter.done():
                waiter.set_result(msg)

    def _abort(self, why: str) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(RuntimeError(why))
        self._pending.clear()
        for waiters in self._events.values():
            for waiter in waiters:
                if not waiter.done():
                    waiter.set_exception(RuntimeError(why))
        self._events.clear()

    async def send(self, command: str, arguments: dict[str, Any] | None = None):
        """Write a request; return the future for its response.

        Kept separate from :meth:`request` because the adapter withholds the
        ``launch`` reply until ``configurationDone`` — awaiting it inline would
        deadlock the handshake.
        """
        if self._proc is None or self._proc.stdin is None or self.exited:
            raise RuntimeError("debug adapter not running")
        self._seq += 1
        seq = self._seq
        body = json.dumps({
            "seq": seq, "type": "request", "command": command,
            "arguments": arguments or {},
        }).encode("utf-8")
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[seq] = fut
        self._proc.stdin.write(b"Content-Length: %d\r\n\r\n%s" % (len(body), body))
        await self._proc.stdin.drain()
        return fut

    async def request(
        self, command: str, arguments: dict[str, Any] | None = None, timeout: float = 20.0
    ) -> dict[str, Any]:
        """Send a request and return its ``body``. Raises on an error reply."""
        return _dap_body(command, await asyncio.wait_for(
            await self.send(command, arguments), timeout=timeout
        ))

    def event(self, name: str) -> asyncio.Future[dict[str, Any]]:
        """A future resolving on the next ``name`` event. Register it *before*
        the request that triggers it, or the event can arrive first and be lost.
        """
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._events.setdefault(name, []).append(fut)
        return fut

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        self.exited = True
        self._abort("debug session closed")
        if proc is None or proc.returncode is not None:
            return
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_GRACE)
        except TimeoutError:
            pass


def _dap_body(command: str, resp: dict[str, Any]) -> dict[str, Any]:
    """Unwrap a DAP response, turning ``success: false`` into an exception."""
    if not resp.get("success", False):
        raise RuntimeError(resp.get("message") or f"{command} failed")
    return resp.get("body") or {}


_DEBUG_ACTIONS = frozenset({
    "launch", "set_breakpoint", "continue", "stack_trace", "evaluate", "terminate",
})


class DebugTool(Tool):
    name = "debug"
    toolset = "debug"
    description = (
        "DAP debug client (debugpy-first). actions: launch, continue, "
        "set_breakpoint, stack_trace, evaluate, terminate. Launches a DAP "
        "server over stdio; one session at a time."
    )
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "program": {"type": "string"},
            "file": {"type": "string"},
            "line": {"type": "integer"},
            "expression": {"type": "string"},
        },
        "required": ["action"],
    }

    _dap: _DapStdio | None = None
    # Breakpoints requested so far, path -> sorted lines. Kept on the tool
    # rather than the adapter so `set_breakpoint` works before `launch` and
    # survives a relaunch; DAP's setBreakpoints replaces the whole set for a
    # file, so the full list is re-sent every time.
    _bps: dict[str, list[int]] = {}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = str(args.get("action", ""))
        # Checked before the session gate so a typo reports itself instead of
        # being masked by "launch first".
        if action not in _DEBUG_ACTIONS:
            return ToolResult.err(
                f"unsupported debug action: {action or '(missing)'} — "
                f"one of {', '.join(sorted(_DEBUG_ACTIONS))}"
            )
        if action == "launch":
            return await self._launch(args, ctx)
        if action == "set_breakpoint":
            return await self._set_breakpoint(args, ctx)
        if self._dap is None:
            return ToolResult.err("no debug session — launch first")
        if self._dap.exited:
            self._dap = None
            return ToolResult.err("debug session ended (program exited) — launch again")
        if action == "continue":
            return await self._continue()
        if action == "stack_trace":
            return await self._stack_trace()
        if action == "evaluate":
            return await self._evaluate(args)
        if action == "terminate":
            return await self._terminate()
        return ToolResult.err(f"unsupported debug action: {action}")

    async def _launch(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        program = str(args.get("program") or "")
        if not program:
            return ToolResult.err("program required")
        path = Path(program)
        if not path.is_absolute():
            path = Path(ctx.cwd) / path
        if not path.exists():
            return ToolResult.err(f"program not found: {path}")
        try:
            import debugpy  # noqa: F401
        except ImportError:
            return ToolResult.err(
                "debugpy not installed — add it to the brain venv "
                "(`uv pip install debugpy` from brain/)"
            )
        if self._dap is not None:
            await self._dap.close()
            self._dap = None
        # `python -m debugpy.adapter` is the DAP-over-stdio server. Plain
        # `python -m debugpy` is the *debuggee* launcher and exits 2 without a
        # target, which is why a stdio client sees the process vanish.
        dap = _DapStdio([sys.executable, "-m", "debugpy.adapter"], cwd=str(path.parent))
        await dap.start()
        try:
            await dap.request("initialize", {
                "adapterID": "debugpy",
                "clientID": "xu",
                "pathFormat": "path",
                "linesStartAt1": True,
                "columnsStartAt1": True,
                "supportsRunInTerminalRequest": False,
            })
            # Order matters: the adapter emits `initialized` and withholds the
            # launch reply until `configurationDone`, so hold the future.
            initialized = dap.event("initialized")
            launched = await dap.send("launch", {
                "request": "launch",
                "type": "python",
                "program": str(path),
                "cwd": str(path.parent),
                "console": "internalConsole",
                # Stop at the first line: otherwise a short program runs to
                # completion before a breakpoint can be set, and every
                # follow-up action reports a dead session.
                "stopOnEntry": True,
                "justMyCode": False,
            })
            await asyncio.wait_for(initialized, timeout=20.0)
            for file, lines in self._bps.items():
                await dap.request("setBreakpoints", {
                    "source": {"path": file},
                    "breakpoints": [{"line": n} for n in lines],
                })
            stopped = dap.event("stopped")
            await dap.request("configurationDone")
            _dap_body("launch", await asyncio.wait_for(launched, timeout=20.0))
            try:
                await asyncio.wait_for(stopped, timeout=10.0)
                where = "stopped at entry"
            except (TimeoutError, RuntimeError):
                where = "running"
        except (RuntimeError, TimeoutError, OSError) as e:
            await dap.close()
            return ToolResult.err(f"launch failed: {e}")
        self._dap = dap
        n = sum(len(v) for v in self._bps.values())
        extra = f", {n} breakpoint(s) applied" if n else ""
        return ToolResult.ok(f"launched {path.name}, {where}{extra}")

    async def _set_breakpoint(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        file = str(args.get("file") or "")
        line = int(args.get("line") or 0)
        if not file or line < 1:
            return ToolResult.err("file and a 1-indexed line required")
        path = Path(file)
        if not path.is_absolute():
            path = Path(ctx.cwd) / path
        key = str(path)
        bps = dict(self._bps)
        bps[key] = sorted({*bps.get(key, []), line})
        self._bps = bps
        if self._dap is None or self._dap.exited:
            return ToolResult.ok(f"breakpoint buffered at {path.name}:{line} (applied on launch)")
        try:
            body = await self._dap.request("setBreakpoints", {
                "source": {"path": key},
                "breakpoints": [{"line": n} for n in bps[key]],
            })
        except (RuntimeError, TimeoutError) as e:
            return ToolResult.err(f"set_breakpoint failed: {e}")
        verified = sum(1 for b in body.get("breakpoints", []) if b.get("verified"))
        return ToolResult.ok(
            f"{verified}/{len(bps[key])} breakpoint(s) verified in {path.name}", raw=body
        )

    async def _continue(self) -> ToolResult:
        assert self._dap
        thread = self._dap.stopped_thread or 1
        stopped = self._dap.event("stopped")
        try:
            await self._dap.request("continue", {"threadId": thread})
            event = await asyncio.wait_for(stopped, timeout=20.0)
        except (RuntimeError, TimeoutError):
            # Adapter gone or nothing hit: the program ran on or finished.
            return ToolResult.ok("continued; did not stop again (still running or exited)")
        body = event.get("body") or {}
        detail = body.get("description") or body.get("text") or ""
        return ToolResult.ok(
            f"stopped: {body.get('reason', '?')}" + (f" — {detail}" if detail else ""), raw=body
        )

    async def _stack_trace(self) -> ToolResult:
        assert self._dap
        thread = self._dap.stopped_thread
        if thread is None:
            return ToolResult.err("not stopped — set a breakpoint and continue first")
        try:
            body = await self._dap.request("stackTrace", {"threadId": thread, "levels": 20})
        except (RuntimeError, TimeoutError) as e:
            return ToolResult.err(f"stack_trace failed: {e}")
        frames = body.get("stackFrames") or []
        lines = [
            f"{i}: {f.get('name')} at "
            f"{(f.get('source') or {}).get('path', '?')}:{f.get('line')}"
            for i, f in enumerate(frames)
        ]
        return ToolResult.ok("\n".join(lines) or "(empty stack)", raw=body)

    async def _evaluate(self, args: dict[str, Any]) -> ToolResult:
        assert self._dap
        expression = str(args.get("expression") or "")
        if not expression:
            return ToolResult.err("expression required")
        payload: dict[str, Any] = {"expression": expression, "context": "repl"}
        thread = self._dap.stopped_thread
        if thread is not None:
            # A frameId is fetched fresh rather than cached: ids are invalidated
            # by every resume, so a stale one evaluates in the wrong scope.
            try:
                top = await self._dap.request(
                    "stackTrace", {"threadId": thread, "levels": 1}
                )
                frames = top.get("stackFrames") or []
                if frames:
                    payload["frameId"] = frames[0].get("id")
            except (RuntimeError, TimeoutError):
                pass          # fall back to global scope
        try:
            body = await self._dap.request("evaluate", payload)
        except (RuntimeError, TimeoutError) as e:
            return ToolResult.err(f"evaluate failed: {e}")
        return ToolResult.ok(str(body.get("result", "")), raw=body)

    async def _terminate(self) -> ToolResult:
        assert self._dap
        try:
            await self._dap.request("disconnect", {"terminateDebuggee": True}, timeout=5.0)
        except (RuntimeError, TimeoutError):
            pass          # closing below kills it regardless
        await self._dap.close()
        self._dap = None
        self._bps = {}
        return ToolResult.ok("terminated")


debug = DebugTool()
