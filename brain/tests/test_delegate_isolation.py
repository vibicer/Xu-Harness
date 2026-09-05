"""Leaf sub-agent isolation: no inherited global memory, no silent stalls.

Three contracts, all learned from one wasted session:
1. a leaf agent does not inherit the user's global memory (it cannot act on
   rules addressed to the user-facing agent, e.g. "ask for approval");
2. ``subagent_message`` against a `delegate` child is an error, not a silent
   ``False`` that invites a retry loop;
3. a child that ends its turn asking permission is a failed delegation.
"""
from __future__ import annotations

import asyncio

import pytest

from xu_brain.features.presets import AgentNode
from xu_brain.features.tools import agents as A


# --- 1. memory scoping ------------------------------------------------------

def _mem_scope(node: AgentNode | None) -> list[str] | None:
    """Mirror of the scoping rule in Agent._build_messages."""
    scope = node.memory if node is not None else None
    if node is not None and node.memory is None and node.role != "orchestrator":
        scope = []
    return scope


def test_leaf_agent_does_not_inherit_global_memory():
    leaf = AgentNode(id="p1", name="coder", role="agent")
    assert _mem_scope(leaf) == []


def test_orchestrator_still_inherits_global_memory():
    root = AgentNode(id="p0", name="dev-main", role="orchestrator")
    assert _mem_scope(root) is None


def test_explicit_leaf_memory_scope_is_respected():
    leaf = AgentNode(id="p2", name="scout", role="agent", memory=["m20043c"])
    assert _mem_scope(leaf) == ["m20043c"]


def test_no_node_means_global_memory():
    assert _mem_scope(None) is None


# --- 2. approval-stall detection -------------------------------------------

@pytest.mark.parametrize("text", [
    "Plan: inspect the banner, edit the copy, run the build. May I proceed?",
    "I understand the requested fix. Shall I proceed?",
    "Rencana sudah siap. Boleh saya lanjut?",
    "Ready to start. Want me to go ahead?",
])
def test_detects_approval_stall(text):
    assert A._is_approval_stall(text)


@pytest.mark.parametrize("text", [
    "Updated the banner copy in web/src/components/ui.jsx. Build passed.",
    "",
    "Found it at ui.jsx:138. Is the copy authoritative? Yes — DONATE_MSG is inlined.",
    "Verdict: approve. No must-fix items.",
])
def test_real_results_are_not_stalls(text):
    assert not A._is_approval_stall(text)


# --- 3. subagent_message on a delegate child -------------------------------

class _FakeOrchestrator:
    def __init__(self, sub=None):
        self._sub = sub

    def get_subagent(self, sub_id):  # noqa: ANN001
        return self._sub

    def owned_subagent(self, sub_id, parent_session):  # noqa: ANN001
        return self._sub  # test double: ignore the ownership boundary

class _Sub:
    def __init__(self, status):
        self.status = status


def _agent(orchestrator):
    from xu_brain.features.agent.loop import Agent
    obj = Agent.__new__(Agent)  # bypass __init__: only this method is exercised
    obj.orchestrator = orchestrator
    return obj


def test_message_to_unregistered_delegate_child_raises():
    agent = _agent(_FakeOrchestrator(None))
    with pytest.raises(LookupError) as exc:
        asyncio.run(agent.subagent_message("coder", "proceed"))
    assert "single-turn" in str(exc.value)


def test_message_to_finished_subagent_raises():
    agent = _agent(_FakeOrchestrator(_Sub("done")))
    with pytest.raises(RuntimeError, match="is done, not running"):
        asyncio.run(agent.subagent_message("s123", "proceed"))


def test_message_to_running_subagent_reports_unsupported():
    agent = _agent(_FakeOrchestrator(_Sub("running")))
    with pytest.raises(RuntimeError, match="single-turn"):
        asyncio.run(agent.subagent_message("s123", "proceed"))
