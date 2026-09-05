"""Xu brain entrypoint: `xu-brain`."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import __version__
from .core.runtime import serve


def main() -> int:
    parser = argparse.ArgumentParser(prog="xu-brain", description="Xu agent brain daemon")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=int(os.environ.get("XU_BRAIN_PORT", "9876")))
    parser.add_argument("--data-home", default=None, help="defaults to ~/.xu")
    parser.add_argument("--web-dir", default=None, help="serve this static dir (+SPA fallback) as the webui")
    parser.add_argument("--version", action="version", version=f"xu-brain {__version__}")
    args = parser.parse_args()

    try:
        asyncio.run(serve(host=args.host, port=args.port, data_home=args.data_home, web_dir=args.web_dir))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
