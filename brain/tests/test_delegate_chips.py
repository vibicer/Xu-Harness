"""Concurrent delegate chips stay distinct in the parent's live transcript.

Four sibling ``delegate`` calls run at once. Two things used to collapse them:

1. every chip claimed the *newest* running delegation record, so all four chips
   pointed at one sub-agent and the rest never showed activity;
2. the live-step merge settled the first chip whose *tool name* matched, so a
   child finishing out of order overwrote another child's chip.
"""
from __future__ import annotations

from xu_brain.features.agent.loop import Agent


def _agent() -> Agent:
    agent = object.__new__(Agent)
    agent._delegations = {}
    agent._live_turns = {}
    agent.data_home = None
    agent._delegations_saved = 0.0
    agent._delegations_dirty = False
    return agent


def _run(agent: Agent, run_id: str, child: str, started: float) -> None:
    agent._delegations[run_id] = {
        "id": run_id, "child": child, "prompt": child, "parent_session": "parent",
        "status": "running", "started": started, "finished": None, "result": "",
        "steps": [], "reasoning": "",
    }


def test_each_concurrent_chip_claims_its_own_run():
    agent = _agent()
    agent._live_turns["parent"] = {"turn_id": "t1", "steps": [], "reasoning": ""}
    for i in range(1, 5):
        _run(agent, f"d{i}", f"reviewer-{i}", float(i))

    steps = agent._live_turns["parent"]["steps"]
    for i in range(1, 5):
        step = agent._capture_tool(
            "delegate", {"label": f"reviewer-{i}", "prompt": f"part {i}"},
            "parent", call_id=f"c{i}",
        )
        steps.append(step)

    assert [s["subagent_run"] for s in steps] == ["d1", "d2", "d3", "d4"]
    assert len({s["subagent_run"] for s in steps}) == 4
    assert [s["call_id"] for s in steps] == ["c1", "c2", "c3", "c4"]


def test_unlabelled_siblings_still_claim_distinct_runs():
    """A normal agent delegates without preset names — every record is 'subagent'."""
    agent = _agent()
    agent._live_turns["parent"] = {"turn_id": "t1", "steps": [], "reasoning": ""}
    for i in range(1, 4):
        _run(agent, f"d{i}", "subagent", float(i))

    steps = agent._live_turns["parent"]["steps"]
    for i in range(1, 4):
        steps.append(agent._capture_tool("delegate", {"prompt": f"part {i}"}, "parent", call_id=f"c{i}"))

    assert [s.get("subagent_run") for s in steps] == ["d1", "d2", "d3"]


def test_out_of_order_completion_settles_the_right_chip():
    agent = _agent()
    live = {"turn_id": "t1", "steps": [], "reasoning": ""}
    for i in range(1, 5):
        agent._merge_live(live, "turn.tool", {
            "tool": "delegate", "args": f"label=reviewer-{i}", "status": "running",
            "call_id": f"c{i}", "subagent_run": f"d{i}", "subagent": f"reviewer-{i}",
        })
    assert len(live["steps"]) == 4

    for i in (4, 2, 1, 3):
        agent._merge_live(live, "turn.tool", {
            "tool": "delegate", "args": f"label=reviewer-{i}", "status": "ok",
            "call_id": f"c{i}", "elapsed": 1.0, "output": f"REPORT {i}",
            "subagent_run": f"d{i}", "subagent": f"reviewer-{i}",
        })

    assert len(live["steps"]) == 4, "settling must not append duplicate chips"
    assert [s["status"] for s in live["steps"]] == ["ok"] * 4
    assert [s["output"] for s in live["steps"]] == [f"REPORT {i}" for i in range(1, 5)]
    assert [s["subagent_run"] for s in live["steps"]] == ["d1", "d2", "d3", "d4"]


def test_chips_without_call_id_keep_the_name_match():
    """Legacy emits carry no call id; the old settle rule still applies."""
    agent = _agent()
    live = {"turn_id": "t1", "steps": [], "reasoning": ""}
    agent._merge_live(live, "turn.tool", {"tool": "bash", "args": "ls", "status": "running"})
    agent._merge_live(live, "turn.tool", {
        "tool": "bash", "args": "ls", "status": "ok", "elapsed": 0.1, "output": "files",
    })
    assert len(live["steps"]) == 1
    assert live["steps"][0]["status"] == "ok"


def test_activity_by_group_returns_the_fan_outs_siblings():
    """One delegate chip may cover a whole `tasks` batch, so the shell asks for
    the group (parent turn id) to tab over every concurrent sub-agent."""
    agent = _agent()
    for i in range(1, 4):
        _run(agent, f"d{i}", f"reviewer-{i}", float(4 - i))  # born out of order
        agent._delegations[f"d{i}"]["group"] = "t1"
    _run(agent, "other", "loner", 9.0)
    agent._delegations["other"]["group"] = "t2"

    got = agent.delegation_activity(group="t1", session_id="parent")
    assert [r["id"] for r in got] == ["d3", "d2", "d1"], "launch order, siblings only"
    assert agent.delegation_activity(group="t1", session_id="someone-else") == []
    assert [r["id"] for r in agent.delegation_activity(group="t2")] == ["other"]


def test_chip_reports_how_many_agents_one_call_spawned():
    """A `tasks` batch fans out inside a single tool call, so the chip carries
    the count — the args summary only shows a truncated list."""
    agent = _agent()
    one = agent._capture_tool("delegate", {"label": "reviewer-1", "prompt": "go"}, None)
    assert one["subagent_count"] == 1

    batch = agent._capture_tool(
        "delegate",
        {"tasks": [{"label": f"reviewer-{i}", "prompt": f"part {i}"} for i in range(1, 5)]},
        None,
    )
    assert batch["subagent_count"] == 4

    assert "subagent_count" not in agent._capture_tool("bash", {"command": "ls"}, None)


def test_count_survives_into_the_live_snapshot():
    agent = _agent()
    live = {"turn_id": "t1", "steps": [], "reasoning": ""}
    agent._merge_live(live, "turn.tool", {
        "tool": "delegate", "args": "tasks=[…]", "status": "running",
        "call_id": "c1", "subagent_count": 4,
    })
    agent._merge_live(live, "turn.tool", {
        "tool": "delegate", "args": "tasks=[…]", "status": "ok",
        "call_id": "c1", "elapsed": 1.0, "output": "4/4 succeeded", "subagent_count": 4,
    })
    assert [s["subagent_count"] for s in live["steps"]] == [4]


def test_sibling_calls_report_the_whole_rounds_spawn_count():
    """Four sibling `delegate` calls each spawn one child, but the label must
    read "4 agents spawned" on every chip — the round is the fan-out, so a
    per-call count showed "1 agent spawned" four times."""
    agent = _agent()
    parsed = [
        (
            {"id": f"c{i}"},
            agent._capture_tool("delegate", {"label": f"reviewer-{i}", "prompt": f"part {i}"}, None),
            "delegate",
        )
        for i in range(1, 5)
    ]
    assert [s["subagent_count"] for _, s, _ in parsed] == [1, 1, 1, 1], "per-call before the sum"

    spawned = sum(s.get("subagent_count") or 0 for _, s, n in parsed if n == "delegate")
    for _, s, n in parsed:
        if n == "delegate":
            s["subagent_count"] = spawned

    assert [s["subagent_count"] for _, s, _ in parsed] == [4, 4, 4, 4]


def test_mixed_round_counts_only_the_delegate_calls():
    """A round may pair delegate calls with unrelated tools; only sub-agents
    count, and non-delegate chips stay unlabelled."""
    agent = _agent()
    parsed = [
        ({"id": "c1"}, agent._capture_tool("bash", {"command": "ls"}, None), "bash"),
        ({"id": "c2"}, agent._capture_tool("delegate", {"label": "a", "prompt": "x"}, None), "delegate"),
        (
            {"id": "c3"},
            agent._capture_tool("delegate", {"tasks": [{"prompt": "y"}, {"prompt": "z"}]}, None),
            "delegate",
        ),
    ]
    spawned = sum(s.get("subagent_count") or 0 for _, s, n in parsed if n == "delegate")
    assert spawned == 3, "one named child + a two-task batch"
    assert "subagent_count" not in parsed[0][1], "bash chip carries no count"
