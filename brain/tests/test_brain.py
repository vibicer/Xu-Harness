"""Brain test suite — provider normalization, registry governance, memory, skills.

Run: uv run pytest tests/ -v
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
import time
from unittest import mock
from pathlib import Path

import pytest

# Ensure the brain is importable.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xu_brain.features.agent.provider import StreamEvent, StopReason
from types import SimpleNamespace
from xu_brain.features.agent.provider import (
    Provider,
    ProviderManager,
    _anthropic_from_openai,
    _anthropic_tool,
    _coerce_text,
    _extract_model_ids,
)
from xu_brain.core.governance import ApprovalLevel, ApprovalManager
from xu_brain.core.config import Config
from xu_brain.features.memory import MemoryStore
from xu_brain.features.tools.registry import ToolRegistry
from xu_brain.features.skills import SkillsEngine
from xu_brain.features.tools.base import Tool, ToolContext, ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_data_home() -> Path:
    return Path(tempfile.mkdtemp(prefix="xu-test-"))


@pytest.fixture
def data_home() -> Path:
    return _new_data_home()


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# ---------------------------------------------------------------------------
# Provider normalization
# ---------------------------------------------------------------------------

class TestExtractModelIds:
    def test_openai_format(self) -> None:
        data = {"data": [{"id": "gpt-4"}, {"id": "gpt-3.5-turbo"}]}
        assert _extract_model_ids(data, anthropic=False) == ["gpt-4", "gpt-3.5-turbo"]

    def test_anthropic_format(self) -> None:
        data = {"data": [{"id": "claude-3-opus"}, {"id": "claude-3-sonnet"}]}
        assert _extract_model_ids(data, anthropic=True) == ["claude-3-opus", "claude-3-sonnet"]

    def test_anthropic_wrapped(self) -> None:
        # Anthropic sometimes nests data under "data"
        data = {"data": {"data": [{"id": "claude-3"}]}}
        assert _extract_model_ids(data, anthropic=True) == ["claude-3"]

    def test_string_list(self) -> None:
        assert _extract_model_ids(["model-a", "model-b"], anthropic=False) == ["model-a", "model-b"]

    def test_empty(self) -> None:
        assert _extract_model_ids({}, anthropic=False) == []
        assert _extract_model_ids(None, anthropic=False) == []
        assert _extract_model_ids([], anthropic=False) == []

    def test_name_field_fallback(self) -> None:
        data = {"data": [{"name": "llama-7b"}]}
        assert _extract_model_ids(data, anthropic=False) == ["llama-7b"]


class TestCoerceText:
    def test_string_passthrough(self) -> None:
        assert _coerce_text("hello") == "hello"

    def test_dict_json(self) -> None:
        result = _coerce_text({"key": "value"})
        assert "key" in result and "value" in result

    def test_number(self) -> None:
        result = _coerce_text(42)
        assert "42" == result


class TestAnthropicFromOpenAI:
    def test_system_stripped(self) -> None:
        msgs = [{"role": "system", "content": "you are xu"}]
        convo, _ = _anthropic_from_openai(msgs)
        assert convo == []

    def test_user_assistant(self) -> None:
        msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        convo, _ = _anthropic_from_openai(msgs)
        assert len(convo) == 2
        assert convo[0]["role"] == "user"
        assert convo[1]["role"] == "assistant"

    def test_tool_result_flushed_to_user(self) -> None:
        msgs = [
            {"role": "user", "content": "run it"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "function": {"name": "bash", "arguments": '{"cmd":"echo"}'}
            }]},
            {"role": "tool", "tool_call_id": "tc1", "content": "output"},
            {"role": "assistant", "content": "done"},
        ]
        convo, _ = _anthropic_from_openai(msgs)
        # Expect: user, assistant(tool_use), user(tool_result), assistant("done")
        assert len(convo) == 4
        assert convo[0]["role"] == "user"
        assert convo[1]["role"] == "assistant"
        # Tool result flushed before the next assistant message
        tool_result_block = convo[2]
        assert tool_result_block["role"] == "user"
        content = tool_result_block["content"]
        assert isinstance(content, list)
        assert content[0]["type"] == "tool_result"
        assert content[0]["tool_use_id"] == "tc1"
        # Final assistant message
        assert convo[3]["role"] == "assistant"

    def test_consecutive_user_merged(self) -> None:
        """Anthropic disallows user→user; the normalizer should not produce that."""
        msgs = [
            {"role": "user", "content": "first"},
            {"role": "user", "content": "second"},
        ]
        convo, _ = _anthropic_from_openai(msgs)
        # Each user message becomes its own block, but no user→user merge bug
        # in the conversion (the merge happens when flushing tool_results).
        # What matters: no crash, both texts present.
        assert all(c["role"] == "user" for c in convo)


class TestMultimodal:
    def test_anthropic_data_image_block(self) -> None:
        content = [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]
        convo, _ = _anthropic_from_openai([{"role": "user", "content": content}])
        blocks = convo[0]["content"]
        assert blocks[0]["type"] == "text" and blocks[0]["text"] == "describe this"
        src = blocks[1]
        assert src["type"] == "image"
        assert src["source"]["type"] == "base64"
        assert src["source"]["media_type"] == "image/png"
        assert src["source"]["data"] == "QUJD"

    def test_anthropic_remote_image_url(self) -> None:
        content = [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}]
        convo, _ = _anthropic_from_openai([{"role": "user", "content": content}])
        src = convo[0]["content"][0]
        assert src["source"]["type"] == "url"
        assert src["source"]["url"] == "https://x/y.png"

    def test_plain_string_content_stays_string(self) -> None:
        convo, _ = _anthropic_from_openai([{"role": "user", "content": "hi"}])
        assert convo[0]["content"] == "hi"


class TestAnthropicTool:
    def test_openai_envelope_translated(self) -> None:
        openai_tool = {
            "type": "function",
            "function": {
                "name": "bash",
                "description": "run a command",
                "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
            },
        }
        result = _anthropic_tool(openai_tool)
        assert result["name"] == "bash"
        assert result["description"] == "run a command"
        assert result["input_schema"]["type"] == "object"
        assert "cmd" in result["input_schema"]["properties"]

    def test_missing_parameters_defaults(self) -> None:
        result = _anthropic_tool({"function": {"name": "test"}})
        assert result["input_schema"] == {"type": "object", "properties": {}}


# ---------------------------------------------------------------------------
# Registry governance
# ---------------------------------------------------------------------------

class _DummyTool:
    """Minimal tool for registry tests."""

    def __init__(self, name="echo", toolset="test", approval=ApprovalLevel.NEVER,
                 fail=False, delay=0.0):
        self.name = name
        self.toolset = toolset
        self.description = f"test tool {name}"
        self.approval = approval
        self.schema = {"type": "object", "properties": {}}
        self._fail = fail
        self._delay = delay

    async def run(self, args, ctx):
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._fail:
            raise RuntimeError("boom")
        return ToolResult.ok(f"echo:{args.get('x', '')}")


@pytest.fixture
def registry():
    mgr = ApprovalManager("yolo")
    reg = ToolRegistry(mgr, output_cap=100)
    return reg

def _make_ctx(registry: ToolRegistry, data_home: Path | None = None) -> ToolContext:
    """Build a minimal ToolContext for governance tests (tools here don't use subsystems)."""
    return ToolContext(
        data_home=data_home or Path("."),
        session_id="test",
        turn_id="t0",
        cwd=".",
        agent=None,  # type: ignore[arg-type]
        events=None,  # type: ignore[arg-type]
        approvals=registry.approvals,
        providers=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        skills=None,  # type: ignore[arg-type]
        flat_plugins=None,  # type: ignore[arg-type]
        config={},
    )

class TestToolRegistry:
    def test_register_and_get(self, registry):
        t = _DummyTool()
        registry.register(t)
        assert registry.get("echo") is t
        assert "echo" in registry.names()

    def test_register_missing_name(self, registry):
        class Bad:
            toolset = "x"
            description = ""
            approval = ApprovalLevel.NEVER
            schema = {}
            async def run(self, args, ctx): ...
        with pytest.raises(ValueError):
            registry.register(Bad())

    def test_enable_disable(self, registry):
        registry.register(_DummyTool())
        assert registry.is_enabled("test") is True
        registry.enable("test", False)
        assert registry.is_enabled("test") is False

    def test_disabled_toolset_returns_error(self, registry):
        registry.register(_DummyTool())
        registry.enable("test", False)
        ctx = _make_ctx(registry)
        result = asyncio.run(registry.run("echo", {}, ctx, emit=_noop_emit))
        assert result.error is True
        assert "disabled" in result.output

    def test_unknown_tool(self, registry):
        ctx = _make_ctx(registry)
        result = asyncio.run(registry.run("nonexistent", {}, ctx, emit=_noop_emit))
        assert result.error is True
        assert "unknown tool" in result.output

    def test_schemas_for_model(self, registry):
        registry.register(_DummyTool())
        schemas = registry.schemas_for_model()
        assert len(schemas) == 1
        assert schemas[0]["type"] == "function"
        assert schemas[0]["function"]["name"] == "echo"

    def test_schemas_excludes_disabled(self, registry):
        registry.register(_DummyTool())
        registry.enable("test", False)
        assert registry.schemas_for_model() == []

    def test_toolsets_shape(self, registry):
        registry.register(_DummyTool(name="a"))
        registry.register(_DummyTool(name="b"))
        ts = registry.toolsets()
        assert len(ts) == 1
        assert ts[0]["toolset"] == "test"
        assert len(ts[0]["tools"]) == 2

    def test_output_cap(self, registry):
        big = _DummyTool()
        big.schema = {"type": "object", "properties": {}}

        async def run_big(args, ctx):
            return ToolResult.ok("x" * 500)
        big.run = run_big
        registry.register(big)
        ctx = _make_ctx(registry)
        result = asyncio.run(registry.run("echo", {}, ctx, emit=_noop_emit))
        assert len(result.output) <= 100
        assert "truncated" in result.output

    def test_breaker_trips_after_threshold(self, registry):
        failing = _DummyTool(fail=True)
        registry.register(failing)
        ctx = _make_ctx(registry)
        # Three failures should trip the breaker
        for _ in range(3):
            asyncio.run(registry.run("echo", {}, ctx, emit=_noop_emit))
        # Next call should be short-circuited
        result = asyncio.run(registry.run("echo", {}, ctx, emit=_noop_emit))
        assert result.error is True
        assert "circuit breaker" in result.output

    def test_reset_breakers(self, registry):
        failing = _DummyTool(fail=True)
        registry.register(failing)
        ctx = _make_ctx(registry)
        for _ in range(3):
            asyncio.run(registry.run("echo", {}, ctx, emit=_noop_emit))
        registry.reset_breakers()
        # After reset, the tool can try again (and fail, but not short-circuited)
        result = asyncio.run(registry.run("echo", {}, ctx, emit=_noop_emit))
        assert "circuit breaker" not in result.output

    def test_approval_gate_risky_in_manual(self, registry):
        """Manual mode: RISKY tools prompt; denying returns an error."""
        registry.approvals.set_mode("manual")
        risky = _DummyTool(approval=ApprovalLevel.RISKY)
        registry.register(risky)
        ctx = _make_ctx(registry)

        async def run_and_deny():
            task = asyncio.create_task(registry.run("echo", {}, ctx, emit=_noop_emit))
            # Wait for the approval to be pending, then deny it.
            await asyncio.sleep(0.05)
            for rid in list(registry.approvals._pending):
                registry.approvals.resolve(rid, False)
            return await task

        result = asyncio.run(run_and_deny())
        assert result.error is True
        assert "denied" in result.output or "not approved" in result.output

    def test_approval_auto_in_yolo(self, registry):
        """Yolo mode: RISKY tools auto-approve."""
        registry.approvals.set_mode("yolo")
        risky = _DummyTool(approval=ApprovalLevel.RISKY)
        registry.register(risky)
        ctx = _make_ctx(registry)
        result = asyncio.run(registry.run("echo", {"x": "hi"}, ctx, emit=_noop_emit))
        assert result.error is False
        assert "echo:hi" in result.output

    def test_always_approve_remembers_for_session(self, registry):
        """`resolve(remember=True)` stops prompting that tool for the session."""
        registry.approvals.set_mode("manual")
        registry.register(_DummyTool(approval=ApprovalLevel.RISKY))
        ctx = _make_ctx(registry)  # session_id="test"

        async def approve_once_then_rerun():
            task = asyncio.create_task(registry.run("echo", {"x": "a"}, ctx, emit=_noop_emit))
            await asyncio.sleep(0.05)
            pending = list(registry.approvals._pending)
            assert pending, "first call must prompt"
            registry.approvals.resolve(pending[0], True, remember=True)
            first = await task
            # Second call: no card may be created at all.
            second = await registry.run("echo", {"x": "b"}, ctx, emit=_noop_emit)
            return first, second, registry.approvals._pending

        first, second, still_pending = asyncio.run(approve_once_then_rerun())
        assert first.error is False and "echo:a" in first.output
        assert second.error is False and "echo:b" in second.output
        assert not still_pending

    def test_always_approve_cannot_clear_the_frozen_gate(self, registry):
        """An ALWAYS-level tool re-prompts even after `remember=True`."""
        registry.approvals.set_mode("yolo")  # yolo must not help either
        registry.register(_DummyTool(approval=ApprovalLevel.ALWAYS))
        ctx = _make_ctx(registry)

        async def approve_always_then_rerun():
            task = asyncio.create_task(registry.run("echo", {"x": "a"}, ctx, emit=_noop_emit))
            await asyncio.sleep(0.05)
            pending = list(registry.approvals._pending)
            assert pending, "ALWAYS must prompt"
            registry.approvals.resolve(pending[0], True, remember=True)
            await task
            # Second call must prompt again despite the remembered allow.
            task2 = asyncio.create_task(registry.run("echo", {"x": "b"}, ctx, emit=_noop_emit))
            await asyncio.sleep(0.05)
            reprompted = list(registry.approvals._pending)
            for rid in reprompted:
                registry.approvals.resolve(rid, False)
            await task2
            return reprompted

        assert asyncio.run(approve_always_then_rerun()), "frozen gate was bypassed"

    def test_custom_mode_auto_downgrades_risky(self, registry):
        """A custom mode's ``auto`` list makes matching risky tools pass silently."""
        registry.approvals.set_modes({"semi": {"auto": ["echo"], "prompt": []}})
        registry.approvals.set_mode("semi")
        risky = _DummyTool(approval=ApprovalLevel.RISKY)
        registry.register(risky)
        ctx = _make_ctx(registry)
        result = asyncio.run(registry.run("echo", {"x": "hi"}, ctx, emit=_noop_emit))
        assert result.error is False
        assert "echo:hi" in result.output

    def test_custom_mode_prefix_wildcard(self):
        """``auto: ["ec*"]`` matches ``echo`` via trailing-* prefix."""
        m = ApprovalManager("manual")
        m.set_modes({"semi": {"auto": ["ec*"], "prompt": []}})
        m.set_mode("semi")
        assert m.level_for("echo", {}, ApprovalLevel.RISKY) is ApprovalLevel.NEVER

    def test_custom_mode_prompt_escalates(self):
        """A custom mode's ``prompt`` list escalates a risky tool to ALWAYS."""
        m = ApprovalManager("manual")
        m.set_modes({"semi": {"auto": [], "prompt": ["bash"]}})
        m.set_mode("semi")
        assert m.level_for("bash", {}, ApprovalLevel.RISKY) is ApprovalLevel.ALWAYS

    def test_custom_mode_cannot_bypass_always_gate(self):
        """The frozen ALWAYS-gate wins even if a custom mode lists the tool auto."""
        m = ApprovalManager("manual")
        m.set_modes({"reckless": {"auto": ["bash"], "prompt": []}})
        m.set_mode("reckless")
        # destructive shell is always gated regardless of any custom mode
        assert m.level_for("bash", {"command": "mkfs /dev/sda"}, ApprovalLevel.RISKY) is ApprovalLevel.ALWAYS

    def test_unknown_mode_rejected(self):
        m = ApprovalManager("manual")
        with pytest.raises(ValueError):
            m.set_mode("nope")

    def test_removing_active_custom_mode_falls_back_to_manual(self):
        m = ApprovalManager("manual")
        m.set_modes({"semi": {"auto": ["echo"], "prompt": []}})
        m.set_mode("semi")
        m.set_modes({})  # definition removed
        assert m.mode == "manual"

    def test_policy_cannot_open_the_frozen_gate(self):
        """A policy plugin refines criteria; it is not an override switch. The
        module contract says a policy "can never remove the gate"."""

        class _Rogue:
            def is_always_gated(self, tool_name, args):
                return False

            def level_for(self, tool_name, args, declared):
                return ApprovalLevel.NEVER  # claims everything is safe

        m = ApprovalManager("manual")
        m.add_policy(_Rogue())
        assert m.level_for("bash", {"command": "mkfs /dev/sda"}, ApprovalLevel.RISKY) is ApprovalLevel.ALWAYS
        # ...but it still gets to re-level what the frozen gate does not claim.
        assert m.level_for("bash", {"command": "ls"}, ApprovalLevel.RISKY) is ApprovalLevel.NEVER

    def test_policy_may_escalate_into_the_gate(self):
        class _Strict:
            def is_always_gated(self, tool_name, args):
                return tool_name == "write"

            def level_for(self, tool_name, args, declared):
                return None

        m = ApprovalManager("yolo")
        m.add_policy(_Strict())
        assert m.level_for("write", {}, ApprovalLevel.RISKY) is ApprovalLevel.ALWAYS
        assert m.level_for("read", {}, ApprovalLevel.NEVER) is ApprovalLevel.NEVER

    def test_broken_policy_fails_closed(self):
        """A policy that raises must not become a hole; it gates instead."""

        class _Broken:
            def is_always_gated(self, tool_name, args):
                raise RuntimeError("bug in plugin")

            def level_for(self, tool_name, args, declared):
                raise RuntimeError("bug in plugin")

        m = ApprovalManager("yolo")
        m.add_policy(_Broken())
        assert m.level_for("bash", {"command": "ls"}, ApprovalLevel.RISKY) is ApprovalLevel.ALWAYS
    def test_approval_event_includes_session_and_structured_payload(self):
        events = []

        async def emit(event, **payload):
            events.append((event, payload))

        manager = ApprovalManager("manual", event_emitter=emit)

        async def request_and_resolve():
            task = asyncio.create_task(
                manager.request(
                    "file_write",
                    {"path": "notes.txt", "content": "hello"},
                    ApprovalLevel.RISKY,
                    "writing a note",
                    session_id="session-42",
                )
            )
            await asyncio.sleep(0)
            request_id = next(iter(manager._pending))
            assert manager.resolve(request_id, True)
            return await task

        assert asyncio.run(request_and_resolve()) == (True, None)
        assert len(events) == 1
        event, payload = events[0]
        assert event == "turn.approval"
        assert payload["request_id"].startswith("a")
        assert payload["tool"] == "file_write"
        assert payload["args"] == {"path": "notes.txt", "content": "hello"}
        assert payload["reason"] == "writing a note"
        assert payload["session_id"] == "session-42"

    def test_approval_request_without_session_remains_compatible(self):
        events = []

        async def emit(event, **payload):
            events.append((event, payload))

        manager = ApprovalManager("manual", event_emitter=emit)

        async def request_and_resolve():
            task = asyncio.create_task(manager.request("bash", {"command": "echo hi"}, ApprovalLevel.RISKY))
            await asyncio.sleep(0)
            manager.resolve(next(iter(manager._pending)), False)
            return await task

        assert asyncio.run(request_and_resolve()) == (False, "denied by user")
        assert "session_id" not in events[0][1]



    def test_registry_approval_event_uses_context_session(self):
        events = []

        async def emit(event, **payload):
            events.append((event, payload))

        manager = ApprovalManager("manual", event_emitter=emit)
        reg = ToolRegistry(manager)
        reg.register(_DummyTool(approval=ApprovalLevel.RISKY))
        ctx = _make_ctx(reg)

        async def run_and_deny():
            task = asyncio.create_task(reg.run("echo", {"x": "hi"}, ctx, emit=_noop_emit))
            await asyncio.sleep(0)
            manager.resolve(next(iter(manager._pending)), False)
            return await task

        result = asyncio.run(run_and_deny())
        assert result.error is True
        assert events[0][0] == "turn.approval"
        assert events[0][1]["session_id"] == "test"
        assert events[0][1]["args"] == {"x": "hi"}
        assert events[0][1]["reason"]


class TestSummarizeArgs:
    """Chip args are a short one-line summary, never a raw payload — they are
    persisted on the step timeline and replayed into later prompts."""

    def test_headline_keys_get_a_longer_leash(self):
        from xu_brain.features.tools.registry import summarize_args

        long_path = "/home/u/projects/app/web/src/lib/components/DeeplyNested.svelte"
        out = summarize_args({"path": long_path})
        assert out == f"path={long_path}", "a path must survive intact"

    def test_bulk_values_are_truncated(self):
        from xu_brain.features.tools.registry import summarize_args

        out = summarize_args({"path": "/tmp/a.txt", "content": "x" * 5000})
        assert "x" * 5000 not in out
        assert len(out) <= 320
        assert out.startswith("path=/tmp/a.txt ")

    def test_edit_patch_becomes_its_target_path(self):
        from xu_brain.features.tools.registry import summarize_args

        patch = "[/home/u/app/web/src/lib/neobrut.css#E1199A]\nPUT 1.=2:\n+.a{color:red}\n"
        assert summarize_args({"patch": patch}) == "path=/home/u/app/web/src/lib/neobrut.css"

    def test_headerless_patch_falls_back_to_truncation(self):
        from xu_brain.features.tools.registry import summarize_args

        out = summarize_args({"patch": "y" * 500})
        assert out.startswith("patch=yyy")
        assert len(out) <= 320

async def _noop_bus(*args, **kw):
    return None


async def _noop_emit(event, **kw):
    pass


# ---------------------------------------------------------------------------
# Memory store
# ---------------------------------------------------------------------------

class TestMemoryStore:
    def test_empty_on_missing_file(self, data_home):
        store = MemoryStore(data_home)
        assert store.list() == []

    def test_append_and_list(self, data_home):
        store = MemoryStore(data_home)
        eid = store.append("first memory")
        assert eid.startswith("m")
        entries = store.list()
        assert len(entries) == 1
        assert entries[0]["text"] == "first memory"
        assert entries[0]["badge"] == "from-session"

    def test_update_marks_user_edited(self, data_home):
        store = MemoryStore(data_home)
        eid = store.append("original")
        assert store.update(eid, "edited") is True
        entries = store.list()
        assert entries[0]["text"] == "edited"
        assert entries[0]["badge"] == "user-edited"

    def test_update_missing_returns_false(self, data_home):
        store = MemoryStore(data_home)
        assert store.update("nonexistent", "x") is False

    def test_delete(self, data_home):
        store = MemoryStore(data_home)
        eid = store.append("delete me")
        assert store.delete(eid) is True
        assert store.list() == []
        assert store.delete(eid) is False

    def test_get(self, data_home):
        store = MemoryStore(data_home)
        eid = store.append("find me")
        got = store.get(eid)
        assert got is not None
        assert got["text"] == "find me"
        assert store.get("nonexistent") is None


class TestDataModuleRpc:
    """Data panel write surface: memory.add / memory.delete /
    persona.set_active (contract/methods.md)."""

    def _call(self, app, method, params=None):
        return asyncio.run(app.dispatch(method, params or {}))

    def test_prune_keep_trims_and_persists(self, data_home):
        """Session cap is count-based and live: lowering prune_keep trims the
        oldest right away and the value persists via config.get."""
        from xu_brain.core.runtime import build_app
        from xu_brain.core.contract import RpcError
        app = build_app(data_home)
        for _ in range(5):
            self._call(app, "session.create", {})
        assert len(self._call(app, "session.list")["sessions"]) == 5
        assert self._call(app, "config.get")["prune_keep"] == 30  # default cap
        # lower the cap -> immediately trimmed to the newest 3
        self._call(app, "config.set", {"key": "prune_keep", "value": 3})
        listing = self._call(app, "session.list")["sessions"]
        assert len(listing) == 3
        assert self._call(app, "config.get")["prune_keep"] == 3
        # invalid values are rejected, cap unchanged
        with pytest.raises(RpcError):
            self._call(app, "config.set", {"key": "prune_keep", "value": 0})
        with pytest.raises(RpcError):
            self._call(app, "config.set", {"key": "prune_keep", "value": "oops"})
        assert len(self._call(app, "session.list")["sessions"]) == 3

    def test_memory_add_and_delete(self, data_home):
        from xu_brain.core.runtime import build_app
        app = build_app(data_home)
        got = self._call(app, "memory.add", {"text": "panel note"})
        assert [e["text"] for e in got["entries"]] == ["panel note"]
        assert got["entries"][0]["badge"] == "user-edited"
        self._call(app, "memory.delete", {"id": got["entries"][0]["id"]})
        assert self._call(app, "memory.list")["entries"] == []

    def test_memory_add_rejects_blank(self, data_home):
        from xu_brain.core.runtime import build_app
        app = build_app(data_home)
        from xu_brain.core.contract import RpcError
        with pytest.raises(RpcError):
            self._call(app, "memory.add", {"text": "   "})

    def test_memory_delete_missing_errors(self, data_home):
        from xu_brain.core.runtime import build_app
        app = build_app(data_home)
        from xu_brain.core.contract import RpcError
        with pytest.raises(RpcError):
            self._call(app, "memory.delete", {"id": "mmissing"})

    def test_persona_set_active_roundtrip(self, data_home):
        from xu_brain.core.runtime import build_app
        app = build_app(data_home)
        self._call(app, "persona.upsert", {"id": "reviewer", "text": "You are strict."})
        assert self._call(app, "persona.set_active", {"id": "reviewer"}) == {"active": "reviewer"}
        assert self._call(app, "persona.list")["active"] == "reviewer"
        assert self._call(app, "persona.set_active", {"id": None}) == {"active": None}
        assert self._call(app, "persona.list")["active"] is None

    def test_persona_set_active_unknown_id_errors(self, data_home):
        from xu_brain.core.runtime import build_app
        app = build_app(data_home)
        from xu_brain.core.contract import RpcError
        with pytest.raises(RpcError):
            self._call(app, "persona.set_active", {"id": "ghost"})

    def test_badge_normalization(self, data_home):
        store = MemoryStore(data_home)
        store.append("x", badge="unknown-badge")
        entries = store.list()
        assert entries[0]["badge"] == "from-session"

    def test_corrupt_file_safe_loads(self, data_home):
        (data_home / "MEMORY.md").write_text("garbage no entry markers\njust text")
        store = MemoryStore(data_home)
        # Should not crash; returns entries (possibly empty or raw text)
        entries = store.list()
        assert isinstance(entries, list)

    def test_round_trip_multiple_entries(self, data_home):
        store = MemoryStore(data_home)
        e1 = store.append("first")
        e2 = store.append("second")
        e3 = store.append("third")
        entries = store.list()
        assert len(entries) == 3
        assert entries[0]["id"] == e1
        assert entries[2]["id"] == e3
        # After reload (fresh store), same data
        store2 = MemoryStore(data_home)
        reloaded = store2.list()
        assert len(reloaded) == 3
        assert [e["text"] for e in reloaded] == ["first", "second", "third"]


# ---------------------------------------------------------------------------
# Skills engine
# ---------------------------------------------------------------------------

class TestSkillsEngine:
    def test_list_empty(self, data_home):
        engine = SkillsEngine(data_home)
        skills = engine.list()
        assert isinstance(skills, list)

    def test_builtin_skills_loaded(self, data_home):
        """The built-in general skill pack ships with the brain."""
        engine = SkillsEngine(data_home)
        # The built-in pack should be discoverable — either loaded or demand
        skills = engine.list()
        skill_names = [s.get("name", s.get("id", "")) for s in skills]
        # If the builtin pack exists, at least one skill is present
        if skills:
            assert any(s.get("state") in ("LOADED", "DEMAND", "OFF") for s in skills)

    def test_seed_picks_up_new_skill_in_existing_pack(self, data_home):
        """A skill added to a shipped pack source must reach the skills dir."""
        import shutil

        from xu_brain.features.skills import BUILTIN_PACK_DIR

        # First boot seeds the whole pack (simulates any install).
        SkillsEngine(data_home)
        installed_pack = data_home / "skills" / BUILTIN_PACK_DIR.name

        # A later version ships a new skill inside the built-in pack source.
        new_skill = BUILTIN_PACK_DIR / "kb-retrieval"
        new_skill.mkdir(parents=True)
        (new_skill / "SKILL.md").write_text(
            "---\nname: KB Retrieval\ndescription: seed-test skill\n---\nbody\n"
        )
        try:
            # Simulate an old install: the new skill never arrived.
            shutil.rmtree(installed_pack / "kb-retrieval", ignore_errors=True)

            # Restart: seeding must re-copy the missing skill, leave the rest alone.
            engine = SkillsEngine(data_home)
            ids = {s["id"] for s in engine.list()}
            assert "general/kb-retrieval" in ids
            assert "general/code-review" in ids
        finally:
            shutil.rmtree(new_skill, ignore_errors=True)

    def test_dir_of_returns_skill_dir_for_dir_skill(self, data_home):
        """dir_of gives the model the skill's own dir so it can reach references/."""
        from xu_brain.features.skills import BUILTIN_PACK_DIR

        engine = SkillsEngine(data_home)
        d = engine.dir_of("general/code-review")
        assert d is not None
        # The dir must be the skill's own folder (containing its SKILL.md).
        assert (d / "SKILL.md").is_file()
        # Unknown id -> None, never an exception.
        assert engine.dir_of("no/such-skill") is None


# ---------------------------------------------------------------------------
# ProviderManager key access
# ---------------------------------------------------------------------------

class TestProviderManager:
    def test_upsert_and_get(self, data_home):
        mgr = ProviderManager(data_home)
        p = Provider(id="test1", type="openai-compatible", base_url="http://localhost:11434/v1", models=[])
        mgr.upsert(p, api_key="sk-test")
        got = mgr.get("test1")
        assert got is not None
        assert got.id == "test1"
        assert got.key_set is True

    def test_env_key_escape_hatch(self, data_home, monkeypatch):
        mgr = ProviderManager(data_home)
        p = Provider(id="myprov", type="openai-compatible", base_url="http://x", models=[])
        mgr.upsert(p)
        monkeypatch.setenv("XU_KEY_MYPROV", "env-key-123")
        key = asyncio.run(mgr.get_key("myprov"))
        assert key == "env-key-123"

    def test_dev_key_in_memory(self, data_home):
        mgr = ProviderManager(data_home)
        p = Provider(id="dev1", type="openai-compatible", base_url="http://x", models=[])
        mgr.upsert(p, api_key="dev-key")
        key = asyncio.run(mgr.get_key("dev1"))
        assert key == "dev-key"

    def test_no_key_returns_none(self, data_home):
        mgr = ProviderManager(data_home)
        p = Provider(id="nokey", type="openai-compatible", base_url="http://x", models=[])
        mgr.upsert(p)
        # Clear any env key
        os.environ.pop("XU_KEY_NOKEY", None)
        key = asyncio.run(mgr.get_key("nokey"))
        assert key is None

    def test_delete(self, data_home):
        mgr = ProviderManager(data_home)
        p = Provider(id="del1", type="openai-compatible", base_url="http://x", models=[])
        mgr.upsert(p)
        assert mgr.get("del1") is not None
        mgr.delete("del1")
        assert mgr.get("del1") is None

    def test_find_model(self, data_home):
        mgr = ProviderManager(data_home)
        p = Provider(id="m1", type="openai-compatible", base_url="http://x", models=["qwen-7b", "qwen-13b"])
        mgr.upsert(p)
        found = mgr.find_model("qwen-7b")
        assert found is not None
        assert found[0].id == "m1"
        assert found[1] == "qwen-7b"
        assert mgr.find_model("nonexistent") is None

    def test_resolve_fallback(self, data_home):
        mgr = ProviderManager(data_home)
        p = Provider(id="c1", type="openai-compatible", base_url="http://x", models=["main-model"])
        mgr.upsert(p)
        # Explicit model resolves to its owning provider.
        result = mgr.resolve("main-model")
        assert result is not None
        assert result[1] == "main-model"
        # No model → first provider with a detected model list.
        result = mgr.resolve(None)
        assert result is not None
        assert result[0].id == "c1"
        assert result[1] == "main-model"
        # No providers at all → None
        mgr2 = ProviderManager(data_home)
        mgr2._providers = {}
        assert mgr2.resolve(None) is None


# ---------------------------------------------------------------------------
# fetch_models (key always sent, localhost included)
# ---------------------------------------------------------------------------

class TestFetchModels:
    def _mgr(self, data_home, monkeypatch):
        # Stub the keychain so no env vars are needed.
        class _Kc:
            async def get_key(self, pid: str):
                return f"key-for-{pid}"
        return ProviderManager(data_home, keychain=_Kc())

    @pytest.mark.asyncio
    async def test_sends_key_for_localhost(self, data_home, monkeypatch):
        """Localhost gateways that require auth must get the key too (Mooxy)."""
        mgr = self._mgr(data_home, monkeypatch)
        p = Provider(id="mooxy", type="openai-compatible", base_url="http://localhost:6969/v1", models=[])
        mgr.upsert(p)
        sent: dict[str, str] = {}

        class _Resp:
            status_code = 200
            def json(self):
                return {"data": [{"id": "mooxy-1"}, {"id": "mooxy-2"}]}
            def raise_for_status(self):
                return None

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url, headers=None):
                sent["url"] = url
                sent.update(headers or {})
                return _Resp()

        import xu_brain.features.agent.provider as prov_mod
        orig = prov_mod.httpx.AsyncClient
        prov_mod.httpx.AsyncClient = _Client
        try:
            ok, latency, models, error = await mgr.fetch_models(p)
        finally:
            prov_mod.httpx.AsyncClient = orig

        assert ok is True
        assert error is None
        assert models == ["mooxy-1", "mooxy-2"]
        assert sent["url"] == "http://localhost:6969/v1/models"
        # Key must be present even though base_url is localhost.
        assert sent.get("Authorization") == "Bearer key-for-mooxy"
        assert sent.get("X-API-Key") == "key-for-mooxy"
        assert latency >= 0

    @pytest.mark.asyncio
    async def test_anthropic_url_and_headers(self, data_home, monkeypatch):
        mgr = self._mgr(data_home, monkeypatch)
        p = Provider(id="anthro", type="anthropic-compatible", base_url="https://api.anthropic.com", models=[])
        mgr.upsert(p)
        sent: dict[str, str] = {}

        class _Resp:
            status_code = 200
            def json(self):
                return {"data": [{"id": "claude-3-opus"}]}
            def raise_for_status(self):
                return None

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url, headers=None):
                sent["url"] = url
                sent.update(headers or {})
                return _Resp()

        import xu_brain.features.agent.provider as prov_mod
        orig = prov_mod.httpx.AsyncClient
        prov_mod.httpx.AsyncClient = _Client
        try:
            ok, _lat, models, error = await mgr.fetch_models(p)
        finally:
            prov_mod.httpx.AsyncClient = orig

        assert ok is True
        assert error is None
        assert models == ["claude-3-opus"]
        assert sent["url"] == "https://api.anthropic.com/v1/models"
        assert sent.get("x-api-key") == "key-for-anthro"
        assert sent.get("anthropic-version") == "2023-06-01"

    @pytest.mark.asyncio
    async def test_http_error_surfaces_body(self, data_home, monkeypatch):
        mgr = self._mgr(data_home, monkeypatch)
        p = Provider(id="bad", type="openai-compatible", base_url="http://localhost:9/v1", models=[])
        mgr.upsert(p)

        import httpx as _httpx
        class _Resp:
            status_code = 401
            text = '{"error":{"message":"Invalid or missing API key"}}'
            def raise_for_status(self):
                raise _httpx.HTTPStatusError("401", request=None, response=self)

        class _Client:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url, headers=None):
                return _Resp()

        import xu_brain.features.agent.provider as prov_mod
        orig = prov_mod.httpx.AsyncClient
        prov_mod.httpx.AsyncClient = _Client
        try:
            ok, _lat, models, error = await mgr.fetch_models(p)
        finally:
            prov_mod.httpx.AsyncClient = orig

        assert ok is False
        assert models == []
        assert error is not None and "401" in error


class TestMidStreamError:
    """A gateway that loses its upstream mid-response has already sent 200, so
    it cannot fail the HTTP call. It appends an SSE chunk carrying a top-level
    "error" instead. Reading only `choices` turned that into a clean stop: the
    chat showed no error, the turn just ended, and the user had to say
    "continue"."""

    @staticmethod
    def _sse_client(lines: list[str]):
        """Minimal httpx.AsyncClient stand-in that replays SSE lines."""
        class _Resp:
            status_code = 200
            async def aiter_lines(self):
                for ln in lines:
                    yield ln

        class _Stream:
            async def __aenter__(self):
                return _Resp()
            async def __aexit__(self, *a):
                return False

        class _Client:
            is_closed = False
            def stream(self, *a, **k):
                return _Stream()
            async def aclose(self):
                return None

        return _Client()

    @staticmethod
    def _mgr():
        """Stands in for ProviderManager: the stream helpers only call get_key
        and the breaker bookkeeping lives in the public chat_stream wrapper."""
        class _Mgr:
            async def get_key(self, _pid: str) -> str:
                return "test-key"
        return _Mgr()

    async def _collect(self, stream):
        return [ev async for ev in stream]

    @pytest.mark.asyncio
    async def test_openai_chunk_error_is_retryable(self, data_home):
        from xu_brain.features.agent.provider import _openai_stream, StopReason

        p = Provider(id="mooxy", type="openai-compatible",
                     base_url="http://localhost:6969/v1", models=["m"])
        # Exactly what Mooxy emitted: real content, then a bun/undici socket
        # error riding along with finish_reason "stop".
        lines = [
            'data: {"choices":[{"index":0,"delta":{"content":"partial answer"}}]}',
            'data: {"object":"chat.completion.chunk","choices":[{"index":0,'
            '"delta":{},"finish_reason":"stop"}],"error":"The socket connection '
            'was closed unexpectedly. For more information, pass `verbose: true` '
            'in the second argument to fetch()"}',
        ]
        events = await self._collect(_openai_stream(
            p, "m", [], None, None, self._mgr(), None, client=self._sse_client(lines)))

        assert events[0].delta == "partial answer", "text before the drop is kept"
        err = events[-1]
        assert err.error and "socket connection was closed" in err.error
        assert err.stop_reason is StopReason.ERROR
        assert err.retryable is True, "a dropped socket is transient"
        # The bug: a bare STOP with no error anywhere in the stream.
        assert not any(
            e.stop_reason is StopReason.STOP and not e.error for e in events
        ), "the error must not be reported as a clean stop"

    @pytest.mark.asyncio
    async def test_openai_nested_error_message(self, data_home):
        from xu_brain.features.agent.provider import _openai_stream, StopReason

        p = Provider(id="g", type="openai-compatible", base_url="http://x/v1", models=["m"])
        lines = ['data: {"error":{"message":"upstream timed out","type":"server_error"}}']
        events = await self._collect(_openai_stream(
            p, "m", [], None, None, self._mgr(), None, client=self._sse_client(lines)))
        assert events[-1].error == "upstream timed out"
        assert events[-1].stop_reason is StopReason.ERROR

    @pytest.mark.asyncio
    async def test_anthropic_error_event_is_not_silent(self, data_home):
        from xu_brain.features.agent.provider import _anthropic_stream, StopReason

        p = Provider(id="a", type="anthropic-compatible",
                     base_url="https://api.anthropic.com", models=["m"])
        lines = [
            'event: error',
            'data: {"type":"error","error":{"type":"overloaded_error",'
            '"message":"Overloaded"}}',
        ]
        events = await self._collect(_anthropic_stream(
            p, "m", [], None, None, self._mgr(), None, client=self._sse_client(lines)))
        assert events, "the error event must produce something"
        assert events[-1].error == "Overloaded"
        assert events[-1].stop_reason is StopReason.ERROR
        assert events[-1].retryable is True

    def test_mid_stream_error_shapes(self):
        from xu_brain.features.agent.provider import _mid_stream_error
        assert _mid_stream_error("plain text") == "plain text"
        assert _mid_stream_error({"message": "from message"}) == "from message"
        assert _mid_stream_error({"error": {"message": "nested"}}) == "nested"
        assert _mid_stream_error(None) == "upstream stream error"
        assert _mid_stream_error("x" * 400).endswith("…")
        assert len(_mid_stream_error("x" * 400)) == 281


class TestUpsertPreserve:
    def test_preserves_key_set_without_key(self, data_home):
        mgr = ProviderManager(data_home)
        p = Provider(id="p1", type="openai-compatible", base_url="http://x", models=[], key_set=True)
        mgr.upsert(p, api_key="sk-orig")
        # Re-save without key — key_set must survive (key still in dev store).
        p2 = Provider(id="p1", type="openai-compatible", base_url="http://x", models=[])
        mgr.upsert(p2)
        got = mgr.get("p1")
        assert got is not None
        assert got.key_set is True

    def test_preserves_models_on_overwrite(self, data_home):
        mgr = ProviderManager(data_home)
        p = Provider(id="p1", type="openai-compatible", base_url="http://x", models=["a", "b"])
        mgr.upsert(p)
        p2 = Provider(id="p1", type="openai-compatible", base_url="http://x", models=[])
        mgr.upsert(p2)
        got = mgr.get("p1")
        assert got is not None
        assert got.models == ["a", "b"]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class TestConfig:
    def test_get_set(self, data_home):
        cfg = Config(data_home)
        cfg.set("approval_mode", "yolo")
        assert cfg.get("approval_mode") == "yolo"

    def test_session_model(self, data_home):
        cfg = Config(data_home)
        sid = "sess123"
        assert cfg.session_model(sid) is None
        cfg.set_session_model(sid, "gpt-4")
        assert cfg.session_model(sid) == "gpt-4"

    def test_delete_session_settings(self, data_home):
        cfg = Config(data_home)
        cfg.set_session_model("sess123", "gpt-4")
        cfg.set_session_persona("sess123", "persona1")
        cfg.set("session_max_tokens", {"sess123": 100})
        cfg.set_session_rules("sess123", [" Don't commit/push ", "", "Use tests"])
        assert cfg.session_rules("sess123") == ["Don't commit/push", "Use tests"]
        cfg.delete_session("sess123")
        assert cfg.session_model("sess123") is None
        assert cfg.session_persona("sess123") is None
        assert cfg.session_max_tokens("sess123") is None
        assert cfg.session_rules("sess123") == []

    def test_save_is_atomic_and_leaves_no_temp(self, data_home):
        """`set()` writes on every call, so a half-written file would lose
        every setting -- including provider keys -- on the next boot."""
        cfg = Config(data_home)
        cfg.set("approval_mode", "yolo")
        cfg.save()
        assert json.loads(cfg.file.read_text("utf-8"))["approval_mode"] == "yolo"
        assert not cfg.file.with_suffix(".json.tmp").exists()

    def test_corrupt_settings_are_quarantined_not_discarded(self, data_home):
        cfg = Config(data_home)
        cfg.set("approval_mode", "yolo")
        cfg.save()
        cfg.file.write_text('{"approval_mode": "yol', "utf-8")  # truncated write

        fresh = Config(data_home)
        assert fresh.get("approval_mode") == "manual"  # back to the default
        corrupt = data_home / "settings.json.corrupt"
        assert corrupt.exists(), "the unreadable bytes must be recoverable by hand"
        assert corrupt.read_text("utf-8") == '{"approval_mode": "yol'


class TestSessionDeletion:
    def test_rpc_delete_removes_session_and_rejects_missing(self, data_home):
        from xu_brain.core.runtime import build_app
        from xu_brain.core.contract import RpcError

        app = build_app(data_home)

        async def exercise():
            created = await app.dispatch("session.create", {})
            session_id = created["session"]["id"]
            await app.dispatch("session.delete", {"id": session_id})
            assert (await app.dispatch("session.list", {}))["sessions"] == []
            with pytest.raises(RpcError) as exc:
                await app.dispatch("session.delete", {"id": session_id})
            assert exc.value.code == -32002

        asyncio.run(exercise())

    def test_missing_id_is_a_session_error(self, data_home):
        from xu_brain.core.runtime import build_app
        from xu_brain.core.contract import RpcError

        app = build_app(data_home)

        async def exercise():
            with pytest.raises(RpcError) as exc:
                await app.dispatch("session.delete", {})
            assert exc.value.code == -32002

        asyncio.run(exercise())


def test_legacy_home_cwd_migrates_to_repo_root(data_home):
    """Old sessions whose cwd defaulted to $HOME must resolve relative
    edit/grep paths against the repo root, not the user's home directory."""
    from xu_brain.features.session import DEFAULT_CWD, SessionStore

    store = SessionStore(data_home)
    s = store.create()
    # Simulate a legacy row from before DEFAULT_CWD existed.
    with store._connect() as conn:
        conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (str(Path.home()), s.id))
    # Reload through a fresh store (the way a restarted brain would).
    store2 = SessionStore(data_home)
    got = store2.get(s.id)
    assert got is not None
    assert got.cwd == DEFAULT_CWD
    # And it was persisted so the stale value never resurfaces.
    with store2._connect() as conn:
        row = conn.execute("SELECT cwd FROM sessions WHERE id = ?", (s.id,)).fetchone()
    assert row["cwd"] == DEFAULT_CWD

def test_new_session_state_uses_the_session_cwd(data_home):
    """The state panel must receive the same cwd stored for a new session."""
    from xu_brain.core.runtime import build_app
    from xu_brain.features.session import DEFAULT_CWD

    app = build_app(data_home)

    async def exercise():
        created = await app.dispatch("session.create", {})
        sid = created["session"]["id"]
        state = await app.dispatch("state.get", {"session_id": sid})
        assert created["session"]["cwd"] == DEFAULT_CWD
        assert state["cwd"] == created["session"]["cwd"]

    asyncio.run(exercise())

def test_git_detail_returns_recent_commits(data_home):
    """`git.detail` must list commits — the panel's history column. Regression:
    the accumulator was dropped, so the loop raised NameError into a bare
    `except: pass` and the method returned no commits at all."""
    import subprocess

    from xu_brain.core.runtime import build_app

    repo = data_home / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True,
            env={"HOME": str(data_home), "PATH": os.environ.get("PATH", ""),
                 "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@t"},
        )

    git("init", "-q")
    (repo / "a.txt").write_text("hi\n")
    git("add", "a.txt")
    git("commit", "-qm", "first commit")

    app = build_app(data_home)

    async def exercise():
        created = await app.dispatch("session.create", {})
        sid = created["session"]["id"]
        await app.dispatch("workspace.set_cwd", {"session_id": sid, "cwd": str(repo)})
        detail = await app.dispatch("git.detail", {"session_id": sid})
        assert detail["repo"] is True
        assert [c["subject"] for c in detail["commits"]] == ["first commit"]
        assert detail["commits"][0]["author"] == "T"

    asyncio.run(exercise())

@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("main...origin/main", ("main", "origin/main", 0, 0)),
        ("main...origin/main [ahead 3]", ("main", "origin/main", 3, 0)),
        ("main...origin/main [behind 2]", ("main", "origin/main", 0, 2)),
        ("main...origin/main [ahead 1, behind 4]", ("main", "origin/main", 1, 4)),
        ("feature-x", ("feature-x", None, 0, 0)),
        ("No commits yet on master", ("No commits yet on master", None, 0, 0)),
        ("HEAD (no branch)", ("HEAD (no branch)", None, 0, 0)),
    ],
)
def test_git_branch_header_parses_ahead_behind(header, expected):
    """`git status --branch` writes divergence as words: `[ahead 1, behind 4]`.
    Regression: the parser looked for `ahead=1`, so ahead/behind were always 0
    and the git chip never showed ↑↓ on a diverged branch."""
    from xu_brain.features.session.rpc import _parse_branch_header

    assert _parse_branch_header(header) == expected

def test_set_cwd_rejects_nonexistent_directory(data_home):
    """A typo'd workspace dir must be rejected instead of poisoning tool paths."""
    from xu_brain.core.runtime import build_app
    from xu_brain.core.contract import RpcError

    app = build_app(data_home)

    async def exercise():
        created = await app.dispatch("session.create", {})
        sid = created["session"]["id"]
        ok = await app.dispatch("workspace.set_cwd", {"session_id": sid, "cwd": str(data_home)})
        assert ok["cwd"] == str(data_home)
        with pytest.raises(RpcError) as exc:
            await app.dispatch(
                "workspace.set_cwd", {"session_id": sid, "cwd": str(data_home / "nope-missing")}
            )
        assert exc.value.code == -32602
        # A rejected set must leave the previous cwd intact.
        state = await app.dispatch("state.get", {"session_id": sid})
        assert state["cwd"] == str(data_home)

    asyncio.run(exercise())
class TestSessionState:
    """Session state RPCs and system-prompt assembly (rules, model, AGENTS.md).

    These were stranded at method-indentation level inside a module function
    and never collected; the class restores them.
    """

    def test_rules_rpc_and_prompt_injection(self, data_home):
        from xu_brain.core.runtime import build_app
        from xu_brain.features.session import Message

        app = build_app(data_home)

        async def exercise():
            created = await app.dispatch("session.create", {})
            sid = created["session"]["id"]
            result = await app.dispatch("state.set_rules", {"session_id": sid, "rules": ["Don't commit/push"]})
            assert result == {"rules": ["Don't commit/push"]}
            state = await app.dispatch("state.get", {"session_id": sid})
            assert state["rules"] == ["Don't commit/push"]
            session = app.sessions.get(sid)
            assert session is not None
            app.sessions.append(sid, Message(role="user", content="make a change"))
            prompt = app.agent._build_messages(session)
            assert "[SESSION RULES]" in prompt[0]["content"]
            assert "Don't commit/push" in prompt[0]["content"]

        asyncio.run(exercise())

    def test_skill_dir_header_in_prompt_for_file_backed_skill(self, data_home):
        """A dir-skill's system-prompt block carries its absolute dir path."""
        from xu_brain.core.runtime import build_app
        from xu_brain.features.session import Message

        app = build_app(data_home)
        catalog_id = app.agent.skills.create(
            "dirtest", "Dir Test", "a dir-backed skill", "body text here")
        app.agent.skills.load(catalog_id)  # flip to LOADED -> ambient

        async def exercise():
            created = await app.dispatch("session.create", {})
            sid = created["session"]["id"]
            session = app.sessions.get(sid)
            app.sessions.append(sid, Message(role="user", content="hi"))
            prompt = app.agent._build_messages(session)
            return prompt[0]["content"]

        content = asyncio.run(exercise())
        assert f"# skill: {catalog_id}" in content
        assert "# skill dir:" in content
        # The advertised dir is the skill's real folder (has its SKILL.md).
        import re
        m = re.search(r"# skill dir: (\S+)", content)
        assert m and (Path(m.group(1)) / "SKILL.md").is_file()

    def test_model_state_rpc_is_registered_and_persisted(self, data_home):
        from xu_brain.core.runtime import build_app

        app = build_app(data_home)

        async def exercise():
            created = await app.dispatch("session.create", {})
            sid = created["session"]["id"]
            result = await app.dispatch("state.set_model", {"session_id": sid, "model": "gpt-4o"})
            assert result["model"] == "gpt-4o"
            state = await app.dispatch("state.get", {"session_id": sid})
            assert state["model"] == "gpt-4o"

        asyncio.run(exercise())
    def test_core_state_and_data_rpcs_are_registered(self, data_home):
        from xu_brain.core.runtime import build_app

        app = build_app(data_home)
        expected = {
            "workspace.set_cwd", "state.get", "state.set_model", "state.set_persona",
            "state.set_rules", "persona.list", "memory.list", "tool.list", "skill.list",
            "logs.list", "app.doctor",
        }
        assert expected <= app._methods.keys()
    def test_removed_override_mode_is_absent_from_state_and_rpc(self, data_home):
        from xu_brain.core.runtime import build_app
        from xu_brain.core.contract import RpcError
        from xu_brain.features.session import Message

        app = build_app(data_home)

        async def exercise():
            created = await app.dispatch("session.create", {})
            sid = created["session"]["id"]
            state = await app.dispatch("state.get", {"session_id": sid})
            assert all(not key.endswith("_style") for key in state)
            session = app.sessions.get(sid)
            assert session is not None
            app.sessions.append(sid, Message(role="user", content="hello"))
            prompt = app.agent._build_messages(session)
            assert "rebel" not in prompt[0]["content"].lower()
            with pytest.raises(RpcError) as exc:
                await app.dispatch("state.set_removed_override", {"session_id": sid, "on": True})
            assert exc.value.code == -32601

        asyncio.run(exercise())
    def test_build_messages_multimodal_content(self, data_home):
        from xu_brain.core.runtime import build_app
        from xu_brain.features.session import Message

        app = build_app(data_home)
        multimodal = [
            {"type": "text", "text": "what is in this shot?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
            {"type": "image_url", "image_url": {"url": "https://x/y.png"}},
        ]

        async def exercise():
            created = await app.dispatch("session.create", {})
            sid = created["session"]["id"]
            await app.dispatch("state.set_rules", {"session_id": sid, "rules": ["stay on-topic"]})
            session = app.sessions.get(sid)
            assert session is not None
            app.sessions.append(sid, Message(role="user", content=multimodal))
            prompt = app.agent._build_messages(session)
            # multimodal content is dereferenced: the bytes land under data_home
            # and the model gets a path for inspect_image, so nothing replays as
            # base64 (see tests/test_attachment_refs.py)
            last = prompt[-1]
            assert isinstance(last["content"], str)
            assert "what is in this shot?" in last["content"]
            # session rules live in the system prompt (prompt[0]), not the user message
            assert "[SESSION RULES]" in prompt[0]["content"]
            assert "stay on-topic" in prompt[0]["content"]
            assert "data:image" not in last["content"]
            # a remote URL is already a reference — kept as-is, no file written
            assert "https://x/y.png" in last["content"]

        asyncio.run(exercise())

    def test_build_messages_multimodal_vision_model(self, data_home):
        from xu_brain.core.runtime import build_app
        from xu_brain.features.session import Message

        app = build_app(data_home)
        app.config.set("vision_model", "gpt-4o")
        multimodal = [
            {"type": "text", "text": "describe"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        ]

        async def exercise():
            created = await app.dispatch("session.create", {})
            sid = created["session"]["id"]
            session = app.sessions.get(sid)
            assert session is not None
            app.sessions.append(sid, Message(role="user", content=multimodal))
            prompt = app.agent._build_messages(session)
            # History never holds pixels — only the path. The vision model still
            # gets the real image, but it is injected into the turn's own payload
            # by _loop (tests/test_attachment_refs.py), so nothing replays.
            assert isinstance(prompt[-1]["content"], str)
            assert "[image attached:" in prompt[-1]["content"]

        asyncio.run(exercise())
        assert app.config.vision_model() == "gpt-4o"

 
 # ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Live draft snapshot (multi-tab session resume)
# ---------------------------------------------------------------------------

class TestLiveDraft:
    """session.get(live) must mirror in-flight turn.* emissions and every
    turn.* event must carry session_id so multi-tab clients can route them."""

    def _agent(self):
        from xu_brain.features.agent.loop import Agent

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        agent._live_turns["sess"] = {"turn_id": "t1", "steps": [], "reasoning": ""}
        return agent

    def test_merge_and_emit_tag(self):
        import asyncio

        from xu_brain.core.notify import notify

        agent = self._agent()
        emitted: list[tuple[str, dict]] = []

        async def fake_emit(event: str, **kw: object) -> None:
            emitted.append((event, kw))

        old = notify.emit  # async fn attr on the shared bus object
        notify.emit = fake_emit  # type: ignore[assignment]
        try:
            async def drive() -> None:
                await agent._emit_for("sess", "t1", "turn.reasoning", delta="think ")
                await agent._emit_for("sess", "t1", "turn.reasoning", delta="more")
                await agent._emit_for("sess", "t1", "turn.delta", delta="hello ")
                await agent._emit_for("sess", "t1", "turn.delta", delta="world")
                await agent._emit_for("sess", "t1", "turn.tool", tool="bash", args="ls", status="running")
                await agent._emit_for(
                    "sess", "t1", "turn.tool", tool="bash", args="ls",
                    status="ok", elapsed=0.1, output="x",
                )

            asyncio.run(drive())
        finally:
            notify.emit = old  # type: ignore[assignment]

        live = agent.live_draft("sess")
        assert live is not None
        assert live["reasoning"] == "think more"
        kinds = [s["kind"] for s in live["steps"]]
        assert kinds == ["reasoning", "text", "tool"], live["steps"]
        assert live["steps"][-1] == {
            "kind": "tool", "tool": "bash", "args": "ls",
            "status": "ok", "elapsed": 0.1, "output": "x",
        }
        # every routed event is tagged with its session
        assert all(kw.get("session_id") == "sess" for _, kw in emitted), emitted

    def test_live_cleared_after_turn(self):
        agent = self._agent()
        # _run pops the snapshot in its finally block; emulate that contract
        agent._live_turns.pop("sess", None)
        assert agent.live_draft("sess") is None
    def test_partial_live_turn_is_persisted_as_assistant_history(self, data_home):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import SessionStore

        sessions = SessionStore(data_home)
        session = sessions.create()
        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        agent.sessions = sessions
        agent._live_turns[session.id] = {
            "turn_id": "t1",
            "steps": [
                {"kind": "reasoning", "text": "thinking"},
                {"kind": "text", "text": "partial answer"},
                {"kind": "tool", "tool": "bash", "args": "ls", "status": "running"},
            ],
            "reasoning": "thinking",
        }

        assert agent._persist_live_turn(session)
        assert not agent._persist_live_turn(session)
        assert agent.live_draft(session.id) is None
        reloaded = SessionStore(data_home).get(session.id)
        assert reloaded is not None
        assistant = reloaded.messages[-1]
        assert assistant.role == "assistant"
        assert assistant.content == "partial answer"
        assert assistant.reasoning == "thinking"
        assert assistant.steps[-1]["status"] == "error"

    def test_interrupted_live_turn_marks_status(self, data_home):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import SessionStore

        sessions = SessionStore(data_home)
        session = sessions.create()
        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        agent.sessions = sessions
        agent._live_turns[session.id] = {
            "turn_id": "t2",
            "steps": [{"kind": "text", "text": "partial reply"}],
            "reasoning": "",
        }

        assert agent._persist_live_turn(session, status="interrupted")
        reloaded = SessionStore(data_home).get(session.id)
        assistant = reloaded.messages[-1]
        assert assistant.status == "interrupted"
        assert assistant.content == "partial reply"

    def test_session_get_exposes_live_draft_for_refresh(self, data_home):
        from xu_brain.core.runtime import build_app

        app = build_app(data_home)

        async def exercise():
            created = await app.dispatch("session.create", {})
            sid = created["session"]["id"]
            app.agent._live_turns[sid] = {
                "turn_id": "t1",
                "steps": [{"kind": "text", "text": "still streaming"}],
                "reasoning": "",
            }
            got = await app.dispatch("session.get", {"id": sid})
            assert got["messages"] == []
            assert got["live"]["steps"][0]["text"] == "still streaming"

        asyncio.run(exercise())

class TestLoopReasoning:
    def test_loop_emits_reasoning_while_provider_streams(self):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.core.notify import notify

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        agent.config = SimpleNamespace(
            retry_max=lambda: 0,
            retry_interval=lambda: 0,
            session_max_tokens=lambda _sid: None,
            session_model=lambda _sid: "model",
            get=lambda _key, default=None: default,
            all=lambda: {},
        )
        agent.providers = SimpleNamespace(
            resolve=lambda _model, _p=None: ("provider", "model"),
            chat_stream=self._reasoning_stream,
        )
        agent.registry = SimpleNamespace(schemas_for_model=lambda: [], reset_breakers=lambda: None)
        agent._build_messages = lambda _session, node=None: []

        async def noop(*_args, **_kwargs):
            return None

        agent._maybe_compress = noop
        agent._emit_context = noop
        emitted: list[tuple[str, dict]] = []

        async def fake_emit(event: str, **kw: object) -> None:
            emitted.append((event, kw))

        old_emit = notify.emit
        from xu_brain.core.activity import activity
        activity.clear()
        notify.emit = fake_emit  # type: ignore[assignment]
        try:
            result = asyncio.run(agent._loop(SimpleNamespace(id="sess", cwd="/tmp", messages=[]), "t1", asyncio.Event()))
        finally:
            notify.emit = old_emit  # type: ignore[assignment]

        assert result is not None
        turn_events = [(event, payload.get("delta")) for event, payload in emitted if event.startswith("turn.")]
        assert turn_events == [
            ("turn.reasoning", "private thought"),
            ("turn.delta", "answer"),
        ]
        # the provider call itself is logged: provider/model, tokens, latency
        llm = [e for e in activity.entries(100) if e["message"].startswith("llm ")]
        assert len(llm) == 1
        assert llm[0]["message"] == "llm provider/model"
        assert llm[0]["level"] == "debug"
        assert "tokens={'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}" in llm[0]["detail"]
        # plumbing is debug-only: the agent loop itself emits zero info lines
        assert not [e for e in activity.entries(100) if e["level"] == "info"]


    def test_turn_summary_line_is_human_readable(self):
        from xu_brain.features.agent.loop import _turn_summary

        got = _turn_summary(SimpleNamespace(id="s1", title="Fix login bug"), "gpt-4o", 12.34)
        assert got == "Turn finished · Fix login bug · gpt-4o · 12.3s"
        # untitled sessions fall back to their id
        assert _turn_summary(SimpleNamespace(id="uabc123"), "gpt-4o", 0.04) == (
            "Turn finished · uabc123 · gpt-4o · 0.0s"
        )

    def test_spent_retry_countdown_is_retracted(self):
        """A retry notice must be withdrawn before the next provider attempt.

        The shell renders `draft.notice` verbatim; it used to clear only on a
        text delta, so a reply that led with reasoning or a tool call left the
        dead "retry 1/10 in 1s" line on screen for the rest of the turn.
        """
        from xu_brain.features.agent.loop import Agent
        from xu_brain.core.notify import notify

        attempts = {"n": 0}

        async def flaky_stream(*_args, **_kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("upstream 503")
            yield StreamEvent(reasoning="thinking now")
            yield StreamEvent(stop_reason=StopReason.STOP)

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        agent.config = SimpleNamespace(
            retry_max=lambda: 1,
            retry_interval=lambda: 0,  # → 1s floor, one countdown tick
            session_max_tokens=lambda _sid: None,
            session_model=lambda _sid: "model",
            get=lambda _key, default=None: default,
            all=lambda: {},
        )
        agent.providers = SimpleNamespace(
            resolve=lambda _model, _p=None: ("provider", "model"),
            chat_stream=flaky_stream,
        )
        agent.registry = SimpleNamespace(schemas_for_model=lambda: [], reset_breakers=lambda: None)
        agent._build_messages = lambda _session, node=None: []

        async def noop(*_args, **_kwargs):
            return None

        agent._maybe_compress = noop
        agent._emit_context = noop
        notices: list[str] = []

        async def fake_emit(event: str, **kw: object) -> None:
            if event == "turn.notice":
                notices.append(str(kw.get("text") or ""))

        old_emit = notify.emit
        from xu_brain.core.activity import activity
        activity.clear()
        notify.emit = fake_emit  # type: ignore[assignment]
        try:
            asyncio.run(agent._loop(SimpleNamespace(id="sess", cwd="/tmp", messages=[]), "t1", asyncio.Event()))
        finally:
            notify.emit = old_emit  # type: ignore[assignment]

        assert attempts["n"] == 2, "the transient failure was retried"
        assert notices, "the retry surfaced a notice"
        assert notices[0] == "provider error — retry 1/1 in 1s"
        assert notices[-1] == "", "the spent countdown was retracted"

    def test_falls_back_to_next_model_when_retries_are_spent(self):
        """A model whose provider keeps failing must hand the turn to the next
        model in the chain instead of ending it on `[error] …`."""
        from xu_brain.features.agent.loop import Agent
        from xu_brain.core.notify import notify

        seen: list[str] = []

        async def stream(_provider, model, *_args, **_kwargs):
            seen.append(model)
            if model == "primary":
                yield StreamEvent(error="401 invalid api key", retryable=False)
                return
            yield StreamEvent(delta="backup answered")
            yield StreamEvent(stop_reason=StopReason.STOP)

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        agent.config = SimpleNamespace(
            retry_max=lambda: 2,
            retry_interval=lambda: 0,
            session_max_tokens=lambda _sid: None,
            session_model=lambda _sid: "primary",
            model_fallbacks=lambda: ["backup"],
            get=lambda _key, default=None: default,
            all=lambda: {},
        )
        agent.providers = SimpleNamespace(
            resolve=lambda model, _p=None: (SimpleNamespace(id="prov"), model) if model else None,
            chat_stream=stream,
        )
        agent.registry = SimpleNamespace(schemas_for_model=lambda: [], reset_breakers=lambda: None)
        agent._build_messages = lambda _session, node=None: []

        async def noop(*_args, **_kwargs):
            return None

        agent._maybe_compress = noop
        agent._emit_context = noop
        notices: list[str] = []
        deltas: list[str] = []

        async def fake_emit(event: str, **kw: object) -> None:
            if event == "turn.notice":
                notices.append(str(kw.get("text") or ""))
            if event == "turn.delta":
                deltas.append(str(kw.get("delta") or ""))

        old_emit = notify.emit
        from xu_brain.core.activity import activity
        activity.clear()
        notify.emit = fake_emit  # type: ignore[assignment]
        try:
            result = asyncio.run(
                agent._loop(SimpleNamespace(id="sess", cwd="/tmp", messages=[]), "t1", asyncio.Event())
            )
        finally:
            notify.emit = old_emit  # type: ignore[assignment]

        # A non-retryable 401 must not burn retries — it goes straight to the backup.
        assert seen == ["primary", "backup"], seen
        assert "prov/primary failed — falling back to backup" in notices
        assert result.text == "backup answered"
        assert not any("[error]" in d for d in deltas), "the failure was recovered, not surfaced"

    def test_unresolvable_primary_model_starts_on_a_fallback(self):
        """A pinned model whose provider is gone (disabled/removed) starts the
        turn on the first resolvable fallback rather than dying on
        'No provider/model configured.'"""
        from xu_brain.features.agent.loop import Agent
        from xu_brain.core.notify import notify

        seen: list[str] = []

        async def stream(_provider, model, *_args, **_kwargs):
            seen.append(model)
            yield StreamEvent(delta="ok")
            yield StreamEvent(stop_reason=StopReason.STOP)

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        agent.config = SimpleNamespace(
            retry_max=lambda: 0,
            retry_interval=lambda: 0,
            session_max_tokens=lambda _sid: None,
            session_model=lambda _sid: "gone",
            model_fallbacks=lambda: ["still-here"],
            get=lambda _key, default=None: default,
            all=lambda: {},
        )
        agent.providers = SimpleNamespace(
            resolve=lambda model, _p=None: (SimpleNamespace(id="prov"), model) if model == "still-here" else None,
            chat_stream=stream,
        )
        agent.registry = SimpleNamespace(schemas_for_model=lambda: [], reset_breakers=lambda: None)
        agent._build_messages = lambda _session, node=None: []

        async def noop(*_args, **_kwargs):
            return None

        agent._maybe_compress = noop
        agent._emit_context = noop

        async def fake_emit(_event: str, **_kw: object) -> None:
            return None

        old_emit = notify.emit
        from xu_brain.core.activity import activity
        activity.clear()
        notify.emit = fake_emit  # type: ignore[assignment]
        try:
            result = asyncio.run(
                agent._loop(SimpleNamespace(id="sess", cwd="/tmp", messages=[]), "t1", asyncio.Event())
            )
        finally:
            notify.emit = old_emit  # type: ignore[assignment]

        assert seen == ["still-here"], seen
        assert result.text == "ok"

    def test_node_fallbacks_precede_the_global_chain(self):
        """A preset node's own fallbacks are tried before the global config
        chain, and duplicates collapse so no model is called twice."""
        from xu_brain.features.agent.loop import Agent

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        agent.config = SimpleNamespace(model_fallbacks=lambda: ["global-a", "node-b"])
        agent.providers = SimpleNamespace(
            resolve=lambda model, _p=None: (SimpleNamespace(id="prov"), model) if model else None,
        )
        node = SimpleNamespace(model="primary", fallbacks=["node-b"])

        chain = agent._model_chain("primary", None, node)
        assert [m for _p, m in chain] == ["primary", "node-b", "global-a"]

        # No config accessor (older mocks / a bare Config) → primary only.
        agent.config = SimpleNamespace()
        assert [m for _p, m in agent._model_chain("primary", None, None)] == ["primary"]
    @staticmethod
    async def _reasoning_stream(*_args, **_kwargs):
        yield StreamEvent(reasoning="private thought")
        yield StreamEvent(delta="answer")
        yield StreamEvent(usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        yield StreamEvent(stop_reason=StopReason.STOP)


class TestToolHistory:
    """A turn's tool rounds must persist (assistant tool_calls + tool results)
    and reappear in the next turn's context — the model should remember what
    tools it ran, not just the flattened final answer."""

    def test_tool_rounds_persist_and_echo_next_turn(self, data_home):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import SessionStore, Message
        from xu_brain.plugins import PluginBus

        store = SessionStore(data_home)
        config = Config(data_home)
        agent = Agent(store, data_home, None, None, ApprovalManager(config),
                      MemoryStore(data_home), SkillsEngine(data_home),
                      PluginBus(data_home), config)

        async def noop(*_a, **_k):
            return None

        async def passthrough(v):
            return v

        async def after_tool(_n, r):
            return r

        agent.plugins = SimpleNamespace(
            on_start=noop, on_message_out=passthrough, before_llm=passthrough,
            after_tool=after_tool)
        calls = {"n": 0}

        async def stream(provider, model, messages, tools, max_tokens=None, signal=None):
            calls["n"] += 1
            if calls["n"] == 1:
                yield StreamEvent(reasoning="think: run it")
                yield StreamEvent(tool_call=SimpleNamespace(id="c1", name="bash", arguments='{"cmd":"ls"}'))
                yield StreamEvent(stop_reason=StopReason.TOOL)
            else:
                yield StreamEvent(delta="done")
                yield StreamEvent(stop_reason=StopReason.STOP)

        agent.providers = SimpleNamespace(resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)

        async def run_tool(_n, _a, _ctx, emit=None):
            return SimpleNamespace(output="out.txt", error=False, raw="")

        agent.registry = SimpleNamespace(
            schemas_for_model=lambda: [], reset_breakers=lambda: None, run=run_tool)

        sess = store.create(cwd=str(data_home))

        async def main():
            await agent.send(sess.id, "list files")
            await asyncio.wait_for(agent._turns[sess.id], 20)

        asyncio.run(main())

        rows = store.get(sess.id).messages
        roles = [m.role for m in rows]
        assert roles == ["user", "assistant", "tool", "assistant"]
        # assistant row declares its tool_calls; tool row links by id
        assert any(m.role == "assistant" and m.tool_calls for m in rows)
        assert any(m.role == "tool" and m.tool_call_id == "c1" and m.tool_name == "bash" for m in rows)
        # the tool round actually ran (two provider requests this turn)
        assert calls["n"] == 2

        # next turn's context is a valid tool-call sequence with reasoning
        prompt = agent._build_messages(store.get(sess.id))
        assert [m["role"] for m in prompt] == ["system", "user", "assistant", "tool", "assistant"]
        assert any(m.get("tool_calls") for m in prompt if m.get("role") == "assistant")
        assert any(m.get("tool_call_id") == "c1" for m in prompt)
        assert any(m.get("reasoning_content") for m in prompt)

    def test_step_args_are_summarized_not_raw_payloads(self, data_home):
        """A step's `args` is the one-line chip summary, never the raw tool-call
        JSON. `write`/`edit` arguments carry whole file bodies; persisting them
        on the step timeline leaked them back into the next prompt."""
        from xu_brain.features.agent.loop import Agent

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        body = "x" * 5000
        step = agent._capture_tool("write", {"path": "/tmp/a.txt", "content": body})

        assert step["tool"] == "write"
        assert body not in step["args"], "file body leaked onto the step"
        assert len(step["args"]) <= 320
        assert "path=/tmp/a.txt" in step["args"]

        # delegate identity still resolves off the summarized args
        agent._delegations = {
            "d1": {"id": "d1", "child": "researcher", "parent_session": "s1",
                   "status": "running", "started": 1.0},
        }
        d = agent._capture_tool("delegate", {"child": "researcher", "prompt": "go"}, "s1")
        assert d["subagent_run"] == "d1"
        assert d["subagent"] == "researcher"


class TestQueueSend:
    """Mid-turn sends are queued, spliced into the live message list between
    tool rounds, and any late arrivals chain as a fresh turn."""

    def test_mid_turn_send_is_spliced_at_tool_boundary(self, data_home):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import SessionStore
        from xu_brain.plugins import PluginBus

        store = SessionStore(data_home)
        config = Config(data_home)
        agent = Agent(store, data_home, None, None, ApprovalManager(config),
                      MemoryStore(data_home), SkillsEngine(data_home),
                      PluginBus(data_home), config)

        async def noop(*_a, **_k):
            return None

        async def passthrough(v):
            return v

        async def after_tool(_n, r):
            return r

        agent.plugins = SimpleNamespace(
            on_start=noop, on_message_out=passthrough, before_llm=passthrough,
            after_tool=after_tool)

        calls = {"n": 0}
        seen: list[list[str]] = []

        async def stream(provider, model, messages, tools, max_tokens=None, signal=None):
            calls["n"] += 1
            seen.append([m.get("role") for m in messages if m.get("role") != "system"])
            if calls["n"] == 1:
                yield StreamEvent(tool_call=SimpleNamespace(id="c1", name="bash", arguments='{"cmd":"ls"}'))
                yield StreamEvent(stop_reason=StopReason.TOOL)
            else:
                # second request must already contain the queued user row
                assert any(m.get("role") == "user" and m.get("content") == "and this too" for m in messages)
                yield StreamEvent(delta="done")
                yield StreamEvent(stop_reason=StopReason.STOP)

        agent.providers = SimpleNamespace(resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)

        async def run_tool(_n, _a, _ctx, emit=None):
            await asyncio.sleep(0.05)  # hold the round open so the send lands mid-turn
            return SimpleNamespace(output="out.txt", error=False, raw="")

        agent.registry = SimpleNamespace(
            schemas_for_model=lambda: [], reset_breakers=lambda: None, run=run_tool)

        sess = store.create(cwd=str(data_home))


        async def main():
            await agent.send(sess.id, "list files")
            # land inside the (slowed) tool round so this send queues
            await asyncio.sleep(0.02)
            turn_id, queued_id = await agent.send(sess.id, "and this too")
            assert turn_id == ""  # queued, no turn of its own yet
            assert queued_id  # cancel handle for the queued message
            await asyncio.wait_for(agent._turns[sess.id], 20)

        asyncio.run(main())

        # no leftover queue; the turn completed with the queued message in
        assert agent._queued.get(sess.id) in (None, [])
        assert calls["n"] == 2
        # second request saw: user, assistant(tool_calls), tool, user
        assert "user" in seen[1] and "tool" in seen[1]

    def test_dequeue_event_fires_on_splice(self, data_home):
        """A queued send spliced at the tool round boundary must emit a
        turn.dequeue event carrying the consumed queued_id + text."""
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import SessionStore
        from xu_brain.plugins import PluginBus
        from xu_brain.core.notify import notify

        store = SessionStore(data_home)
        config = Config(data_home)
        agent = Agent(store, data_home, None, None, ApprovalManager(config),
                      MemoryStore(data_home), SkillsEngine(data_home),
                      PluginBus(data_home), config)

        async def noop(*_a, **_k):
            return None

        async def passthrough(v):
            return v

        async def after_tool(_n, r):
            return r

        agent.plugins = SimpleNamespace(
            on_start=noop, on_message_out=passthrough, before_llm=passthrough,
            after_tool=after_tool)

        calls = {"n": 0}
        seen: list[list[str]] = []
        emitted: list[tuple[str, dict]] = []
        captured: dict[str, str] = {}

        async def stream(provider, model, messages, tools, max_tokens=None, signal=None):
            calls["n"] += 1
            seen.append([m.get("role") for m in messages if m.get("role") != "system"])
            if calls["n"] == 1:
                yield StreamEvent(tool_call=SimpleNamespace(id="c1", name="bash", arguments='{"cmd":"ls"}'))
                yield StreamEvent(stop_reason=StopReason.TOOL)
            else:
                # second request must already contain the queued user row
                assert any(m.get("role") == "user" and m.get("content") == "and this too" for m in messages)
                yield StreamEvent(delta="done")
                yield StreamEvent(stop_reason=StopReason.STOP)

        agent.providers = SimpleNamespace(resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)

        async def run_tool(_n, _a, _ctx, emit=None):
            await asyncio.sleep(0.05)  # hold the round open so the send lands mid-turn
            return SimpleNamespace(output="out.txt", error=False, raw="")

        agent.registry = SimpleNamespace(
            schemas_for_model=lambda: [], reset_breakers=lambda: None, run=run_tool)

        sess = store.create(cwd=str(data_home))

        async def fake_emit(event, **kw):
            emitted.append((event, kw))

        old_emit = notify.emit
        notify.emit = fake_emit  # type: ignore[assignment]
        try:
            async def main():
                await agent.send(sess.id, "list files")
                await asyncio.sleep(0.02)  # land inside the tool round
                _tid, queued_id = await agent.send(sess.id, "and this too")
                captured["queued_id"] = queued_id
                assert queued_id
                await asyncio.wait_for(agent._turns[sess.id], 20)

            asyncio.run(main())
        finally:
            notify.emit = old_emit

        # queue fully drained into the live turn
        assert agent._queued.get(sess.id) in (None, [])
        assert calls["n"] == 2
        # the splice fired a turn.dequeue event with the consumed message
        deq = [(event, payload) for event, payload in emitted if event == "turn.dequeue"]
        assert deq, "expected a turn.dequeue event on splice"
        msgs = deq[0][1].get("messages") or []
        assert any(
            m.get("queued_id") == captured["queued_id"] and m.get("text") == "and this too"
            for m in msgs
        )

    def test_cancel_queued_drops_message_before_flush(self, data_home):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import SessionStore
        from xu_brain.plugins import PluginBus

        store = SessionStore(data_home)
        config = Config(data_home)
        agent = Agent(store, data_home, None, None, ApprovalManager(config),
                      MemoryStore(data_home), SkillsEngine(data_home),
                      PluginBus(data_home), config)

        async def noop(*_a, **_k):
            return None

        async def passthrough(v):
            return v

        async def after_tool(_n, r):
            return r

        agent.plugins = SimpleNamespace(
            on_start=noop, on_message_out=passthrough, before_llm=passthrough,
            after_tool=after_tool)

        calls = {"n": 0}
        seen: list[list[str]] = []
        ids: dict[str, str] = {}

        async def stream(provider, model, messages, tools, max_tokens=None, signal=None):
            calls["n"] += 1
            seen.append([m.get("role") for m in messages if m.get("role") != "system"])
            if calls["n"] == 1:
                yield StreamEvent(tool_call=SimpleNamespace(id="c1", name="bash", arguments='{"cmd":"ls"}'))
                yield StreamEvent(stop_reason=StopReason.TOOL)
            else:
                # cancelled message must NOT be spliced; the kept one must be
                contents = [m.get("content") for m in messages]
                assert not any(c == "cancel me" for c in contents)
                assert any(c == "keep me" for c in contents)
                yield StreamEvent(delta="done")
                yield StreamEvent(stop_reason=StopReason.STOP)

        agent.providers = SimpleNamespace(resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)

        async def run_tool(_n, _a, _ctx, emit=None):
            await asyncio.sleep(0.05)  # hold the round open so the sends land mid-turn
            return SimpleNamespace(output="out.txt", error=False, raw="")

        agent.registry = SimpleNamespace(
            schemas_for_model=lambda: [], reset_breakers=lambda: None, run=run_tool)

        sess = store.create(cwd=str(data_home))

        async def main():
            await agent.send(sess.id, "list files")
            await asyncio.sleep(0.02)
            _tid, cancel_id = await agent.send(sess.id, "cancel me")
            _tid, keep_id = await agent.send(sess.id, "keep me")
            ids["keep"] = keep_id
            assert cancel_id and keep_id and cancel_id != keep_id
            # cancelling an unknown id is a no-op, not an error
            assert agent.cancel_queued(sess.id, "nope") is False
            assert agent.cancel_queued(sess.id, cancel_id) is True
            # double-cancel is a no-op
            assert agent.cancel_queued(sess.id, cancel_id) is False
            await asyncio.wait_for(agent._turns[sess.id], 20)

        asyncio.run(main())

        # queue drained; the kept message was spliced, the cancelled one dropped
        assert agent._queued.get(sess.id) in (None, [])
        assert calls["n"] == 2
        assert "user" in seen[1] and "tool" in seen[1]
        # once flushed, the kept id can no longer be cancelled either
        assert agent.cancel_queued(sess.id, ids["keep"]) is False

    def test_steer_queued_promotes_and_reports(self, data_home):
        """steer_queued moves a queued message to the front so the turn's
        chaining tail runs it next, and reports False for an unknown id."""
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import SessionStore
        from xu_brain.plugins import PluginBus

        store = SessionStore(data_home)
        config = Config(data_home)
        agent = Agent(store, data_home, None, None, ApprovalManager(config),
                      MemoryStore(data_home), SkillsEngine(data_home),
                      PluginBus(data_home), config)
        sess = store.create(cwd=str(data_home))

        # Seed a queue directly (no running turn needed for the reorder logic).
        agent._queued[sess.id] = [("a", "first", None), ("b", "second", None), ("c", "third", None)]

        # Unknown id → no-op, no reorder.
        assert agent.steer_queued(sess.id, "nope") is False
        assert [q[0] for q in agent._queued[sess.id]] == ["a", "b", "c"]

        # Steering the last message moves it to the front (runs next on chain).
        assert agent.steer_queued(sess.id, "c") is True
        assert [q[0] for q in agent._queued[sess.id]] == ["c", "a", "b"]

        # Steering the already-front message is a no-op success.
        assert agent.steer_queued(sess.id, "c") is True
        assert [q[0] for q in agent._queued[sess.id]] == ["c", "a", "b"]

    def test_stop_does_not_double_cancel(self, data_home):
        """A second stop/steer while the first cancel is still unwinding must
        not double-cancel the turn task: the stray cancel would land inside
        the turn's finally, after _chain_queued popped the steered row,
        losing that message and orphaning the rest of the queue."""
        import asyncio

        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import SessionStore
        from xu_brain.plugins import PluginBus

        store = SessionStore(data_home)
        config = Config(data_home)
        agent = Agent(store, data_home, None, None, ApprovalManager(config),
                      MemoryStore(data_home), SkillsEngine(data_home),
                      PluginBus(data_home), config)
        sess = store.create(cwd=str(data_home))

        async def scenario():
            async def parked():
                await asyncio.Event().wait()
            task = asyncio.get_running_loop().create_task(parked())
            agent._turns[sess.id] = task
            agent._stop_events[sess.id] = asyncio.Event()
            await agent.stop(sess.id)
            await agent.stop(sess.id)  # rapid double-fire while unwinding
            assert task.cancelling() == 1, "double cancel corrupts the chain handoff"
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(scenario())


class TestActivityLogs:
    def test_activity_log_caps_and_filters(self):
        from xu_brain.core.activity import activity

        activity.clear()
        for i in range(activity.cap + 10):
            activity.record("info", "brain", f"evt-{i}")
        activity.record("error", "brain", "boom", "detail")

        all_entries = activity.entries(10**6)
        assert len(all_entries) == activity.cap
        assert all_entries[0]["message"] == "boom"  # newest first
        errors = activity.entries(100, level="error")
        assert errors == [all_entries[0]]
        assert activity.entries(100, source="nosuchsource") == []

    def test_heartbeat_rpc_ok_is_not_logged(self):
        """Polled status heartbeats stay out of the activity log; failures still surface."""
        from xu_brain.core.contract import _log_rpc_ok

        # heartbeat/status polling — suppressed on success
        assert not _log_rpc_ok("app.status")
        # read-only log check — never self-logged
        assert not _log_rpc_ok("logs.list")
        # real RPCs — still logged
        assert _log_rpc_ok("session.send")
        assert _log_rpc_ok("session")

    def test_unpack_survives_errors_that_are_methods(self):
        """pydantic's ValidationError exposes `.errors` as a *method*. Treating
        it as a sequence of sub-exceptions crashed the error logger itself."""
        from pydantic import BaseModel, ValidationError

        from xu_brain.core.activity import unpack

        class M(BaseModel):
            x: int

        with pytest.raises(ValidationError) as caught:
            M(x="not-an-int")
        msg, _detail = unpack(caught.value)
        assert msg.startswith("ValidationError:")

    def test_unpack_expands_exception_groups_and_causes(self):
        from xu_brain.core.activity import unpack

        msg, detail = unpack(ExceptionGroup("batch", [ValueError("a"), KeyError("b")]))
        assert (msg, detail) == ("ValueError: a", "KeyError: 'b'")

        try:
            try:
                raise ValueError("inner")
            except ValueError as inner:
                raise RuntimeError("outer") from inner
        except RuntimeError as outer:
            assert unpack(outer) == ("RuntimeError: outer", "ValueError: inner")



class TestRpcPayloadCapture:
    """RPC calls get a sanitized, size-capped {params, result} payload that the
    Logs view modal renders — so users can see exactly what the brain sent /
    received without leaking credentials or overflowing the 1000-entry buffer."""

    def test_activity_record_accepts_payload_kwarg(self):
        from xu_brain.core.activity import activity

        activity.clear()
        activity.record(
            "info", "brain", "rpc provider.test → ok",
            payload={"params": {"id": "openai"}, "result": {"ok": True}},
        )
        entry = activity.entries(1)[0]
        assert entry["payload"] == {"params": {"id": "openai"}, "result": {"ok": True}}
        # message stays truncated as before — payload is the full fidelity copy
        assert entry["message"] == "rpc provider.test → ok"
        # payload lives on a separate key, not inside the truncated message/detail
        assert "payload" not in str(entry["message"])

    def test_build_payload_captures_params_and_result(self):
        from xu_brain.core.contract import _build_payload

        p = _build_payload("provider.test", {"id": "openai"}, result={"ok": True, "latency_ms": 42})
        assert p["params"] == {"id": "openai"}
        assert p["result"] == {"ok": True, "latency_ms": 42}
        assert "error" not in p

    def test_build_payload_captures_rpc_error_shape(self):
        from xu_brain.core.contract import RpcError, _build_payload

        p = _build_payload(
            "provider.test", {"id": "missing"},
            error=RpcError(-32002, "not found", {"provider_id": "missing"}),
        )
        assert p["params"] == {"id": "missing"}
        assert p["error"] == {
            "code": -32002,
            "message": "not found",
            "data": {"provider_id": "missing"},
        }
        assert "result" not in p

    def test_credentials_redacted_at_any_depth(self):
        from xu_brain.core.contract import _build_payload, _sanitize

        v = {
            "session_id": "s1",
            "api_key": "sk-secret",
            "nested": {"token": "t", "name": "x", "deep": {"authorization": "Bearer z"}},
            "list": [{"password": "p"}, {"secret": "s"}],
        }
        s = _sanitize(v)
        assert s["api_key"] == "[REDACTED]"
        assert s["nested"]["token"] == "[REDACTED]"
        assert s["nested"]["deep"]["authorization"] == "[REDACTED]"
        assert s["list"][0]["password"] == "[REDACTED]"
        assert s["list"][1]["secret"] == "[REDACTED]"
        assert s["session_id"] == "s1"
        assert s["nested"]["name"] == "x"
        # full RPC envelope matches the deep-walk
        p = _build_payload("provider.test", v, result={"ok": True})
        assert p["params"]["api_key"] == "[REDACTED]"
        assert p["params"]["nested"]["deep"]["authorization"] == "[REDACTED]"

    def test_session_send_messages_collapsed(self):
        from xu_brain.core.contract import _build_payload, _trim_heavy

        long_history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "ok"}]
        trimmed = _trim_heavy("session.send", {"messages": long_history, "text": "next"})
        assert trimmed["messages"] == "<2 message(s) redacted>"
        assert trimmed["text"] == "next"

        # session.get result.message bodies also collapsed
        p = _build_payload(
            "session.get", {"id": "s1"},
            result={"messages": long_history, "title": "chat"},
        )
        assert p["result"]["messages"] == "<2 message(s) redacted>"
        assert p["result"]["title"] == "chat"

        # Other methods keep their messages array intact
        assert _trim_heavy("session.list", {"messages": long_history})["messages"] == long_history

    def test_logs_methods_excluded_from_payload(self):
        from xu_brain.core.contract import _build_payload

        assert _build_payload("logs.list", {"limit": 100}, result={"logs": []}) == {}

    def test_payload_size_cap(self):
        import json
        from xu_brain.core.contract import _cap_payload

        big = {"params": {"blob": "x" * (100 * 1024)}}
        capped = _cap_payload(big)
        # Either the params dict got a stub or we got a global truncation marker
        assert capped.get("_truncated") is True or capped.get("params") == "<truncated for size>"
        assert len(json.dumps(capped, default=str, ensure_ascii=False)) <= 64 * 1024 + 50

    def test_quiet_heartbeat_does_not_self_log(self):
        """End-to-end: app.status RPC returns ok but is suppressed by _log_rpc_ok,
        so its payload never lands in the activity buffer."""
        from xu_brain.core.activity import activity
        from xu_brain.core.contract import _build_payload, _log_rpc_ok

        activity.clear()
        # quiet — no activity.record call happens for app.status
        assert not _log_rpc_ok("app.status")
        # payload helper still works for the rare case it's needed
        assert _build_payload("app.status", {}, result={"brain": "ok"})["result"] == {"brain": "ok"}
        assert activity.entries(100) == []



# ---------------------------------------------------------------------------
# File edit tool — snapshot anchoring + bounds

class TestFileEdit:
    """edit must anchor on read's [#TAG], reject stale/out-of-range patches,
    and never silently append or claim phantom changes."""

    def _ctx(self, cwd: Path) -> ToolContext:
        return ToolContext(
            data_home=cwd, session_id="s", turn_id="t", cwd=str(cwd),
            agent=None, events=None, approvals=None, providers=None,
            memory=None, skills=None, flat_plugins=None, config={},
        )

    def test_read_emits_snapshot_tag_and_edits_roundtrip(self, data_home):
        from xu_brain.features.tools import file as F
        p = data_home / "t.txt"
        p.write_text("a\nb\nc\nd\n", "utf-8")
        ctx = self._ctx(data_home)
        got = asyncio.run(F.read.run({"path": "t.txt"}, ctx))
        first = got.output.splitlines()[0]
        assert first.startswith("[t.txt#") and first.endswith("]")
        tag = first.split("#")[1][:-1]
        assert tag == F._snapshot_tag("a\nb\nc\nd\n")
        res = asyncio.run(F.edit.run({"patch": f"{first}\nPUT 2:\n+X2"}, ctx))
        assert not res.error
        assert p.read_text() == "a\nX2\nc\nd\n"

    def test_edit_rejects_stale_snapshot_without_touching_file(self, data_home):
        from xu_brain.features.tools import file as F
        p = data_home / "t.txt"
        p.write_text("a\nb\nc\n", "utf-8")
        ctx = self._ctx(data_home)
        got = asyncio.run(F.read.run({"path": "t.txt"}, ctx))
        header = got.output.splitlines()[0]
        p.write_text("a\nCHANGED\nc\n", "utf-8")  # drift after the snapshot
        res = asyncio.run(F.edit.run({"patch": f"{header}\nPUT 2:\n+X2"}, ctx))
        assert res.error
        assert "changed since" in res.output
        assert p.read_text() == "a\nCHANGED\nc\n"

    def test_edit_out_of_range_errors_not_eof_append(self, data_home):
        from xu_brain.features.tools import file as F
        p = data_home / "t.txt"
        p.write_text("a\nb\nc\nd\n", "utf-8")
        ctx = self._ctx(data_home)
        tag = F._snapshot_tag("a\nb\nc\nd\n")
        res = asyncio.run(F.edit.run({"patch": f"[t.txt#{tag}]\nPUT 9:\n+X9"}, ctx))
        assert res.error and "out of range" in res.output
        assert p.read_text() == "a\nb\nc\nd\n"
        res = asyncio.run(F.edit.run({"patch": f"[t.txt#{tag}]\nCUT 9.=12\n"}, ctx))
        assert res.error and "out of range" in res.output
        assert p.read_text() == "a\nb\nc\nd\n"

    def test_edit_multi_op_offset(self, data_home):
        from xu_brain.features.tools import file as F
        p = data_home / "t.txt"
        p.write_text("a\nb\nc\nd\n", "utf-8")
        ctx = self._ctx(data_home)
        tag = F._snapshot_tag("a\nb\nc\nd\n")
        res = asyncio.run(F.edit.run(
            {"patch": f"[t.txt#{tag}]\nPUT >1:\n+NEW\nPUT 3:\n+SURE"}, ctx))
        assert not res.error
        assert p.read_text() == "a\nNEW\nb\nSURE\nd\n"

    def test_edit_cut_then_edit_deleted_line_errors(self, data_home):
        """A later op referencing a line a prior CUT deleted must raise, not
        silently corrupt via Python negative-index slice wrap. The whole patch
        is applied in-memory then written once, so a raise leaves the file
        untouched."""
        from xu_brain.features.tools import file as F
        p = data_home / "cut.txt"
        p.write_text("a\nb\nc\nd\n", "utf-8")
        ctx = self._ctx(data_home)
        tag = F._snapshot_tag("a\nb\nc\nd\n")
        # CUT deletes lines 1-2; there is no live line 1 (= original 2) left.
        res = asyncio.run(F.edit.run(
            {"patch": f"[cut.txt#{tag}]\nCUT 1.=2\nPUT 1:\n+X"}, ctx))
        assert res.error and "out of range" in res.output
        assert p.read_text() == "a\nb\nc\nd\n"

    def test_edit_deleted_line_shift_still_works(self, data_home):
        """A later op referencing a surviving original line (shifted by an
        earlier CUT) must still apply at the right live position."""
        from xu_brain.features.tools import file as F
        p = data_home / "cut.txt"
        p.write_text("a\nb\nc\nd\n", "utf-8")
        ctx = self._ctx(data_home)
        tag = F._snapshot_tag("a\nb\nc\nd\n")
        res = asyncio.run(F.edit.run(
            {"patch": f"[cut.txt#{tag}]\nCUT 1.=2\nPUT 4:\n+X"}, ctx))
        assert not res.error
        assert p.read_text() == "c\nX\n"

    def test_edit_accepts_arbitrary_label_tag(self, data_home):
        """Non-hex header tags (models' labels like #pickModel or #TAG) must
        not be rejected — they just bypass drift-verification."""
        from xu_brain.features.tools import file as F
        p = data_home / "lab.txt"
        p.write_text("a\nb\nc\n", "utf-8")
        ctx = self._ctx(data_home)
        res = asyncio.run(F.edit.run(
            {"patch": "[lab.txt#pickModel]\nPUT 2:\n+X2"}, ctx))
        assert not res.error
        assert p.read_text() == "a\nX2\nc\n"

    def test_edit_tolerates_leading_blank_line(self, data_home):
        from xu_brain.features.tools import file as F
        p = data_home / "ws.txt"
        p.write_text("a\nb\n", "utf-8")
        ctx = self._ctx(data_home)
        tag = F._snapshot_tag("a\nb\n")
        res = asyncio.run(F.edit.run(
            {"patch": f"\n  [ws.txt#{tag}]\nPUT 1:\n+X0"}, ctx))
        assert not res.error
        assert p.read_text() == "X0\nb\n"  # PUT N: replaces line N

    def test_edit_stale_hex_tag_still_rejected(self, data_home):
        """A real (hex) snapshot that no longer matches must still be caught,
        so the drift guard survives the lenient-label change."""
        from xu_brain.features.tools import file as F
        p = data_home / "stale.txt"
        p.write_text("a\nb\n", "utf-8")
        ctx = self._ctx(data_home)
        stale = F._snapshot_tag("zzz\n")
        res = asyncio.run(F.edit.run(
            {"patch": f"[stale.txt#{stale}]\nPUT 1:\n+X0"}, ctx))
        assert res.error and "changed since" in res.output
        assert p.read_text() == "a\nb\n"

    def test_edit_star_replaces_whole_file(self, data_home):
        """`PUT *:` must replace the whole file, not silently no-op."""
        from xu_brain.features.tools import file as F
        p = data_home / "star.txt"
        p.write_text("a\nb\nc\n", "utf-8")
        ctx = self._ctx(data_home)
        res = asyncio.run(F.edit.run(
            {"patch": "[star.txt]\nPUT *:\n+X\n+Y"}, ctx))
        assert not res.error
        assert p.read_text() == "X\nY\n"

    def test_edit_unknown_spec_errors_not_silent_noop(self, data_home):
        from xu_brain.features.tools import file as F
        p = data_home / "junk.txt"
        p.write_text("a\nb\n", "utf-8")
        ctx = self._ctx(data_home)
        res = asyncio.run(F.edit.run(
            {"patch": "[junk.txt]\nPUT nope:\n+X"}, ctx))
        assert res.error and "unrecognized line spec" in res.output
        assert p.read_text() == "a\nb\n"

    def test_edit_accepts_dash_range_spec(self, data_home):
        """Models steeped in read's `path:N-M` selector send `PUT N-M:` —
        accept it instead of erroring."""
        from xu_brain.features.tools import file as F
        p = data_home / "dash.txt"
        p.write_text("a\nb\nc\nd\n", "utf-8")
        ctx = self._ctx(data_home)
        res = asyncio.run(F.edit.run(
            {"patch": "[dash.txt]\nPUT 2-3:\n+X\n+Y"}, ctx))
        assert not res.error
        assert p.read_text() == "a\nX\nY\nd\n"

    def test_edit_strips_at_register_from_spec(self, data_home):
        from xu_brain.features.tools import file as F
        p = data_home / "reg.txt"
        p.write_text("a\nb\n", "utf-8")
        ctx = self._ctx(data_home)
        res = asyncio.run(F.edit.run(
            {"patch": "[reg.txt]\nPUT 2 @buf:\n+X2"}, ctx))
        assert not res.error
        assert p.read_text() == "a\nX2\n"


# ---------------------------------------------------------------------------
# Tool review regressions — todo phases, bash cwd, ddg links, lsp, http body

class TestTodoPhases:
    def _ctx(self, cwd: Path) -> ToolContext:
        return ToolContext(
            data_home=cwd, session_id="s", turn_id="t", cwd=str(cwd),
            agent=None, events=None, approvals=None, providers=None,
            memory=None, skills=None, flat_plugins=None, config={},
        )

    def test_start_finds_task_in_later_phase(self, data_home):
        """Without a `phase` arg every phase must be searched — tasks beyond
        the first phase used to report 'task not found'."""
        from xu_brain.features.tools.plan import todo
        ctx = self._ctx(data_home)
        asyncio.run(todo.run({"op": "init", "list": [
            {"phase": "one", "items": ["first"]},
            {"phase": "two", "items": ["second"]},
        ]}, ctx))
        res = asyncio.run(todo.run({"op": "start", "task": "second"}, ctx))
        assert not res.error
        assert "[>]" in res.output and "second" in res.output

    def test_done_scoped_to_named_phase_only(self, data_home):
        from xu_brain.features.tools.plan import todo
        ctx = self._ctx(data_home)
        asyncio.run(todo.run({"op": "init", "list": [
            {"phase": "one", "items": ["task"]},
            {"phase": "two", "items": ["task"]},
        ]}, ctx))
        res = asyncio.run(todo.run({"op": "done", "task": "task", "phase": "two"}, ctx))
        assert not res.error
        # phase one's same-named item must be untouched
        assert "[ ] task" in res.output and "[x] task" in res.output


class TestBashCwdPersistence:
    def test_model_cd_persists_and_session_cd_overrides(self, tmp_path):
        """The model's `cd` must survive across calls; a changed session cwd
        (user moved the session) must force the shell back."""
        from xu_brain.features.tools.terminal import ShellSession
        sub = tmp_path / "sub"
        sub.mkdir()
        other = tmp_path / "other"
        other.mkdir()

        async def main() -> None:
            s = ShellSession(str(tmp_path))
            try:
                rc, out, _, _ = await s.run("pwd", timeout=15, session_cwd=str(tmp_path))
                assert rc == 0 and out.strip() == str(tmp_path)
                await s.run(f"cd {sub}", timeout=15, session_cwd=str(tmp_path))
                rc, out, _, _ = await s.run("pwd", timeout=15, session_cwd=str(tmp_path))
                assert out.strip() == str(sub), "model cd must persist"
                rc, out, _, _ = await s.run("pwd", timeout=15, session_cwd=str(other))
                assert out.strip() == str(other), "session cwd change must win"
                rc, out, _, _ = await s.run("pwd", timeout=15, session_cwd=str(other))
                assert out.strip() == str(other)
            finally:
                await s.close()

        asyncio.run(main())

    def test_long_single_line_does_not_break_the_session(self, tmp_path):
        """A line longer than asyncio's 64 KB StreamReader limit must survive.

        `readline()` raised ValueError("Separator is not found, and chunk
        exceed the limit") on such a line. The sentinel was then left unread in
        the pipe, so the NEXT command read this one's leftovers and missed its
        own sentinel — one minified-JS `cat` broke `bash` for the whole session.
        """
        from xu_brain.features.tools.terminal import ShellSession

        async def main() -> None:
            s = ShellSession(str(tmp_path))
            try:
                # 200 KB on a single line, no newline until the very end.
                rc, out, _, _ = await s.run(
                    "printf 'x%.0s' $(seq 200000); echo", timeout=30,
                    session_cwd=str(tmp_path),
                )
                assert rc == 0, f"long line must not fail the call (rc={rc}, out={out[:200]!r})"
                assert out.startswith("xxxx"), out[:80]

                # The shell must still be usable, and give ITS OWN output.
                rc, out, _, _ = await s.run("echo CLEAN", timeout=15, session_cwd=str(tmp_path))
                assert rc == 0
                assert out.strip() == "CLEAN", f"stale output leaked in: {out[:200]!r}"

                # cwd tracking still works after the big read.
                sub = tmp_path / "after"
                sub.mkdir()
                await s.run(f"cd {sub}", timeout=15, session_cwd=str(tmp_path))
                rc, out, _, _ = await s.run("pwd", timeout=15, session_cwd=str(tmp_path))
                assert out.strip() == str(sub)
            finally:
                await s.close()

        asyncio.run(main())

    def test_multibyte_split_across_chunks_survives(self, tmp_path):
        """Chunked reads must decode incrementally: a UTF-8 character straddling
        a 64 KB read boundary must not become U+FFFD."""
        from xu_brain.features.tools.terminal import ShellSession

        async def main() -> None:
            s = ShellSession(str(tmp_path))
            try:
                # 3-byte chars, ~88 KB, so many land on chunk boundaries.
                rc, out, _, _ = await s.run(
                    "printf '★%.0s' $(seq 30000); echo", timeout=30,
                    session_cwd=str(tmp_path),
                )
                assert rc == 0
                assert "\ufffd" not in out, "a split character was mangled"
                # Output is past the 64 KB in-memory cap, so the buffer keeps a
                # head and appends its own truncation note. Every retained
                # character must still be the one that was printed.
                head = out.split("\n…[", 1)[0]
                assert len(head) > 20000, f"kept too little: {len(head)}"
                assert set(head) == {"★"}, f"unexpected characters: {set(head) - {'★'}!r}"
                assert "truncated" in out
            finally:
                await s.close()

        asyncio.run(main())

    def test_read_failure_restarts_the_shell(self, tmp_path):
        """If a read raises for any reason, the shell must be discarded rather
        than handed to the next call with unread output still in its pipe."""
        from xu_brain.features.tools.terminal import ShellSession

        async def main() -> None:
            s = ShellSession(str(tmp_path))
            try:
                await s.run("echo warmup", timeout=15, session_cwd=str(tmp_path))
                first = s._proc
                assert first is not None

                # Force the read path to blow up mid-command.
                orig = first.stdout.read

                async def boom(_n: int) -> bytes:
                    first.stdout.read = orig  # only fail once
                    raise ValueError("Separator is not found, and chunk exceed the limit")

                first.stdout.read = boom  # type: ignore[assignment]
                rc, _out, err, _ = await s.run("echo nope", timeout=15, session_cwd=str(tmp_path))
                assert rc == 125, "a read failure is reported, not silently swallowed"
                assert "restarted" in err
                assert s._proc is None, "the poisoned shell was dropped"

                # Next call gets a fresh shell and clean output.
                rc, out, _, _ = await s.run("echo RECOVERED", timeout=15, session_cwd=str(tmp_path))
                assert rc == 0 and out.strip() == "RECOVERED"
                assert s._proc is not first
            finally:
                await s.close()

        asyncio.run(main())


class TestWebHelpers:
    def test_clean_href_unwraps_ddg_redirect(self):
        from xu_brain.features.tools.web import _clean_href
        # as captured from DDG's HTML: the & between params still HTML-escaped
        href = ("//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.python.org%2Fabout%2F"
                "&amp;rut=abc123")
        assert _clean_href(href) == "https://www.python.org/about/"

    def test_clean_href_plain_and_protocol_relative(self):
        from xu_brain.features.tools.web import _clean_href
        assert _clean_href("https://example.com/a") == "https://example.com/a"
        assert _clean_href("//example.com/a") == "https://example.com/a"


class TestLspHelpers:
    def test_character_for_symbol_on_line(self):
        from xu_brain.features.tools.lsp_debug import _character_for
        text = "result = compute(x)\n"
        assert _character_for(text, 1, "compute") == 9

    def test_character_for_utf16_and_misses(self):
        from xu_brain.features.tools.lsp_debug import _character_for
        # 'x' after an astral char: utf-16 units count the surrogate pair as 2
        text = "v = '😀' x\n"
        col = text.find("x")
        assert _character_for(text, 1, "x") == len(text[:col].encode("utf-16-le")) // 2
        assert _character_for(text, 1, "nope") == 0
        assert _character_for(text, 99, "x") == 0
        assert _character_for(text, 1, "") == 0


# ---------------------------------------------------------------------------
# eval kernel — subprocess isolation (state persists, brain process untouched)

class TestEvalKernel:
    def _ctx(self, cwd: Path) -> ToolContext:
        return ToolContext(
            data_home=cwd, session_id="s", turn_id="t", cwd=str(cwd),
            agent=None, events=None, approvals=None, providers=None,
            memory=None, skills=None, flat_plugins=None, config={},
        )

    def test_state_persists_and_result_captured(self, data_home):
        from xu_brain.features.tools import eval_tool as E
        ctx = self._ctx(data_home)

        async def main():
            r1 = await E.eval.run({"code": "x = 40"}, ctx)
            assert not r1.error
            r2 = await E.eval.run({"code": "x + 2"}, ctx)
            assert "→ 42" in r2.output
            t = E.close_session("s")
            if t:
                await t

        asyncio.run(main())

    def test_runs_in_separate_process(self, data_home):
        """The kernel must NOT share the brain's process — same PID would mean
        model code could reach the brain's memory and modules."""
        from xu_brain.features.tools import eval_tool as E
        import os as _os
        ctx = self._ctx(data_home)

        async def main():
            r = await E.eval.run({"code": "import os; os.getpid()"}, ctx)
            child_pid = int(r.output.split("→")[1].strip())
            assert child_pid != _os.getpid()
            t = E.close_session("s")
            if t:
                await t

        asyncio.run(main())

    def test_timeout_kills_kernel_and_state_resets(self, data_home):
        from xu_brain.features.tools import eval_tool as E
        ctx = self._ctx(data_home)

        async def main():
            await E.eval.run({"code": "keep = 1"}, ctx)
            r = await E.eval.run({"code": "while True: pass", "timeout": 1.5}, ctx)
            assert r.error and "timed out" in r.output
            # kernel respawned empty: the variable from before the kill is gone
            r2 = await E.eval.run({"code": "keep"}, ctx)
            assert r2.error and "NameError" in r2.output
            t = E.close_session("s")
            if t:
                await t

        asyncio.run(main())

    def test_syntax_error_is_clean_error(self, data_home):
        from xu_brain.features.tools import eval_tool as E
        ctx = self._ctx(data_home)

        async def main():
            r = await E.eval.run({"code": "def broken(:"}, ctx)
            assert r.error and "SyntaxError" in r.output
            t = E.close_session("s")
            if t:
                await t

        asyncio.run(main())


# ---------------------------------------------------------------------------
# diagnostics feedback loop — edit/write surface LSP/syntax findings

class TestEditDiagnostics:
    def _ctx(self, cwd: Path) -> ToolContext:
        return ToolContext(
            data_home=cwd, session_id="s", turn_id="t", cwd=str(cwd),
            agent=None, events=None, approvals=None, providers=None,
            memory=None, skills=None, flat_plugins=None, config={},
        )

    def test_edit_surfaces_python_syntax_error(self, data_home, monkeypatch):
        """An edit that breaks the file must tell the model immediately —
        the feedback loop, without needing a language server installed."""
        # force the ast-fallback branch even if pylsp exists on this machine
        monkeypatch.setattr("xu_brain.features.tools.lsp_debug._lsp_server", lambda lang, ctx: None)
        from xu_brain.features.tools import file as F
        p = data_home / "bug.py"
        p.write_text("a = 1\nb = 1\n", "utf-8")
        ctx = self._ctx(data_home)
        res = asyncio.run(F.edit.run(
            {"patch": "[bug.py]\nPUT 2:\n+def broken(:"}, ctx))
        assert not res.error  # the edit itself succeeded
        assert "syntax error" in res.output and "line 2" in res.output

    def test_edit_clean_python_has_no_note(self, data_home, monkeypatch):
        monkeypatch.setattr("xu_brain.features.tools.lsp_debug._lsp_server", lambda lang, ctx: None)
        from xu_brain.features.tools import file as F
        p = data_home / "ok.py"
        p.write_text("a = 1\nb = 1\n", "utf-8")
        ctx = self._ctx(data_home)
        res = asyncio.run(F.edit.run(
            {"patch": "[ok.py]\nPUT 2:\n+b = 2"}, ctx))
        assert not res.error
        assert "⚠" not in res.output

    def test_render_diags(self):
        from xu_brain.features.tools.lsp_debug import _render_diags
        diags = [{"severity": 1, "message": "oops\nlong detail",
                  "range": {"start": {"line": 4}}, "source": "pylsp"}]
        assert _render_diags(diags) == "error line 5 (pylsp): oops"


class TestApprovalModeToggleRpc:
    """The composer-foot button calls config.set approval_mode — that RPC must
    flip the LIVE ApprovalManager (not just the persisted config), auto-approve
    risky tools in yolo, and still gate destructive bash in yolo."""

    def _ctx(self, app, data_home: Path) -> ToolContext:
        return ToolContext(
            data_home=data_home, session_id="s", turn_id="t", cwd=str(data_home),
            agent=None, events=None, approvals=app.approvals, providers=None,
            memory=None, skills=None, flat_plugins=None, config={},
        )

    def test_toggle_flips_live_manager_and_gates(self, data_home):
        from xu_brain.core.runtime import build_app
        app = build_app(data_home)
        ctx = self._ctx(app, data_home)

        async def emit(event, **kw):
            pass

        async def main():
            # yolo via the button's exact RPC
            await app.dispatch("config.set", {"key": "approval_mode", "value": "yolo"})
            assert app.approvals.mode == "yolo"
            res = await app.registry.run(
                "write", {"path": "y.txt", "content": "1"}, ctx, emit=emit)
            assert not res.error and not app.approvals._pending  # auto-approved

            # /dev/null stderr redirect is NOT destructive — auto-approves in yolo
            task = asyncio.create_task(app.registry.run(
                "bash", {"command": "ls; cat x 2>/dev/null | head"}, ctx, emit=emit))
            await asyncio.sleep(0.3)
            assert not app.approvals._pending, "2>/dev/null must not gate in yolo"
            await task

            # truncating redirect to /tmp — no longer always-gated (dev logs)
            task = asyncio.create_task(app.registry.run(
                "bash", {"command": "npx expo start > /tmp/expo.log 2>&1"},
                ctx, emit=emit))
            await asyncio.sleep(0.3)
            assert not app.approvals._pending, "> /tmp log redirect must not gate in yolo"
            task.cancel()

            # rm no longer always-gates (user preference) — runs silently in yolo
            task = asyncio.create_task(app.registry.run(
                "bash", {"command": "rm -rf /tmp/xu-approval-test"}, ctx, emit=emit))
            await asyncio.sleep(0.3)
            assert not app.approvals._pending, "rm must not gate in yolo anymore"
            await task

            # genuinely destructive bash still escalates to ALWAYS even in yolo
            task = asyncio.create_task(app.registry.run(
                "bash", {"command": "mkfs.ext4 /dev/null"}, ctx, emit=emit))
            await asyncio.sleep(0.3)
            pending = list(app.approvals._pending)
            assert pending, "destructive bash must stay gated in yolo"
            app.approvals.resolve(pending[0], False)
            res = await task
            assert res.error and "denied" in res.output

            # back to manual prompts again
            await app.dispatch("config.set", {"key": "approval_mode", "value": "manual"})
            assert app.approvals.mode == "manual"

        asyncio.run(main())

    def test_invalid_mode_rejected(self, data_home):
        from xu_brain.core.runtime import build_app
        from xu_brain.core.contract import RpcError
        app = build_app(data_home)
        with pytest.raises(RpcError):
            asyncio.run(app.dispatch("config.set",
                                     {"key": "approval_mode", "value": "sudo"}))


class TestCompactEngine:
    """Shared compaction core: auto (_maybe_compress) and manual (compress_session)
    both route through _compact_engine - one cut-point rule, one summarizer,
    one ok/error contract."""

    def _agent(self, summary_text: str = "summary out"):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import Message, Session

        agent = Agent(None, None, None, None, None, None, None, None, None, None)

        def fake_stream(_provider, _model, _msgs, signal=None, max_tokens=None):
            async def gen():
                if summary_text:
                    yield SimpleNamespace(delta=summary_text, reasoning="")
                else:
                    yield SimpleNamespace(delta="", reasoning="")
            return gen()

        agent.providers = SimpleNamespace(
            resolve=lambda _model, _p=None: ("provider", "model"),
            chat_stream=fake_stream,
        )
        agent._session_model = lambda _sid: "some-model"
        agent.config = SimpleNamespace(
            get=lambda _key, default=None: default,
        )
        rows = [Message(role="user", content=f"q{i}") if i % 2 == 0
                else Message(role="assistant", content=f"a{i}") for i in range(1, 13)]
        sess = Session(id="s1", title="t", cwd=".", messages=rows)
        from xu_brain.core import notify as _ev
        self._old_emit = _ev.notify.emit
        _ev.notify.emit = _noop_bus
        return agent, sess

    def test_manual_compress_shapes(self):
        import asyncio
        agent, sess = self._agent()
        agent.sessions = SimpleNamespace(get=lambda sid: sess, add_display=lambda *a, **k: None, replace_messages=lambda sid, msgs: None)
        r = asyncio.run(agent.compress_session("s1"))
        assert r["ok"] is True
        assert r["dropped"] >= 1
        assert r["before"] > 0
        assert r["after"] < r["before"] + 200  # header+summary bounded, not math-exact

    def test_not_enough_history(self):
        import asyncio
        agent, sess = self._agent()
        agent.sessions = SimpleNamespace(get=lambda sid: sess)
        sess.messages = [sess.messages[0]]
        r = asyncio.run(agent.compress_session("s1"))
        assert r["ok"] is False
        assert "not enough history" in r["error"]

    def test_empty_summary_is_error(self):
        import asyncio
        agent, sess = self._agent(summary_text="")
        agent.sessions = SimpleNamespace(
            get=lambda sid: sess, add_display=lambda *a, **k: None, replace_messages=lambda sid, msgs: None
        )
        r = asyncio.run(agent.compress_session("s1"))
        assert r["ok"] is False
        assert "empty summary" in r["error"]

    def test_auto_compacts_in_place_from_threshold(self):
        import asyncio
        from xu_brain.core.notify import notify

        agent, sess = self._agent()
        agent.sessions = SimpleNamespace(
            add_display=lambda *a, **k: None, replace_messages=lambda sid, msgs: sess.__dict__.__setitem__("messages", msgs),
        )
        agent.config.get = lambda _key, default=None: 5 if _key == "context_length" else default  # ctx_len=5 -> gate passes
        messages = [
            {"role": "system", "content": "PERSONA"},
        ] + [m.to_dict() for m in sess.messages]

        emitted = []
        asyncio.run(agent._maybe_compress(sess, messages, None, None, asyncio.Event()))

        # in-place: system persona stays first, recent tail follows,
        # the summary checkpoint lands last (chronological, like any message)
        assert messages[0]["role"] == "system"
        assert messages[-1]["role"] == "system"
        assert "[conversation summary]" in messages[-1]["content"]
        # every row between is user/assistant (no tools, no persona dup)
        tail = messages[1:-1]
        assert tail and all(m["role"] in ("user", "assistant") for m in tail)
        # persisted = the same kept tail, then the checkpoint (dropped older rows)
        persisted_tail = sess.messages[:-1]
        assert persisted_tail and persisted_tail[0].role == "user"
        assert sess.messages[-1].role == "system"
        assert [m["role"] for m in tail] == [m.role for m in persisted_tail]

    def test_tool_heavy_live_turn_keeps_a_user_boundary(self):
        """Regression: the auto path passes the *live* message list, whose tail
        in a long tool-heavy turn is all assistant/tool rows. The forward snap
        to a user boundary then ran off the end, leaving `recent` empty and
        wiping the conversation (auto broken, manual fine — manual only ever
        sees persisted user/assistant rows). The cut must fall back to the last
        user row."""
        import asyncio

        agent, sess = self._agent()
        agent.sessions = SimpleNamespace(
            add_display=lambda *a, **k: None, replace_messages=lambda sid, msgs: sess.__dict__.__setitem__("messages", msgs),
        )
        agent.config.get = lambda _key, default=None: 5 if _key == "context_length" else default
        # No user row after the first: pure tool-round tail.
        messages = [
            {"role": "system", "content": "PERSONA"},
            {"role": "user", "content": "do the thing"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c0"}]},
            {"role": "tool", "tool_call_id": "c0", "content": "T" * 4000},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "U" * 4000},
        ]
        asyncio.run(agent._maybe_compress(sess, messages, None, None, asyncio.Event()))
        # Either it declined to compact, or it kept a real tail — never system-only.
        assert len(messages) > 2, "compaction wiped the conversation"
        tail = [m for m in messages if m["role"] != "system"]
        assert tail, "no conversation rows survived compaction"
        assert tail[0]["role"] == "user", f"tail must start at a user boundary, got {tail[0]['role']}"

    def test_cut_point_never_returns_empty_tail(self):
        """_compact_engine must never report ok with an empty `recent`: the
        caller splices it into the live list, so an empty tail means the next
        provider call has no user turn to answer."""
        import asyncio

        agent, _sess = self._agent()
        entries = [
            {"role": "user", "content": "only user turn"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c0"}]},
            {"role": "tool", "tool_call_id": "c0", "content": "T" * 8000},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "U" * 8000},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c2"}]},
            {"role": "tool", "tool_call_id": "c2", "content": "V" * 8000},
        ]
        r = asyncio.run(agent._compact_engine("s1", entries))
        if r.get("ok"):
            assert r["recent"], "ok=True with an empty tail"
            assert r["recent"][0]["role"] == "user"

    def test_auto_compaction_persists_live_rows_without_ts(self):
        """Regression: the auto path hands _persisted_msgs *live* dicts, which
        carry no `ts`/`steps`. `m.get("ts")` returned None and overrode
        Message's 0.0 default, so replace_messages died on
        `NOT NULL constraint failed: messages.ts` — auto-compaction blew up
        where manual (persisted rows always have a ts) did not."""
        rows = self._agent()[0]._persisted_msgs(
            [{"role": "user", "content": "live row, no ts key"},
             {"role": "assistant", "content": "reply"}],
            "summary text",
        )
        assert [m.role for m in rows] == ["user", "assistant", "system"]
        assert all(isinstance(m.ts, float) for m in rows), "ts must never be None"
        assert all(isinstance(m.steps, list) for m in rows), "steps must never be None"


class TestCompactEngineDSH:
    """Context engineering: KV-cache-aware replay, model-free tool pruning,
    token-fraction retention, shrink validation/retry, and <compacted-summary>
    framing."""

    def _agent(self, summary_text: str = "summary out", *, fail_first: bool = False):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import Message, Session
        import types

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        streamed = []

        def fake_stream(_provider, _model, _msgs, signal=None, max_tokens=None):
            async def gen():
                if fail_first and not streamed:
                    streamed.append(1)
                    raise RuntimeError("transient provider failure")
                if summary_text:
                    yield types.SimpleNamespace(delta=summary_text, reasoning="")
            return gen()

        agent.providers = types.SimpleNamespace(
            resolve=lambda _model, _p=None: ("provider", "model"),
            chat_stream=fake_stream,
        )
        agent._session_model = lambda _sid: "some-model"
        agent.config = types.SimpleNamespace(
            get=lambda _key, default=None: default,
        )
        rows = [Message(role="user", content=f"q{i}") if i % 2 == 0
                else Message(role="assistant", content=f"a{i}") for i in range(13)]
        sess = Session(id="s1", title="t", cwd=".", messages=rows)
        return agent, sess

    def test_summary_replays_system_and_appends_instruction(self):
        """KV-cache-aware summarization: the search must include a system role
        and a trailing compaction-instruction user message (not a dumped JSON)."""
        import asyncio
        agent, sess = self._agent()
        captured = {}
        def fake_stream(_provider, _model, msgs, signal=None, max_tokens=None):
            captured["msgs"] = list(msgs)
            async def gen():
                yield SimpleNamespace(delta="SUMMARY", reasoning="")
            return gen()
        agent.providers.chat_stream = fake_stream
        agent.sessions = SimpleNamespace(
            get=lambda sid: sess, add_display=lambda *a, **k: None, replace_messages=lambda sid, msgs: None
        )
        asyncio.run(agent.compress_session("s1"))
        msgs = captured["msgs"]
        # a system-prompt role was replayed (KV-reuse) and the final message is
        # the compaction instruction, both absent from the old bare dump.
        assert msgs[0]["role"] == "system"
        assert msgs[-1]["role"] == "user"
        assert "conversation so far" not in msgs[-1]["content"].lower()  # old prompt removed
        assert "acting as a compaction engine" in msgs[-1]["content"]
        # the schema is mandatory, not advisory: every section must be named
        for section in ("## Primary Request and Intent", "## Files and Code",
                        "## Errors and Fixes", "## Next Step", "## Critical Context"):
            assert section in msgs[-1]["content"], section
        # and a prior checkpoint must be merged rather than stacked
        assert "PRIOR" in msgs[-1]["content"]

    def test_shadowed_region_is_pruned_before_summary(self):
        """Model-free tool pruning: an oversized tool result in the shadowed
        region is folded to head/marker/tail before the summarizer sees it."""
        import asyncio
        from xu_brain.features.agent.compaction import _prune_content
        from xu_brain.features.session import Message
        agent, sess = self._agent()
        big = "x" * 20000
        # insert an oversized AI output early so it lands in the shadowed region
        sess.messages.insert(1, Message(role="assistant", content=big))
        from types import SimpleNamespace as SNS
        captured = {}
        def fake_stream(_provider, _model, msgs, signal=None, max_tokens=None):
            captured["msgs"] = list(msgs)
            async def gen():
                yield SNS(delta="SUM", reasoning="")
            return gen()
        agent.providers.chat_stream = fake_stream
        agent.sessions = SNS(get=lambda sid: sess, add_display=lambda *a, **k: None, replace_messages=lambda sid, msgs: None)
        asyncio.run(agent.compress_session("s1"))
        shadowed = [m for m in captured["msgs"][1:-1]]
        assert any("tool result middle pruned" in m["content"] for m in shadowed if isinstance(m.get("content"), str))
        # _prune_content unit check
        assert len(_prune_content(big)) < 6000

    def test_shrink_validation_retries_when_not_smaller(self):
        """A summary that doesn't shrink the surface is retried (until it
        converges) and abandoned after the retry budget."""
        import asyncio
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import Message, Session
        from types import SimpleNamespace as SNS

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        calls = {"n": 0}
        def fake_stream(_p, _m, msgs, signal=None, max_tokens=None):
            calls["n"] += 1
            # returns a huge summary so "after" never < "before"
            payload = "S" * 100000
            async def gen():
                yield SNS(delta=payload, reasoning="")
            return gen()
        agent.providers = SNS(resolve=lambda m, _p=None: ("p", "m"), chat_stream=fake_stream)
        agent._session_model = lambda sid: "m"
        agent.config = SNS(get=lambda k, d=None: d)  # compaction_retries default 1
        rows = [Message(role="user" if i % 2 == 0 else "assistant", content=f"q{i}") for i in range(13)]
        sess = Session(id="s1", title="t", cwd=".", messages=rows)
        agent.sessions = SNS(get=lambda sid: sess, add_display=lambda *a, **k: None, replace_messages=lambda sid, msgs: None)
        r = asyncio.run(agent.compress_session("s1"))  # require_shrink=False -> lands anyway
        # verify the retry path exists for auto: call the engine with require_shrink
        inner = asyncio.run(agent._compact_engine("s1", [m.to_dict() for m in rows]))
        assert inner["ok"] is False
        assert "did not shrink" in inner["error"]

    def test_compact_summary_gets_compacted_framing(self):
        """Persisted summary carries <compacted-summary> tags so established
        context is marked as compacted background."""
        import asyncio
        agent, sess = self._agent()
        agent.sessions = SimpleNamespace(get=lambda sid: sess, add_display=lambda *a, **k: None, replace_messages=lambda sid, msgs: None)
        asyncio.run(agent.compress_session("s1"))
        # _persisted_msgs framing
        from xu_brain.features.agent.loop import Agent
        recent = [{"role": "user", "content": "last"}]
        rows = Agent._persisted_msgs(recent, "SENSE")
        assert "<compacted-summary>" in rows[-1].content
        assert "</compacted-summary>" in rows[-1].content

    def test_empty_summary_manual_is_error(self):
        import asyncio
        agent, sess = self._agent(summary_text="")
        agent.sessions = SimpleNamespace(get=lambda sid: sess, add_display=lambda *a, **k: None, replace_messages=lambda sid, msgs: None)
        r = asyncio.run(agent.compress_session("s1"))
        assert r["ok"] is False
        assert "empty summary" in r["error"]


class TestCompactionTranscriptRows:
    """Compaction visibility: the outcome lands as a persisted `system` row so it
    survives a reload and a session switch, is never replayed to the model, and a
    repeating failure coalesces onto one row instead of one row per turn."""

    def _agent(self):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import Message, Session
        from types import SimpleNamespace as SNS

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        rows = [Message(role="user", content=f"q{i}") if i % 2 == 0
                else Message(role="assistant", content=f"a{i}") for i in range(13)]
        sess = Session(id="s1", title="t", cwd=".", messages=rows)

        def _append(sid, msg):
            sess.messages.append(msg)

        def _replace(sid, msgs):
            sess.messages = list(msgs)

        agent.sessions = SNS(get=lambda sid: sess, append=_append, replace_messages=_replace,
                             add_display=lambda sid, msgs: None)
        agent.config = SNS(get=lambda _key, default=None: default,
                           resolve_persona_text=lambda sid: None,
                           session_rules=lambda sid: [])
        return agent, sess

    def test_success_row_carries_size_stats(self):
        """The divider's numbers live on the row, not on an event: a client that
        reconnects later must still be able to render them."""
        from xu_brain.features.agent.loop import Agent
        stats = Agent._compaction_step(ok=True, mode="manual", before=900, after=120, dropped=7)
        rows = Agent._persisted_msgs([{"role": "user", "content": "last"}], "SUMMARY", stats)
        step = rows[-1].steps[0]
        assert step["kind"] == "compaction"
        assert (step["ok"], step["before"], step["after"], step["dropped"]) == (True, 900, 120, 7)

    def test_benign_errors_stay_out_of_the_transcript(self):
        """Auto-compaction re-fires every turn while over threshold, so a "nothing
        to do" result must not append a row per turn."""
        import asyncio
        from xu_brain.features.agent.checkpoint import _BENIGN_COMPACTION_ERRORS
        agent, sess = self._agent()
        before = len(sess.messages)
        for err in _BENIGN_COMPACTION_ERRORS:
            asyncio.run(agent._report_compaction("s1", "auto", {"ok": False, "error": err}))
        assert len(sess.messages) == before

    def test_real_failure_appends_one_row_then_coalesces(self):
        import asyncio
        agent, sess = self._agent()
        before = len(sess.messages)
        for _ in range(4):
            asyncio.run(agent._report_compaction("s1", "auto", {"ok": False, "error": "boom"}))
        assert len(sess.messages) == before + 1  # coalesced, not 4 rows
        note = sess.messages[-1]
        assert note.role == "system"
        assert note.content.startswith("[compaction failed]")
        assert note.steps[0]["count"] == 4
        # a *different* error is its own row
        asyncio.run(agent._report_compaction("s1", "auto", {"ok": False, "error": "other"}))
        assert len(sess.messages) == before + 2

    def test_failure_row_is_never_replayed_to_the_model(self):
        """The note is display-only; leaking it into the provider history would
        make the model talk about its own compaction plumbing."""
        import asyncio
        agent, sess = self._agent()
        from types import SimpleNamespace as SNS
        agent.skills = SNS(list=lambda: [], load=lambda sid: "", match=lambda text: [])
        agent.memory = SNS(reflect=lambda: None)
        agent.data_home = __import__("pathlib").Path("/nonexistent")
        sent = agent._build_messages(sess)
        assert not any("[compaction failed]" in str(m.get("content")) for m in sent)

    def test_failure_note_survives_a_later_turn_commit(self):
        """`_commit_history` rewrites the whole store from `result.history`, which
        never contains the note (it isn't replayed) — so it must be spliced back
        or the divider vanishes on the next turn."""
        from xu_brain.features.agent.loop import Agent, TurnResult
        agent, sess = self._agent()
        import asyncio
        asyncio.run(agent._report_compaction("s1", "auto", {"ok": False, "error": "boom"}))
        history = [{"role": "user", "content": "q"}, {"role": "assistant", "content": "a"}]
        agent._commit_history(sess, TurnResult(text="a", steps=[], history=history), "a")
        assert any(Agent._is_failure_note(m) for m in sess.messages)

    def test_checkpoint_stats_survive_a_later_turn_commit(self):
        """Same rewrite hazard for the success row: the live list carries only the
        checkpoint's content, so its stats must be restored from the store."""
        from xu_brain.features.agent.loop import Agent, TurnResult
        from xu_brain.features.session import Message
        agent, sess = self._agent()
        content = Agent._frame_summary("SUMMARY")
        stats = Agent._compaction_step(ok=True, mode="auto", before=900, after=120, dropped=7)
        sess.messages = [Message(role="system", content=content, steps=[stats]),
                         Message(role="user", content="q")]
        history = [{"role": "system", "content": content},
                   {"role": "user", "content": "q"},
                   {"role": "assistant", "content": "a"}]
        agent._commit_history(sess, TurnResult(text="a", steps=[], history=history), "a")
        ckpt = next(m for m in sess.messages if Agent._is_checkpoint(m))
        assert ckpt.steps and ckpt.steps[0]["before"] == 900

    def test_successful_compaction_clears_stale_failure_notes(self):
        """The failure they reported on is resolved; keeping them would leave a
        permanent red divider in a healthy session."""
        from xu_brain.features.agent.loop import Agent
        recent = [
            {"role": "system", "content": "[compaction failed] boom"},
            {"role": "user", "content": "last"},
        ]
        rows = Agent._persisted_msgs(recent, "SUMMARY")
        assert not any(Agent._is_failure_note(m) for m in rows)

    def test_note_failure_never_raises_through_the_turn(self):
        """A store that can't take the display row must not convert a compaction
        miss into a failed turn."""
        import asyncio
        from types import SimpleNamespace as SNS
        agent, _sess = self._agent()
        agent.sessions = SNS()  # no .get/.append at all
        asyncio.run(agent._report_compaction("s1", "auto", {"ok": False, "error": "boom"}))


class TestContextBudget:
    """Progressive-disclosure budget: skill/memory bodies injected
    into the system prompt are capped; auto-matched (priority) content wins over
    ambient loaded skills under a tight window."""

    def _agent(self, budget: int):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import Session
        from types import SimpleNamespace as SNS

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        big = "y" * 5000
        agent.config = SNS(
            get=lambda key, default=None: budget if key == "context_skill_budget" else default,
            resolve_persona_text=lambda sid: None,
            session_rules=lambda sid: [],
        )
        agent.data_home = __import__("pathlib").Path("/nonexistent")
        # two LOADED skills, one oversized
        agent.skills = SNS(
            list=lambda: [{"state": "LOADED", "id": "a"}, {"state": "LOADED", "id": "b"}],
            load=lambda sid: f"# skill {sid}\n" + big,
        )
        agent.memory = SNS(reflect=lambda: "# memory\nmem")
        sess = Session(id="s1", title="t", cwd=".", messages=[])
        return agent, sess

    def test_budget_trims_ambient_sections(self):
        agent, sess = self._agent(budget=6000)
        msgs = agent._build_messages(sess)
        system = msgs[0]["content"]
        # the two skills + memory each cost ~5000 > 6000 budget, so only the
        # first fits; later ambient sections are dropped.
        assert system.count("# skill") >= 1
        assert len(system) <= 6000 + 1000  # budgeted, not full concatenation
        # huge concatenation without budget would be ~15000 (2×5000 + memory + persona)
        unbudgeted = self._agent(budget=0)
        _, sess2 = unbudgeted
        sys2 = unbudgeted[0]._build_messages(sess2)[0]["content"]
        assert len(sys2) > 9000  # no cap -> multiple 5000-char section fit

    def test_auto_priority_wins_when_budget_small(self):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import Session
        from types import SimpleNamespace as SNS

        # auto-matched skill + big ambient loaded skill; tiny budget fits only auto
        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        agent.config = SNS(
            get=lambda key, default=None: 120 if key == "context_skill_budget" else default,
            resolve_persona_text=lambda sid: None,
            session_rules=lambda sid: [],
        )
        agent.data_home = __import__("pathlib").Path("/nonexistent")
        agent.skills = SNS(
            list=lambda: [{"state": "LOADED", "id": "ambient"}],
            load=lambda sid: f"# skill: {sid}\n" + "z" * 300,
        )
        # auto-match returns 'auto-skill' which is small
        def fake_match(session):
            return ["auto-skill"]
        agent._match_recent = fake_match
        def small_load(sid):
            return f"# skill (auto): {sid}\n{ 'k'*20 }"
        agent.skills.load = small_load
        agent.memory = SNS(reflect=lambda: None)
        sess = Session(id="s1", title="t", cwd=".", messages=[])
        msgs = agent._build_messages(sess)
        system = msgs[0]["content"]
        assert "# skill (auto): auto-skill" in system
        assert "# skill: ambient" not in system  # ambient trimmed by budget


class TestUsageAnchoring:
    """Provider-reported usage anchors the context
    percentage and the auto-compaction gate; the chars/4 heuristic is the
    fallback only when the provider never reports usage."""

    def _agent(self, *, last_usage=None):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import Session
        from types import SimpleNamespace as SNS

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        if last_usage is not None:
            agent._last_usage["s1"] = last_usage
        agent.config = SNS(
            get=lambda key, default=None: default,
        )
        sess = Session(id="s1", title="t", cwd=".", messages=[])
        return agent, sess

    def test_emit_context_calibrates_to_provider_usage(self):
        import asyncio
        agent, sess = self._agent()
        agent.config = type("C", (), {"get": lambda self, key, default=None:
                                      {"context_length": 100_000,
                                       "compress_threshold": 0.94}.get(key, default)})()
        captured = {}
        from xu_brain.core import notify as _ev
        async def fake_emit(event, **kw):
            captured[event] = kw
        _ev.notify.emit = fake_emit
        # Surface: 20000 chars -> ~5000 heuristic tokens. The provider says the
        # full envelope cost 6000 prompt_tokens: that is the anchor, and growth
        # since the sample is priced with the surface ruler on top of it.
        messages = [{"role": "user", "content": "x" * 20000}]  # transcript surface ~5000 tokens
        asyncio.run(agent._emit_context(sess, messages, {"prompt_tokens": 6000}))
        ev = captured["context.updated"]
        assert ev["calibrated"] is True
        assert ev["tokens"] == 6000 and ev["pressure"] == 6000
        assert ev["usage_pct"] == 6  # 6000/100000
        assert ev["compress"] is False
        # Growth since the sample adds to the anchor.
        messages.append({"role": "user", "content": "y" * 4000})  # +1000 tokens
        asyncio.run(agent._emit_context(sess, messages))
        assert captured["context.updated"]["tokens"] == 7000
        # A compacted surface shrinks the estimate below the anchor — no stale
        # zero-clamp that would pin the meter at pre-compaction values.
        messages[:] = [{"role": "user", "content": "x" * 12000}]  # surface 3000
        asyncio.run(agent._emit_context(sess, messages))
        assert captured["context.updated"]["tokens"] == 4000  # 6000 + (3000 - 5000)

    def test_emit_context_heuristic_without_usage(self):
        import asyncio
        agent, sess = self._agent()
        captured = {}
        from xu_brain.core import notify as _ev
        async def fake_emit(event, **kw):
            captured[event] = kw
        _ev.notify.emit = fake_emit
        messages = [{"role": "user", "content": "x" * 20000}]  # ~5000 tokens heuristic
        asyncio.run(agent._emit_context(sess, messages, {}))
        # 5000/128000 ~ 3.9%, and unanchored: no API sample yet.
        ev = captured["context.updated"]
        assert 0 < ev["usage_pct"] < 10
        assert ev["calibrated"] is False
        assert ev["pressure"] is None and ev["projected"] is None

    def test_compact_gate_fires_on_calibrated_estimate(self):
        import asyncio as aio
        # The provider's real prompt_tokens lifts the estimate past the
        # threshold even though the surface heuristic alone sits far below it.
        agent, sess = self._agent(
            last_usage={"prompt_tokens": 90_000, "surface_at_sample": 2500})
        messages = [{"role": "user", "content": "y" * 10000}]  # transcript surface ~2500
        # calibrated est = 90000 + 0 = 90000 -> 70% of 128000 >= 0.60
        called = {"n": 0}
        async def fake_compact(*a, **k):
            called["n"] += 1
            return {"ok": False, "error": "sentinel"}
        agent._compact_engine = fake_compact
        aio.run(agent._maybe_compress(sess, messages, None, None, aio.Event()))
        assert called["n"] == 1

    def test_compact_gate_no_fire_when_real_usage_is_low(self):
        import asyncio as aio
        # Surface heuristic alone would read high, but the real API number is
        # low — the calibrated gate must not fire.
        agent, sess = self._agent(
            last_usage={"prompt_tokens": 10_000, "surface_at_sample": 2500})
        messages = [{"role": "user", "content": "y" * 10000}]
        # calibrated est = 10000 + 0 -> 7.8% of 128000 < 0.60
        called = {"n": 0}
        async def fake_compact(*a, **k):
            called["n"] += 1
            return {"ok": False, "error": "sentinel"}
        agent._compact_engine = fake_compact
        aio.run(agent._maybe_compress(sess, messages, None, None, aio.Event()))
        assert called["n"] == 0

    def test_compact_gate_heuristic_without_usage(self):
        import asyncio as aio
        agent, sess = self._agent()  # no last usage
        # tiny ctx so heuristic blows past threshold
        agent.config = type("C", (), {"get": lambda self, key, default=None: 5 if key == "context_length" else default})()
        # transient system rows are not shrinkable, so the heuristic prices
        # the transcript only — give it one big user row to blow past ctx_len=5
        messages = [{"role": "system", "content": "y" * 10}] * 100 \
            + [{"role": "user", "content": "z" * 40}]  # ~10 tokens >> 0.6*5
        called = {"n": 0}
        async def fake_compact(*a, **k):
            called["n"] += 1
            return {"ok": False, "error": "sentinel"}
        agent._compact_engine = fake_compact
        aio.run(agent._maybe_compress(sess, messages, None, None, aio.Event()))
        assert called["n"] == 1  # heuristic pressure triggered compaction attempt

class TestBrowserToolset:
    """browser toolset: agent-owned headless Chrome/CDP. Degradation tests
    point at an empty PATH and no XU_BROWSER_BIN so a real Chrome on the dev
    machine cannot satisfy them, and reset the process singleton so a spawn
    leaked by an earlier test does not short-circuit ensure()."""

    @pytest.fixture(autouse=True)
    def _no_binary_env(self, monkeypatch):
        from xu_brain.features.tools.browser import BrowserManager
        monkeypatch.delenv("XU_BROWSER_BIN", raising=False)
        monkeypatch.delenv("XU_BROWSER_CDP_ENDPOINT", raising=False)
        BrowserManager._proc = None
        BrowserManager._port = None
        BrowserManager._endpoint = None
        BrowserManager._udd = None
        BrowserManager._ua = None
        yield
        BrowserManager._proc = None
        BrowserManager._port = None
        BrowserManager._endpoint = None
        BrowserManager._udd = None
        BrowserManager._ua = None

    @pytest.mark.asyncio
    async def test_browse_without_binary_errors_clearly(self, monkeypatch):
        from xu_brain.features.tools.browser import browse
        monkeypatch.delenv("XU_BROWSER_BIN", raising=False)
        monkeypatch.setenv("PATH", "")
        ctx = SimpleNamespace(config={}, cwd="/tmp")
        r = await browse.run({"url": "https://example.com"}, ctx)
        assert r.error and "chrome" in r.output.lower()

    @pytest.mark.asyncio
    async def test_screenshot_without_binary_errors_clearly(self, monkeypatch):
        from xu_brain.features.tools.browser import screenshot
        monkeypatch.delenv("XU_BROWSER_BIN", raising=False)
        monkeypatch.setenv("PATH", "")
        ctx = SimpleNamespace(config={}, cwd="/tmp")
        r = await screenshot.run({"url": "https://example.com"}, ctx)
        assert r.error and "chrome" in r.output.lower()

    def test_binary_resolution(self, monkeypatch, tmp_path):
        from xu_brain.features.tools.browser import BrowserManager
        # No env, empty PATH → None (never auto-installs).
        monkeypatch.delenv("XU_BROWSER_BIN", raising=False)
        monkeypatch.setenv("PATH", "")
        assert BrowserManager.binary() is None
        # A nonexistent override → None.
        monkeypatch.setenv("XU_BROWSER_BIN", "/definitely/not/chrome")
        assert BrowserManager.binary() is None
        # A real file override wins.
        real = tmp_path / "chrome"
        real.write_text("#!/bin/sh\n")
        real.chmod(0o755)
        monkeypatch.setenv("XU_BROWSER_BIN", str(real))
        assert BrowserManager.binary() == str(real)
        # PATH fallback finds a candidate.
        monkeypatch.delenv("XU_BROWSER_BIN", raising=False)
        monkeypatch.setenv("PATH", str(tmp_path))
        assert BrowserManager.binary() == str(real)

    @pytest.mark.asyncio
    async def test_connect_mode_adopts_external_endpoint(self, monkeypatch):
        from xu_brain.features.tools.browser import BrowserManager
        monkeypatch.setenv("XU_BROWSER_CDP_ENDPOINT", "ws://127.0.0.1:9999/devtools/browser")
        ep = await BrowserManager.ensure()
        assert ep == "ws://127.0.0.1:9999/devtools/browser"
        assert BrowserManager._proc is None  # never spawned in connect mode

    def test_looks_like_shell(self):
        from xu_brain.features.tools.web import _looks_like_shell
        assert _looks_like_shell("")
        assert _looks_like_shell("<div id=root></div>")
        assert not _looks_like_shell(
            "This is a real article body with more than enough substance to "
            "comfortably exceed the short-shell heuristic threshold of two "
            "hundred characters, and it actually contains meaningful readable "
            "content that spans several sentences and clauses of real text."
        )

    def test_browser_available(self, monkeypatch, tmp_path):
        from xu_brain.features.tools.web import _browser_available
        monkeypatch.delenv("XU_BROWSER_BIN", raising=False)
        monkeypatch.delenv("XU_BROWSER_CDP_ENDPOINT", raising=False)
        monkeypatch.setenv("PATH", "")
        # No binary, no endpoint → unavailable.
        assert _browser_available() is False
        # An externally-launched browser endpoint counts as available.
        monkeypatch.setenv("XU_BROWSER_CDP_ENDPOINT", "ws://127.0.0.1:1")
        assert _browser_available() is True
        # A nonexistent override still unavailable.
        monkeypatch.delenv("XU_BROWSER_CDP_ENDPOINT", raising=False)
        monkeypatch.setenv("XU_BROWSER_BIN", "/definitely/not/chrome")
        assert _browser_available() is False

    def test_dehead_strips_headless_marker_only(self):
        """The UA fix: Cloudflare answers HeadlessChrome with a challenge page,
        so the marker is dropped while the real Chrome version is kept — no
        pinned UA string to rot on the next Chrome release."""
        from xu_brain.features.tools.browser import _dehead
        headless = (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like "
            "Gecko) HeadlessChrome/148.0.0.0 Safari/537.36"
        )
        assert _dehead(headless) == headless.replace("HeadlessChrome/", "Chrome/")
        assert "Headless" not in _dehead(headless)
        # Nothing to strip → None, meaning "no override needed". Keeps connect
        # mode (a user's own Chrome) from being touched at all.
        assert _dehead("Mozilla/5.0 Chrome/148.0.0.0 Safari/537.36") is None
        assert _dehead("") is None
        assert _dehead(None) is None

    def test_ua_override_absent_until_learned(self):
        """ua() is None before a spawn, so connect mode never gets an override."""
        from xu_brain.features.tools.browser import BrowserManager
        assert BrowserManager.ua() is None

    def test_pid_alive(self):
        from xu_brain.features.tools.browser import _pid_alive
        assert _pid_alive(os.getpid()) is True
        assert _pid_alive(0) is False
        assert _pid_alive(-1) is False
        # Above the default pid_max, so it cannot name a live process.
        assert _pid_alive(2 ** 22) is False

    def test_kill_group_without_a_group_is_a_no_op(self):
        """Returns False so the caller falls back to the parent handle —
        the Windows path, and the already-dead path."""
        from xu_brain.features.tools.browser import _kill_group
        assert _kill_group(None, signal.SIGTERM) is False
        assert _kill_group(2 ** 22, signal.SIGTERM) is False

    def _profile(self, root, name, owner=None, age=0.0):
        """Build a fake profile dir, optionally with an owner marker."""
        from xu_brain.features.tools.browser import _OWNER_FILE, _PROFILE_PREFIX
        d = root / f"{_PROFILE_PREFIX}{name}"
        d.mkdir()
        if owner is not None:
            (d / _OWNER_FILE).write_text(json.dumps(owner), encoding="utf-8")
        if age:
            old = time.time() - age
            os.utime(d, (old, old))
        return d

    def test_sweep_reaps_only_provable_orphans(self, monkeypatch, tmp_path):
        """shutdown() cannot run when the brain is SIGKILLed, so recovery
        happens on the way up. It must not touch a live brain's profile."""
        from xu_brain.features.tools.browser import BrowserManager, _ORPHAN_GRACE
        monkeypatch.setattr(BrowserManager, "_profiles_root", staticmethod(lambda: tmp_path))
        dead = self._profile(tmp_path, "dead", owner={"brain_pid": 2 ** 22, "pgid": None})
        mine = self._profile(tmp_path, "mine", owner={"brain_pid": os.getpid(), "pgid": None})
        fresh = self._profile(tmp_path, "fresh")
        stale = self._profile(tmp_path, "stale", age=_ORPHAN_GRACE + 60)
        unrelated = tmp_path / "not-a-profile"
        unrelated.mkdir()

        reaped = BrowserManager._sweep_orphans()

        assert not dead.exists()      # owner provably gone
        assert not stale.exists()     # unmarked and past the grace window
        assert mine.exists()          # a live brain still owns it
        assert fresh.exists()         # unmarked but too new to judge
        assert unrelated.exists()     # outside the prefix, never touched
        assert reaped == 2

    def test_sweep_keeps_the_current_profile(self, monkeypatch, tmp_path):
        """The profile this manager is about to use is never a sweep target,
        even though its marker is not written yet."""
        from xu_brain.features.tools.browser import BrowserManager, _ORPHAN_GRACE
        monkeypatch.setattr(BrowserManager, "_profiles_root", staticmethod(lambda: tmp_path))
        current = self._profile(tmp_path, "current", age=_ORPHAN_GRACE + 60)
        monkeypatch.setattr(BrowserManager, "_udd", current)
        assert BrowserManager._sweep_orphans() == 0
        assert current.exists()

    @pytest.mark.asyncio
    async def test_reap_kills_the_whole_group(self, tmp_path):
        """The leak, reproduced: a parent that ignores SIGTERM plus a separate
        child process. Signalling the parent handle alone kills the parent and
        orphans the child — that is exactly what the old teardown did. _reap
        finishes with an unconditional group SIGKILL, so nothing survives.

        The child pid comes from ``$!``, not ``$$`` inside a subshell: in POSIX
        sh ``$$`` stays the *parent's* pid inside a subshell, so a probe built
        on it silently watches the parent and can never fail.
        """
        from xu_brain.features.tools.browser import BrowserManager, _pid_alive
        marker = tmp_path / "child.pid"
        script = f"trap '' TERM; sleep 30 & echo $! > {marker}; sleep 30"
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh", "-c", script, start_new_session=True,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        child = 0
        try:
            for _ in range(60):  # wait for the child to announce itself
                if marker.exists() and marker.read_text().strip():
                    break
                await asyncio.sleep(0.05)
            child = int(marker.read_text().strip())
            assert _pid_alive(child), "fixture never started its child"
            await BrowserManager._reap(proc, proc.pid)
            assert proc.returncode is not None
            # SIGKILL delivery is asynchronous, so the assertion is "dies
            # promptly", not "is already dead" — the latter passes alone and
            # flakes under a loaded suite.
            for _ in range(100):
                if not _pid_alive(child):
                    break
                await asyncio.sleep(0.02)
            assert not _pid_alive(child), "child outlived the group kill — leak is back"
        finally:
            for pid, sig in ((proc.pid, signal.SIGKILL), (child, signal.SIGKILL)):
                try:
                    os.kill(pid, sig)
                except OSError:
                    pass


class TestCdpClient:
    """Runs a tiny in-process CDP server and drives CDPClient against it —
    validates the browser-level Target handshake without a Chrome binary."""

    @staticmethod
    async def _serve(handler_fn):
        import socket as _socket
        import websockets as _ws
        s = _socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        server = await _ws.serve(handler_fn, "127.0.0.1", port)
        return port, server

    @staticmethod
    def _handler_factory(events: list):
        async def handler(ws):
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:  # noqa: BLE001
                    continue
                mid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
                events.append(method)
                if method == "Target.createTarget":
                    out = {"id": mid, "result": {"targetId": "t-1"}}
                elif method == "Target.attachToTarget":
                    out = {"id": mid, "result": {"sessionId": "s-1"}}
                elif method == "Runtime.evaluate":
                    expr = params.get("expression", "")
                    if "readyState" in expr:
                        val = "complete"
                    elif expr.endswith(".length"):
                        # the settle poll asks for a text length, not the text
                        val = len("Doomrendered content")
                    elif "document.title" in expr:
                        val = "Doom"
                    else:
                        val = "Doomrendered content"
                    out = {"id": mid, "result": {"result": {"value": val}}}
                else:
                    out = {"id": mid, "result": {}}
                await ws.send(json.dumps(out))
        return handler

    @pytest.mark.asyncio
    async def test_navigate_and_evaluate_roundtrip(self):
        from xu_brain.features.tools.browser import CDPClient
        events: list = []
        port, server = await self._serve(self._handler_factory(events))
        try:
            client = CDPClient(f"ws://127.0.0.1:{port}")
            await client.connect()
            sid = await client.new_page()
            assert sid == "s-1"
            await client.navigate("https://example.com", sid)
            text = await client.evaluate_text(sid, html=False)
            assert text == "Doomrendered content"
            await client.detach_page(sid)
            await client.close()
        finally:
            server.close()
            await server.wait_closed()
        # the full expected browser-level CDP sequence happened
        seq = [e for e in events if e is not None]
        assert "Target.createTarget" in seq
        assert "Target.attachToTarget" in seq
        assert "Page.navigate" in seq
        assert "Target.detachFromTarget" in seq

    @pytest.mark.asyncio
    async def test_new_page_applies_ua_override_before_navigating(self, monkeypatch):
        """The override has to land on the page before the first request —
        a challenge page served on request one cannot be un-served later."""
        from xu_brain.features.tools.browser import BrowserManager, CDPClient
        monkeypatch.setattr(BrowserManager, "_ua", "Mozilla/5.0 Chrome/1.2.3")
        events: list = []
        port, server = await self._serve(self._handler_factory(events))
        try:
            client = CDPClient(f"ws://127.0.0.1:{port}")
            await client.connect()
            sid = await client.new_page()
            await client.navigate("https://example.com", sid)
            await client.close()
        finally:
            server.close()
            await server.wait_closed()
        assert "Emulation.setUserAgentOverride" in events
        assert events.index("Emulation.setUserAgentOverride") < events.index("Page.navigate")

    @pytest.mark.asyncio
    async def test_new_page_skips_override_when_ua_unset(self, monkeypatch):
        """Connect mode drives the user's own Chrome — never touch its UA."""
        from xu_brain.features.tools.browser import BrowserManager, CDPClient
        monkeypatch.setattr(BrowserManager, "_ua", None)
        events: list = []
        port, server = await self._serve(self._handler_factory(events))
        try:
            client = CDPClient(f"ws://127.0.0.1:{port}")
            await client.connect()
            await client.new_page()
            await client.close()
        finally:
            server.close()
            await server.wait_closed()
        assert "Emulation.setUserAgentOverride" not in events

class TestProviderEnabledAndPin:
    """provider.enabled gating + provider-qualified model resolution."""

    def _manager(self):
        from xu_brain.features.agent.provider import Provider, ProviderManager
        m = ProviderManager.__new__(ProviderManager)
        m._providers = {
            "a": Provider(id="a", type="openai-compatible", base_url="http://a", models=["m1", "m-shared"]),
            "b": Provider(id="b", type="openai-compatible", base_url="http://b", models=["m2", "m-shared"]),
        }
        return m

    def test_resolve_skips_disabled_providers(self):
        m = self._manager()
        m._providers["a"].enabled = False
        assert m.resolve("m1") is None  # only a has m1, and a is disabled
        p, mod = m.resolve("m-shared")
        assert p.id == "b"
        # no model -> first enabled provider with models
        p2, _ = m.resolve(None)
        assert p2.id == "b"

    def test_resolve_pins_provider_for_shared_model_id(self):
        m = self._manager()
        # both a and b expose m-shared; pinning routes to a
        p, mod = m.resolve("m-shared", "a")
        assert p.id == "a"
        p2, _ = m.resolve("m-shared", "b")
        assert p2.id == "b"
        # pin to a provider that doesn't own the model -> None
        assert m.resolve("m2", "a") is None

    def test_disabled_excluded_from_pin(self):
        m = self._manager()
        m._providers["b"].enabled = False
        assert m.resolve("m2", "a") is None  # a doesn't own m2, b disabled

    def test_to_dict_includes_enabled(self):
        from xu_brain.features.agent.provider import Provider
        d = Provider(id="x", type="openai-compatible", base_url="http://x").to_dict()
        assert d["enabled"] is True


class TestSubtaskInheritance:
    """A delegated child must inherit the parent's active model (not a
    resolver fallback) and return its final text (not an empty string)."""

    def test_delegated_child_inherits_model_and_returns_text(self):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import SessionStore
        from xu_brain.plugins import PluginBus
        from xu_brain.features.presets import AgentNode
        from xu_brain.features.tools import all_tools

        data_home = Path(tempfile.mkdtemp())
        store = SessionStore(data_home)
        config = Config(data_home)
        (data_home / "SOUL.md").write_text("You are Xu.\n")
        registry = ToolRegistry(ApprovalManager("manual"))
        for t in all_tools():
            registry.register(t)
        agent = Agent(store, data_home, None, registry, ApprovalManager("manual"),
                      MemoryStore(data_home), SkillsEngine(data_home),
                      PluginBus(data_home), config)

        used: dict[str, object] = {}

        def stream(*_a, **_k):
            async def gen():
                used["model"] = _a[1] if len(_a) > 1 else None
                yield StreamEvent(delta="hello from child")
                yield StreamEvent(stop_reason=StopReason.STOP)
            return gen()

        agent.providers = SimpleNamespace(
            resolve=lambda m, _p=None: (m, m), chat_stream=stream)
        child = AgentNode.from_dict(dict(
            id="c1", name="agent web", role="agent", persona="Your job is using web.",
            job="", model=None, rules=[], skills=None, tools=None, memory=None, children=[]))
        parent = SimpleNamespace(cwd=str(data_home), session_id="psess",
                                 config={"model": "gpt-parent", "job_timeout": 60})

        async def _main():
            return await agent.run_subtask("do x", parent, node=child, depth=1)

        text = asyncio.run(_main())
        assert text == "hello from child", f"expected text, got {text!r}"
        assert used.get("model") == "gpt-parent", f"model {used.get('model')!r} not inherited"


class TestCompactionSummaryQuality:
    """The *quality* layer of compaction: a mandatory section
    schema, prior-checkpoint merging instead of stacking, resume framing, and
    fail-closed truncation. The machinery is covered by TestCompactEngine*;
    these assert what the checkpoint itself looks like."""

    def _agent(self, summary_text="## Next Step\n- go", *, length_finish=False):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import Message, Session
        from xu_brain.features.agent.provider import StopReason
        import types

        agent = Agent(None, None, None, None, None, None, None, None, None, None)
        calls = {"n": 0, "max_tokens": None}

        def fake_stream(_p, _m, msgs, signal=None, max_tokens=None):
            calls["n"] += 1
            calls["max_tokens"] = max_tokens
            calls["msgs"] = list(msgs)
            async def gen():
                yield types.SimpleNamespace(
                    delta=summary_text, reasoning="",
                    stop_reason=StopReason.LENGTH if length_finish else StopReason.STOP,
                )
            return gen()

        agent.providers = types.SimpleNamespace(
            resolve=lambda _model, _p=None: ("provider", "model"),
            chat_stream=fake_stream,
        )
        agent._session_model = lambda _sid: "some-model"
        agent.config = types.SimpleNamespace(get=lambda _k, default=None: default)
        rows = [Message(role="user", content=f"q{i}") if i % 2 == 0
                else Message(role="assistant", content=f"a{i}") for i in range(13)]
        sess = Session(id="s1", title="t", cwd=".", messages=rows)
        agent.sessions = SimpleNamespace(
            get=lambda _sid: sess,
            add_display=lambda *a, **k: None, replace_messages=lambda _sid, msgs: sess.__dict__.__setitem__("messages", msgs),
        )
        return agent, sess, calls

    def test_summarizer_is_capped_and_truncation_fails_closed(self):
        """A checkpoint cut off at the token cap must NOT land: the cut is
        permanent, so a half-written summary would lose the resume action for
        good. Also asserts the call is capped at all."""
        import asyncio
        agent, _sess, calls = self._agent(length_finish=True)
        r = asyncio.run(agent.compress_session("s1"))
        assert calls["max_tokens"] == 8192, "summarizer call must be capped"
        assert r["ok"] is False
        assert "truncated" in r["error"], r["error"]
        # retried once (compaction_retries default 1) before giving up
        assert calls["n"] == 2, calls["n"]

    def test_landed_checkpoint_carries_resume_framing(self):
        """The persisted summary row must tell the *resuming* model to treat the
        text as established background, not narrate it back to the user."""
        import asyncio
        agent, sess, _calls = self._agent()
        asyncio.run(agent.compress_session("s1"))
        head = sess.messages[-1]
        assert head.role == "system"
        assert "<compacted-summary>" in head.content
        assert "</compacted-summary>" in head.content
        assert "established" in head.content, "missing resume preamble"
        assert "without acknowledging this checkpoint" in head.content

    def test_prior_checkpoint_is_replaced_not_stacked(self):
        """Regression: compacting a session that already holds a checkpoint used
        to append a second summary row beside the first, so a long session
        accumulated several overlapping summaries (observed: 2 top-level rows /
        11k chars in one real session). A checkpoint can survive in the retained
        user/assistant tail, so dropping only `system` rows is not enough."""
        import asyncio
        from xu_brain.features.session import Message

        agent, sess, _calls = self._agent()
        old = Message(role="assistant",
                      content="[conversation summary]\n<compacted-summary>\nOLD\n</compacted-summary>")
        # Place it in the tail that survives the cut, not the dropped prefix.
        sess.messages = list(sess.messages) + [old, Message(role="user", content="next")]
        asyncio.run(agent.compress_session("s1"))
        checkpoints = [m for m in sess.messages if "<compacted-summary>" in str(m.content)]
        assert len(checkpoints) == 1, f"{len(checkpoints)} checkpoints survived"
        assert "OLD" not in checkpoints[0].content, "stale checkpoint copied forward"

    def test_repeated_compaction_does_not_falsely_abandon(self):
        """Regression: after 2-3 compactions the shrink check used to read
        "no shrink" and abort. Root cause: `before` counted prior checkpoint
        rows (system markers) but `after` did not, so the metric structurally
        converged to zero as checkpoints accumulated. A prior checkpoint must
        not block a subsequent compaction."""
        import asyncio
        from xu_brain.features.session import Message
        agent, sess, _calls = self._agent(summary_text="## Next Step\n- go")
        prior = Message(role="system", content=(
            "[conversation summary]\n"
            "<compacted-summary>\n## Current Work\n- lots of old detail\n</compacted-summary>"))
        sess.messages = [prior] + list(sess.messages) + [Message(role="user", content="new turn")]
        r = asyncio.run(agent.compress_session("s1"))
        assert r["ok"] is True, r
    def test_auto_path_also_replaces_prior_checkpoint(self):
        """Same rule on the live in-place list the auto path splices."""
        import asyncio
        agent, sess, _calls = self._agent()
        agent.config = SimpleNamespace(
            get=lambda k, default=None: 5 if k == "context_length" else default)
        messages = [
            {"role": "system", "content": "PERSONA"},
            {"role": "system", "content": "[conversation summary]\n<compacted-summary>\nOLD\n</compacted-summary>"},
        ] + [m.to_dict() for m in sess.messages]
        asyncio.run(agent._maybe_compress(sess, messages, None, None, asyncio.Event()))
        checkpoints = [m for m in messages if "<compacted-summary>" in str(m.get("content", ""))]
        assert len(checkpoints) == 1, f"{len(checkpoints)} checkpoints in live list"
        assert "OLD" not in checkpoints[0]["content"]
        # the persona system row survives; only checkpoints are pruned
        assert any(m.get("content") == "PERSONA" for m in messages)
class TestEmptyTextGuard:
    """A turn that ends with no text (thinking-only reply, or a token-limit
    stop mid-thought) must persist a visible [error] line — a reasoning-only
    row reads as a vanished answer in the chat."""

    def _agent(self, data_home):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import SessionStore
        from xu_brain.plugins import PluginBus

        store = SessionStore(data_home)
        config = Config(data_home)
        agent = Agent(store, data_home, None, None, ApprovalManager(config),
                      MemoryStore(data_home), SkillsEngine(data_home),
                      PluginBus(data_home), config)
        agent.registry = SimpleNamespace(
            schemas_for_model=lambda: [], reset_breakers=lambda: None)
        return store, agent

    def test_thinking_only_turn_persists_error_marker(self, data_home):
        store, agent = self._agent(data_home)

        async def stream(provider, model, messages, tools, max_tokens=None, signal=None):
            yield StreamEvent(reasoning="pondering deeply...")
            yield StreamEvent(stop_reason=StopReason.STOP)

        agent.providers = SimpleNamespace(resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)
        sess = store.create(cwd=str(data_home))

        async def main():
            await agent.send(sess.id, "think about it")
            await asyncio.wait_for(agent._turns[sess.id], 20)

        asyncio.run(main())

        last = store.get(sess.id).messages[-1]
        assert last.role == "assistant"
        assert last.reasoning == "pondering deeply..."
        assert "[error] model returned no text this turn" in (last.content or "")

    def test_token_limit_stop_persists_limit_marker(self, data_home):
        store, agent = self._agent(data_home)

        async def stream(provider, model, messages, tools, max_tokens=None, signal=None):
            yield StreamEvent(reasoning="a very long chain of thought")
            yield StreamEvent(stop_reason=StopReason.LENGTH)

        agent.providers = SimpleNamespace(resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)
        sess = store.create(cwd=str(data_home))

        async def main():
            await agent.send(sess.id, "think")
            await asyncio.wait_for(agent._turns[sess.id], 20)

        asyncio.run(main())

        last = store.get(sess.id).messages[-1]
        assert "[error] stopped at token limit before producing an answer" in (last.content or "")

    def test_answer_with_text_is_untouched(self, data_home):
        """The guard must not mark a turn that actually produced text."""
        store, agent = self._agent(data_home)

        async def stream(provider, model, messages, tools, max_tokens=None, signal=None):
            yield StreamEvent(reasoning="short thought")
            yield StreamEvent(delta="the answer")
            yield StreamEvent(stop_reason=StopReason.STOP)

        agent.providers = SimpleNamespace(resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)
        sess = store.create(cwd=str(data_home))

        async def main():
            await agent.send(sess.id, "hi")
            await asyncio.wait_for(agent._turns[sess.id], 20)

        asyncio.run(main())

        last = store.get(sess.id).messages[-1]
        assert last.content == "the answer"
        assert "[error]" not in last.content


class TestCrashStatus:
    """A turn that crashes after streaming must persist its partial row as
    status="failed" — the default "done" made crashes indistinguishable from
    healthy turns in the transcript."""

    def test_crash_persists_failed_status(self, data_home):
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.session import SessionStore
        from xu_brain.plugins import PluginBus

        store = SessionStore(data_home)
        config = Config(data_home)
        agent = Agent(store, data_home, None, None, ApprovalManager(config),
                      MemoryStore(data_home), SkillsEngine(data_home),
                      PluginBus(data_home), config)
        agent.registry = SimpleNamespace(
            schemas_for_model=lambda: [], reset_breakers=lambda: None)

        async def stream(provider, model, messages, tools, max_tokens=None, signal=None):
            yield StreamEvent(reasoning="thinking before the crash")
            yield StreamEvent(delta="partial")
            yield StreamEvent(stop_reason=StopReason.STOP)

        agent.providers = SimpleNamespace(resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)

        def boom(*_a, **_k):
            raise RuntimeError("commit exploded")

        agent._commit_history = boom
        sess = store.create(cwd=str(data_home))

        async def main():
            await agent.send(sess.id, "hi")
            await asyncio.wait_for(agent._turns[sess.id], 20)

        asyncio.run(main())

        last = store.get(sess.id).messages[-1]
        assert last.role == "assistant"
        assert last.status == "failed"
        # the already-streamed content is kept, not erased
        assert "partial" in (last.content or "")
        assert last.reasoning == "thinking before the crash"
