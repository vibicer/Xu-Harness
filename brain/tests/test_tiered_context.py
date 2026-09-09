"""Tiered context loading for skill/memory sections under budget constraints.

Tests that when ``context_skill_budget`` is tight, sections fall through a 3-tier
ladder instead of vanishing entirely:
  - Tier 1: full body (cost = len(body))
  - Tier 2: overview (~600–700 chars for skills via SkillsEngine.overview;
            memory with entry_cap=200)
  - Tier 3: drop

Run:
    python -m pytest tests/test_tiered_context.py tests/test_cwd_prompt.py -q


Marker strings used in this file — unique to avoid cross-test matching:
  TIERED_BANNER_8X9K2L — first line of a long skill body  
  MID_BODY_UNIQUE_TIERED — mid-body distinctive phrase
  MEMORY_TAIL_XQWERTY — tail content beyond 200 chars
"""
import asyncio
import tempfile
from pathlib import Path

import pytest


def _make_skill(data_home, sid, body):
    """Create a skill file on disk BEFORE app init."""
    skill_dir = data_home / "skills" / "general" / sid
    skill_dir.mkdir(parents=True)
    meta = f"name: {sid.replace('/', '-')}\ndescription: Test skill\nkeywords: test"
    front = f"---\n{meta}\ntype: loose\n---\n# {sid.replace('/', '-')}\n\n{body}"
    (skill_dir / "SKILL.md").write_text(front, "utf-8")


@pytest.fixture()
def data_home():
    return Path(tempfile.mkdtemp(prefix="xu-tc-"))


def _prompt(app, cwd, extra_message=""):
    from xu_brain.features.session import Message

    async def exercise():
        session = app.sessions.create(cwd=str(cwd))
        msg_content = f"hi testing {extra_message}" if extra_message else "hi"
        app.sessions.append(session.id, Message(role="user", content=msg_content))
        return app.agent._build_messages(app.sessions.get(session.id))[0]["content"]

    return asyncio.run(exercise())


def test_big_budget_full_bodies_present(data_home, tmp_path):
    """Big budget → Tier 1: full bodies present, no regression.
    
    Create a skill FIRST before app init. With context_skill_budget very large,
    both distinctive markers appear.
    """
    from xu_brain.core.runtime import build_app
    
    # Create skill file FIRST before app init
    _make_skill(data_home, "tierskill", 
                "TIERED_BANNER_8X9K2L\n\nThis should appear.\n\nMID_BODY_UNIQUE_TIERED")
    
    app = build_app(data_home)
    app.config.set("context_skill_budget", 1000000)
    project = tmp_path / "fullbody"
    project.mkdir()
    
    # Auto-match will pick up general/tierskill when we mention "tier"
    result = _prompt(app, project, extra_message="use tierskill")
    
    assert "TIERED_BANNER_8X9K2L" in result, "First line missing"
    assert "MID_BODY_UNIQUE_TIERED" in result, "Mid-body missing"


def test_tiny_budget_tier2_overviews_kick_in(data_home, tmp_path):
    """Tiny budget → Tier 2: overviews carry name, full mid-body drops.
    
    Create a long skill (> 800 chars). Name and first line appear (via tier 2),
    but mid-body-only distinctive does NOT.
    """
    from xu_brain.core.runtime import build_app
    
    # Create a very long skill body (> 800 chars)
    long_body = "# LongSkillTitle\n\nFIRST_LINE_UNIQUE_ABC\n\n"
    long_body += "X" * 1000  # filler
    long_body += "\nMID_BODY_UNIQUE_TIERED\n\n"
    long_body += "Y" * 500
    _make_skill(data_home, "longskill", long_body)
    
    app = build_app(data_home)
    app.config.set("context_skill_budget", 800)
    project = tmp_path / "tieredtier2"
    project.mkdir()
    
    result = _prompt(app, project, extra_message="ask about longskill")
    
    # Overview carries skill ID (which includes name-like text)
    assert "longskill" in result.lower() or "FIRST_LINE" in result, (
        "Name/first line should appear via tier 2"
    )
    # Mid-body distinctive should NOT appear since full body was dropped
    assert "MID_BODY_UNIQUE_TIERED" not in result, "full body leaked under tiny budget"


def test_zero_budget_uncapped_path(data_home, tmp_path):
    """Budget=0 → uncapped path unchanged: full bodies still appear.
    
    When context_skill_budget=0, mid-body distinctives DO appear.
    """
    from xu_brain.core.runtime import build_app
    
    _make_skill(data_home, "nocapskill", 
                "NOCAP_MIDBODY_UNIQUE_ZXC\n\nThis always appears at budget=0.")
    
    app = build_app(data_home)
    app.config.set("context_skill_budget", 0)  # disables cap
    project = tmp_path / "zerobudget"
    project.mkdir()
    
    result = _prompt(app, project, extra_message="use nocapskill")
    
    assert "NOCAP_MIDBODY_UNIQUE_ZXC" in result, (
        "Mid-body MUST appear when budget=0 (uncapped path)"
    )


def test_memory_tier_under_budget(tmp_path):
    """Memory tier: head appears via truncation, tail does not.
    
    Truncated memory (entry_cap=200) is ~122 chars; set budget slightly above.
    """
    from xu_brain.core.runtime import build_app
    
    data_home = Path(tempfile.mkdtemp(prefix="xu-tc-mem-"))
    app = build_app(data_home)
    # Budget must exceed truncated memory (~122 chars), use 200
    app.config.set("context_skill_budget", 200)
    project = tmp_path / "memtier"
    project.mkdir()
    
    # Memory entry with head (~100 chars) + tail (> 200 chars)
    unique_head = "MEMORY_HEAD_DISTINCTIVE_START_" + "A" * 80
    unique_tail = "MEMORY_TAIL_XQWERTY" + "B" * 200
    app.memory.append(f"{unique_head}\n\n{unique_tail}")
    
    result = _prompt(app, project)
    
    # Head should appear due to entry_cap=200 truncation
    assert "MEMORY_HEAD_DISTINCTIVE_START_" in result, "Memory head missing (expected tier 2)"
    
    # Tail beyond ~200 chars should NOT appear  
    # Word-boundary cut at entry_cap=200 keeps only the head; the tail is
    # fully absent from the system prompt.
    assert "MEMORY_TAIL_XQWERTY" not in result, "Memory tail leaked past entry_cap=200"


def test_cwd_survives_all_budget_modes(tmp_path):
    """Budget-exempt: cwd stamp survives in all three cases."""
    from xu_brain.core.runtime import build_app
    
    project = tmp_path / "cwdcheck"
    project.mkdir()
    
    # Each app needs its own data_home
    for budget_name, budget in [("big", 1000000), ("tiny", 800), ("zero", 0)]:
        data_home = Path(tempfile.mkdtemp(prefix=f"xu-cwd-{budget_name}-"))
        app = build_app(data_home)
        app.config.set("context_skill_budget", budget)
        prompt = _prompt(app, project)
        assert str(project) in prompt, (
            f"CWD missing with budget={budget} ({budget_name})"
        )
