"""The LSP/DAP stdio children must not deadlock on an unread stderr pipe.

Both used to spawn with `stderr=PIPE` and never read it. Once the server wrote
more than the ~64 KB pipe buffer — rust-analyzer progress output, adapter logs,
the debuggee's own stderr — it blocked in `write()` forever, so every later
request timed out until the 300 s idle reap. A stderr flood must not stop the
framed stdout replies from arriving.

Run: brain/.venv/bin/python -m pytest tests/test_stdio_stderr.py -q
"""
from __future__ import annotations

import asyncio
import sys

from xu_brain.features.tools import lsp_debug as L

# Child side: flood stderr, then answer exactly one Content-Length framed
# request read from stdin. The flood is far larger than any pipe buffer, so an
# unread stderr pipe blocks the child before it ever reaches the stdin read.
_FLOOD = 'sys.stderr.write("x" * (512 * 1024))\nsys.stderr.flush()\n'

_READ_FRAME = r'''
inp = sys.stdin.buffer
length = 0
while True:
    line = inp.readline()
    if not line:
        sys.exit(0)
    if line in (b"\r\n", b"\n"):
        break
    if line.lower().startswith(b"content-length:"):
        length = int(line.split(b":", 1)[1])
msg = json.loads(inp.read(length))
'''

_WRITE_FRAME = (
    'sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n%s" % (len(body), body))\n'
    "sys.stdout.buffer.flush()\n"
)

_LSP_FLOOD_SERVER = "import json, sys\n" + _FLOOD + _READ_FRAME + (
    'body = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}}).encode()\n'
) + _WRITE_FRAME

_DAP_FLOOD_ADAPTER = "import json, sys\n" + _FLOOD + _READ_FRAME + (
    'body = json.dumps({"type": "response", "request_seq": msg["seq"], "success": True,'
    ' "body": {"ok": True}}).encode()\n'
) + _WRITE_FRAME


def test_lsp_stderr_flood_does_not_deadlock_requests():
    srv = L._JsonRpcStdio([sys.executable, "-c", _LSP_FLOOD_SERVER])

    async def main():
        try:
            result = await asyncio.wait_for(srv.call("initialize", {}), timeout=15)
            assert result == {"ok": True}, result
        finally:
            await srv.close()

    asyncio.run(main())


def test_dap_stderr_flood_does_not_deadlock_requests():
    dap = L._DapStdio([sys.executable, "-c", _DAP_FLOOD_ADAPTER])

    async def main():
        await dap.start()
        try:
            body = await asyncio.wait_for(dap.request("initialize", {}), timeout=15)
            assert body == {"ok": True}, body
        finally:
            await dap.close()

    asyncio.run(main())
