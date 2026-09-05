"""Attached images live on disk; the model context carries a path, never base64.

The composer sends an image as a data URL. Persisting that into the message
history meant every later turn replayed the whole base64 — expensive on a vision
model and fatal on a text-only one (the same 400, forever, with no way out).

So the bytes are written once to ``data_home/attachments/<session>/<sha1>.<ext>``
and the provider payload carries ``[image attached: <path>]``, which the agent
hands to ``inspect_image`` when it actually needs to look. The display transcript
keeps the data URL, so the shell still shows the thumbnail.

Run: python -m pytest tests/test_attachment_refs.py
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

import pytest

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
DATA_URL = "data:image/png;base64," + base64.b64encode(PNG).decode()


@pytest.fixture()
def app(tmp_path: Path):
    from xu_brain.core.runtime import build_app

    return build_app(tmp_path / "xu")


def _session_with_image(app, tmp_path: Path, url: str = DATA_URL, text: str = "what is this?"):
    from xu_brain.features.session import Message

    session = app.sessions.create(cwd=str(tmp_path))
    app.sessions.append(session.id, Message(role="user", content=[
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": url}},
    ]))
    return session


def _user_row(app, session) -> str:
    sent = app.agent._build_messages(app.sessions.get(session.id))
    rows = [m for m in sent if m["role"] == "user"]
    assert rows, "expected a user row in the payload"
    return rows[-1]["content"]


def _referenced_path(content: str) -> Path:
    m = re.search(r"\[image attached: (\S+)", content)
    assert m, f"no image reference in: {content!r}"
    return Path(m.group(1))


def test_the_payload_carries_a_path_the_agent_can_inspect(app, tmp_path: Path) -> None:
    session = _session_with_image(app, tmp_path)

    content = _user_row(app, session)

    assert isinstance(content, str)
    assert "data:image" not in content
    assert "what is this?" in content
    assert "inspect_image" in content
    assert _referenced_path(content).read_bytes() == PNG


def test_the_whole_payload_is_free_of_base64(app, tmp_path: Path) -> None:
    """Also the cure for a session poisoned before this change: legacy rows are
    dereferenced on replay, so a text-only model stops 400ing forever."""
    session = _session_with_image(app, tmp_path)

    sent = app.agent._build_messages(app.sessions.get(session.id))

    assert "data:image" not in json.dumps(sent)


def test_the_display_row_keeps_the_data_url_for_the_thumbnail(app, tmp_path: Path) -> None:
    session = _session_with_image(app, tmp_path)
    app.agent._build_messages(app.sessions.get(session.id))

    shown = app.sessions.display(session.id)[-1]["content"]

    assert any(p.get("image_url", {}).get("url") == DATA_URL for p in shown)


def test_the_same_image_is_stored_once(app, tmp_path: Path) -> None:
    session = _session_with_image(app, tmp_path)

    first = _referenced_path(_user_row(app, session))
    second = _referenced_path(_user_row(app, session))

    assert first == second
    assert list(first.parent.iterdir()) == [first]


def test_a_remote_url_is_already_a_reference(app, tmp_path: Path) -> None:
    session = _session_with_image(app, tmp_path, url="https://example.com/cat.png")

    content = _user_row(app, session)

    assert "https://example.com/cat.png" in content
    assert not (tmp_path / "xu" / "attachments").exists()


def test_a_queued_send_splices_a_reference_not_base64(app, tmp_path: Path) -> None:
    """Queued rows bypass `_build_messages` — they are spliced straight into the
    live message list mid-turn, so they need the same treatment."""
    session = app.sessions.create(cwd=str(tmp_path))
    app.agent._queued[session.id] = [("q1", "and this one?", [DATA_URL])]
    messages: list[dict] = []

    app.agent._drain_queue(session.id, messages)

    assert messages, "expected the queued row to be spliced"
    assert "data:image" not in json.dumps(messages)
    assert _referenced_path(messages[0]["content"]).read_bytes() == PNG


def test_deleting_a_session_removes_its_attachments(app, tmp_path: Path) -> None:
    session = _session_with_image(app, tmp_path)
    stored = _referenced_path(_user_row(app, session))
    assert stored.is_file()

    app.sessions.delete(session.id)

    assert not stored.exists()
    assert not stored.parent.exists()


def test_inspect_image_is_the_only_place_pixels_go(app, tmp_path: Path) -> None:
    """The tool builds its own single-message request with the vision model, so a
    text-only session model never sees an image and never 400s on it."""
    import asyncio
    from types import SimpleNamespace

    from xu_brain.features.tools.image import inspect_image

    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG)
    seen: dict[str, object] = {}

    async def chat_stream(provider, model, messages, **kw):
        seen.update(provider=provider, model=model, messages=messages)
        yield SimpleNamespace(delta="a red dot", error=None)

    ctx = SimpleNamespace(
        cwd=str(tmp_path),
        config={"model": "text-only-model", "vision_model": "eyes-9000"},
        providers=SimpleNamespace(
            resolve=lambda m: (SimpleNamespace(id="p1"), m),
            list=lambda: [],
            chat_stream=chat_stream,
        ),
    )

    result = asyncio.run(inspect_image.run({"path": str(shot), "question": "what?"}, ctx))

    assert result.output == "a red dot"
    assert seen["model"] == "eyes-9000"
    assert len(seen["messages"]) == 1, "no session history rides along"
    assert "data:image/png;base64," in json.dumps(seen["messages"])


# --- the attach turn itself: pixels go out, but never into history -----------
#
# History always holds `[image attached: <path>]` (the tests above), so the model
# sees an image exactly once — on the turn it was attached. A model that turns out
# to be blind fails that one request, gets retried without the image, and the next
# turn is clean either way.


def _turn_agent(vision: str | None, session_model: str, stream):
    """Bare Agent wired to a fake provider, enough to drive `_loop` once."""
    from types import SimpleNamespace

    from xu_brain.features.agent.loop import Agent

    agent = Agent(None, None, None, None, None, None, None, None, None, None)
    agent.config = SimpleNamespace(
        retry_max=lambda: 0,
        retry_interval=lambda: 0,
        session_max_tokens=lambda _sid: None,
        session_model=lambda _sid: session_model,
        vision_model=lambda: vision,
        model_fallbacks=lambda: [],
        get=lambda _key, default=None: default,
        all=lambda: {},
    )
    agent.providers = SimpleNamespace(
        resolve=lambda model, _p=None: (SimpleNamespace(id="prov"), model) if model else None,
        chat_stream=stream,
    )
    agent.registry = SimpleNamespace(schemas_for_model=lambda: [], reset_breakers=lambda: None)
    agent._build_messages = lambda _session, node=None: [{
        "role": "user",
        "content": "look at this\n\n[image attached: /tmp/x.png — call inspect_image on it]",
    }]

    async def noop(*_args, **_kwargs):
        return None

    agent._maybe_compress = noop
    agent._emit_context = noop
    return agent


def _drive(agent, images):
    """Run one turn; return (calls, notices, result)."""
    import asyncio
    from types import SimpleNamespace

    from xu_brain.core.notify import notify

    notices: list[str] = []

    async def fake_emit(event: str, **kw: object) -> None:
        if event == "turn.notice" and kw.get("text"):
            notices.append(str(kw["text"]))

    old = notify.emit
    notify.emit = fake_emit  # type: ignore[assignment]
    try:
        result = asyncio.run(agent._loop(
            SimpleNamespace(id="sess", cwd="/tmp", messages=[]), "t1", asyncio.Event(), images))
    finally:
        notify.emit = old  # type: ignore[assignment]
    return notices, result


def _stream_recorder(calls: list[tuple[str, str]], blind: bool):
    from xu_brain.features.agent.provider import StopReason, StreamEvent

    async def stream(_provider, model, messages, *_args, **_kwargs):
        payload = json.dumps(messages)
        calls.append((model, payload))
        if blind and "data:image" in payload:
            yield StreamEvent(error="400 this model does not support image input", retryable=False)
            return
        yield StreamEvent(delta="a bakery recruitment poster")
        yield StreamEvent(stop_reason=StopReason.STOP)

    return stream


def test_the_attach_turn_sends_the_pixels_to_the_vision_model() -> None:
    calls: list[tuple[str, str]] = []
    agent = _turn_agent("eyes-9000", "text-brain", _stream_recorder(calls, blind=False))

    notices, result = _drive(agent, [DATA_URL])

    assert len(calls) == 1
    assert calls[0][0] == "eyes-9000", "an image-bearing turn routes to the vision model"
    assert "data:image/png;base64," in calls[0][1], "the model actually got the pixels"
    assert "bakery" in (result.text or "")
    assert notices == []


def test_a_blind_vision_model_is_reported_and_the_turn_survives() -> None:
    """vision_model set but broken: say so, finish the turn on the session model,
    leave nothing behind that can break the next one."""
    calls: list[tuple[str, str]] = []
    agent = _turn_agent("eyes-9000", "text-brain", _stream_recorder(calls, blind=True))

    notices, result = _drive(agent, [DATA_URL])

    assert len(calls) == 2, "one blind attempt, one retry without the image"
    assert calls[0][0] == "eyes-9000" and "data:image" in calls[0][1]
    assert calls[1][0] == "text-brain", "fell back to the session model to answer"
    assert "data:image" not in calls[1][1]
    assert "[image attached:" in calls[1][1], "the path survives the strip"
    assert any("eyes-9000" in n for n in notices), notices
    assert "[error]" not in (result.text or "")


def test_no_vision_model_configured_says_so_instead_of_failing() -> None:
    calls: list[tuple[str, str]] = []
    agent = _turn_agent(None, "text-brain", _stream_recorder(calls, blind=True))

    notices, result = _drive(agent, [DATA_URL])

    assert [c[0] for c in calls] == ["text-brain", "text-brain"], "no vision model to route to"
    assert "data:image" not in calls[1][1]
    assert any("set a vision model" in n.lower() for n in notices), notices
    assert "[error]" not in (result.text or "")
