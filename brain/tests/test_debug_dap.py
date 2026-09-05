"""debug tool — the DAP client, end to end against a real debugpy adapter.

DAP is not JSON-RPC: replies carry ``request_seq``, not ``id``, and the adapter
withholds the ``launch`` reply until ``configurationDone``. Both of those were
silently broken before, so this exercises the whole loop — launch, breakpoint,
continue, inspect, terminate — rather than mocking the wire.

Run: brain/.venv/bin/python -m pytest tests/test_debug_dap.py -q
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from xu_brain.core.governance import ApprovalManager
from xu_brain.features.tools.base import ToolContext
from xu_brain.features.tools.lsp_debug import debug as debug_tool
from xu_brain.features.tools.registry import ToolRegistry

debugpy = pytest.importorskip("debugpy", reason="debug tool needs debugpy")

PROGRAM = """\
import time


def work(n):
    total = 0
    for i in range(n):
        total += i
        time.sleep(0.05)
    return total


print(work(5))
"""
# 1-indexed line of `total += i` in PROGRAM — the breakpoint target.
BREAK_LINE = 7


def _ctx(tmp_path: Path) -> ToolContext:
    registry = ToolRegistry(ApprovalManager("yolo"), output_cap=10_000)
    return ToolContext(
        data_home=tmp_path,
        session_id="test",
        turn_id="t0",
        cwd=str(tmp_path),
        agent=None,  # type: ignore[arg-type]
        events=None,  # type: ignore[arg-type]
        approvals=registry.approvals,
        providers=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        skills=None,  # type: ignore[arg-type]
        flat_plugins=None,  # type: ignore[arg-type]
        config={},
    )


async def _run(action: str, ctx: ToolContext, **kw):
    return await debug_tool.run({"action": action, **kw}, ctx)


@pytest.fixture
def program(tmp_path: Path) -> Path:
    path = tmp_path / "target.py"
    path.write_text(PROGRAM, "utf-8")
    return path


def test_session_reaches_a_breakpoint_and_reads_a_local(program, tmp_path):
    """The load-bearing case: a stopped session yields a frame and a local."""
    ctx = _ctx(tmp_path)

    async def scenario():
        try:
            # Buffered before launch — no session exists yet.
            bp = await _run("set_breakpoint", ctx, file=str(program), line=BREAK_LINE)
            assert "buffered" in bp.output, bp.output

            launched = await _run("launch", ctx, program=str(program))
            assert not launched.error, launched.output
            assert "stopped at entry" in launched.output, launched.output

            hit = await _run("continue", ctx)
            assert "stopped: breakpoint" in hit.output, hit.output

            stack = await _run("stack_trace", ctx)
            assert not stack.error, stack.output
            assert f"work at {program}:{BREAK_LINE}" in stack.output, stack.output

            # `i` is a local of `work`; without a frameId the adapter would
            # evaluate in global scope and this would fail.
            value = await _run("evaluate", ctx, expression="i")
            assert not value.error, value.output
            assert value.output.strip() == "0", value.output
        finally:
            await _run("terminate", ctx)

    asyncio.run(asyncio.wait_for(scenario(), timeout=90))


def test_actions_before_launch_do_not_raise(tmp_path):
    ctx = _ctx(tmp_path)

    async def scenario():
        for action in ("continue", "stack_trace", "evaluate", "terminate"):
            r = await _run(action, ctx)
            assert r.error and "launch first" in r.output, (action, r.output)
        missing = await _run("launch", ctx, program="nope.py")
        assert missing.error and "not found" in missing.output
        bad = await _run("wat", ctx)
        assert bad.error and "unsupported" in bad.output

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))


def test_terminate_leaves_no_session(program, tmp_path):
    ctx = _ctx(tmp_path)

    async def scenario():
        assert not (await _run("launch", ctx, program=str(program))).error
        assert (await _run("terminate", ctx)).output == "terminated"
        # Buffered breakpoints are cleared with the session.
        again = await _run("stack_trace", ctx)
        assert again.error and "launch first" in again.output

    asyncio.run(asyncio.wait_for(scenario(), timeout=90))
