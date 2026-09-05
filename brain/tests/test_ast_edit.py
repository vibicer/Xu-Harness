"""ast_edit — the rewrite must reach disk.

`ast-grep` only writes with `-U`, and `--json` suppresses the write even
alongside it. Without both facts the tool reported "rewrote N matches" for a
file it never touched, so this asserts the file content, not the message.

Run: brain/.venv/bin/python -m pytest tests/test_ast_edit.py -q
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from xu_brain.core.governance import ApprovalManager
from xu_brain.features.tools.ast import ast_edit, ast_grep
from xu_brain.features.tools.base import ToolContext
from xu_brain.features.tools.registry import ToolRegistry

if not (shutil.which("ast-grep") or shutil.which("sg")):
    pytest.skip("ast-grep binary not installed", allow_module_level=True)

SOURCE = """\
def main():
    print(42)
    print("hi")
"""


def _ctx(tmp_path: Path) -> ToolContext:
    registry = ToolRegistry(ApprovalManager("yolo"), output_cap=10_000)
    return ToolContext(
        data_home=tmp_path,
        session_id="test",
        turn_id="t0",
        cwd=str(tmp_path),
        agent=None,  # type: ignore[arg-type]
        events=None,  # type: ignore[arg-type]
        approvals=registry.approvals,
        providers=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        skills=None,  # type: ignore[arg-type]
        flat_plugins=None,  # type: ignore[arg-type]
        config={},
    )


@pytest.fixture
def target(tmp_path: Path) -> Path:
    path = tmp_path / "probe.py"
    path.write_text(SOURCE, "utf-8")
    return path


def test_rewrite_is_written_to_disk(target, tmp_path):
    result = asyncio.run(ast_edit.run({
        "pattern": "print($X)",
        "rewrite": "logging.info($X)",
        "paths": [str(target)],
        "lang": "python",
    }, _ctx(tmp_path)))

    assert not result.error, result.output
    assert result.output == "rewrote 2 matches", result.output
    body = target.read_text("utf-8")
    assert "logging.info(42)" in body and 'logging.info("hi")' in body
    assert "print(" not in body


def test_no_match_reports_zero_and_leaves_the_file_alone(target, tmp_path):
    before = target.read_text("utf-8")
    result = asyncio.run(ast_edit.run({
        "pattern": "nonexistent_call($X)",
        "rewrite": "other($X)",
        "paths": [str(target)],
        "lang": "python",
    }, _ctx(tmp_path)))

    assert not result.error, result.output
    assert result.output.startswith("rewrote 0 matches")
    assert target.read_text("utf-8") == before


def test_relative_path_resolves_against_cwd(target, tmp_path):
    result = asyncio.run(ast_edit.run({
        "pattern": "print($X)",
        "rewrite": "logging.info($X)",
        "paths": ["probe.py"],
        "lang": "python",
    }, _ctx(tmp_path)))

    assert result.output == "rewrote 2 matches", result.output
    assert "logging.info(42)" in target.read_text("utf-8")


def test_ast_grep_still_finds_matches(target, tmp_path):
    """Search must keep using --json; only the edit path drops it."""
    result = asyncio.run(ast_grep.run({
        "pattern": "print($X)",
        "path": str(target),
        "lang": "python",
    }, _ctx(tmp_path)))

    assert not result.error, result.output
    assert "print(42)" in result.output
