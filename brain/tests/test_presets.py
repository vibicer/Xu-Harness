"""Orchestration-preset tests — store CRUD, tree round-trip, and the
preset.* / session.set_preset RPC surface."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def data_home(tmp_path: Path) -> Path:
    return tmp_path


TREE = {
    "id": "root",
    "name": "Main",
    "role": "orchestrator",
    "persona": "You are the coordinator.",
    "job": "orchestrate the squad",
    "model": "gpt-4o",
    "rules": ["delegate to the expert"],
    "skills": ["code-review"],
    "tools": ["builtin"],
    "children": [
        {
            "id": "coder",
            "name": "Coder",
            "role": "agent",
            "persona": "You write code.",
            "model": "gpt-4o",
            "skills": [],
        },
        {
            "id": "critic",
            "name": "Critic",
            "role": "agent",
            "persona": "You review.",
            "tools": ["builtin", "dropin"],
            "children": [],
        },
    ],
}


class TestPresetStore:
    def test_round_trip_and_persistence(self, data_home):
        from xu_brain.features.presets import PresetStore

        s1 = PresetStore(data_home)
        p = s1.upsert(None, "Squad", TREE)
        assert p.id.startswith("p")
        assert p.root.role == "orchestrator"
        assert [c.name for c in p.root.children] == ["Coder", "Critic"]
        # children round-trip their own fields
        coder = p.root.children[0]
        assert coder.skills == []
        assert coder.model == "gpt-4o"

        s2 = PresetStore(data_home)
        loaded = s2.get(p.id)
        assert loaded.name == "Squad"
        assert len(loaded.root.children) == 2
        assert loaded.root.children[1].tools == ["builtin", "dropin"]
        assert loaded.root.rules == ["delegate to the expert"]

    def test_unspecified_slots_default_to_none_inherit(self, data_home):
        from xu_brain.features.presets import PresetStore

        s = PresetStore(data_home)
        p = s.upsert(None, "Minimal", {"id": "r", "name": "Root"})
        assert p.root.skills is None      # inherit global
        assert p.root.tools is None       # inherit global
        assert p.root.memory is None      # inherit global
        assert p.root.model is None
        assert p.root.rules == []
        assert p.root.persona == ""
        assert p.root.fallbacks == []     # no backup models

    def test_fallbacks_round_trip_and_drop_blanks(self, data_home):
        """Per-node backup models survive a reload; blanks/non-strings are
        dropped so the chain never stalls on an unresolvable "" entry."""
        from xu_brain.features.presets import PresetStore

        s = PresetStore(data_home)
        p = s.upsert(None, "Chained", {
            "id": "r", "name": "Root", "role": "orchestrator", "model": "gpt-4o",
            "fallbacks": ["claude-sonnet-4", "", None, "local-qwen"],
            "children": [{"id": "c", "name": "Coder", "fallbacks": ["local-qwen"]}],
        })
        assert p.root.fallbacks == ["claude-sonnet-4", "local-qwen"]
        assert p.root.to_dict()["fallbacks"] == ["claude-sonnet-4", "local-qwen"]

        reloaded = PresetStore(data_home).get(p.id)
        assert reloaded.root.fallbacks == ["claude-sonnet-4", "local-qwen"]
        assert reloaded.root.children[0].fallbacks == ["local-qwen"]

    def test_list_and_delete(self, data_home):
        from xu_brain.features.presets import PresetStore

        s = PresetStore(data_home)
        a = s.upsert(None, "Alpha", TREE)
        s.upsert(None, "Beta", {"id": "b", "name": "Beta"})
        lst = s.list()
        assert {x["name"] for x in lst} == {"Alpha", "Beta"}
        node_count = next(x["node_count"] for x in lst if x["name"] == "Alpha")
        assert node_count == 3  # root + 2 children

        assert s.delete(a.id) is True
        assert s.delete(a.id) is False  # already gone
        assert s.get(a.id) is None

    def test_upsert_overwrites_keeps_id(self, data_home):
        from xu_brain.features.presets import PresetStore

        s = PresetStore(data_home)
        p = s.upsert(None, "V1", TREE)
        p2 = s.upsert(p.id, "V2", {"id": "root", "name": "Root"})
        assert p2.id == p.id
        assert p2.name == "V2"
        assert len(s.list()) == 1


class TestPresetRPC:
    def test_preset_crud_cycle(self, data_home):
        from xu_brain.core.runtime import build_app
        from xu_brain.core.contract import RpcError

        app = build_app(data_home)

        async def exercise():
            up = await app.dispatch("preset.upsert", {"name": "Squad", "tree": TREE})
            pid = up["preset"]["id"]
            got = await app.dispatch("preset.get", {"id": pid})
            assert got["preset"]["root"]["role"] == "orchestrator"
            lst = await app.dispatch("preset.list", {})
            assert any(x["id"] == pid for x in lst["presets"])
            await app.dispatch("preset.delete", {"id": pid})
            with pytest.raises(RpcError) as exc:
                await app.dispatch("preset.get", {"id": pid})
            assert exc.value.code == -32002

        asyncio.run(exercise())

    def test_session_bind_and_state(self, data_home):
        from xu_brain.core.runtime import build_app
        from xu_brain.core.contract import RpcError

        app = build_app(data_home)

        async def exercise():
            sess = await app.dispatch("session.create", {})
            sid = sess["session"]["id"]
            up = await app.dispatch("preset.upsert", {"name": "Squad", "tree": TREE})
            pid = up["preset"]["id"]

            # default: no preset
            st = await app.dispatch("state.get", {"session_id": sid})
            assert st["preset"] is None

            # bind
            r = await app.dispatch("session.set_preset", {"session_id": sid, "preset_id": pid})
            assert r["preset_id"] == pid
            st = await app.dispatch("state.get", {"session_id": sid})
            assert st["preset"]["id"] == pid
            assert st["preset"]["root"]["name"] == "Main"

            # unbind
            await app.dispatch("session.set_preset", {"session_id": sid, "preset_id": None})
            st = await app.dispatch("state.get", {"session_id": sid})
            assert st["preset"] is None

            # unknown preset rejected
            with pytest.raises(RpcError) as exc:
                await app.dispatch("session.set_preset", {"session_id": sid, "preset_id": "p-nope"})
            assert exc.value.code == -32002

            # deleted preset resolves to None in state
            await app.dispatch("session.set_preset", {"session_id": sid, "preset_id": pid})
            await app.dispatch("preset.delete", {"id": pid})
            st = await app.dispatch("state.get", {"session_id": sid})
            assert st["preset"] is None

        asyncio.run(exercise())

class TestRuntimeBinding:
    def test_roster_and_persona_in_system(self, tmp_path):
        import asyncio
        from xu_brain.features.presets import AgentNode
        from xu_brain.core.runtime import build_app

        app = build_app(tmp_path)
        root = AgentNode(
            id="r", name="Main", role="orchestrator", persona="I coordinate.",
            job="run the squad",
            children=[AgentNode(id="c1", name="Coder", role="agent",
                                persona="I write code.", job="coding")],
        )
        sess = asyncio.run(app.dispatch("session.create", {}))["session"]
        msgs = app.agent._build_messages(app.sessions.get(sess["id"]), root)
        system = msgs[0]["content"]
        assert "I coordinate." in system
        assert "Role: run the squad" in system
        assert "[SQUAD ROSTER]" in system
        assert "Coder — coding" in system

    def test_node_persona_only_applies_when_set(self, tmp_path):
        import asyncio
        from xu_brain.features.presets import AgentNode
        from xu_brain.core.runtime import build_app

        app = build_app(tmp_path)
        root = AgentNode(id="r", name="Leaf", role="agent", persona="")
        sess = asyncio.run(app.dispatch("session.create", {}))["session"]
        msgs = app.agent._build_messages(app.sessions.get(sess["id"]), root)
        assert "Leaf" not in msgs[0]["content"]          # no inline persona
        assert "[SQUAD ROSTER]" not in msgs[0]["content"]  # leaf: no roster


class TestDelegateTool:
    def _ctx(self, node_dict, depth=0):
        class FakeAgent:
            captured = None
            async def run_subtask(self, prompt, ctx, node=None, depth=0, run_id=None):
                self.captured = (prompt, getattr(node, "name", None), depth)
                return "child result"
        class FakeCtx:
            def __init__(self, agent):
                self.agent = agent
                self.config = {"preset_depth": depth, "preset_node": node_dict}
        agent = FakeAgent()
        return FakeCtx(agent), agent

    def test_delegates_to_child(self):
        import asyncio
        from xu_brain.features.presets import AgentNode
        from xu_brain.features.tools.agents import DelegateTool

        root = AgentNode(id="r", name="Main", role="orchestrator",
                         children=[AgentNode(id="c1", name="Coder", role="agent",
                                             persona="code", job="write code")])
        ctx, agent = self._ctx(root.to_dict(), depth=0)
        result = asyncio.run(DelegateTool().run({"child": "Coder", "prompt": "do x"}, ctx))
        assert not result.error
        assert agent.captured[0] == "do x"
        assert agent.captured[1] == "Coder"
        assert agent.captured[2] == 1  # depth incremented

    def test_unknown_child_errors(self):
        import asyncio
        from xu_brain.features.presets import AgentNode
        from xu_brain.features.tools.agents import DelegateTool

        root = AgentNode(id="r", name="Main", role="orchestrator",
                         children=[AgentNode(id="c1", name="Coder", role="agent")])
        ctx, _ = self._ctx(root.to_dict())
        result = asyncio.run(DelegateTool().run({"child": "Nope", "prompt": "x"}, ctx))
        assert result.error

    def test_depth_cap_rejects(self):
        import asyncio
        from xu_brain.features.presets import AgentNode
        from xu_brain.features.tools.agents import DelegateTool

        root = AgentNode(id="r", name="Main", role="orchestrator",
                         children=[AgentNode(id="c1", name="Coder", role="agent")])
        ctx, _ = self._ctx(root.to_dict(), depth=3)
        result = asyncio.run(DelegateTool().run({"child": "Coder", "prompt": "x"}, ctx))
        assert result.error
        assert "recursion cap" in result.output

    def test_no_children_delegates_to_default_child(self):
        """A leaf / normal agent (no preset children) now delegates to a
        default child (node=None → global persona/skills/tools/memory) instead
        of erroring. The label is a free-form display name."""
        import asyncio
        from xu_brain.features.presets import AgentNode
        from xu_brain.features.tools.agents import DelegateTool

        leaf = AgentNode(id="l", name="Leaf", role="agent")
        ctx, agent = self._ctx(leaf.to_dict())
        result = asyncio.run(DelegateTool().run({"label": "researcher", "prompt": "x"}, ctx))
        assert not result.error, result.output
        assert agent.captured[0] == "x"
        assert agent.captured[1] is None           # default child: no preset node
        assert agent.captured[2] == 1


class TestPresetCreateTool:
    def test_creates_and_overwrites_preset(self):
        import asyncio
        from types import SimpleNamespace
        from xu_brain.features.presets import PresetStore
        from xu_brain.features.tools.agents import preset_create

        store = PresetStore(self._tmp())
        ctx = SimpleNamespace(presets=store)
        tree = {
            "role": "orchestrator", "persona": "you are the main",
            "children": [{"name": "coder", "role": "agent", "persona": "writes code", "tools": ["web"]}],
        }
        r = asyncio.run(preset_create.run({"name": "Team A", "tree": tree}, ctx))
        assert not r.error
        assert store.list()[0]["name"] == "Team A"
        assert store.list()[0]["node_count"] == 2

        pid = store.list()[0]["id"]
        r2 = asyncio.run(preset_create.run({"name": "Team A v2", "tree": tree, "id": pid}, ctx))
        assert not r2.error
        assert len(store.list()) == 1 and store.list()[0]["name"] == "Team A v2"

    def test_validation(self):
        import asyncio
        from types import SimpleNamespace
        from xu_brain.features.presets import PresetStore
        from xu_brain.features.tools.agents import preset_create

        ctx = SimpleNamespace(presets=PresetStore(self._tmp()))
        tree = {"role": "orchestrator"}
        assert asyncio.run(preset_create.run({"tree": tree}, ctx)).error  # no name
        assert asyncio.run(preset_create.run({"name": "x", "tree": "nope"}, ctx)).error  # tree not object
        assert asyncio.run(preset_create.run({"name": "x", "tree": tree}, SimpleNamespace(presets=None))).error

    @staticmethod
    def _tmp():
        from pathlib import Path
        import tempfile
        return Path(tempfile.mkdtemp())


class TestDelegationActivity:
    """A delegated child's steps must be visible to the parent: the delegate
    chip carries a run id, and that run records the child's tool/reasoning
    steps + final result."""

    def test_delegate_records_child_activity(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        from xu_brain.features.agent.loop import Agent
        from xu_brain.features.agent.provider import StreamEvent, StopReason
        from xu_brain.core.governance import ApprovalManager
        from xu_brain.core.config import Config
        from xu_brain.features.memory import MemoryStore
        from xu_brain.plugins import PluginBus
        from xu_brain.features.presets import AgentNode
        from xu_brain.features.tools.registry import ToolRegistry
        from xu_brain.features.session import SessionStore
        from xu_brain.features.skills import SkillsEngine
        from xu_brain.features.tools import all_tools
        from xu_brain.features.tools.agents import DelegateTool

        dh = Path(tempfile.mkdtemp())
        (dh / "SOUL.md").write_text("You are Xu.\n")
        registry = ToolRegistry(ApprovalManager("yolo"))
        for t in all_tools():
            registry.register(t)
        config = Config(dh)
        agent = Agent(SessionStore(dh), dh, None, registry, ApprovalManager("yolo"),
                      MemoryStore(dh), SkillsEngine(dh), PluginBus(dh), config)

        calls = {"n": 0}

        def stream(*_a, **_k):
            async def gen():
                calls["n"] += 1
                if calls["n"] == 1:
                    yield StreamEvent(reasoning="child thinking")
                    yield StreamEvent(tool_call=SimpleNamespace(
                        id="c1", name="glob", arguments='{"pattern":"*.md"}'))
                    yield StreamEvent(stop_reason=StopReason.TOOL)
                else:
                    yield StreamEvent(delta="child final answer")
                    yield StreamEvent(stop_reason=StopReason.STOP)
            return gen()

        agent.providers = SimpleNamespace(
            resolve=lambda m, _p=None: (m or "m", m or "m"), chat_stream=stream)

        root = AgentNode.from_dict(dict(
            id="r", name="Main", role="orchestrator", persona="main", job="",
            model=None, rules=[], skills=None, tools=None, memory=None,
            children=[dict(id="c1", name="agent web", role="agent", persona="web work",
                           job="uses web", model=None, rules=[], skills=None,
                           tools=None, memory=None, children=[])]))
        ctx = SimpleNamespace(agent=agent, session_id="psess", cwd=str(dh),
                              config={"preset_depth": 0, "preset_node": root.to_dict(),
                                      "job_timeout": 60, "model": "m"})

        result = asyncio.run(DelegateTool().run(
            {"child": "agent web", "prompt": "find the docs"}, ctx))
        assert not result.error
        run_id = result.meta.get("subagent_run")
        assert run_id, "delegate chip must carry a subagent_run id"
        assert result.meta.get("subagent") == "agent web"

        # The run must not be readable from another parent session.
        by_id = agent.delegation_activity(run_id=run_id)
        assert len(by_id) == 1
        assert agent.delegation_activity(run_id=run_id, session_id="other") == []
        by_session = agent.delegation_activity(session_id="psess")
        assert len(by_session) == 1
        assert agent.delegation_activity(session_id="other") == []

        rec = by_id[0]
        assert rec["child"] == "agent web"
        assert rec["prompt"] == "find the docs"
        assert rec["status"] == "ok"
        assert rec["result"] == "child final answer"
        assert "child thinking" in rec["reasoning"]
        kinds = [s.get("kind") for s in rec["steps"]]
        assert "tool" in kinds and "reasoning" in kinds
        assert any(s.get("tool") == "glob" for s in rec["steps"])

    def test_activity_empty_for_unknown_run(self):
        from types import SimpleNamespace
        from xu_brain.features.agent.loop import Agent

        agent = Agent.__new__(Agent)
        agent._delegations = {}
        assert agent.delegation_activity(run_id="nope") == []
        assert agent.delegation_activity(session_id="nope") == []
