"""Focused tests for skill persistence and built-in read-only behavior."""
from __future__ import annotations

from pathlib import Path

import pytest

from xu_brain.features.skills import SkillsEngine


def test_save_preserves_unknown_frontmatter_keys(tmp_path: Path) -> None:
    skill_path = tmp_path / "skills" / "custom" / "extra" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        "---\n"
        "name: Extra\n"
        "description: Before\n"
        "keywords: [one]\n"
        "match_limit: 3\n"
        "custom_key: preserved\n"
        "extra_number: 7\n"
        "---\n"
        "Original body\n",
        encoding="utf-8",
    )

    engine = SkillsEngine(tmp_path)
    engine.save("custom/extra", name="Updated")

    metadata, body = engine._read(skill_path)
    assert metadata["custom_key"] == "preserved"
    assert metadata["extra_number"] == 7
    assert metadata["name"] == "Updated"
    assert body == "Original body\n"


def test_seeded_skills_are_managed_from_data_home(tmp_path: Path) -> None:
    engine = SkillsEngine(tmp_path)
    skill = next(skill for skill in engine.list(all=True) if skill["id"].startswith("general/"))
    skill_path = engine._skills[skill["id"]].path
    assert skill_path is not None
    engine.save(skill["id"], name="Changed")
    metadata, _ = engine._read(skill_path)
    assert metadata["name"] == "Changed"
    engine.remove(skill["id"])
    assert skill["id"] not in {row["id"] for row in engine.list(all=True)}


def test_read_body_preserves_state_and_reads_current_file(tmp_path: Path) -> None:
    engine = SkillsEngine(tmp_path)
    skill_id = engine.create("body-read", "Body Read", "Read body", "Initial body\n")
    assert engine._skills[skill_id].state == "DEMAND"

    assert engine.read_body(skill_id) == "Initial body\n"
    assert engine._skills[skill_id].state == "DEMAND"

    engine.disable(skill_id)
    assert engine.read_body(skill_id) == "Initial body\n"
    assert engine._skills[skill_id].state == "OFF"

    from xu_brain.core.runtime import build_app

    app = build_app(tmp_path)

    async def exercise() -> None:
        result = await app.dispatch("skill.body", {"id": skill_id})
        assert result == {"id": skill_id, "body": "Initial body\n"}

    import asyncio

    asyncio.run(exercise())


def test_a_pack_shipped_later_reaches_an_existing_install(tmp_path):
    """Seeding runs every boot, so a new builtin pack is not fresh-installs-only.

    It must still leave an existing pack alone — user edits and per-skill
    enable/disable state live in those files.
    """
    import shutil

    from xu_brain.features.skills import BUILTIN_PACK_DIR

    shutil.copytree(BUILTIN_PACK_DIR, tmp_path / "skills" / "general")
    marker = tmp_path / "skills" / "general" / "code-review" / "SKILL.md"
    marker.write_text(marker.read_text() + "\n<!-- user edit -->\n", encoding="utf-8")

    engine = SkillsEngine(tmp_path)

    ids = {row["id"] for row in engine.list(all=True)}
    assert "xu/authoring-plugins" in ids, "a pack added by a later version must be seeded"
    assert "<!-- user edit -->" in marker.read_text(), "an existing pack must not be overwritten"

def test_install_rejects_invalid_pack_without_creating_destination(tmp_path: Path) -> None:
    source = tmp_path / "bad-pack"
    source.mkdir()
    (source / "README.md").write_text("not a skill", encoding="utf-8")
    engine = SkillsEngine(tmp_path)

    with pytest.raises(ValueError, match="at least one"):
        engine.install(source)
    assert not (tmp_path / "skills" / "bad-pack").exists()


def test_create_places_skill_in_pack_structure(tmp_path: Path) -> None:
    engine = SkillsEngine(tmp_path)
    assert engine.create("writer", "Writer", "Writing help", "Do the thing.", pack="custom") == "custom/writer"
    skill = tmp_path / "skills" / "custom" / "writer" / "SKILL.md"
    assert skill.is_file()
    metadata, body = engine._read(skill)
    assert metadata["name"] == "Writer"
    assert "Do the thing." in body
