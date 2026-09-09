"""Turn-tail memory auto-capture wiring.

    python -m pytest tests/test_memcapture_trigger.py -q

`_run` schedules `_memcapture` as a background task on completed root turns;
`_memcapture` delegates to the extractor and emits one `memory.captured`
event per accepted candidate. This file pins the wiring, not the extractor.
"""
from __future__ import annotations

import asyncio
import inspect

import pytest

pytest.importorskip("xu_brain.features.memory.autocapture",
                    reason="autocapture module not present")

from xu_brain.core.runtime import build_app  # noqa: E402


def test_events_tuple_contains_memory_captured() -> None:
    from xu_brain.api.events import EVENTS
    assert "memory.captured" in EVENTS


def test_run_wires_memcapture_on_root_turns(tmp_path) -> None:
    from xu_brain.core.runtime import build_app

    app = build_app(tmp_path / "xu")
    src = inspect.getsource(type(app.agent)._run)
    # the scheduling site: root turns only, background task, after finish
    assert "create_task(self._memcapture" in src
    assert "depth == 0" in src
    assert "memory capture" in inspect.getsource(type(app.agent)._memcapture).lower()


def test_memcapture_emits_event_per_candidate(tmp_path, monkeypatch) -> None:
    from xu_brain.features.memory import autocapture as ac

    app = build_app(tmp_path / "xu2")

    async def fake_capture(agent, session_id, user_text, assistant_text, *, turn_id=""):
        return [{"content": "fact a", "importance": 0.6, "kind": "preference"},
                {"content": "fact b", "importance": 0.4, "kind": "correction"}]

    monkeypatch.setattr(ac, "capture_turn", fake_capture)

    import xu_brain.features.agent.loop as loop_mod
    emitted: list[tuple[str, dict]] = []

    async def fake_emit(event, **kw):
        emitted.append((event, kw))

    monkeypatch.setattr(loop_mod.notify, "emit", fake_emit)

    asyncio.run(app.agent._memcapture("sess-1", "turn-1", "user text", "assistant text"))

    got = [kw for event, kw in emitted if event == "memory.captured"]
    assert len(got) == 2
    assert {g["content"] for g in got} == {"fact a", "fact b"}
    assert all(g["session_id"] == "sess-1" for g in got)


def test_memcapture_silent_when_nothing_captured(tmp_path, monkeypatch) -> None:
    from xu_brain.features.memory import autocapture as ac

    app = build_app(tmp_path / "xu3")

    async def fake_capture(agent, session_id, user_text, assistant_text, *, turn_id=""):
        return []

    monkeypatch.setattr(ac, "capture_turn", fake_capture)

    import xu_brain.features.agent.loop as loop_mod
    emitted: list[tuple[str, dict]] = []

    async def fake_emit(event, **kw):
        emitted.append((event, kw))

    monkeypatch.setattr(loop_mod.notify, "emit", fake_emit)

    asyncio.run(app.agent._memcapture("sess-1", "turn-1", "u", "a"))
    assert not [e for e in emitted if e[0] == "memory.captured"]


def test_memcapture_fail_soft_on_extractor_crash(tmp_path, monkeypatch) -> None:
    from xu_brain.features.memory import autocapture as ac

    app = build_app(tmp_path / "xu4")

    async def boom(agent, session_id, user_text, assistant_text, *, turn_id=""):
        raise RuntimeError("extractor exploded")

    monkeypatch.setattr(ac, "capture_turn", boom)
    # must not raise
    asyncio.run(app.agent._memcapture("sess-1", "turn-1", "u", "a"))
