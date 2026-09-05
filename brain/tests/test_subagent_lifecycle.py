"""Subagent lifecycle: a child that never ends must not strand the record.

Covers the "subagent never ended its job" bug family:

1. a managed subagent whose child turn never resolves its future (provider
   hang / runaway loop / escapee BaseException) is closed by a bounded wait,
   not stuck "running" forever;
2. a ``send`` failure while spawning a managed subagent closes it as an error
   instead of orphaning a tracker on a dead future;
3. a ``run_subtask`` timeout finishes the delegation as an error AND cleans up
   the ephemeral session (the old code's cleanup sat after a ``raise`` and
   never ran);
4. a successful ``run_subtask`` returns the text and still cleans up.
"""
from __future__ import annotations

import asyncio

import pytest



from xu_brain.core.notify import notify
# --- fakes -----------------------------------------------------------------

class _Sess:
    def __init__(self, sid: str) -> None:
        self.id = sid


class _Sessions:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.deleted: list[str] = []
        self._n = 0

    def create(self, cwd: str | None = None) -> _Sess:
        self._n += 1
        sid = f"c{self._n}"
        self.created.append(sid)
        return _Sess(sid)

    def delete(self, sid: str) -> None:
        self.deleted.append(sid)


class _Config:
    def __init__(self, job_timeout: float) -> None:
        self._job_timeout = job_timeout
        self.models: dict[str, object] = {}

    def get(self, key: str, default=None):  # noqa: ANN001
        return self._job_timeout if key == "job_timeout" else default

    def set_session_model(self, sid: str, model) -> None:  # noqa: ANN001
        if model is None:
            self.models.pop(sid, None)
        else:
            self.models[sid] = model


class _Ctx:
    def __init__(self, config: _Config) -> None:
        self.cwd = "/tmp"
        self.session_id = "parent"
        self.config = config


class _FakeBus:
    async def emit(self, *_a, **_k) -> None:
        return None


class _Sub:
    def __init__(self) -> None:
        self.id = "s1"
        self.parent_session = "parent"
        self.session_id = None
        self.status = "running"


class _Orch:
    def __init__(self) -> None:
        self.sub = _Sub()
        self.finishes: list[dict] = []

    def register_subagent(self, *, parent_session, prompt, session_id, label=None) -> _Sub:  # noqa: ANN001
        self.sub.session_id = session_id
        return self.sub

    def finish_subagent(self, sub_id, result="", *, interrupted=False, error=None) -> None:  # noqa: ANN001
        self.finishes.append({"id": sub_id, "result": result, "interrupted": interrupted, "error": error})
        self.sub.status = "error" if error else ("interrupted" if interrupted else "done")


def _agent(job_timeout: float = 900.0, orch: _Orch | None = None) -> object:
    from xu_brain.features.agent.loop import Agent
    obj = Agent.__new__(Agent)
    obj.sessions = _Sessions()
    obj._subtask_sessions = set()
    obj._subtasks = {}
    obj._delegation_by_session = {}
    obj._delegations = {}
    obj._live_turns = {}
    obj._turns = {}
    obj.config = _Config(job_timeout)
    obj.orchestrator = orch
    obj._MAX_RUNS = 60
    obj._MAX_RUN_STEPS = 200
    obj._save_delegations = lambda force=False: None  # avoid disk writes
    obj._inherit_parent_model = lambda *_a, **_k: None
    return obj


def _seed_delegation(agent: object, run_id: str) -> None:
    """run_subtask consumes but does not create the record (begin_delegation
    does, before the tool calls it); seed it for the test."""
    import time
    agent._delegations[run_id] = {  # type: ignore[attr-defined]
        "id": run_id, "child": "coder", "prompt": "do work",
        "parent_session": "parent", "status": "running",
        "started": time.time(), "finished": None, "result": "",
        "steps": [], "reasoning": "",
    }


@pytest.fixture(autouse=True)
def _fake_bus(monkeypatch):
    # Patch the singleton's method: every agent module shares one `notify`,
    # so rebinding a single module's name would miss the others.
    monkeypatch.setattr(notify, "emit", _FakeBus().emit)


# --- managed subagent ------------------------------------------------------

def test_hung_child_is_finished_not_stranded():
    """A child turn that never resolves its future is closed by the bounded
    wait, not left 'running' forever (the headline 'never ended its job' bug)."""
    orch = _Orch()
    agent = _agent(job_timeout=0.05, orch=orch)

    async def noop_send(*_a, **_k) -> None:
        return None  # never resolves the future → simulates a hung child turn

    agent.send = noop_send  # type: ignore[assignment]
    ctx = _Ctx(agent.config)

    async def body() -> str:
        # spawn schedules _track as a background task on THIS loop; keep the
        # loop alive long enough for its bounded wait to time out + finish.
        sid = await agent.spawn_managed_subagent("do the thing", ctx)
        await asyncio.sleep(0.15)
        return sid

    sid = asyncio.run(body())

    assert sid == "s1"
    assert orch.sub.status != "running", "subagent stranded in 'running'"
    assert orch.finishes, "finish_subagent was never called"
    # the hung-child path reports a timeout error
    assert any(f["error"] for f in orch.finishes)
    # ephemeral session + tracking entries cleaned up
    assert agent.sessions.deleted, "ephemeral session not deleted"


def test_send_failure_closes_subagent_as_error():
    """If send() raises while spawning, the subagent is finished as an error
    instead of orphaning a tracker on a future that will never resolve."""
    orch = _Orch()
    agent = _agent(job_timeout=900.0, orch=orch)

    async def boom_send(*_a, **_k) -> None:
        raise RuntimeError("session gone")

    agent.send = boom_send  # type: ignore[assignment]
    ctx = _Ctx(agent.config)

    with pytest.raises(RuntimeError, match="session gone"):
        asyncio.run(agent.spawn_managed_subagent("work", ctx))

    assert orch.sub.status == "error"
    assert orch.finishes and orch.finishes[0]["error"] == "failed to start"
    assert agent.sessions.deleted, "ephemeral session not deleted on send failure"


# --- run_subtask (delegate path) ------------------------------------------

def test_run_subtask_waits_past_job_timeout_and_cleans_up():
    """Foreground delegation is governed by child completion, not job_timeout."""
    agent = _agent(job_timeout=0.01)

    async def resolving_send(sid, prompt, **_k) -> None:  # noqa: ANN001
        await asyncio.sleep(0.03)
        agent._subtasks[sid].set_result("the real result")

    agent.send = resolving_send  # type: ignore[assignment]
    ctx = _Ctx(agent.config)
    _seed_delegation(agent, "dtest")

    text = asyncio.run(agent.run_subtask("do work", ctx, run_id="dtest"))

    assert text == "the real result"
    assert agent._delegations["dtest"]["status"] == "ok"
    assert agent.sessions.deleted, "ephemeral session leaked after foreground completion"
    assert "c1" not in agent._subtasks


def test_run_subtask_success_returns_text_and_cleans_up():
    """A successful run_subtask returns the text, marks delegation ok, and
    still cleans up the ephemeral session (old success path leaked it)."""
    agent = _agent(job_timeout=900.0)

    async def resolving_send(sid, prompt, **_k) -> None:  # noqa: ANN001
        agent._subtasks[sid].set_result("the real result")

    agent.send = resolving_send  # type: ignore[assignment]
    ctx = _Ctx(agent.config)
    _seed_delegation(agent, "dok")

    text = asyncio.run(agent.run_subtask("do work", ctx, run_id="dok"))
    assert text == "the real result"
    assert agent._delegations["dok"]["status"] == "ok"
    assert agent.sessions.deleted, "ephemeral session leaked on success"
    assert "c1" not in agent._subtasks


# --- managed subagents feed the live-activity panel ------------------------


def test_four_completed_reports_are_retained_out_of_order(tmp_path):
    """Each concurrent child keeps its own full report after cleanup."""
    import json
    from xu_brain.features.agent.loop import Agent

    async def scenario():
        agent = object.__new__(Agent)
        agent.data_home = tmp_path
        agent._delegations = {}
        agent._delegations_saved = 0.0
        agent._delegations_dirty = False
        agent._live_turns = {"parent": {"turn_id": "t1", "steps": [], "reasoning": ""}}
        ids = [agent.begin_delegation(f"reviewer-{i}", f"part {i}", "parent") for i in range(1, 5)]
        for run_id in reversed(ids):
            agent.finish_delegation(run_id, f"FULL REPORT {run_id}", "ok")
        await asyncio.sleep(0)
        return ids

    ids = asyncio.run(scenario())
    saved = json.loads((tmp_path / "delegations.json").read_text("utf-8"))
    assert [saved[run_id]["result"] for run_id in ids] == [f"FULL REPORT {run_id}" for run_id in ids]
    assert all(saved[run_id]["status"] == "ok" for run_id in ids)
    # Siblings of one fan-out share the parent turn id: that is what the shell
    # tabs over when a single delegate chip covers several sub-agents.
    assert {saved[run_id]["group"] for run_id in ids} == {"t1"}


def test_managed_subagent_streams_activity_and_closes_delegation(monkeypatch):
    """A managed subagent must open a delegation record and route the child's
    events into it. Regression: spawn never set _delegation_by_session, so
    every subagent.activity emit carried run=None and the web store dropped
    them — background subagents were invisible in the UI's live panel."""
    orch = _Orch()
    agent = _agent(job_timeout=900.0, orch=orch)

    class CapBus:
        def __init__(self):
            self.events: list[tuple[str, dict]] = []

        async def emit(self, event, **kw):
            self.events.append((event, kw))

    cap = CapBus()
    monkeypatch.setattr(notify, "emit", cap.emit)

    async def fast_send(sid, prompt, **_k) -> None:
        fut = agent._subtasks.get(sid)
        if fut and not fut.done():
            fut.set_result("child said hi")

    agent.send = fast_send  # type: ignore[assignment]
    ctx = _Ctx(agent.config)

    async def body() -> str:
        sid = await agent.spawn_managed_subagent("do the thing", ctx)
        await asyncio.sleep(0.05)  # let _track finish
        return sid

    asyncio.run(body())

    assert agent._delegations, "no delegation record opened for the managed subagent"
    rec = next(iter(agent._delegations.values()))
    assert rec["child"] == "subagent"
    assert rec["parent_session"] == "parent"
    assert rec["status"] == "ok"
    assert rec["result"] == "child said hi"

    acts = [kw["run"] for e, kw in cap.events if e == "subagent.activity"]
    assert acts, "no subagent.activity event emitted for the managed subagent"
    assert all(r is not None for r in acts), "activity emit carried run=None"
    assert acts[-1]["status"] == "ok", "terminal event did not carry the finished record"
    # tracking entries cleaned up
    assert not agent._delegation_by_session
    assert agent.sessions.deleted


def test_parent_receives_completion_notice_with_full_result():
    """A finished background subagent pushes its FULL result to the parent
    session instantly, tagged `[subagent result]` so the UI never renders it
    as a message the user typed."""
    orch = _Orch()
    agent = _agent(job_timeout=900.0, orch=orch)

    parent_sends: list[tuple[str, str]] = []

    async def fast_send(sid, prompt, node=None, depth=0):  # noqa: ANN001
        if sid == "c1":  # the child session
            fut = agent._subtasks.get(sid)
            if fut and not fut.done():
                fut.set_result("full child report")
            return None, None
        parent_sends.append((sid, prompt))
        return None, None

    agent.send = fast_send  # type: ignore[assignment]
    ctx = _Ctx(agent.config)

    async def body() -> None:
        await agent.spawn_managed_subagent("do the thing", ctx)
        await asyncio.sleep(0.05)  # let _track finish

    asyncio.run(body())

    assert parent_sends, "no completion notice sent to the parent session"
    sid, notice = parent_sends[0]
    assert sid == "parent"
    assert notice.startswith("[subagent result]")
    assert "full child report" in notice
    assert "s1" in notice



def test_send_failure_closes_delegation_record():
    """A failed spawn must close BOTH records (subagent + delegation), not
    just the orchestrator one."""
    orch = _Orch()
    agent = _agent(job_timeout=900.0, orch=orch)

    async def boom_send(*_a, **_k) -> None:
        raise RuntimeError("session gone")

    agent.send = boom_send  # type: ignore[assignment]
    ctx = _Ctx(agent.config)

    with pytest.raises(RuntimeError, match="session gone"):
        asyncio.run(agent.spawn_managed_subagent("work", ctx))

    assert orch.sub.status == "error"
    recs = list(agent._delegations.values())
    assert recs and recs[0]["status"] == "error", "delegation record not closed on spawn failure"


# --- restart bookkeeping ----------------------------------------------------

def test_stale_running_delegation_marked_interrupted_on_load(tmp_path):
    """A delegation 'running' at crash time cannot survive a restart (child
    turns are in-memory); it loads as interrupted, not ghosted in the panel."""
    import json
    import time

    (tmp_path / "delegations.json").write_text(json.dumps({
        "d1": {"id": "d1", "child": "coder", "prompt": "x", "parent_session": "p",
               "status": "running", "started": time.time(), "finished": None,
               "result": "", "steps": [], "reasoning": ""},
    }))
    from xu_brain.features.agent.loop import Agent

    agent = Agent.__new__(Agent)
    agent.data_home = tmp_path
    agent._delegations = {}
    agent._load_delegations()
    assert agent._delegations["d1"]["status"] == "interrupted"


def test_subagent_list_recovers_foreground_report(tmp_path):
    """A normal delegate report remains queryable after its child session ends."""
    from types import SimpleNamespace
    from xu_brain.features.agent.loop import Agent
    from xu_brain.features.tools import agents as agent_tools

    agent = object.__new__(Agent)
    agent.data_home = tmp_path
    agent._delegations = {
        "d1": {
            "id": "d1", "child": "reviewer-1", "prompt": "part 1",
            "parent_session": "parent", "status": "ok", "started": 1,
            "finished": 2, "result": "FULL REPORT: all 200 items reviewed",
            "steps": [], "reasoning": "",
        }
    }
    result = asyncio.run(agent_tools.subagent_list.run(
        {}, SimpleNamespace(agent=agent, session_id="parent")
    ))

    assert not result.error
    assert "FULL REPORT: all 200 items reviewed" in result.output
    assert result.raw[0]["result"].startswith("FULL REPORT")
