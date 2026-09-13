"""ShellNotifier fan-out.

One stalled shell must not freeze approvals (or anything else) for the others:
a client that stops reading fills its TCP window, so its ``send`` blocks. The
old ``emit`` held the shared lock across that ``send``, so every other client's
events stalled behind it — including ``ApprovalManager.request``'s
``turn.approval`` emission, which made approval cards silently stop appearing.
"""
from __future__ import annotations

import asyncio
import json

from xu_brain.core import notify as notify_mod
from xu_brain.core.notify import ShellNotifier


class _Transport:
    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


class FakeShell:
    """Minimal stand-in for a ``ServerConnection``."""

    def __init__(self, *, stall: bool = False) -> None:
        self.stall = stall
        self.messages: list[str] = []
        self.delivered = asyncio.Event()
        self.transport = _Transport()

    async def send(self, message: str) -> None:
        if self.stall:
            # A client that stopped reading: this never resolves on its own.
            await asyncio.Event().wait()
        self.messages.append(message)
        self.delivered.set()

    def events(self) -> list[str]:
        return [json.loads(m)["params"]["event"] for m in self.messages]
async def test_stalled_client_does_not_block_a_healthy_one(monkeypatch):
    monkeypatch.setattr(notify_mod, "_SEND_TIMEOUT", 0.5)
    n = ShellNotifier()
    stalled = FakeShell(stall=True)
    healthy = FakeShell()
    n.attach(stalled)
    n.attach(healthy)

    emit_task = asyncio.create_task(n.emit("turn.approval", request_id="r1"))

    # The healthy client is delivered promptly, while the stalled client is
    # still holding up its own send.
    await asyncio.wait_for(healthy.delivered.wait(), timeout=0.25)
    assert not emit_task.done()

    await asyncio.wait_for(emit_task, timeout=2.0)
    assert healthy.events() == ["turn.approval"]
    # The stalled client was dropped, not left to stall a later fan-out.
    assert stalled not in n._clients
    assert stalled.transport.aborted
    assert healthy.events() == ["turn.approval"]
    # The stalled client was dropped, not left to stall a later fan-out.
    assert stalled not in n._clients
    assert stalled.transport.aborted


async def test_send_error_drops_only_the_broken_client():
    n = ShellNotifier()
    broken = FakeShell()
    healthy = FakeShell()

    async def boom(_message: str) -> None:
        raise ConnectionResetError("peer gone")

    broken.send = boom  # type: ignore[method-assign]
    n.attach(broken)
    n.attach(healthy)

    await n.emit("state.updated", session_id="s1")

    assert healthy.events() == ["state.updated"]
    assert broken not in n._clients
    assert broken.transport.aborted


async def test_healthy_client_receives_events_in_order():
    n = ShellNotifier()
    healthy = FakeShell()
    n.attach(healthy)

    for i in range(5):
        await n.emit("turn.notice", seq=i)

    assert [json.loads(m)["params"]["seq"] for m in healthy.messages] == [0, 1, 2, 3, 4]


async def test_detached_client_is_not_sent_to():
    n = ShellNotifier()
    shell = FakeShell()
    n.attach(shell)
    n.detach(shell)

    await n.emit("state.updated", session_id="s1")

    assert shell.messages == []


async def test_emit_with_no_clients_is_a_noop():
    n = ShellNotifier()
    await n.emit("state.updated", session_id="s1")
