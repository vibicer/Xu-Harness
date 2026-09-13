"""Server→shell notifications (contract/methods.md)."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from websockets.asyncio.server import ServerConnection

log = logging.getLogger(__name__)

#: A single fan-out send that hasn't drained within this many seconds means the
#: shell stopped reading (its TCP receive window is full). Waiting on it would
#: stall every other shell, so the client is dropped instead.
_SEND_TIMEOUT = 5.0


def _abort(ws: ServerConnection) -> None:
    """Force-close a client without waiting for its close handshake.

    ``ws.close()`` performs the closing handshake and would itself block on a
    peer that has stopped reading — exactly the client we are dropping.
    """
    transport = getattr(ws, "transport", None)
    if transport is not None:
        transport.abort()


class ShellNotifier:
    """Broadcasts `event` notifications to every connected shell."""

    def __init__(self) -> None:
        self._clients: set[ServerConnection] = set()
        # One send-lock per client: a stalled client holds only *its* lock, so
        # it can't delay the fan-out to anyone else, and two concurrent emits
        # never interleave frames on the same socket.
        self._send_locks: dict[ServerConnection, asyncio.Lock] = {}
        self._lock = asyncio.Lock()

    def attach(self, ws: ServerConnection) -> None:
        self._clients.add(ws)
        self._send_locks.setdefault(ws, asyncio.Lock())

    def detach(self, ws: ServerConnection) -> None:
        self._clients.discard(ws)
        self._send_locks.pop(ws, None)

    async def emit(self, event: str, **payload: Any) -> None:
        message = json.dumps(
            {"jsonrpc": "2.0", "method": "event", "params": {"event": event, **payload}}
        )
        # Snapshot the clients under the lock, then send *outside* it. Holding
        # the lock across `send` let one stalled client (a full TCP window)
        # freeze the fan-out for every other shell — including
        # `turn.approval`, so approval cards silently stopped appearing for
        # everyone else.
        async with self._lock:
            clients = list(self._clients)
        # Send concurrently so one stalled client can't delay the others; each
        # client still receives exactly one message per emit, so a healthy
        # client sees events in order.
        await asyncio.gather(*(self._send(ws, message) for ws in clients))

    async def _send(self, ws: ServerConnection, message: str) -> None:
        lock = self._send_locks.get(ws)
        if lock is None:  # detached between the snapshot and now
            return
        try:
            async with lock:
                await asyncio.wait_for(ws.send(message), _SEND_TIMEOUT)
        except Exception as exc:  # noqa: BLE001
            # Stalled or broken: drop it so it can't hold up later fan-outs,
            # and force the socket closed. Log the cause — a swallowed send
            # error turns a genuinely broken shell into a mystery.
            log.warning("shell client dropped from notify fan-out: %r", exc)
            self.detach(ws)
            try:
                _abort(ws)
            except Exception as abort_exc:  # noqa: BLE001
                # Best-effort teardown must not take the whole fan-out down.
                log.warning("failed to abort dropped shell client: %r", abort_exc)


notify = ShellNotifier()
