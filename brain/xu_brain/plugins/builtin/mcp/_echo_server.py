"""A minimal stdio MCP server, for the plugin's own check and tests.

Not part of the plugin's runtime path. Run it as a child process the way a real
server is run::

    python _echo_server.py

Tools: ``echo`` (returns its ``text``), ``sleep`` (blocks, to exercise the
request timeout), ``boom`` (returns an ``isError`` result).
"""
from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the given text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "sleep",
        "description": "Block for `seconds` before answering.",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}},
        },
    },
    {
        "name": "boom",
        "description": "Always fails.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _call(name: str, args: dict) -> dict:
    if name == "echo":
        return {"content": [{"type": "text", "text": str(args.get("text", ""))}]}
    if name == "sleep":
        time.sleep(float(args.get("seconds", 1)))
        return {"content": [{"type": "text", "text": "awake"}]}
    if name == "boom":
        return {"content": [{"type": "text", "text": "exploded"}], "isError": True}
    return {"content": [{"type": "text", "text": f"no such tool: {name}"}], "isError": True}


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method")
        rid = message.get("id")
        if rid is None:  # a notification — nothing to answer
            continue
        params = message.get("params") or {}
        if method == "initialize":
            result = {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "echo", "version": "1.0.0"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = _call(str(params.get("name", "")), params.get("arguments") or {})
        else:
            _send({"jsonrpc": "2.0", "id": rid,
                   "error": {"code": -32601, "message": f"unknown method: {method}"}})
            continue
        _send({"jsonrpc": "2.0", "id": rid, "result": result})


if __name__ == "__main__":
    main()
