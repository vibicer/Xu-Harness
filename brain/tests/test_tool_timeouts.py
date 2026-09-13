"""Per-call timeout bounds for `bash` and `eval`.

An unbounded timeout holds the shell lock (and, for `bash`, the shared
concurrency slot) for as long as the model asked; a negative one expires
instantly, which discards the persistent shell and silently loses the model's
`cd`/exports. Both are clamped at the tool boundary.

Run: brain/.venv/bin/python -m pytest tests/test_tool_timeouts.py -q
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from xu_brain.core.governance import ApprovalManager
from xu_brain.features.tools import eval_tool as E
from xu_brain.features.tools import terminal as T
from xu_brain.features.tools.base import ToolContext
from xu_brain.features.tools.registry import ToolRegistry


def _ctx(tmp_path: Path, session_id: str = "timeout-test") -> ToolContext:
    registry = ToolRegistry(ApprovalManager("yolo"), output_cap=10_000)
    return ToolContext(
        data_home=tmp_path,
        session_id=session_id,
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


# --- the clamp itself ------------------------------------------------------


def test_bash_timeout_is_clamped_into_range():
    assert T._clamp_timeout(-5, 120.0) == T._BASH_TIMEOUT_MIN
    assert T._clamp_timeout(0, 120.0) == T._BASH_TIMEOUT_MIN
    assert T._clamp_timeout(10**9, 120.0) == T._BASH_TIMEOUT_MAX
    assert T._clamp_timeout(30, 120.0) == 30.0
    assert T._clamp_timeout("nonsense", 120.0) == 120.0
    assert T._clamp_timeout(float("nan"), 120.0) == 120.0


def test_eval_timeout_is_clamped_into_range():
    assert E._clamp_timeout(-5, 60.0) == E._EVAL_TIMEOUT_MIN
    assert E._clamp_timeout(0, 60.0) == E._EVAL_TIMEOUT_MIN
    assert E._clamp_timeout(10**9, 60.0) == E._EVAL_TIMEOUT_MAX
    assert E._clamp_timeout(30, 60.0) == 30.0
    assert E._clamp_timeout(None, 60.0) == 60.0


# --- the clamp is what reaches the shell/kernel ----------------------------


def test_an_absurd_timeout_is_bounded_before_it_reaches_the_shell(tmp_path, monkeypatch):
    seen: dict[str, float] = {}

    async def fake_run(self, command, timeout=120.0, *, session_cwd=None):
        seen["timeout"] = timeout
        return 0, "ok", "", 0.0

    monkeypatch.setattr(T.ShellSession, "run", fake_run)

    asyncio.run(T.bash.run({"command": "echo hi", "timeout": 10**9}, _ctx(tmp_path)))

    assert seen["timeout"] == T._BASH_TIMEOUT_MAX


def test_an_absurd_timeout_is_bounded_before_it_reaches_the_kernel(tmp_path, monkeypatch):
    seen: dict[str, float] = {}

    async def fake_run(self, code, timeout=60.0):
        seen["timeout"] = timeout
        return {"out": "", "err": "", "result": None, "error": None}

    monkeypatch.setattr(E.EvalKernel, "run", fake_run)
    ctx = _ctx(tmp_path)

    asyncio.run(E.eval.run({"code": "1", "timeout": 10**9}, ctx))
    assert seen["timeout"] == E._EVAL_TIMEOUT_MAX

    asyncio.run(E.eval.run({"code": "1", "timeout": -3}, ctx))
    assert seen["timeout"] == E._EVAL_TIMEOUT_MIN


# --- behaviour: a negative timeout must not cost the persistent shell ------


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell behaviour")
def test_a_negative_timeout_does_not_discard_the_persistent_shell(tmp_path):
    """Before the clamp a negative timeout made `wait_for` expire at once, so
    `_discard()` killed the shell and the next call lost the model's `cd`."""
    ctx = _ctx(tmp_path)
    T._SHELLS.pop("timeout-test", None)
    (tmp_path / "sub").mkdir()

    async def scenario():
        first = await T.bash.run({"command": "cd sub && echo here", "timeout": -5}, ctx)
        assert "exit 124" not in first.output, first.output
        assert "here" in first.output, first.output
        # cwd survived -> the shell was clamped, not discarded
        second = await T.bash.run({"command": 'basename "$PWD"'}, ctx)
        assert "sub" in second.output, second.output
        await T._SHELLS["timeout-test"].close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    T._SHELLS.pop("timeout-test", None)
