"""Built-in Mnemosyne wrapper + tools (no plugin).

    python -m pytest tests/test_mnemo.py -q

Covers the accessor wrappers, the `before_llm` digest injection (cap, off,
empty-recall and unavailable passthrough), and the three built-in tools
(round-trip + fail-soft). Everything uses a fake `mnemosyne` module, so no
store and no package install are required.
"""
from __future__ import annotations

import asyncio
import sys
import types

import pytest

from xu_brain.features.memory import mnemo
from xu_brain.features.tools import all_tools


# ---- fake store -----------------------------------------------------------

class _FakeStore(types.ModuleType):
    def __init__(self) -> None:
        super().__init__("mnemosyne")
        self.stored: list[dict] = []

    def remember(self, content, importance=0.5, metadata=None, **kw):
        self.stored.append({"content": content, "importance": importance,
                            "metadata": metadata})
        return "id-1"
    def recall(self, query, top_k=5, **kw):
        return [{"content": s["content"]} for s in self.stored][:top_k]

    def forget(self, memory_id, **kw):
        self.stored.clear()
        return True


@pytest.fixture()
def fake_store(monkeypatch):
    mod = _FakeStore()
    monkeypatch.setitem(sys.modules, "mnemosyne", mod)
    monkeypatch.setattr(mnemo, "_avail", None)
    return mod


def _cfg(**vals):
    class _C:
        def get(self, key, default=None):
            return vals.get(key, default)
    return _C()


# ---- availability and accessors ------------------------------------------

def test_available_is_true_with_store_present(fake_store) -> None:
    assert mnemo.available() is True


def test_available_is_false_when_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(mnemo, "_avail", False)
    assert mnemo.available() is False


def test_remember_recall_forget_roundtrip(fake_store) -> None:
    mnemo.remember("apples are red", importance=0.8)
    hits = mnemo.recall("apple", top_k=1)
    assert hits and hits[0]["content"] == "apples are red"
    assert fake_store.stored[0]["importance"] == 0.8
    mnemo.forget("id-1")
    assert mnemo.recall("apple", top_k=1) == []


def test_accessors_raise_when_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(mnemo, "_avail", False)
    with pytest.raises(RuntimeError):
        mnemo.remember("x")
    with pytest.raises(RuntimeError):
        mnemo.recall("x")
    with pytest.raises(RuntimeError):
        mnemo.forget("x")


# ---- before_llm digest ----------------------------------------------------

def test_before_llm_injects_digest_into_system_prompt(fake_store) -> None:
    fake_store.stored.append({"content": "project codename BLUE-HERON"})
    msgs = [{"role": "system", "content": "You are Xu."},
            {"role": "user", "content": "what is the codename?"}]
    out = mnemo.before_llm(msgs, _cfg())
    assert "BLUE-HERON" in out[0]["content"]
    assert out[1]["content"] == "what is the codename?"


def test_before_llm_inject_off_passthrough(fake_store) -> None:
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert mnemo.before_llm(msgs, _cfg(memory_mnemosyne_inject=False)) == msgs


def test_before_llm_empty_recall_passthrough(fake_store) -> None:
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "zzz-no-such-fact-qqq"}]
    assert mnemo.before_llm(msgs, _cfg()) == msgs


def test_before_llm_unavailable_passthrough(monkeypatch) -> None:
    monkeypatch.setattr(mnemo, "_avail", False)
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    assert mnemo.before_llm(msgs, _cfg()) == msgs


def test_before_llm_caps_digest(fake_store) -> None:
    for i in range(20):
        fake_store.stored.append({"content": f"filler fact number {i} " * 6})
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "filler fact"}]
    out = mnemo.before_llm(msgs, _cfg(memory_mnemosyne_max_chars=300))
    block = out[0]["content"].split("long-term memory (mnemosyne)", 1)[-1]
    assert len(block) <= 300 + 50


def test_before_llm_prepends_system_row_when_none(fake_store) -> None:
    fake_store.stored.append({"content": "user drinks oolong tea"})
    out = mnemo.before_llm([{"role": "user", "content": "what tea?"}], _cfg())
    assert out[0]["role"] == "system"
    assert "oolong" in out[0]["content"]


# ---- built-in tools -------------------------------------------------------

def _tool(name):
    return next(t for t in all_tools() if t.name == name)


def test_tools_registered_as_mnemosyne_toolset() -> None:
    for name in ("mnemosyne_remember", "mnemosyne_recall", "mnemosyne_forget"):
        tool = _tool(name)
        assert tool.toolset == "mnemosyne", name


def test_tools_roundtrip(fake_store) -> None:
    remember = _tool("mnemosyne_remember")
    recall = _tool("mnemosyne_recall")
    forget = _tool("mnemosyne_forget")

    r = asyncio.run(remember.run({"content": "likes tea", "importance": 0.9}, None))
    assert not r.error
    r2 = asyncio.run(recall.run({"query": "tea"}, None))
    assert not r2.error and "likes tea" in r2.output
    assert isinstance(r2.raw, list)  # structured payload for the chat chip
    r3 = asyncio.run(forget.run({"memory_id": "id-1"}, None))
    assert not r3.error


def test_tool_fails_soft_when_unavailable(monkeypatch) -> None:
    monkeypatch.setattr(mnemo, "_avail", False)
    r = asyncio.run(_tool("mnemosyne_recall").run({"query": "x"}, None))
    assert r.error and "mnemosyne" in r.output
def test_embeddings_toggle_env_flag(monkeypatch) -> None:
    monkeypatch.setattr(mnemo, "_embed_off_applied", False)
    monkeypatch.delenv("MNEMOSYNE_EMBEDDINGS_OFF", raising=False)
    mnemo.apply_embeddings(False)
    assert not mnemo.embeddings_enabled()
    mnemo.apply_embeddings(True)
    assert mnemo.embeddings_enabled()


def test_embeddings_toggle_respects_external_flag(monkeypatch) -> None:
    monkeypatch.setattr(mnemo, "_embed_off_applied", False)
    # A flag set outside Xu (user env) survives enabling via the toggle —
    # only the toggle's own OFF may turn it back on.
    monkeypatch.setenv("MNEMOSYNE_EMBEDDINGS_OFF", "1")
    mnemo.apply_embeddings(True)
    assert not mnemo.embeddings_enabled()
    mnemo.apply_embeddings(False)
    assert not mnemo.embeddings_enabled()
