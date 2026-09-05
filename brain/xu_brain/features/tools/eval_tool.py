"""eval toolset — persistent Python kernel in a subprocess.

One long-lived child interpreter per session (JSON-lines over stdio), so
model-generated code can never touch the brain's own process: an infinite
loop costs a timeout+kill of the kernel only, and the brain's modules are
not importable from inside it. Kernel state (variables, imports) survives
across calls; on timeout the kernel is killed and respawns empty.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from typing import Any

from ...core.governance import ApprovalLevel
from . import _process
from .base import Tool, ToolContext, ToolResult

# The child side: read one JSON request line per call, exec the code with
# stdout/stderr captured, write one JSON response line. Runs until stdin EOF.
_KERNEL_SRC = r"""
import sys, json, io, ast, contextlib, traceback

_EVAL_CAP = 64 * 1024  # bytes of stdout/stderr kept per call

class _Bounded(io.StringIO):
# StringIO that keeps the first _EVAL_CAP chars and counts overflow.
    def __init__(self, limit=_EVAL_CAP):
        super().__init__()
        self.limit = limit
        self.dropped = 0
    def write(self, s):
        have = self.tell()
        if have >= self.limit:
            self.dropped += len(s)
            return len(s)
        take = s[: self.limit - have]
        self.dropped += len(s) - len(take)
        return super().write(take)

def main():
    g = {"__name__": "__xu_eval__"}
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        code = req.get("code", "")
        out, err = _Bounded(), _Bounded()
        result = None
        error = None
        try:
            tree = ast.parse(code)
            last = None
            if tree.body and isinstance(tree.body[-1], ast.Expr):
                last = tree.body.pop()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                exec(compile(tree, "<eval>", "exec"), g)
                if last is not None:
                    result = eval(compile(ast.Expression(last.value), "<eval>", "eval"), g)
        except SystemExit:
            error = "SystemExit suppressed"
        except BaseException as e:
            err.write(traceback.format_exc(limit=3))
            error = "%s: %s" % (type(e).__name__, e)
        result_txt = None if result is None else repr(result)
        truncated = bool(out.dropped or err.dropped)
        if result_txt is not None and len(result_txt) > _EVAL_CAP:
            result_txt = result_txt[:_EVAL_CAP] + "...[result truncated]"
            truncated = True
        try:
            resp = json.dumps({
                "out": out.getvalue(),
                "err": err.getvalue(),
                "result": result_txt,
                "error": error,
                "truncated": truncated,
            })
        except Exception:
            resp = json.dumps({"out": out.getvalue(), "err": err.getvalue(),
                               "result": result_txt, "error": "result not serializable",
                               "truncated": truncated})
        sys.stdout.write(resp + "\n")
        sys.stdout.flush()

main()
"""

# Kernel processes per session (mirrors terminal.py's shell pool).
_KERNELS: dict[str, EvalKernel] = {}


class EvalKernel:
    """One persistent child interpreter, serialized per session."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        self._proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-c", _KERNEL_SRC,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.cwd,
            **_process.spawn_isolation(),
        )
        # The kernel's JSON response line can exceed asyncio's default 64 KB
        # StreamReader limit when output is near its cap; raise it once here.
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream is not None:
                stream._limit = 2 ** 21  # noqa: SLF001 -- asyncio reader cap
        return self._proc

    async def run(self, code: str, timeout: float = 60.0) -> dict[str, Any]:
        async with self._lock:
            proc = await self._ensure()
            assert proc.stdin and proc.stdout
            proc.stdin.write(json.dumps({"code": code}).encode() + b"\n")
            await proc.stdin.drain()
            try:
                line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                await self.kill()
                return {"out": "", "err": "",
                        "error": f"eval timed out after {timeout:g}s (kernel killed — variables reset)"}
            if not line:
                # kernel died (crash/OOM); respawn fresh on the next call
                self._proc = None
                return {"out": "", "err": "", "error": "kernel exited unexpectedly"}
            try:
                return json.loads(line.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                return {"out": line.decode("utf-8", "replace"), "err": "",
                        "error": "kernel sent a malformed response"}

    async def kill(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            await _process.kill_tree(self._proc.pid, force=True)
            with contextlib.suppress(ProcessLookupError, OSError):
                await self._proc.wait()
        self._proc = None

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.stdin.close()  # type: ignore[union-attr]
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except (OSError, asyncio.TimeoutError):
                await self.kill()
                return
        self._proc = None


def _kernel(ctx: ToolContext) -> EvalKernel:
    k = _KERNELS.get(ctx.session_id)
    if k is None:
        k = EvalKernel(ctx.cwd)
        _KERNELS[ctx.session_id] = k
    elif k.cwd != ctx.cwd:
        # session moved — close the old kernel and start fresh in the new dir
        try:
            asyncio.get_event_loop().create_task(k.close())
        except RuntimeError:
            pass
        k = EvalKernel(ctx.cwd)
        _KERNELS[ctx.session_id] = k
    return k


class EvalTool(Tool):
    name = "eval"
    toolset = "eval"
    description = (
        "Run Python code in a persistent isolated interpreter (state survives "
        "across calls; runs in its own process, not the agent's). Returns "
        "captured stdout/stderr and the last expression value."
    )
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {
            "code": {"type": "string"},
            "timeout": {"type": "number", "description": "seconds before the kernel is killed (default 60)"},
        },
        "required": ["code"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code = str(args.get("code", ""))
        if not code.strip():
            return ToolResult.err("empty code")
        try:
            timeout = float(args.get("timeout", 60))
        except (TypeError, ValueError):
            timeout = 60.0
        kernel = _kernel(ctx)
        resp = await kernel.run(code, timeout=timeout)
        parts = []
        if resp.get("out"):
            parts.append(resp["out"].rstrip())
        if resp.get("err"):
            parts.append(f"[stderr]\n{resp['err'].rstrip()}")
        if resp.get("result") is not None:
            parts.append(f"→ {resp['result']}")
        error = resp.get("error")
        if error:
            parts.append(f"[error] {error}")
        if resp.get("truncated"):
            parts.append("…[output truncated at 64 KB]")
        text = "\n".join(parts) if parts else "(no output)"
        return ToolResult.err(text, raw=text) if error else ToolResult.ok(text, raw=text)


eval = EvalTool()


def close_session(session_id: str) -> "asyncio.Task[None] | None":
    """Drop this session's kernel from the pool (mirrors terminal.close_session).
    Returns the closing task so callers may await it."""
    k = _KERNELS.pop(session_id, None)
    if k is None:
        return None
    try:
        return asyncio.get_event_loop().create_task(k.close())
    except RuntimeError:
        return None
