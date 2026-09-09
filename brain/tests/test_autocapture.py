"""Memory auto-capture extractor — parse, overlap, and fail-soft capture.

    python -m pytest tests/test_autocapture.py -q

The extractor turns a finished turn into 0-3 durable memory candidates and
persists them via Mnemosyne. Everything is fail-soft: a broken model or memory
backend must never affect a completed turn.
"""
from __future__ import annotations

import asyncio
import sys
import time
import types
from types import SimpleNamespace

import pytest

from xu_brain.features.memory import autocapture as ac
from xu_brain.features.memory import mnemo as mnemo_module


# ---- _parse ---------------------------------------------------------------

def test_parse_garbage_returns_empty() -> None:
    assert ac._parse("no json here at all") == []
    assert ac._parse("{not an array}") == []


def test_parse_prose_wrapped_array_still_parses() -> None:
    text = 'Here is what I found:\n[{"content": "uses pytest", "kind": "preference"}]\nDone.'
    out = ac._parse(text)
    assert len(out) == 1
    assert out[0]["content"] == "uses pytest"
    assert out[0]["importance"] == 0.5  # default applied


def test_parse_caps_at_three_and_drops_bad_entries() -> None:
    items = [{"content": f"fact {i}", "kind": "preference"} for i in range(5)]
    items.append({"content": "bad", "kind": "task-state"})  # unknown kind
    items.append({"nope": 1})  # missing content
    out = ac._parse(repr(items).replace("'", '"'))
    assert len(out) == 3
    assert all(o["content"] != "bad" for o in out)


def test_parse_truncates_long_content_at_word_boundary() -> None:
    long = "word " * 300
    out = ac._parse(f'[{{"content": "{long.strip()}", "kind": "project-fact"}}]')
    assert len(out[0]["content"]) <= 600
    assert out[0]["content"].endswith("…")


def test_parse_clamps_importance() -> None:
    out = ac._parse('[{"content": "x", "importance": 42, "kind": "correction"}]')
    assert out[0]["importance"] == 1.0
    out = ac._parse('[{"content": "x", "importance": "not-a-number", "kind": "correction"}]')
    assert out[0]["importance"] == 0.5


# ---- _overlap --------------------------------------------------------------

def test_overlap_identical_and_disjoint() -> None:
    assert ac._overlap("alpha beta gamma", "alpha beta gamma") == 1.0
    assert ac._overlap("alpha beta", "delta epsilon") == 0.0
    partial = ac._overlap("the user prefers pytest", "user prefers pytest")
    assert 0.0 < partial < 1.0


# ---- capture_turn ----------------------------------------------------------

class _FakeProviders:
    def __init__(self, delta: str) -> None:
        self.delta = delta
        self.calls: list[tuple] = []

    def resolve(self, model: str | None):
        return (SimpleNamespace(id="fake"), model or "fake-model")

    async def chat_stream(self, provider, model, messages, **kw):
        self.calls.append((provider, model, messages))
        yield SimpleNamespace(delta=self.delta)


def _agent(delta: str, **config: object) -> SimpleNamespace:
    defaults = {"memory_autocapture": True, "memory_capture_min_interval": 0,
                "memory_capture_model": None}
    defaults.update(config)
    cfg = SimpleNamespace(get=lambda k, d=None: defaults.get(k, d),
                          session_model=lambda sid: "fake-model")
    return SimpleNamespace(config=cfg, providers=_FakeProviders(delta))


class _FakeMnemo(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("mnemosyne")
        self.stored: list[dict] = []
        self.fail = False

    def remember(self, content, importance=0.5, metadata=None, **kw):
        if self.fail:
            raise RuntimeError("backend down")
        self.stored.append({"content": content, "importance": importance,
                            "metadata": metadata})
        return "id-1"

    def recall(self, query, top_k=5, **kw):
        return [{"content": s["content"]} for s in self.stored][:top_k]

    def forget(self, memory_id, **kw):
        return True


@pytest.fixture()
def fake_mnemo(monkeypatch):
    mod = _FakeMnemo()
    monkeypatch.setitem(sys.modules, "mnemosyne", mod)
    monkeypatch.setattr(mnemo_module, "_avail", None)
    ac._LAST_CAPTURE.clear()
    return mod


def test_capture_turn_writes_new_fact(fake_mnemo) -> None:
    agent = _agent('[{"content": "user prefers pytest", "importance": 0.7, "kind": "preference"}]')
    out = asyncio.run(ac.capture_turn(agent, "s1", "use pytest please", "Sure, pytest it is."))
    assert len(out) == 1
    assert fake_mnemo.stored[0]["metadata"]["kind"] == "preference"


def test_capture_turn_dedupes_near_duplicate(fake_mnemo) -> None:
    delta = '[{"content": "user prefers pytest runner", "kind": "preference"}]'
    agent = _agent(delta)
    first = asyncio.run(ac.capture_turn(agent, "s1", "u", "a"))
    assert len(first) == 1
    second = asyncio.run(ac.capture_turn(agent, "s1", "u2", "a2"))
    assert second == []  # overlapping with the stored fact
    assert len(fake_mnemo.stored) == 1


def test_capture_turn_respects_flag_and_rate_limit(fake_mnemo) -> None:
    off = _agent("[]", memory_autocapture=False)
    assert asyncio.run(ac.capture_turn(off, "s1", "u", "a")) == []
    assert not off.providers.calls

    agent = _agent("[]")
    asyncio.run(ac.capture_turn(agent, "s1", "u", "a"))
    # rate limit: min_interval defaults are 0 in _agent, so set it high now
    agent.config.get = lambda k, d=None: 9999 if k == "memory_capture_min_interval" else d
    assert asyncio.run(ac.capture_turn(agent, "s1", "u2", "a2")) == []
    assert len(agent.providers.calls) == 1


def test_capture_turn_fail_soft_on_backend_error(fake_mnemo) -> None:
    fake_mnemo.fail = True
    agent = _agent('[{"content": "some fact", "kind": "preference"}]')
    out = asyncio.run(ac.capture_turn(agent, "s1", "u", "a"))
    assert out == []


def test_capture_turn_fail_soft_on_model_error(fake_mnemo) -> None:
    class _Boom:
        def resolve(self, model):
            return (SimpleNamespace(id="fake"), "m")

        async def chat_stream(self, *a, **kw):
            raise RuntimeError("provider down")
            yield  # pragma: no cover

    agent = _agent("")
    agent.providers = _Boom()
    assert asyncio.run(ac.capture_turn(agent, "s1", "u", "a")) == []


def test_capture_turn_skips_everything_when_mnemo_unavailable(monkeypatch) -> None:
    """No store → no extractor LLM call, no write; the turn is untouched."""
    monkeypatch.setattr(mnemo_module, "_avail", False)
    agent = _agent('[]')
    assert asyncio.run(ac.capture_turn(agent, "s1", "u", "a")) == []
    assert not agent.providers.calls


def test_rate_limit_uses_monotonic_wall(fake_mnemo, monkeypatch) -> None:
    """Interval check reads the shared dict even when config interval is 0."""
    agent = _agent("[]", memory_capture_min_interval=0)
    asyncio.run(ac.capture_turn(agent, "s1", "u", "a"))
    assert "s1" in ac._LAST_CAPTURE
    assert ac._LAST_CAPTURE["s1"] <= time.monotonic()
