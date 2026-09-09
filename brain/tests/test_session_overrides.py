"""Per-session skill/tool overrides — runnable check.

    python -m pytest tests/test_session_overrides.py -q

Agent State (skills/tools) must be per-session: a session's toggles are
overrides on top of the global Config defaults, and every new session
follows the defaults (empty override set).

Guarantees:
- config stores per-session deltas; None clears; delete_session purges
- a session with no overrides resolves to the global default everywhere
- tool registry: schemas hidden + execution denied for session-disabled
- skills: catalog overlay, prompt injection on/off, stale override safe
- RPCs: session-scoped set never mutates the global state
"""
from __future__ import annotations

import asyncio
import types
from pathlib import Path

import pytest

from xu_brain.core.config import Config
from xu_brain.core.contract import RpcError
from xu_brain.features.skills import SkillState


@pytest.fixture()
def data_home(tmp_path: Path) -> Path:
    return tmp_path / "xu"

# ---------------------------------------------------------------------------
# Config: per-session override stores


def test_skill_override_roundtrip_and_default_follow(tmp_path: Path) -> None:
    cfg = Config(tmp_path)
    assert cfg.session_skill_overrides("s1") == {}  # new session = defaults
    cfg.set_session_skill("s1", "a", True)
    cfg.set_session_skill("s1", "b", False)
    assert cfg.session_skill_overrides("s1") == {"a": True, "b": False}
    # another session is untouched
    assert cfg.session_skill_overrides("s2") == {}
    # clearing with None returns the entry to "follow default"
    cfg.set_session_skill("s1", "a", None)
    assert cfg.session_skill_overrides("s1") == {"b": False}


def test_tool_override_and_delete_session_purge(tmp_path: Path) -> None:
    cfg = Config(tmp_path)
    cfg.set_session_tool("s1", "browser", False)
    cfg.set_session_tool("s1", "my_tool", True)
    assert cfg.session_tool_overrides("s1") == {"browser": False, "my_tool": True}
    cfg.set_session_tool("s1", "browser", None)
    cfg.set_session_tool("s1", "my_tool", None)
    assert cfg.session_tool_overrides("s1") == {}
    # re-set + delete_session must purge
    cfg.set_session_skill("s1", "x", True)
    cfg.set_session_tool("s1", "y", False)
    cfg.delete_session("s1")
    assert cfg.session_skill_overrides("s1") == {}
    assert cfg.session_tool_overrides("s1") == {}


# ---------------------------------------------------------------------------
# Tool registry: session-effective enable state


def _registry(tmp_path: Path):
    from xu_brain.core.governance import ApprovalManager
    from xu_brain.features.tools.registry import ToolRegistry

    reg = ToolRegistry(ApprovalManager("manual"))

    class _T:
        toolset = "demo"
        name = "demo_a"
        description = "a"
        schema = {"type": "object"}

    class _U:
        toolset = "demo"
        name = "demo_b"
        description = "b"
        schema = {"type": "object"}

    class _C:
        toolset = "custom"
        name = "demo_c"
        description = "c"
        schema = {"type": "object"}

    reg.register(_T())
    reg.register(_U())
    reg.register(_C())
    return reg


def test_tool_enabled_for_session_override(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    # Tool-name override (drop-ins): off demo_b only in s1
    reg.session_overrides = lambda sid: ({"demo_b": False} if sid == "s1" else {})
    assert reg.tool_enabled_for("demo_b") is True        # global
    assert reg.tool_enabled_for("demo_b", "s1") is False
    assert reg.tool_enabled_for("demo_a", "s1") is True  # untouched

    # Toolset override (built-ins): off "custom" in s1; wins over per-tool global on
    reg.session_overrides = lambda sid: ({"custom": False} if sid == "s1" else {})
    reg.enable_tool("demo_c", True)
    assert reg.tool_enabled_for("demo_c") is True
    assert reg.tool_enabled_for("demo_c", "s1") is False
    assert reg.tool_enabled_for("demo_a", "s1") is True


def test_schemas_for_model_hide_session_disabled(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.session_overrides = lambda sid: ({"demo_b": False} if sid == "s1" else {})
    assert {t["function"]["name"] for t in reg.schemas_for_model()} == {"demo_a", "demo_b", "demo_c"}
    assert {t["function"]["name"] for t in reg.schemas_for_model("s1")} == {"demo_a", "demo_c"}
    assert {t["function"]["name"] for t in reg.schemas_for_model("s2")} == {"demo_a", "demo_b", "demo_c"}


def test_run_denies_session_disabled_tool(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    reg.session_overrides = lambda sid: ({"demo_b": False} if sid == "s1" else {})
    ctx = types.SimpleNamespace(session_id="s1")

    async def go():
        return await reg.run("demo_b", {}, ctx, emit=lambda *a, **k: None)

    result = asyncio.run(go())
    assert result.error is True
    assert "disabled for this session" in result.output


# ---------------------------------------------------------------------------
# Skills engine: catalog overlay


def test_skills_list_session_overlay(tmp_path: Path) -> None:
    from xu_brain.features.skills import SkillsEngine

    engine = SkillsEngine(tmp_path)
    a = engine.create("alpha", "Alpha", "A", "# A", pack="custom")
    b = engine.create("beta", "Beta", "B", "# B", pack="custom")
    engine.enable(a)   # global default: alpha ambient
    engine.disable(b)  # global default: beta off-catalog

    # no session: global view
    ids = {r["id"] for r in engine.list()}
    assert a in ids

    engine.session_overrides = lambda sid: (
        {a: False, b: True} if sid == "s1" else {}
    )
    # session s1: alpha off, beta on
    rows = {r["id"]: r for r in engine.list(session_id="s1")}
    assert a not in rows
    assert b in rows and rows[b]["ambient"] is True
    # all=True keeps disabled rows for the management UI
    rows_all = {r["id"]: r for r in engine.list(all=True, session_id="s1")}
    assert rows_all[a]["ambient"] is False and rows_all[a]["state"] == SkillState.OFF
    # another session still follows the global default
    rows_s2 = {r["id"] for r in engine.list(session_id="s2")}
    assert a in rows_s2 and b not in rows_s2


# ---------------------------------------------------------------------------
# Agent prompt: injection follows the session


def _app(data_home: Path):
    from xu_brain.core.runtime import build_app

    return build_app(data_home)


def _prompt_for(app, session_id: str, text: str = "hi") -> str:
    from xu_brain.features.session import Message

    async def exercise():
        app.sessions.append(session_id, Message(role="user", content=text))
        return app.agent._build_messages(app.sessions.get(session_id))[0]["content"]

    return asyncio.run(exercise())


def test_prompt_session_skill_on_off(data_home: Path) -> None:
    app = _app(data_home)
    sid_on = app.skills.create("sess-on", "Sess On", "marker SESSON", "# SESSON body",
                               pack="custom", keywords=["flamegraph"])
    sid_off = app.skills.create("sess-off", "Sess Off", "marker SESOFF", "# SESOFF body",
                                pack="custom", keywords=["flamegraph2"])
    app.skills.enable(sid_off)  # global default: injected everywhere

    s1 = app.sessions.create(cwd=str(data_home)).id
    s2 = app.sessions.create(cwd=str(data_home)).id
    app.config.set_session_skill(s1, sid_off, False)  # s1: off
    app.config.set_session_skill(s1, sid_on, True)    # s1: on

    p1 = _prompt_for(app, s1)
    p2 = _prompt_for(app, s2)
    # s1: globally-enabled skill excluded, session-only skill included
    assert "# SESOFF body" not in p1
    assert "# SESSON body" in p1
    # s2: unchanged — follows the global default, and the session-only skill
    # from s1 must not leak in (read_body keeps it out of the LOADED loop)
    assert "# SESOFF body" in p2
    assert "# SESSON body" not in p2


def test_prompt_auto_match_excludes_session_disabled(data_home: Path) -> None:
    app = _app(data_home)
    sid = app.skills.create("sess-auto", "Sess Auto", "marker", "# marker body",
                            pack="custom", keywords=["flamegraph"])
    app.skills.enable(sid)

    s1 = app.sessions.create(cwd=str(data_home)).id
    app.config.set_session_skill(s1, sid, False)

    # the user message keyword-matches the skill; the session disabled it
    p1 = _prompt_for(app, s1, text="please draw a flamegraph")
    assert "marker body" not in p1

    s2 = app.sessions.create(cwd=str(data_home)).id
    p2 = _prompt_for(app, s2, text="please draw a flamegraph")
    assert "marker body" in p2


def test_prompt_stale_override_does_not_break_turn(data_home: Path) -> None:
    app = _app(data_home)
    s1 = app.sessions.create(cwd=str(data_home)).id
    app.config.set_session_skill(s1, "ghost/skill", True)  # never existed

    content = _prompt_for(app, s1)
    assert "# cwd (session)" in content  # prompt still built


# ---------------------------------------------------------------------------
# RPC: session-scoped setters never touch the global state


def test_skill_set_session_scoped(data_home: Path) -> None:
    app = _app(data_home)
    sid = app.skills.create("rpc-skill", "RPC Skill", "d", "# body", pack="custom")
    app.skills.enable(sid)
    session_id = app.sessions.create(cwd=str(data_home)).id

    async def go():
        await app._methods["skill.set"]({"id": sid, "enabled": False, "session_id": session_id})

    asyncio.run(go())
    # global state untouched
    assert app.skills._skills[sid].ambient is True
    # session view shows the override
    rows = {r["id"]: r for r in app.skills.list(all=True, session_id=session_id)}
    assert rows[sid]["ambient"] is False
    # config holds the delta
    assert app.config.session_skill_overrides(session_id) == {sid: False}


def test_skill_set_validates(data_home: Path) -> None:
    app = _app(data_home)
    session_id = app.sessions.create(cwd=str(data_home)).id

    async def go():
        return await app._methods["skill.set"]({"id": "nope", "enabled": True, "session_id": session_id})

    with pytest.raises(RpcError) as ei:
        asyncio.run(go())
    assert ei.value.code == -32602

    async def go2():
        sid = app.skills.create("v2", "V2", "d", "# b", pack="custom")
        return await app._methods["skill.set"]({"id": sid, "enabled": True, "session_id": "missing"})

    with pytest.raises(RpcError) as ei2:
        asyncio.run(go2())
    assert ei2.value.code == -32002


def test_tool_set_enabled_session_scoped(data_home: Path) -> None:
    app = _app(data_home)
    session_id = app.sessions.create(cwd=str(data_home)).id
    ts = app.registry.toolsets()[0]["toolset"]

    async def go():
        await app._methods["tool.set_enabled"]({"toolset": ts, "enabled": False, "session_id": session_id})

    asyncio.run(go())
    # global registry/config untouched
    assert app.registry.is_enabled(ts) is True
    assert app.config.toolset_enabled(ts, True) is True
    # session override recorded
    assert app.config.session_tool_overrides(session_id) == {ts: False}

    # and the model no longer sees that toolset's schemas in this session
    schemas = {t["function"]["name"] for t in app.registry.schemas_for_model(session_id)}
    for tool in app.registry.toolsets():
        if tool["toolset"] == ts:
            for t in tool["tools"]:
                assert t["name"] not in schemas
