"""Approval / ask timeout pause: the agent halts instead of barrelling on.

Run: uv run pytest tests/test_approval_pause.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xu_brain.core.governance import ApprovalLevel, TIMEOUT_REASON
from xu_brain.features.tools.base import ToolContext, ToolResult
from xu_brain.features.tools.registry import ToolRegistry


class _StubApprovals:
    """ApprovalManager stand-in returning a fixed (approved, reason)."""

    def __init__(self, approved: bool, reason: str | None = None) -> None:
        self._approved = approved
        self._reason = reason

    async def request(self, tool_name, args, declared, *, session_id=None):
        return self._approved, self._reason


class _FakeAgent:
    """Records pause_turn calls — the only Agent surface the registry touches."""

    def __init__(self) -> None:
        self.paused: list[tuple[str | None, str | None]] = []

    def pause_turn(self, session_id, message=None) -> None:
        self.paused.append((session_id, message))


class _RiskyTool:
    name = "risky"
    toolset = "test"
    description = "risky tool"
    approval = ApprovalLevel.RISKY
    schema = {"type": "object", "properties": {}}

    async def run(self, args, ctx):
        return ToolResult.ok("ran")


def _ctx(agent) -> ToolContext:
    return ToolContext(
        data_home=Path("."),
        session_id="s1",
        turn_id="t1",
        cwd=".",
        agent=agent,
        events=None,  # type: ignore[arg-type]
        approvals=None,  # type: ignore[arg-type]
        providers=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        skills=None,  # type: ignore[arg-type]
        flat_plugins=None,  # type: ignore[arg-type]
        config={},
    )


async def test_approval_timeout_pauses_turn():
    registry = ToolRegistry(_StubApprovals(False, TIMEOUT_REASON))
    registry.register(_RiskyTool())  # type: ignore[arg-type]
    agent = _FakeAgent()

    result = await registry.run("risky", {}, _ctx(agent), emit=lambda *a, **k: None)

    assert result.error is True
    assert agent.paused == [("s1", None)]


async def test_approval_deny_does_not_pause():
    registry = ToolRegistry(_StubApprovals(False, "denied by user"))
    registry.register(_RiskyTool())  # type: ignore[arg-type]
    agent = _FakeAgent()

    result = await registry.run("risky", {}, _ctx(agent), emit=lambda *a, **k: None)

    assert result.error is True
    assert result.output == "denied: denied by user"
    assert agent.paused == []
