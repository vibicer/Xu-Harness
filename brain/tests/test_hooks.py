import asyncio

from xu_brain.core.bus import HookBus
from xu_brain.features.tools.registry import ToolRegistry
from types import SimpleNamespace
from xu_brain.features.tools.base import ToolResult


def test_hook_dispose_and_waterfall_short_circuit():
    hooks = HookBus()
    seen = []
    first = hooks.on("tool.pre_execute", lambda p: seen.append("first") or {**p, "handled": True, "reason": "blocked"})
    hooks.on("tool.pre_execute", lambda p: seen.append("second") or p)
    result = asyncio.run(hooks.waterfall("tool.pre_execute", {"tool": "x"}))
    assert result["handled"] is True
    assert seen == ["first"]
    first.dispose()
    result = asyncio.run(hooks.waterfall("tool.pre_execute", {"tool": "x"}))
    assert result == {"tool": "x"}


def test_registry_pre_hook_denies_without_running_tool():
    hooks = HookBus()
    registry = ToolRegistry(None, hooks=hooks)
    called = []

    class Demo:
        name = "demo"
        toolset = "test"
        schema = {"type": "object"}
        approval = None
        description = "demo"
        async def run(self, args, ctx):
            called.append(True)
            return ToolResult.ok("ran")

    registry.register(Demo())
    hooks.on("tool.pre_execute", lambda p: {**p, "handled": True, "reason": "denied"})
    result = asyncio.run(registry.run("demo", {}, SimpleNamespace(session_id="s"), emit=_noop))
    assert result.output == "denied"
    assert not called


async def _noop(*args, **kwargs):
    return None


def test_tool_registration_returns_disposer():
    registry = ToolRegistry(None)
    class Demo:
        name = "demo_disposable"
        toolset = "test"
        schema = {"type": "object"}
        approval = None
        description = "demo"
        async def run(self, args, ctx):
            return ToolResult.ok("ran")
    dispose = registry.register(Demo())
    assert registry.get("demo_disposable") is not None
    dispose()
    dispose()
    assert registry.get("demo_disposable") is None
