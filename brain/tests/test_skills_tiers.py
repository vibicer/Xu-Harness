"""Tests for compact and full skill loading tiers."""
from __future__ import annotations

from pathlib import Path

import pytest

from xu_brain.features.skills import SkillsEngine, SkillState, _digest


def test_digest_extracts_structure_and_respects_cap() -> None:
    body = "# Heading\n\nFirst paragraph.\nSecond line.\n\n"
    body += "- bullet one\n* bullet two\n" + "x" * 1000
    digest = _digest(body, 60)
    assert digest.startswith("# Heading\nFirst paragraph.")
    assert "- bullet one" in digest or "* bullet two" in digest
    assert len(digest) <= 60
    assert _digest("") == ""


def test_overview_does_not_load_skill(tmp_path: Path) -> None:
    engine = SkillsEngine(tmp_path)
    skill_id = engine.create("writer", "Writer", "Help\nMore", "# Guide\n\nDo this.", pack="custom")
    skill = engine._skills[skill_id]
    assert engine.overview(skill_id) == "Writer — Help\n# Guide\nDo this."
    assert skill.state == SkillState.DEMAND
    assert skill.body is None


def test_overview_errors_and_uses_cached_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = SkillsEngine(tmp_path)
    skill_id = engine.create("writer", "Writer", "Help", "Cached body", pack="custom")
    with pytest.raises(KeyError):
        engine.overview("missing")
    engine.disable(skill_id)
    with pytest.raises(ValueError):
        engine.overview(skill_id)
    engine.enable(skill_id)
    engine.load(skill_id)
    monkeypatch.setattr(engine, "_read", lambda path: (_ for _ in ()).throw(AssertionError("read")))
    assert engine.overview(skill_id) == "Writer — Help\nCached body"
