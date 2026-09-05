"""Server→shell notifications (contract/methods.md)."""
from __future__ import annotations

import asyncio
import json
from typing import Any

from websockets.asyncio.server import ServerConnection


class ShellNotifier:
    """Broadcasts `event` notifications to every connected shell."""

    def __init__(self) -> None:
        self._clients: set[ServerConnection] = set()
        self._lock = asyncio.Lock()

    def attach(self, ws: ServerConnection) -> None:
        self._clients.add(ws)

    def detach(self, ws: ServerConnection) -> None:
        self._clients.discard(ws)

    async def emit(self, event: str, **payload: Any) -> None:
        message = json.dumps(
            {"jsonrpc": "2.0", "method": "event", "params": {"event": event, **payload}}
        )
        async with self._lock:
            for ws in list(self._clients):
                try:
                    await ws.send(message)
                except Exception:
                    self.detach(ws)


notify = ShellNotifier()
