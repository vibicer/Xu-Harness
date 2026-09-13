"""The loop guard: what it counts, what it misses, and what it leaves behind.

Run: uv run pytest tests/test_loopguard.py -v

One turn is the unit of detection. A round is a provider response: its tool
calls (as a set) and its reasoning (as a fingerprint) are compared against the
turn's previous round, so the tests here exercise exactly that — consecutive
identity advancing a chain, a different round restarting it, thresholds firing
once per run, and the reminder landing in the model's message list without
ever reaching the store.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xu_brain.core.config import Config
from xu_brain.core.governance import ApprovalManager
from xu_brain.features.agent.loopguard import (
    BOOKKEEPING_TOOLS,
    DEFAULT_THRESHOLDS,
    LoopGuard,
    Signal,
    _calls_key,
    _similar,
    _tokens,
)
from xu_brain.features.agent.provider import StopReason, StreamEvent
from xu_brain.features.memory import MemoryStore
from xu_brain.features.session import SessionStore
from xu_brain.features.skills import SkillsEngine
from xu_brain.plugins import PluginBus

# ---------------------------------------------------------------------------
# Round identity

class TestCallsKey:
    """A round's identity is its distinct (tool, args) pairs, arg order free."""

    def test_same_args_different_key_order_match(self) -> None:
        a = [("bash", {"cmd": "ls", "cwd": "/tmp"})]
        b = [("bash", {"cwd": "/tmp", "cmd": "ls"})]
        assert _calls_key(a) == _calls_key(b)

    def test_extra_call_is_a_different_round(self) -> None:
        a = [("bash", {"cmd": "ls"})]
        b = [("bash", {"cmd": "ls"}), ("read", {"path": "x"})]
        assert _calls_key(a) != _calls_key(b)

    def test_argument_value_change_is_a_different_round(self) -> None:
        assert _calls_key([("bash", {"cmd": "ls"})]) != _calls_key([("bash", {"cmd": "pwd"})])

    def test_tool_order_does_not_matter(self) -> None:
        a = [("read", {"path": "/a"}), ("bash", {"cmd": "x"})]
        b = [("bash", {"cmd": "x"}), ("read", {"path": "/a"})]
        assert _calls_key(a) == _calls_key(b)

    def test_bookkeeping_only_round_is_not_comparable(self) -> None:
        assert "todo" in BOOKKEEPING_TOOLS
        assert _calls_key([("todo", {"items": []})]) is None

    def test_empty_round_is_not_comparable(self) -> None:
        assert _calls_key([]) is None


# ---------------------------------------------------------------------------
# Tool chain

class TestToolChain:
    """A consecutive run of identical rounds advances; anything else restarts."""

    def test_reaches_first_threshold_at_the_third_round(self) -> None:
        guard = LoopGuard()
        calls = [("bash", {"cmd": "ls"})]
        assert guard.observe_tools(calls) is None  # round 1
        assert guard.observe_tools(calls) is None  # round 2
        s = guard.observe_tools(calls)             # round 3
        assert s is not None
        assert (s.kind, s.count, s.tier) == ("tools", 3, 1)

    def test_escalates_at_the_second_threshold(self) -> None:
        guard = LoopGuard()
        calls = [("bash", {"cmd": "ls"})]
        for _ in range(4):
            guard.observe_tools(calls)
        s = guard.observe_tools(calls)  # 5th identical round
        assert s is not None
        assert (s.count, s.tier) == (5, 2)

    def test_each_threshold_fires_once_per_run(self) -> None:
        guard = LoopGuard()
        calls = [("bash", {"cmd": "ls"})]
        fired = []
        for _ in range(12):
            signal = guard.observe_tools(calls)
            if signal:
                fired.append(signal.count)
        assert fired == [3, 5]

    def test_a_different_round_breaks_the_chain(self) -> None:
        guard = LoopGuard()
        for _ in range(3):
            guard.observe_tools([("bash", {"cmd": "ls"})])
        assert guard.observe_tools([("bash", {"cmd": "ls -la"})]) is None
        # restarted: the fresh run needs its own consecutive rounds again
        assert guard.observe_tools([("bash", {"cmd": "ls -la"})]) is None
        third = guard.observe_tools([("bash", {"cmd": "ls -la"})])
        assert (third.count, third.tier) == (3, 1)

    def test_bookkeeping_between_work_rounds_breaks_the_chain(self) -> None:
        guard = LoopGuard()
        for _ in range(2):
            guard.observe_tools([("bash", {"cmd": "ls"})])
        assert guard.observe_tools([("todo", {"items": []})]) is None
        assert guard.observe_tools([("bash", {"cmd": "ls"})]) is None  # count reset

    def test_no_tools_is_not_a_repeat(self) -> None:
        guard = LoopGuard()
        for _ in range(2):
            guard.observe_tools([("bash", {"cmd": "ls"})])
        assert guard.observe_tools([]) is None

    def test_reset_restores_the_fresh_turn_shape(self) -> None:
        guard = LoopGuard()
        calls = [("bash", {"cmd": "ls"})]
        for _ in range(3):
            guard.observe_tools(calls)
        guard.reset()
        assert guard.observe_tools(calls) is None

    def test_thresholds_are_cleaned(self) -> None:
        assert LoopGuard([1, 4]).thresholds == [4]        # a single round is never a loop
        assert LoopGuard(["7", 3]).thresholds == [3, 7]   # hand-edited string still counts
        assert LoopGuard(None).thresholds == list(DEFAULT_THRESHOLDS)
        assert LoopGuard([[1], "oops"]).thresholds == list(DEFAULT_THRESHOLDS)


# ---------------------------------------------------------------------------
# Reasoning chain

SAME = ("So the fix is to read the config file first and then rewrite the "
        "handler before restarting the service.")
OTHER = ("Nothing is obviously wrong, so let me look at the test suite and "
         "check whether the failing case was ever exercised at all.")


class TestSimilarityFingerprint:
    """Thinking repeats are fingerprinted: same thought, not same byte string."""

    def test_casing_and_whitespace_do_not_count(self) -> None:
        assert _similar(_tokens(SAME), _tokens(SAME.upper()))

    def test_a_different_thought_is_not_similar(self) -> None:
        assert not _similar(_tokens(SAME), _tokens(OTHER))

    def test_tiny_thinking_must_be_equal(self) -> None:
        assert _similar(_tokens("reading it"), _tokens("reading it"))
        assert not _similar(_tokens("reading it"), _tokens("reading the file now"))


class TestReasoningChain:
    """Thinking repeats are fingerprinted against the chain's anchor thought."""

    def test_identical_rounds_count_consecutively(self) -> None:
        guard = LoopGuard()
        assert guard.observe_reasoning(SAME) is None
        assert guard.observe_reasoning(SAME) is None
        s = guard.observe_reasoning(SAME)
        assert (s.kind, s.count, s.tier) == ("reasoning", 3, 1)

    def test_a_different_thought_resets(self) -> None:
        guard = LoopGuard()
        guard.observe_reasoning(SAME)
        guard.observe_reasoning(SAME)
        assert guard.observe_reasoning(OTHER) is None
        assert guard.observe_reasoning(SAME) is None   # new chain, count 1
        assert guard.observe_reasoning(SAME) is None   # count 2

    def test_a_round_without_thinking_is_not_a_repeat(self) -> None:
        guard = LoopGuard()
        guard.observe_reasoning(SAME)
        guard.observe_reasoning(SAME)
        assert guard.observe_reasoning("") is None
        assert guard.observe_reasoning(SAME) is None   # chain broke, fresh run restarts

    def test_can_be_turned_off(self) -> None:
        guard = LoopGuard(watch_reasoning=False)
        for _ in range(6):
            assert guard.observe_reasoning(SAME) is None


# ---------------------------------------------------------------------------
# Surfaces

class TestSurfaces:
    def test_note_explains_the_run_and_escalates(self) -> None:
        guard = LoopGuard()
        first = guard.note([Signal(kind="tools", count=3, tier=1)])
        harder = guard.note([Signal(kind="tools", count=5, tier=2)])
        assert "3" in first and "5" in harder
        assert first != harder
        assert "Do NOT" in harder

        reasoning = guard.note([Signal(kind="reasoning", count=3, tier=1)])
        assert "reasoning" in reasoning
        assert reasoning != first

    def test_chip_is_short_and_counts(self) -> None:
        guard = LoopGuard()
        chip = guard.chip([Signal(kind="tools", count=3, tier=1),
                           Signal(kind="reasoning", count=3, tier=1)])
        assert "tool round" in chip and "reasoning" in chip and "3" in chip

    def test_inject_appends_once_then_replaces_in_place(self) -> None:
        guard = LoopGuard()
        msgs = [{"role": "user", "content": "hi"}]
        guard.inject(msgs, "first reminder")
        guard.inject(msgs, "second reminder, louder")
        system = [m for m in msgs if m["role"] == "system"]
        assert len(system) == 1  # never a stack
        assert system[0]["content"].startswith("[loop guard]")
        assert "louder" in system[0]["content"]
        assert msgs[-1].get("role") == "system"

    def test_inject_replaces_an_older_marked_row_anywhere(self) -> None:
        guard = LoopGuard()
        msgs = [{"role": "system", "content": "[loop guard] older"},
                {"role": "user", "content": "hi"}]
        guard.inject(msgs, "newer")
        assert len(msgs) == 2
        assert "newer" in msgs[0]["content"]


# ---------------------------------------------------------------------------
# Config wiring

class TestForConfig:
    def test_real_config_defaults(self, tmp_path: Path) -> None:
        guard = LoopGuard.for_config(Config(tmp_path))
        assert guard is not None
        assert guard.thresholds == [3, 5]
        assert guard.watch_reasoning is True

    def test_config_values_are_honoured(self, tmp_path: Path) -> None:
        config = Config(tmp_path)
        config.set("loop_guard_thresholds", [4, 9])
        config.set("loop_guard_reasoning", False)
        guard = LoopGuard.for_config(config)
        assert guard is not None
        assert guard.thresholds == [4, 9]
        assert guard.watch_reasoning is False

    def test_disabled_config_builds_nothing(self, tmp_path: Path) -> None:
        config = Config(tmp_path)
        config.set("loop_guard", False)
        assert LoopGuard.for_config(config) is None

    def test_plain_mock_falls_back_to_defaults(self) -> None:
        guard = LoopGuard.for_config(SimpleNamespace(unrelated=lambda: 1))
        assert guard is not None
        assert guard.thresholds == [3, 5]


# ---------------------------------------------------------------------------
# One real turn

def _agent(tmp_path: Path):
    """An Agent wired the way the loop tests do it: real stores, no providers."""
    from xu_brain.features.agent.loop import Agent

    store = SessionStore(tmp_path)
    config = Config(tmp_path)
    agent = Agent(store, tmp_path, None, None, ApprovalManager(config),
                  MemoryStore(tmp_path), SkillsEngine(tmp_path),
                  PluginBus(tmp_path), config)
    agent.plugins = SimpleNamespace(
        on_start=lambda *a, **k: None,
        before_llm=lambda v: v,
        after_tool=lambda n, r: r,
    )
    return store, agent


def _wire(agent, stream) -> None:
    agent.providers = SimpleNamespace(
        resolve=lambda _m, _p=None: ("p", "m"), chat_stream=stream)
    async def run_tool(*_a, **_k):
        return SimpleNamespace(output="out", error=False, raw="")
    agent.registry = SimpleNamespace(
        schemas_for_model=lambda _s=None: [], reset_breakers=lambda: None,
        run=run_tool)


class _RoundProbe:
    """Counts the rounds the fake provider got, and records its notes."""

    def __init__(self, rounds: int) -> None:
        self.rounds = rounds
        self.calls = 0
        self.notes: list[str] = []

    async def stream(
        self, provider, model, messages,
        tools, max_tokens=None, reasoning_effort=None, signal=None,
    ):
        self.calls += 1
        self.notes.extend(str(m.get("content") or "") for m in messages
                          if m.get("role") == "system"
                          and str(m.get("content", "")).startswith("[loop guard]"))
        yield StreamEvent(reasoning=SAME)
        if self.calls < self.rounds:
            yield StreamEvent(tool_call=SimpleNamespace(
                id=f"c{self.calls}", name="bash", arguments='{"cmd":"ls"}'))
            yield StreamEvent(stop_reason=StopReason.TOOL)
        else:
            yield StreamEvent(delta="done")
            yield StreamEvent(stop_reason=StopReason.STOP)


class TestGuardTurn:
    """A turn that keeps repeating gets one reminder, then a louder one — and
    neither the reminder nor its chip survives into the next turn."""

    def _run(self, agent, session_id: str) -> None:
        async def main():
            await agent.send(session_id, "fix it")
            await asyncio.wait_for(agent._turns[session_id], 20)

        asyncio.run(main())

    def test_reminders_fly_and_their_chips_persist(self, tmp_path: Path) -> None:
        store, agent = _agent(tmp_path)
        probe = _RoundProbe(rounds=5)
        _wire(agent, probe.stream)

        captured: list[list[str]] = []
        original = agent._commit_history

        def capture(session, result, final_text):
            captured.append([s.get("kind") for s in result.steps])
            return original(session, result, final_text)

        agent._commit_history = capture
        session = store.create(cwd=str(tmp_path))
        self._run(agent, session.id)

        # five identical rounds: the nudge at 3, the escalation at 5
        assert probe.notes, "the turn never reminded the model"
        assert len(probe.notes) == 2
        kinds = captured[0]
        assert kinds.count("guard") == 2
        chips = [i for i, k in enumerate(kinds) if k == "guard"]
        tools = [i for i, k in enumerate(kinds) if k == "tool"]
        # the nudge lands with the last round it counted, right after that
        # round's tool chip — a warning always follows what it talks about
        assert chips[0] == tools[2] + 1
        # the escalation at 5 lands with its own round too (the round's
        # reasoning + final text precede it)
        assert chips[1] == len(kinds) - 1

    def test_reminder_never_persists(self, tmp_path: Path) -> None:
        store, agent = _agent(tmp_path)
        probe = _RoundProbe(rounds=4)
        _wire(agent, probe.stream)
        session = store.create(cwd=str(tmp_path))
        self._run(agent, session.id)

        assert probe.calls >= 4
        assert probe.notes, "round 3 should have fired the guard"
        rows = store.get(session.id).messages
        assert not any(m.role == "system" and "[loop guard]" in (m.content or "")
                       for m in rows), "the per-turn reminder leaked into history"
        assert any(any(s.get("kind") == "guard" for s in (m.steps or []))
                   for m in rows if m.role == "assistant")

    def test_disabled_guard_leaves_no_traces(self, tmp_path: Path) -> None:
        store, agent = _agent(tmp_path)
        agent.config.set("loop_guard", False)
        probe = _RoundProbe(rounds=4)
        _wire(agent, probe.stream)
        session = store.create(cwd=str(tmp_path))
        self._run(agent, session.id)

        assert probe.calls >= 4
        assert probe.notes == []
        rows = store.get(session.id).messages
        assert not any(m.role == "system" for m in rows)
        assert not any(any(s.get("kind") == "guard" for s in (m.steps or [])) for m in rows)


class TestGuardConfigRpc:
    """The config RPC is the guard's only tuning surface, so it validates its
    two trip points and never stores a disabled-looking oddity."""

    def _app(self, tmp_path: Path):
        from xu_brain.core.runtime import build_app

        return build_app(tmp_path)

    async def _set(self, app, value) -> None:
        await app.dispatch("config.set", {"key": "loop_guard_thresholds", "value": value})

    def test_roundtrip_through_the_rpc(self, tmp_path: Path) -> None:
        import asyncio

        app = self._app(tmp_path)
        asyncio.run(self._set(app, [7, 19]))
        assert app.config.get("loop_guard_thresholds") == [7, 19]
        cfg = asyncio.run(app.dispatch("config.get", {}))
        assert cfg["loop_guard_thresholds"] == [7, 19]
        assert cfg["loop_guard"] is True
        assert cfg["loop_guard_reasoning"] is True

    def test_pair_is_normalized_case_insensitive_of_input_order(self, tmp_path: Path) -> None:
        import asyncio

        app = self._app(tmp_path)
        asyncio.run(self._set(app, [7, 3]))
        assert app.config.get("loop_guard_thresholds") == [3, 7]

    def test_invalid_pairs_are_rejected(self, tmp_path: Path) -> None:
        import asyncio

        from xu_brain.core.contract import RpcError

        app = self._app(tmp_path)
        for bad in (["3"], [3, 5, 9], [1, 5], [3, 3], [3, 90], ["3", "five"], "3,5", {}):
            with pytest.raises(RpcError):
                asyncio.run(self._set(app, bad))
        assert app.config.get("loop_guard_thresholds") == [3, 5]  # default untouched

    def test_guard_switches_pass_through(self, tmp_path: Path) -> None:
        import asyncio

        app = self._app(tmp_path)
        asyncio.run(app.dispatch("config.set", {"key": "loop_guard", "value": False}))
        asyncio.run(app.dispatch("config.set", {"key": "loop_guard_reasoning", "value": False}))
        assert app.config.get("loop_guard") is False
        assert app.config.get("loop_guard_reasoning") is False
        cfg = asyncio.run(app.dispatch("config.get", {}))
        assert cfg["loop_guard"] is False and cfg["loop_guard_reasoning"] is False
