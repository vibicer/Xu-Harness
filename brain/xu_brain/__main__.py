"""Xu brain entrypoint: `xu-brain`."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import __version__
from .core.runtime import serve

# Set on the process that os.execv()s a replacement, so the fresh image knows
# its predecessor may still be releasing the WS port and retries the bind.
RESTARTED_ENV = "XU_RESTARTED"


def restart_argv() -> list[str]:
    """argv that re-runs this brain exactly as it was launched.

    ``sys.orig_argv`` (3.10+) is the real command line — ``python -m xu_brain
    --port … --data-home … --web-dir …`` — so the replacement inherits every
    flag. The fallback rebuilds the ``-m`` form for an embedded interpreter
    that has no ``orig_argv``.
    """
    orig = getattr(sys, "orig_argv", None)
    if orig and len(orig) > 1:
        return [sys.executable, *orig[1:]]
    return [sys.executable, "-m", "xu_brain", *sys.argv[1:]]


def main() -> int:
    parser = argparse.ArgumentParser(prog="xu-brain", description="Xu agent brain daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("XU_BRAIN_PORT", "9876")))
    parser.add_argument("--data-home", default=None, help="defaults to ~/.xu")
    parser.add_argument("--web-dir", default=None, help="serve this static dir (+SPA fallback) as the webui")
    parser.add_argument("--version", action="version", version=f"xu-brain {__version__}")
    args = parser.parse_args()

    try:
        intent = asyncio.run(
            serve(host=args.host, port=args.port, data_home=args.data_home, web_dir=args.web_dir)
        )
    except KeyboardInterrupt:
        return 0

    if intent == "restart":
        # Replace this process image with a fresh brain. POSIX keeps the pid
        # (so `xu status`/`xu stop` stay valid and the launcher's log fd stays
        # attached); Windows swaps in a new pid, which runtime._rewrite_pid_file
        # repoints. The marker env var is what lets the new image tolerate the
        # old socket lingering for a moment.
        os.environ[RESTARTED_ENV] = "1"
        os.execv(sys.executable, restart_argv())
    return 0


if __name__ == "__main__":
    sys.exit(main())
