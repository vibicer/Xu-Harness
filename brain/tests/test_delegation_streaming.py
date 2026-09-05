"""Delegation streaming must not re-persist or re-broadcast the whole record.

Mirroring a delegated child's events into its activity record used to do two
O(total history) things per streamed token, synchronously on the event loop:

1. ``_save_delegations()`` — serialize and rewrite the single shared
   ``delegations.json`` (measured at ~5ms dumps + ~1.5ms write on a real 630KB
   file, and it grows with every past run, not just the live one);
2. ``bus.emit("subagent.activity", run=rec)`` — ship the entire run record,
   which reaches 100KB+, over the WebSocket.

Both scaled with token count, so a delegated turn slowed down the more the
child said, and stalled the *parent's* stream too. Now token events are
throttled on disk and carry a ``subagent.delta`` patch on the wire, while
structural events (tool chips) still force a save and a full snapshot.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from xu_brain.core.notify import notify


class _RecordingBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def emit(self, event: str, **payload) -> None:  # noqa: ANN003
        self.events.append((event, payload))

    def of(self, name: str) -> list[dict]:
        return [p for e, p in self.events if e == name]


@pytest.fixture
def recbus(monkeypatch):
    """Patch the shared notifier's ``emit``, not a module-global alias.

    Every agent module imports the same ``notify`` singleton, so the seam has
    to be the object's method — rebinding one module's name would leave the
    others (``live``, ``delegation``) emitting to the real shell.
    """
    b = _RecordingBus()
    monkeypatch.setattr(notify, "emit", b.emit)
    return b


def _agent(tmp_path):
    """Agent with just enough wiring for _emit_for's delegation mirror."""
    from xu_brain.features.agent.loop import Agent

    obj = Agent.__new__(Agent)
    obj.data_home = tmp_path
    obj._live_turns = {}
    obj._delegations = {}
    obj._delegation_by_session = {"child-sess": "drun"}
    obj._delegations_saved = 0.0
    obj._delegations_dirty = False
    obj._delegations[

        "drun"
    ] = {
        "id": "drun", "child": "coder", "prompt": "do work",
        "parent_session": "parent", "status": "running",
        "started": time.time(), "finished": None, "result": "",
        "steps": [], "reasoning": "",
    }
    return obj


async def _stream(agent, n: int, event: str = "turn.delta") -> None:
    for i in range(n):
        await agent._emit_for("child-sess", "t1", event, delta=f"tok{i} ")


# --- disk ------------------------------------------------------------------

def test_token_stream_does_not_write_per_token(tmp_path, recbus):
    """100 tokens must not mean 100 rewrites of the shared blob."""
    agent = _agent(tmp_path)
    writes = {"n": 0}
    real = agent._save_delegations

    def counting(force: bool = False) -> None:
        writes["n"] += 1 if force or (time.monotonic() - agent._delegations_saved) >= agent._SAVE_INTERVAL else 0
        real(force=force)

    agent._save_delegations = counting
    asyncio.run(_stream(agent, 100))

    # The throttle allows at most the first write plus one per _SAVE_INTERVAL;
    # a fast 100-token burst finishes well inside one interval.
    assert writes["n"] <= 2, f"still writing per token ({writes['n']} writes)"


def test_structural_event_forces_a_save(tmp_path, recbus):
    """A tool chip is a real checkpoint: it must hit disk even mid-throttle."""
    agent = _agent(tmp_path)
    asyncio.run(_stream(agent, 5))  # arms the throttle
    asyncio.run(agent._emit_for("child-sess", "t1", "turn.tool",
                                tool="read", args="x.py", status="ok"))

    on_disk = json.loads((tmp_path / "delegations.json").read_text("utf-8"))
    kinds = [s.get("kind") for s in on_disk["drun"]["steps"]]
    assert "tool" in kinds, "tool chip never reached disk"


def test_terminal_finish_always_persists(tmp_path, recbus):
    """finish_delegation is the last chance to persist; it cannot be throttled."""
    agent = _agent(tmp_path)
    asyncio.run(_stream(agent, 5))
    agent.finish_delegation("drun", "the real result", "ok")

    on_disk = json.loads((tmp_path / "delegations.json").read_text("utf-8"))
    assert on_disk["drun"]["status"] == "ok"
    assert on_disk["drun"]["result"] == "the real result"


# --- wire ------------------------------------------------------------------

def test_token_stream_emits_deltas_not_full_records(tmp_path, recbus):
    agent = _agent(tmp_path)
    asyncio.run(_stream(agent, 10))

    assert not recbus.of("subagent.activity"), \
        "full record still broadcast per token"
    deltas = recbus.of("subagent.delta")
    assert len(deltas) == 10
    assert deltas[0]["run_id"] == "drun"
    assert deltas[0]["kind"] == "text"
    assert deltas[0]["delta"] == "tok0 "
    assert deltas[0]["session_id"] == "parent", "delta not routed to the parent"


def test_reasoning_deltas_are_tagged(tmp_path, recbus):
    agent = _agent(tmp_path)
    asyncio.run(_stream(agent, 3, event="turn.reasoning"))

    kinds = {p["kind"] for p in recbus.of("subagent.delta")}
    assert kinds == {"reasoning"}


def test_structural_event_still_sends_a_full_snapshot(tmp_path, recbus):
    """The snapshot is what resyncs a shell whose deltas drifted."""
    agent = _agent(tmp_path)
    asyncio.run(_stream(agent, 5))
    asyncio.run(agent._emit_for("child-sess", "t1", "turn.tool",
                                tool="read", args="x.py", status="running"))

    snaps = recbus.of("subagent.activity")
    assert len(snaps) == 1
    assert snaps[0]["run"]["id"] == "drun"


def test_record_still_accumulates_the_streamed_text(tmp_path, recbus):
    """Throttling persistence must not lose content from the in-memory record —
    the shell's snapshot and `subagent.activity` RPC both read from it."""
    agent = _agent(tmp_path)
    asyncio.run(_stream(agent, 4))

    steps = agent._delegations["drun"]["steps"]
    assert [s["kind"] for s in steps] == ["text"]
    assert steps[0]["text"] == "tok0 tok1 tok2 tok3 "
