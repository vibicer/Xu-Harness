"""Reasoning steps carry an `elapsed`, the same way a tool chip does.

The settled thinking row shows how long the model spent on that thought instead
of a show/hide word, so the number has to come from the turn itself and survive
into the persisted row. Three things this pins:

1. the duration is measured first-delta to last-delta of *that* segment, so two
   segments in one turn get two different numbers;
2. the private `_t0`/`_t1` stamps never reach the persisted step — a key the
   shell has no meaning for would ride along in the session row forever;
3. a segment still open when the turn is cut short keeps no duration. There is
   nothing honest to report for a thought that never finished.
"""
from __future__ import annotations

from xu_brain.features.agent import live as live_mod
from xu_brain.features.agent.loop import Agent


class Clock:
    """Hand-fed monotonic clock, so the test says what the durations are."""

    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


def _agent(clock: Clock) -> Agent:
    agent = object.__new__(Agent)
    agent._live_turns = {}
    agent._delegations = {}
    agent.data_home = None
    return agent


def _merge(agent: Agent, live: dict, event: str, **kw) -> None:
    agent._merge_live(live, event, kw)


def test_segment_elapsed_is_measured_first_delta_to_last(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(live_mod.time, "perf_counter", clock)
    agent, live = _agent(clock), {"turn_id": "t1", "steps": [], "reasoning": ""}

    clock.t = 10.0
    _merge(agent, live, "turn.reasoning", delta="thinking ")
    clock.t = 12.5
    _merge(agent, live, "turn.reasoning", delta="hard.")
    _merge(agent, live, "turn.delta", delta="the answer")

    steps = agent._settle_reasoning(live["steps"])
    assert steps[0]["kind"] == "reasoning"
    assert steps[0]["elapsed"] == 2.5, steps[0]
    assert steps[1]["kind"] == "text"


def test_two_segments_get_their_own_durations(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(live_mod.time, "perf_counter", clock)
    agent, live = _agent(clock), {"turn_id": "t1", "steps": [], "reasoning": ""}

    clock.t = 0.0
    _merge(agent, live, "turn.reasoning", delta="first thought")
    clock.t = 1.0
    _merge(agent, live, "turn.tool", tool="bash", status="ok", elapsed=0.2)
    clock.t = 5.0
    _merge(agent, live, "turn.reasoning", delta="second thought")
    clock.t = 8.0
    _merge(agent, live, "turn.delta", delta="done")

    kinds = [(s["kind"], s.get("elapsed")) for s in agent._settle_reasoning(live["steps"])]
    assert kinds == [("reasoning", 1.0), ("tool", 0.2), ("reasoning", 3.0), ("text", None)]


def test_private_stamps_never_reach_the_persisted_step(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(live_mod.time, "perf_counter", clock)
    agent, live = _agent(clock), {"turn_id": "t1", "steps": [], "reasoning": ""}

    clock.t = 0.0
    _merge(agent, live, "turn.reasoning", delta="a thought")
    clock.t = 2.0
    _merge(agent, live, "turn.delta", delta="an answer")

    for step in agent._settle_reasoning(live["steps"]):
        assert "_t0" not in step and "_t1" not in step, step


def test_a_thought_still_open_when_the_turn_ends_keeps_no_duration(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(live_mod.time, "perf_counter", clock)
    agent, live = _agent(clock), {"turn_id": "t1", "steps": [], "reasoning": ""}

    clock.t = 0.0
    _merge(agent, live, "turn.reasoning", delta="interrupted mid-thought")

    steps = agent._settle_reasoning(live["steps"])
    assert "elapsed" not in steps[0], steps[0]
    assert "_t0" not in steps[0] and "_t1" not in steps[0]
