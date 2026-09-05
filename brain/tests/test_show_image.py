"""`show_image` puts pixels in the transcript without paying for them in context.

The model-facing output stays one short line; the image itself rides on the chip
meta (`image` / `image_alt`), which must (a) be on the registry's allowlist so it
reaches the shell at all, and (b) survive the two step-merge paths that persist
the display transcript — otherwise the picture vanishes on reload.

Run: python -m pytest tests/test_show_image.py
"""
from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from xu_brain.core.governance import ApprovalManager
from xu_brain.features.agent.live import LiveMixin
from xu_brain.features.tools import image as image_mod
from xu_brain.features.tools.registry import _CHIP_META, ToolRegistry

# 1×1 transparent PNG.
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)


def _show(args: dict[str, Any], cwd: Path):
    ctx = SimpleNamespace(cwd=str(cwd), config={})
    return asyncio.run(image_mod.show_image.run(args, ctx))


class _Live(LiveMixin):
    """Minimal host for the two step-merge paths (no Agent, no bus)."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, dict[str, Any]]] = []

    def _emit_tool(self, turn_id: str, session_id: str):  # type: ignore[override]
        async def emit(event: str, **kw: Any) -> None:
            self.seen.append((event, kw))
        return emit


def test_png_becomes_a_data_url_and_the_model_never_sees_the_base64(tmp_path: Path) -> None:
    (tmp_path / "chart.png").write_bytes(PNG)

    res = _show({"path": "chart.png", "caption": "revenue by week"}, tmp_path)

    assert not res.error
    assert res.meta["image"].startswith("data:image/png;base64,")
    assert base64.b64decode(res.meta["image"].split(",", 1)[1]) == PNG
    assert res.meta["image_alt"] == "revenue by week"
    # the model-facing line names the file and stays small
    assert "chart.png" in res.output
    assert len(res.output) < 200
    assert "base64" not in res.output


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    res = _show({"path": "nope.png"}, tmp_path)

    assert res.error
    assert "not found" in res.output
    assert "image" not in res.meta


def test_non_image_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("hello", encoding="utf-8")

    res = _show({"path": "notes.txt"}, tmp_path)

    assert res.error
    assert "not an image" in res.output


def test_oversize_image_is_rejected_before_reading(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "huge.png").write_bytes(PNG)
    monkeypatch.setattr(image_mod, "MAX_IMAGE_BYTES", 10)

    res = _show({"path": "huge.png"}, tmp_path)

    assert res.error
    assert "too large" in res.output


def test_alt_text_falls_back_to_the_filename(tmp_path: Path) -> None:
    (tmp_path / "shot.png").write_bytes(PNG)

    res = _show({"path": "shot.png"}, tmp_path)

    assert res.meta["image_alt"] == "shot.png"


def test_chip_meta_is_allowed_through_the_registry() -> None:
    assert {"image", "image_alt"} <= _CHIP_META


def test_settled_step_keeps_the_image_so_it_survives_reload() -> None:
    live = _Live()
    step = {"kind": "tool", "tool": "show_image", "args": "path=shot.png",
            "status": "running", "call_id": "c1"}
    emit = live._steps_emit("t1", step, "s1")

    asyncio.run(emit("turn.tool", tool="show_image", args="path=shot.png", status="ok",
                     elapsed=0.02, output="shot.png",
                     image="data:image/png;base64,AAA", image_alt="shot.png"))

    assert step["image"] == "data:image/png;base64,AAA"
    assert step["image_alt"] == "shot.png"


def test_live_snapshot_chip_carries_the_image() -> None:
    snap: dict[str, Any] = {"steps": []}

    _Live()._merge_live(snap, "turn.tool", {
        "tool": "show_image", "args": "path=shot.png", "status": "ok", "elapsed": 0.02,
        "output": "shot.png", "image": "data:image/png;base64,AAA", "image_alt": "shot.png",
    })

    assert snap["steps"][0]["image"] == "data:image/png;base64,AAA"
    assert snap["steps"][0]["image_alt"] == "shot.png"


def test_registry_forwards_the_image_to_the_chip_event(tmp_path: Path) -> None:
    """End of the wire: tool meta → `turn.tool` chip, with the model kept clean."""
    (tmp_path / "shot.png").write_bytes(PNG)
    registry = ToolRegistry(ApprovalManager("yolo"))
    registry.register(image_mod.show_image)
    ctx = SimpleNamespace(cwd=str(tmp_path), config={})
    seen: list[dict[str, Any]] = []

    async def emit(event: str, **kw: Any) -> None:
        if event == "turn.tool":
            seen.append(kw)

    res = asyncio.run(registry.run("show_image", {"path": "shot.png"}, ctx, emit=emit))

    settled = seen[-1]
    assert settled["status"] == "ok"
    assert settled["image"].startswith("data:image/png;base64,")
    assert settled["image_alt"] == "shot.png"
    assert "base64" not in res.output


def test_a_shown_image_never_reaches_the_provider(tmp_path: Path) -> None:
    """A model without vision must not choke on show_image.

    The data URL lives on the chip step; `_build_messages` rebuilds provider rows
    from role/content (+tool_calls) only, so no base64 and no `steps` key ever
    goes out — while the display transcript keeps the picture for the shell.
    """
    import json as _json

    from xu_brain.core.runtime import build_app
    from xu_brain.features.session import Message

    app = build_app(tmp_path / "xu")
    session = app.sessions.create(cwd=str(tmp_path))
    app.sessions.append(session.id, Message(role="user", content="show me the chart"))
    app.sessions.append(session.id, Message(
        role="assistant",
        content="here it is",
        steps=[{"kind": "tool", "tool": "show_image", "args": "path=chart.png",
                "status": "ok", "output": "chart.png",
                "image": "data:image/png;base64,AAA", "image_alt": "chart.png"}],
    ))

    sent = app.agent._build_messages(app.sessions.get(session.id))

    assert sent, "expected a provider payload"
    assert not any("data:image" in _json.dumps(m) for m in sent)
    assert all("steps" not in m for m in sent)
    assert app.sessions.display(session.id)[-1]["steps"][0]["image"] == "data:image/png;base64,AAA"
