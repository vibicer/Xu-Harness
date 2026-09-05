"""Focused checks for hash-anchored edit guidance and no-op protection."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from xu_brain.features.tools.file import FileEditTool, _apply_ops


def test_inverted_absolute_range_explains_absolute_end() -> None:
    try:
        _apply_ops([f"line {i}" for i in range(1, 20)], [{"op": "PUT", "spec": "54.=19", "body": []}])
    except ValueError as exc:
        message = str(exc)
    else:  # pragma: no cover - assertion guard
        raise AssertionError("inverted range should fail")

    assert "start 54, end 19" in message
    assert "PUT 54.=72:" in message
    assert "PUT 54:" in message


def test_repeated_noop_edit_is_rejected_without_writing(tmp_path: Path) -> None:
    target = tmp_path / "sample.txt"
    target.write_text("same\n", encoding="utf-8")
    tool = FileEditTool()
    ctx = SimpleNamespace(cwd=str(tmp_path), config={})
    patch = f"[{target}]\nPUT 1:\n+same"

    results = [asyncio.run(tool.run({"patch": patch}, ctx)) for _ in range(3)]

    assert all(result.error for result in results)
    assert "no changes" in results[0].output
    assert "blocked after 3 attempts" in results[2].output
    assert target.read_text(encoding="utf-8") == "same\n"
