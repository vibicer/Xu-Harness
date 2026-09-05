"""Orchestration tests — managed subagents.

Covers the orchestration layer that remains: manageable subagents
(subagent register/finish + the subagent.* RPC surface). Jobs and goals were
removed with the orchestrator-preset rework.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def data_home(tmp_path: Path) -> Path:
    return tmp_path


class TestSubagents:
    def test_register_and_finish(self, data_home):
        from xu_brain.features.agent.orchestrator import Orchestrator

        orch = Orchestrator(data_home)
        sub = orch.register_subagent("sessx", "do the thing", "child-session")
        assert sub.id.startswith("s")
        assert orch.get_subagent(sub.id).status == "running"

        orch.finish_subagent(sub.id, "result text")
        rec = orch.get_subagent(sub.id)
        assert rec.status == "done"
        assert rec.result == "result text"

    def test_interrupt_flag(self, data_home):
        from xu_brain.features.agent.orchestrator import Orchestrator

        orch = Orchestrator(data_home)
        sub = orch.register_subagent(None, "work", "c1")
        orch.finish_subagent(sub.id, interrupted=True)
        assert orch.get_subagent(sub.id).status == "interrupted"

    def test_error_flag(self, data_home):
        from xu_brain.features.agent.orchestrator import Orchestrator

        orch = Orchestrator(data_home)
        sub = orch.register_subagent(None, "work", "c1")
        orch.finish_subagent(sub.id, error="boom")
        assert orch.get_subagent(sub.id).status == "error"
        assert orch.get_subagent(sub.id).error == "boom"

    def test_finish_unknown_is_noop(self, data_home):
        from xu_brain.features.agent.orchestrator import Orchestrator

        orch = Orchestrator(data_home)
        orch.finish_subagent("s-nope", "hi")
        assert orch.list_subagents() == []
    def test_session_isolation(self, data_home):
        """Subagents are scoped to their parent session: list filters, and
        ownership gates the steering paths. Admin (None) stays unrestricted."""
        from xu_brain.features.agent.orchestrator import Orchestrator

        orch = Orchestrator(data_home)
        a = orch.register_subagent("sessA", "A work", "child-a")
        b = orch.register_subagent("sessB", "B work", "child-b")

        # list is scoped
        assert [s["id"] for s in orch.list_subagents("sessA")] == [a.id]
        assert [s["id"] for s in orch.list_subagents("sessB")] == [b.id]
        # admin (None) sees all
        assert {s["id"] for s in orch.list_subagents()} == {a.id, b.id}

        # ownership gates steering: A cannot reach B's subagent
        assert orch.owned_subagent(b.id, "sessA") is None
        assert orch.owned_subagent(a.id, "sessB") is None
        # the owner (and admin) can
        assert orch.owned_subagent(a.id, "sessA").id == a.id
        assert orch.owned_subagent(b.id, None).id == b.id
    def test_persistence_survives_restart(self, data_home):
        from xu_brain.features.agent.orchestrator import Orchestrator

        o1 = Orchestrator(data_home)
        sub = o1.register_subagent("sessx", "persist me", "child")
        o1.finish_subagent(sub.id, "done")
        # New Orchestrator on same data_home reloads.
        o2 = Orchestrator(data_home)
        rec = o2.get_subagent(sub.id)
        assert rec.status == "done"
        assert rec.result == "done"

    def test_load_skips_malformed_record(self, data_home):
        """A drifted/extra-key record must be skipped, not crash the boot."""
        import json
        from xu_brain.features.agent.orchestrator import Orchestrator

        (data_home / "orchestrator.json").write_text(json.dumps({"subagents": [
            {"id": "sBad", "parent_session": None, "prompt": "old", "status": "done",
             "created": 1.0, "finished": 2.0, "result": "r", "error": None,
             "session_id": None, "unexpected_field": 42},
            {"id": "sGood", "parent_session": None, "prompt": "new", "status": "done",
             "created": 3.0, "finished": 4.0, "result": "r2", "error": None,
             "session_id": None},
        ]}))
        o = Orchestrator(data_home)  # must not raise
        assert o.get_subagent("sBad") is None
        assert o.get_subagent("sGood") is not None

    def test_stale_running_marked_interrupted_on_load(self, data_home):
        """A subagent 'running' at crash time cannot survive a restart; it is
        loaded as interrupted, not ghosted 'running' forever in the UI."""
        from xu_brain.features.agent.orchestrator import Orchestrator

        o1 = Orchestrator(data_home)
        sub = o1.register_subagent("sessx", "in flight at crash", "child")
        o2 = Orchestrator(data_home)
        rec = o2.get_subagent(sub.id)
        assert rec.status == "interrupted"
        assert rec.finished is not None


class TestSubagentRPC:
    def test_rpc_spawn_and_interrupt(self, data_home, monkeypatch):
        from xu_brain.core.runtime import build_app
        from xu_brain.core.contract import RpcError

        app = build_app(data_home)

        async def exercise():
            # Spawn against a sessionless ctx; the child will try to run a real
            # turn. Monkeypatch send to avoid hitting a provider.
            calls = {}

            async def fake_send(sid, text, images=None, node=None, depth=0):
                calls[sid] = text
                # simulate completion so _track finishes and deletes session
                return "t1"

            app.agent.send = fake_send
            r = await app.dispatch("subagent.send", {"prompt": "hello sub"})
            sid = r["id"]
            assert sid.startswith("s")
            subs = await app.dispatch("subagent.list", {})
            assert any(s["id"] == sid for s in subs["subagents"])
            # interrupt it
            ok = await app.dispatch("subagent.interrupt", {"id": sid})
            assert ok["ok"] is True
            with pytest.raises(RpcError) as exc:
                await app.dispatch("subagent.interrupt", {"id": sid})
            assert exc.value.code == -32002

        asyncio.run(exercise())
