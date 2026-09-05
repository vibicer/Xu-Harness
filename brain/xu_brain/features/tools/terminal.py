"""Terminal toolset — persistent shell session, governed.

A single long-lived shell per session (process group, env inherited) so the
agent's `cd`/exports persist across calls. PTY-backed for interactive safety
and so prompts/ANSI behave. Per-call timeout; output capped by the registry.
"""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import ntpath
import os
import shlex
import shutil
import stat
import tempfile
from typing import Any

from ...core.governance import ApprovalLevel
from . import _process
from .base import Tool, ToolContext, ToolResult

# Per-stream output bounds: an in-memory cap with overflow spilling to a temp
# file, a spill cap, and an explicit truncation flag. Keeps the daemon from
# buffering unbounded command output in RAM.
_BASH_MAX_STREAM = 64 * 1024         # in-memory bytes kept per stream (stdout/stderr)
_BASH_MAX_SPILL = 64 * 1024 * 1024   # temp-file spill cap per stream
_BASH_READ_CHUNK = 64 * 1024         # bytes per stream read (NOT a line cap)


# ---------------------------------------------------------------------------
# Platform: which bash to drive
# ---------------------------------------------------------------------------
#
# The tool stays bash on every platform rather than growing a PowerShell
# dialect, because the whole ecosystem around it is bash-shaped: skills invoke
# `.sh` scripts, the sentinel wrapper below is bash, and every command the
# model writes assumes pipes and `$?`. Windows therefore needs a real bash,
# which Git for Windows provides. Process-group handling lives in `_process`,
# shared with the `eval` tool.


def _no_bash_message() -> str:
    if _process.IS_WINDOWS:
        return (
            "no bash binary found. Install Git for Windows "
            "(`winget install Git.Git`), which ships one, or point XU_BASH at "
            "a bash.exe."
        )
    return "no bash binary found; install bash or point XU_BASH at one."


def _runnable(path: str) -> bool:
    """True if `path` looks like a runnable file.

    ``lstat`` rather than ``exists``: a Windows app-execution alias (under
    ``%LocalAppData%\\Microsoft\\WindowsApps``) carries an ACL that makes
    ``exists`` answer False, which would silently skip a working binary. A
    dangling symlink is accepted too, so a broken install fails loudly at spawn
    instead of quietly falling through to some other shell.
    """
    if not path:
        return False
    try:
        st = os.lstat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(st.st_mode) or stat.S_ISLNK(st.st_mode)


def _windows_bash_candidates(env: dict[str, str] | None = None) -> list[str]:
    """Windows bash candidates, best first.

    Git for Windows is preferred over whatever ``bash`` is on PATH because
    ``%SystemRoot%\\System32\\bash.exe`` is the **WSL** launcher: it works, but
    resolves paths under ``/mnt/c`` while every other tool uses ``C:\\``, so a
    session would silently disagree with itself. WSL stays opt-in through
    ``XU_BASH``.
    """
    e = os.environ if env is None else env
    out: list[str] = []
    # PATH entries and env values can carry literal quotes from `setx`-style
    # definitions, so strip them before joining.
    override = e.get("XU_BASH", "").strip().strip('"')
    if override:
        out.append(override)
    for root in (
        e.get("ProgramFiles", r"C:\Program Files"),
        e.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ntpath.join(e.get("LocalAppData", ""), "Programs") if e.get("LocalAppData") else "",
    ):
        root = root.strip().strip('"')
        if root:
            out.append(ntpath.join(root, "Git", "bin", "bash.exe"))
    found = shutil.which("bash", path=e.get("PATH"))
    # ntpath, not os.path: these are Windows paths whichever host builds them,
    # which also keeps the branch testable from Linux.
    system32 = ntpath.join(e.get("SystemRoot", r"C:\Windows"), "System32").lower()
    if found and not found.lower().startswith(system32):
        out.append(found)
    return out


def _bash_path(env: dict[str, str] | None = None) -> str | None:
    """The bash binary to drive, or None when none is installed.

    Not cached: the probe is a few ``lstat`` calls and runs once per shell
    lifetime, and caching would keep reporting "no bash" for the rest of the
    process after a user installs one.
    """
    e = os.environ if env is None else env
    if _process.IS_WINDOWS:
        return next((c for c in _windows_bash_candidates(e) if _runnable(c)), None)
    override = e.get("XU_BASH", "").strip()
    if override:
        return override if _runnable(override) else None
    if _runnable("/bin/bash"):
        return "/bin/bash"
    return shutil.which("bash", path=e.get("PATH"))


def _pwd_expr() -> str:
    """The shell word that yields a cwd this process can spawn into.

    ``$PWD`` is exact on POSIX. Under Git Bash it would report an MSYS path
    (``/c/Users/...``) that cannot be handed back as a ``cwd``, so ask for the
    native form; the ``|| pwd`` keeps a non-MSYS bash working.
    """
    return '"$(pwd -W 2>/dev/null || pwd)"' if _process.IS_WINDOWS else '"$PWD"'


def _fmt_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


class _StreamBuffer:
    """Per-stream output collector: a bounded in-memory head, overflow spilled
    to an anonymous temp file (capped), and overflow beyond the spill cap only
    counted."""

    def __init__(self, cap: int, spill_max: int) -> None:
        self._cap = cap
        self._spill_max = spill_max
        self._head: list[str] = []
        self._head_bytes = 0
        self._spilled = 0
        self._dropped = 0
        self._file: tempfile.TemporaryFile[bytes] | None = None
        self.truncated = False

    def write(self, text: str) -> None:
        b = text.encode("utf-8", "replace")
        if self._file is None and self._head_bytes + len(b) <= self._cap:
            self._head.append(text)
            self._head_bytes += len(b)
            return
        self.truncated = True
        if self._spilled + len(b) <= self._spill_max:
            if self._file is None:
                self._file = tempfile.TemporaryFile(mode="w+b", prefix="xu-bash-", suffix=".log")
            room = self._spill_max - self._spilled
            self._file.write(b[:room])
            self._spilled += len(b[:room])
            self._dropped += max(0, len(b) - len(b[:room]))
        else:
            self._dropped += len(b)

    def head(self) -> str:
        return "".join(self._head)

    def spill_path(self) -> str | None:
        return getattr(self._file, "name", None) if self._file is not None else None

    def detail(self) -> str:
        """Model-facing note when output overflowed; '' otherwise."""
        if not self.truncated:
            return ""
        part = f"truncated to {_fmt_bytes(self._head_bytes + self._spilled)}"
        if self._dropped:
            part += f", {_fmt_bytes(self._dropped)} dropped"
        return f"\n…[{part}]"

    def close(self) -> None:
        if self._file is not None:
            self._file.close()
            self._file = None
_SHELLS: dict[str, "ShellSession"] = {}


class ShellSession:
    """One persistent shell per session id."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd  # live shell cwd — follows the model's own `cd`
        self._session_cwd = cwd  # cwd the session last demanded (user-set)
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    async def _ensure(self) -> asyncio.subprocess.Process:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc
        bash = _bash_path()
        if bash is None:
            raise FileNotFoundError(_no_bash_message())
        self._proc = await asyncio.create_subprocess_exec(
            bash,
            "--norc",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env={**os.environ, "PS1": "", "TERM": "dumb"},
            **_process.spawn_isolation(),
        )
        return self._proc

    async def run(
        self, command: str, timeout: float = 120.0, *, session_cwd: str | None = None
    ) -> tuple[int, str, str, float]:
        async with self._lock:
            proc = await self._ensure()
            assert proc.stdin and proc.stdout and proc.stderr
            # A unique sentinel printed to stdout only delimits this command's
            # output and carries the exit code + post-command $PWD. stdout is
            # read until the sentinel; stderr is drained for the same window.
            sentinel = f"__XU_DONE_{os.getpid()}_{abs(hash(command)) % 100000}__"
            # Only force `cd` when the *session* cwd changed since the last
            # call (user moved the session). Otherwise leave the shell where
            # the model's own `cd` left it — the description promises cwd
            # persistence across calls.
            prefix = ""
            if session_cwd is not None and session_cwd != self._session_cwd:
                # Git Bash accepts a native path, but only with forward
                # slashes — `cd 'C:\Users\x'` would eat the separators as
                # escapes inside the quotes.
                target = session_cwd.replace("\\", "/") if _process.IS_WINDOWS else session_cwd
                prefix = f"cd {shlex.quote(target)} 2>/dev/null; "
                self._session_cwd = session_cwd
                self.cwd = session_cwd
            # The command runs inside a group with stdin redirected from
            # /dev/null: anything that would otherwise consume the shell's
            # stdin (cat, ssh, an unclosed heredoc, a REPL) sees instant EOF
            # instead of swallowing the sentinel bookkeeping line below —
            # which used to hang the call for the full timeout and cost us
            # the persistent shell. The group is not a subshell, so a `cd`
            # inside it persists in the shell.
            wrapper = (
                f"{prefix}"
                f"{{ {command}\n}} < /dev/null\n"
                f"__xu_rc=$?; printf '%s %s %s\\n' "
                f"'{sentinel}' \"$__xu_rc\" {_pwd_expr()} 1>&1\n"
            )
            proc.stdin.write(wrapper.encode())
            await proc.stdin.drain()
            started = asyncio.get_event_loop().time()
            out_buf = _StreamBuffer(_BASH_MAX_STREAM, _BASH_MAX_SPILL)
            err_buf = _StreamBuffer(_BASH_MAX_STREAM, _BASH_MAX_SPILL)
            self._last_rc = 0

            # Read in fixed-size chunks, never by line. `StreamReader.readline`
            # raises ValueError ("Separator is not found, and chunk exceed the
            # limit") on any line longer than its 64 KB limit — minified JS, a
            # one-line JSON body, base64 — and a shell may legitimately emit
            # one. Chunked reads have no line-length ceiling; the sentinel scan
            # works the same because the sentinel is short and ASCII.
            #
            # Decoding is incremental so a multi-byte character split across a
            # chunk boundary survives instead of becoming U+FFFD.
            keep = len(sentinel) - 1  # tail that could hold a split sentinel
            _stdout_done = [False]

            async def _read_stdout() -> None:
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                pending = ""
                while True:
                    chunk = await proc.stdout.read(_BASH_READ_CHUNK)
                    if not chunk:
                        pending += decoder.decode(b"", final=True)
                        if pending:
                            out_buf.write(pending)
                        return
                    pending += decoder.decode(chunk)
                    idx = pending.find(sentinel)
                    if idx >= 0:
                        out_buf.write(pending[:idx])
                        # The sentinel's own line ("<rc> <pwd>") may still be
                        # in flight — keep reading until it is complete.
                        rest = pending[idx + len(sentinel):]
                        while "\n" not in rest:
                            more = await proc.stdout.read(_BASH_READ_CHUNK)
                            if not more:
                                rest += decoder.decode(b"", final=True)
                                break
                            rest += decoder.decode(more)
                        parts = rest.split("\n", 1)[0].strip().split(None, 1)
                        try:
                            self._last_rc = int(parts[0])
                        except (IndexError, ValueError):
                            self._last_rc = 0
                        if len(parts) > 1 and parts[1]:
                            self.cwd = parts[1]
                        return
                    # No sentinel yet: flush all but a short tail, so a long
                    # line streams into the bounded buffer instead of growing
                    # here unbounded.
                    if len(pending) > keep:
                        out_buf.write(pending[:len(pending) - keep])
                        pending = pending[len(pending) - keep:]

            async def _read_stderr() -> None:
                # Drain stderr until stdout finds the sentinel or timeout.
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            proc.stderr.read(_BASH_READ_CHUNK), timeout=0.1
                        )
                    except asyncio.TimeoutError:
                        # nothing right now; if stdout is done, stop
                        if _stdout_done[0]:
                            return
                        continue
                    if not chunk:
                        return
                    err_buf.write(decoder.decode(chunk))

            async def _read_stdout_wrapped() -> None:
                try:
                    await _read_stdout()
                finally:
                    _stdout_done[0] = True

            try:
                await asyncio.wait_for(
                    asyncio.gather(_read_stdout_wrapped(), _read_stderr()),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                await self._discard()
                rc = 124
            except Exception as exc:  # noqa: BLE001 — see below
                # A failed read leaves this command's sentinel, and any amount
                # of its output, unread in the pipe. Reusing the shell would
                # make the NEXT call read this command's leftovers and miss its
                # own sentinel — one bad command used to break `bash` for the
                # rest of the session. The shell is untrusted now: drop it.
                await self._discard()
                rc = 125
                err_buf.write(
                    f"shell read failed ({type(exc).__name__}: {exc}); "
                    "the session shell was restarted, so `cd` and exported "
                    "variables are back to their defaults"
                )
            else:
                rc = getattr(self, "_last_rc", 0)
            out = out_buf.head()
            err = err_buf.head()
            detail = out_buf.detail() or err_buf.detail()
            out_buf.close()
            err_buf.close()
            if detail:
                out = (out if out else err) + detail
            return rc, out, err, asyncio.get_event_loop().time() - started

    async def _discard(self) -> None:
        """Kill the shell's process group and forget it, so the next call gets
        a clean one. Used whenever a command's output can no longer be trusted
        to line up with its sentinel (timeout, read error)."""
        proc, self._proc = self._proc, None
        if proc is None:
            return
        await _process.kill_tree(proc.pid, force=True)
        with contextlib.suppress(ProcessLookupError):
            await proc.wait()

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            await _process.kill_tree(self._proc.pid, force=False)
            try:
                await self._proc.wait()
            except ProcessLookupError:
                pass
        self._proc = None


def _session(ctx: ToolContext) -> ShellSession:
    s = _SHELLS.get(ctx.session_id)
    if s is None:
        s = ShellSession(ctx.cwd)
        _SHELLS[ctx.session_id] = s
    return s


class BashTool(Tool):
    name = "bash"
    toolset = "terminal"
    description = (
        "Run a shell command in the session's persistent bash shell. The working "
        "directory persists across calls. Returns combined stdout, stderr, and exit code. "
        "Destructive commands (rm, mkfs, dd, shutdown…) always require approval."
    )
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "shell command to execute"},
            "timeout": {"type": "number", "description": "seconds before kill (default 120)", "default": 120},
        },
        "required": ["command"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult.err("empty command")
        timeout = float(args.get("timeout", 120))
        shell = _session(ctx)
        try:
            rc, out, err, elapsed = await shell.run(
                command, timeout=timeout, session_cwd=ctx.cwd
            )
        except FileNotFoundError as e:
            # No usable bash: report it, don't raise into the agent loop.
            return ToolResult.err(str(e))
        parts = []
        if out:
            parts.append(out.rstrip("\n"))
        if err:
            parts.append(f"[stderr]\n{err.rstrip('\n')}")
        body = "\n".join(parts) if parts else "(no output)"
        text = f"$ {command}\n{body}\n[exit {rc} · {elapsed:.2f}s]"
        return ToolResult.ok(text, raw=text, rc=rc, elapsed=round(elapsed, 3))


bash = BashTool()


def close_session(session_id: str) -> None:
    s = _SHELLS.pop(session_id, None)
    if s is not None:
        import asyncio

        try:
            asyncio.get_event_loop().create_task(s.close())
        except RuntimeError:
            pass
