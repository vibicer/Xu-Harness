"""Process control that differs by platform.

Both the `bash` and `eval` tools hold a long-lived child and need to take its
whole tree down on timeout. The POSIX way (`start_new_session` + `killpg`) has
no Windows equivalent — `start_new_session` is *silently ignored* there and
`os.killpg` does not exist at all — so the two moves live here once instead of
being wrong twice.

`IS_WINDOWS` is read through the module (`_process.IS_WINDOWS`) rather than
imported by value, so a test can flip it in one place and both callers follow.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
from typing import Any

IS_WINDOWS = os.name == "nt"

# Only defined on Windows; the literal keeps the branch importable — and
# testable — from a POSIX host.
CREATE_NEW_PROCESS_GROUP = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

_TASKKILL_TIMEOUT = 5.0   # seconds to wait for taskkill before giving up


def spawn_isolation() -> dict[str, Any]:
    """Kwargs that give a child its own killable group."""
    if IS_WINDOWS:
        return {"creationflags": CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


async def kill_tree(pid: int, *, force: bool) -> None:
    """Kill a child and everything it spawned. Never raises.

    On Windows the graceful/forceful ladder is fiction — a signal becomes
    TerminateProcess and the OS reports the result as exit 1 with no signal —
    so `force` is ignored there and the tree goes down with ``/T /F``.
    """
    if IS_WINDOWS:
        try:
            proc = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=_TASKKILL_TIMEOUT)
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(pid), signal.SIGKILL if force else signal.SIGTERM)
