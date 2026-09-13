"""Tests for parallel tool execution within an agent turn."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from xu_brain.core.config import Config
from xu_brain.core.governance import ApprovalLevel, ApprovalManager
from xu_brain.features.agent.loop import Agent
from xu_brain.features.agent.provider import StopReason, StreamEvent
from xu_brain.features.memory import MemoryStore
from xu_brain.features.session import SessionStore
from xu_brain.features.skills import SkillsEngine
from xu_brain.features.tools.base import Tool, ToolContext, ToolResult
from xu_brain.features.tools.registry import ToolRegistry
from xu_brain.plugins import PluginBus


class SleepyReadTool(Tool):
    name = "sleepy_read"
    toolset = "sleepy"
    description = "Slow read tool to measure concurrency"
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "delay": {"type": "number"},
        },
    }
    output_schema = None

    def __init__(self, tracker: dict[str, int]) -> None:
        self.tracker = tracker

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        self.tracker["active"] += 1
        self.tracker["peak"] = max(self.tracker["peak"], self.tracker["active"])
        delay = float(args.get("delay", 0.05))
        await asyncio.sleep(delay)
        self.tracker["active"] -= 1
        return ToolResult.ok(f"read:{args.get('path')}")


class OrderedWriteTool(Tool):
    name = "ordered_write"
    toolset = "sleepy"
    description = "Write tool that records execution order"
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
    }
    output_schema = None

    def __init__(self, executed: list[str]) -> None:
        self.executed = executed

    async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
        self.executed.append(f"write:{args.get('path')}")
        return ToolResult.ok(f"wrote:{args.get('path')}")


@pytest.mark.asyncio
async def test_concurrent_reads_overlap_in_agent_loop(tmp_path: Path):
    store = SessionStore(tmp_path)
    config = Config(tmp_path)
    approvals = ApprovalManager(config)
    registry = ToolRegistry(approvals)

    tracker = {"active": 0, "peak": 0}
    tool = SleepyReadTool(tracker)
    registry.register(tool)

    agent = Agent(
        store,
        tmp_path,
        None,
        registry,
        approvals,
        MemoryStore(tmp_path),
        SkillsEngine(tmp_path),
        PluginBus(tmp_path),
        config,
    )

    async def noop(*_a, **_k):
        return None

    async def passthrough(v):
        return v

    async def after_tool(_n, r):
        return r

    agent.flat_plugins = SimpleNamespace(
        on_start=noop,
        on_message_out=passthrough,
        before_llm=passthrough,
        after_tool=after_tool,
    )

    session = store.create(cwd=str(tmp_path))

    calls = {"n": 0}

    async def stream(
        provider, model, messages, tools,
        max_tokens=None, reasoning_effort=None, signal=None,
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            yield StreamEvent(tool_call=SimpleNamespace(
                id="c1", name="sleepy_read", arguments='{"path":"1.txt","delay":0.05}',
            ))
            yield StreamEvent(tool_call=SimpleNamespace(
                id="c2", name="sleepy_read", arguments='{"path":"2.txt","delay":0.05}',
            ))
            yield StreamEvent(tool_call=SimpleNamespace(
                id="c3", name="sleepy_read", arguments='{"path":"3.txt","delay":0.05}',
            ))
            yield StreamEvent(stop_reason=StopReason.TOOL)
        else:
            yield StreamEvent(delta="Finished reading all 3 files")

    agent.providers = SimpleNamespace(resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)
    agent.config.set_session_model(session.id, "test-model")

    stop = asyncio.Event()
    res = await agent._loop(session, "t1", stop)

    assert res is not None
    assert "Finished reading all 3 files" in res.text
    # 3 reads ran concurrently and peaked together at 3!
    assert tracker["peak"] == 3

    # Check tool messages returned in exact input order
    history = res.history
    tool_msgs = [m for m in history if m.get("role") == "tool"]
    assert len(tool_msgs) == 3
    assert tool_msgs[0]["content"] == "read:1.txt"
    assert tool_msgs[1]["content"] == "read:2.txt"
    assert tool_msgs[2]["content"] == "read:3.txt"


@pytest.mark.asyncio
async def test_conflicting_calls_serialize_across_waves_in_agent_loop(tmp_path: Path):
    store = SessionStore(tmp_path)
    config = Config(tmp_path)
    approvals = ApprovalManager(config)
    registry = ToolRegistry(approvals)

    executed: list[str] = []
    tracker = {"active": 0, "peak": 0}
    registry.register(SleepyReadTool(tracker))
    registry.register(OrderedWriteTool(executed))

    agent = Agent(
        store,
        tmp_path,
        None,
        registry,
        approvals,
        MemoryStore(tmp_path),
        SkillsEngine(tmp_path),
        PluginBus(tmp_path),
        config,
    )

    async def noop(*_a, **_k):
        return None

    async def passthrough(v):
        return v

    async def after_tool(_n, r):
        return r

    agent.flat_plugins = SimpleNamespace(
        on_start=noop,
        on_message_out=passthrough,
        before_llm=passthrough,
        after_tool=after_tool,
    )

    session = store.create(cwd=str(tmp_path))

    calls = {"n": 0}

    async def stream(
        provider, model, messages, tools,
        max_tokens=None, reasoning_effort=None, signal=None,
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            # write(a) -> read(a): RAW dependency forces Wave 1 write(a), Wave 2 read(a)
            yield StreamEvent(tool_call=SimpleNamespace(
                id="c1", name="ordered_write", arguments='{"path":"a.txt"}',
            ))
            yield StreamEvent(tool_call=SimpleNamespace(
                id="c2", name="sleepy_read", arguments='{"path":"a.txt","delay":0.01}',
            ))
            yield StreamEvent(stop_reason=StopReason.TOOL)
        else:
            yield StreamEvent(delta="Done")

    agent.providers = SimpleNamespace(resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)
    agent.config.set_session_model(session.id, "test-model")

    stop = asyncio.Event()
    res = await agent._loop(session, "t1", stop)

    assert res is not None
    assert "Done" in res.text
    # write must execute before read
    assert executed == ["write:a.txt"]
    tool_msgs = [m for m in res.history if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assert tool_msgs[0]["content"] == "wrote:a.txt"
    assert tool_msgs[1]["content"] == "read:a.txt"


@pytest.mark.asyncio
async def test_concurrent_mixed_delegate_and_reads_overlap(tmp_path: Path):
    store = SessionStore(tmp_path)
    config = Config(tmp_path)
    approvals = ApprovalManager(config)
    registry = ToolRegistry(approvals)

    tracker = {"active": 0, "peak": 0}
    registry.register(SleepyReadTool(tracker))

    class MockDelegateTool(Tool):
        name = "delegate"
        toolset = "agents"
        description = "Mock delegate"
        approval = ApprovalLevel.NEVER
        schema = {"type": "object", "properties": {"prompt": {"type": "string"}}}
        output_schema = None

        async def run(self, args: dict, ctx: ToolContext) -> ToolResult:
            tracker["active"] += 1
            tracker["peak"] = max(tracker["peak"], tracker["active"])
            await asyncio.sleep(0.05)
            tracker["active"] -= 1
            return ToolResult.ok(f"subagent:{args.get('prompt')}")

    registry.register(MockDelegateTool())

    agent = Agent(
        store,
        tmp_path,
        None,
        registry,
        approvals,
        MemoryStore(tmp_path),
        SkillsEngine(tmp_path),
        PluginBus(tmp_path),
        config,
    )

    async def noop(*_a, **_k):
        return None

    async def passthrough(v):
        return v

    async def after_tool(_n, r):
        return r

    agent.flat_plugins = SimpleNamespace(
        on_start=noop,
        on_message_out=passthrough,
        before_llm=passthrough,
        after_tool=after_tool,
    )

    session = store.create(cwd=str(tmp_path))
    calls = {"n": 0}

    async def stream(
        provider, model, messages, tools,
        max_tokens=None, reasoning_effort=None, signal=None,
    ):
        calls["n"] += 1
        if calls["n"] == 1:
            # 2 delegates + 2 reads in the SAME round: all 4 must run concurrently!
            yield StreamEvent(tool_call=SimpleNamespace(
                id="c1", name="delegate", arguments='{"prompt":"task1"}',
            ))
            yield StreamEvent(tool_call=SimpleNamespace(
                id="c2", name="delegate", arguments='{"prompt":"task2"}',
            ))
            yield StreamEvent(tool_call=SimpleNamespace(
                id="c3", name="sleepy_read", arguments='{"path":"1.txt","delay":0.05}',
            ))
            yield StreamEvent(tool_call=SimpleNamespace(
                id="c4", name="sleepy_read", arguments='{"path":"2.txt","delay":0.05}',
            ))
            yield StreamEvent(stop_reason=StopReason.TOOL)
        else:
            yield StreamEvent(delta="Mixed batch done")

    agent.providers = SimpleNamespace(resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)
    agent.config.set_session_model(session.id, "test-model")

    stop = asyncio.Event()
    res = await agent._loop(session, "t1", stop)

    assert res is not None
    assert "Mixed batch done" in res.text
    # Proves all 4 ran concurrently and peaked together at 4!
    assert tracker["peak"] == 4
    tool_msgs = [m for m in res.history if m.get("role") == "tool"]
    assert len(tool_msgs) == 4
    assert tool_msgs[0]["content"] == "subagent:task1"
    assert tool_msgs[1]["content"] == "subagent:task2"
    assert tool_msgs[2]["content"] == "read:1.txt"
    assert tool_msgs[3]["content"] == "read:2.txt"
