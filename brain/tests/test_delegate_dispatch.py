"""Delegation actually dispatches, and a finished child's work is never lost.

Two bugs in the "the API request succeeded but the delegation failed" family:

1. ``_loop`` returned early whenever ``stop_reason`` was ``STOP``, even with
   tool calls buffered. Providers and gateways routinely pair ``tool_calls``
   with a finish_reason that ``_map_stop_reason`` folds to ``STOP``
   ("end_turn", "tool_use", anything unmapped), so the model's ``delegate``
   call was parsed, logged, and then silently dropped — the turn ended on
   whatever text preceded it and no sub-agent ever started.

2. ``DelegateTool`` wrapped ``run_subtask`` in a *second* ``wait_for`` on the
   same ``job_timeout``. The outer deadline fires first and cancels the child,
   so ``run_subtask``'s salvage path saw ``CancelledError`` — which it refuses
   to salvage on purpose, so user stops stay hard — and a child that had just
   finished real work was reported to the orchestrator as "timed out".
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from xu_brain.features.agent.provider import StreamEvent, StopReason
from xu_brain.features.tools import agents as A


# --- 1. tool calls outrank a claimed stop ----------------------------------

def _loop_agent(stream):
    """Minimal Agent wired for a single ``_loop`` call."""
    from xu_brain.features.agent.loop import Agent

    agent = Agent(None, None, None, None, None, None, None, None, None, None)
    agent.config = SimpleNamespace(
        retry_max=lambda: 0,
        retry_interval=lambda: 0,
        session_max_tokens=lambda _sid: None,
        session_model=lambda _sid: "model",
        vision_model=lambda: None,
        get=lambda _key, default=None: default,
        all=lambda: {},
    )
    agent.providers = SimpleNamespace(
        resolve=lambda _model, _p=None: ("provider", "model"),
        chat_stream=stream,
    )
    agent._build_messages = lambda _session, node=None: []

    async def noop(*_a, **_k):
        return None

    async def passthrough(v):
        return v

    async def after_tool(_name, res):
        return res

    agent.flat_plugins = SimpleNamespace(
        on_start=noop, on_message_out=passthrough,
        before_llm=passthrough, after_tool=after_tool,
    )
    agent._maybe_compress = noop
    agent._emit_context = noop
    return agent


def _run_loop(agent):
    from xu_brain.core.notify import notify

    old_emit = notify.emit

    async def fake_emit(*_a, **_k):
        return None

    notify.emit = fake_emit  # type: ignore[assignment]
    try:
        session = SimpleNamespace(id="sess", cwd="/tmp", messages=[])
        return asyncio.run(agent._loop(session, "t1", asyncio.Event()))
    finally:
        notify.emit = old_emit  # type: ignore[assignment]


@pytest.mark.parametrize("finish", [StopReason.STOP, StopReason.TOOL])
def test_tool_calls_are_dispatched_whatever_the_stop_reason(finish):
    """A buffered tool call runs even when the provider also claims STOP."""
    calls = {"n": 0}

    async def stream(_provider, _model, _messages, _tools, max_tokens=None, signal=None):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(tool_call=SimpleNamespace(
                id="c1", name="delegate",
                arguments='{"child":"coder","prompt":"do work"}'))
            # The bug: this STOP made the loop return before dispatching.
            yield StreamEvent(stop_reason=finish)
        else:
            yield StreamEvent(delta="child reported: done")
            yield StreamEvent(stop_reason=StopReason.STOP)

    agent = _loop_agent(stream)
    ran: list[str] = []

    async def run_tool(name, _args, _ctx, emit=None):
        ran.append(name)
        from xu_brain.features.tools.base import ToolResult
        return ToolResult.ok("the real result")

    agent.registry = SimpleNamespace(
        schemas_for_model=lambda: [], reset_breakers=lambda: None, run=run_tool)

    result = _run_loop(agent)

    assert ran == ["delegate"], "the model's delegate call was never dispatched"
    assert calls["n"] == 2, "tool result was never fed back to the model"
    assert result is not None and result.text == "child reported: done"


def test_plain_stop_without_tool_calls_still_ends_the_turn():
    """The guard must not turn a normal finish into an extra provider round."""
    calls = {"n": 0}

    async def stream(_provider, _model, _messages, _tools, max_tokens=None, signal=None):
        calls["n"] += 1
        yield StreamEvent(delta="just an answer")
        yield StreamEvent(stop_reason=StopReason.STOP)

    agent = _loop_agent(stream)
    agent.registry = SimpleNamespace(
        schemas_for_model=lambda: [], reset_breakers=lambda: None)

    result = _run_loop(agent)

    assert calls["n"] == 1
    assert result is not None and result.text == "just an answer"


# --- 2. delegate owns no second deadline -----------------------------------

class _DelegateAgent:
    """Stands in for Agent's delegation surface."""

    def __init__(self, subtask):
        self._subtask = subtask
        self.begins = 0
        self.finished: list[tuple[str, str, str]] = []

    def begin_delegation(self, child, prompt, parent_session):  # noqa: ANN001
        self.begins += 1
        return "drun"

    def finish_delegation(self, run_id, result="", status="ok"):  # noqa: ANN001
        self.finished.append((run_id, result, status))

    async def run_subtask(self, prompt, ctx, tools=None, node=None, depth=0, run_id=None):  # noqa: ANN001
        return await self._subtask()


_SQUAD = {
    "id": "p0", "name": "main", "role": "orchestrator",
    "children": [{"id": "p1", "name": "coder", "role": "agent"}],
}


def _ctx(agent, job_timeout=0.05):
    return SimpleNamespace(
        agent=agent,
        session_id="parent",
        config={"job_timeout": job_timeout, "preset_depth": 0, "preset_node": _SQUAD},
    )


def test_slow_child_result_is_returned_not_reported_as_timeout():
    """A child that outlives the parent's own patience but *does* return work
    hands that work back. Previously the tool's duplicate wait_for cancelled
    the child first and reported 'delegated sub-task timed out'."""
    async def slow_but_finishes():
        await asyncio.sleep(0.15)  # > job_timeout below
        return "the real result"

    agent = _DelegateAgent(slow_but_finishes)
    res = asyncio.run(A.delegate.run(
        {"child": "coder", "prompt": "do work"}, _ctx(agent, job_timeout=0.05)))

    assert not res.error, f"finished child reported as failure: {res.output}"
    assert res.output == "the real result"
    assert res.meta.get("subagent_run") == "drun"
    assert agent.begins == 1, "one delegate call must open exactly one run"


def test_run_subtask_timeout_is_still_surfaced():
    """run_subtask owns the deadline now; its TimeoutError must still close
    the delegation record as an error rather than escaping the tool."""
    async def never_returns():
        raise asyncio.TimeoutError

    agent = _DelegateAgent(never_returns)
    res = asyncio.run(A.delegate.run(
        {"child": "coder", "prompt": "do work"}, _ctx(agent)))

    assert res.error and "timed out" in res.output
    assert agent.finished == [("drun", "", "error")]


# --- 3. rejected delegate calls leak no ghost runs -------------------------

def test_rejected_delegate_call_leaves_no_ghost_run():
    """A delegate call the tool rejects must not leave a 'running' delegation
    record. Regression: _loop pre-reserved the run BEFORE the registry ran the
    tool, so every rejection path (validation error, user stop, denied
    approval, disabled tool) leaked a record stuck 'running' forever — which
    also clogged the 60-run pruner, since it never removes running records."""
    calls = {"n": 0}

    async def stream(_provider, _model, _messages, _tools, max_tokens=None, signal=None):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(tool_call=SimpleNamespace(
                id="c1", name="delegate",
                arguments='{"child":"nobody","prompt":"do work"}'))
            yield StreamEvent(stop_reason=StopReason.STOP)
        else:
            yield StreamEvent(delta="ok")
            yield StreamEvent(stop_reason=StopReason.STOP)

    agent = _loop_agent(stream)

    async def run_tool(_name, _args, _ctx, emit=None):
        from xu_brain.features.tools.base import ToolResult
        return ToolResult.err("no squad member 'nobody'")

    agent.registry = SimpleNamespace(
        schemas_for_model=lambda: [], reset_breakers=lambda: None, run=run_tool)

    _run_loop(agent)

    assert not agent._delegations, f"ghost delegation record leaked: {agent._delegations}"
