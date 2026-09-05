from xu_brain.features.session import Message, SessionStore


def test_session_event_log_and_display_survives_compaction(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(session.id, Message(role="user", content="hello"))
    store.append(session.id, Message(role="assistant", content="hi"))

    events = store.events(session.id)
    assert [event["kind"] for event in events] == ["message.appended", "message.appended"]
    assert [m["content"] for m in store.display(session.id)] == ["hello", "hi"]

    # Compaction shrinks the model context; the display transcript must not lose
    # a row the frontend already showed.
    store.replace_messages(session.id, [Message(role="user", content="compacted")])
    assert [m.content for m in store.get(session.id).messages] == ["compacted"]
    assert [m["content"] for m in store.display(session.id)] == ["hello", "hi"]
    assert store.events(session.id)[-1]["kind"] == "messages.replaced"


def test_display_caps_at_1000_dropping_oldest(tmp_path):
    import xu_brain.features.session as sess

    store = SessionStore(tmp_path)
    store._display_cap = 5  # same code path, smaller window
    session = store.create()
    for i in range(8):
        store.append(session.id, Message(role="user", content=f"m{i}"))
    assert [m["content"] for m in store.display(session.id)] == ["m3", "m4", "m5", "m6", "m7"]
    assert sess._DISPLAY_CAP == 1000


def test_display_survives_ten_compactions(tmp_path):
    """The stated requirement: a response stays visible after 10 compactions."""
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(session.id, Message(role="user", content="first question"))
    store.append(session.id, Message(role="assistant", content="first answer"))
    for i in range(10):
        store.append(session.id, Message(role="user", content=f"q{i}"))
        store.append(session.id, Message(role="assistant", content=f"a{i}"))
        store.replace_messages(session.id, [Message(role="system", content=f"summary {i}")])
    shown = [m["content"] for m in store.display(session.id)]
    assert shown[0] == "first question"
    assert shown[1] == "first answer"
    assert "a9" in shown
    assert len(store.get(session.id).messages) == 1  # model context stayed small


def test_delete_and_prune_clear_display_and_events(tmp_path):
    import contextlib

    store = SessionStore(tmp_path)
    session = store.create()
    store.append(session.id, Message(role="user", content="hello"))
    store.delete(session.id)
    assert store.events(session.id) == []
    assert store.display(session.id) == []

    other = store.create()
    store.append(other.id, Message(role="user", content="hi"))
    store._sessions.clear()
    with contextlib.closing(store._connect()) as conn, conn:
        conn.execute("DELETE FROM sessions WHERE id = ?", (other.id,))
    store.prune(keep=30)  # orphan rows must not outlive their session
    with contextlib.closing(store._connect()) as conn, conn:
        for table in ("messages", "display", "session_events"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE session_id <> '_meta'"
            ).fetchone()[0]
            assert n == 0, table


def test_delete_removes_session_events(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create()
    store.append(session.id, Message(role="user", content="hello"))
    store.delete(session.id)
    assert store.events(session.id) == []


def test_prune_keeps_newest_by_count(tmp_path):
    """Sessions are capped by count (newest by activity), not by age — the
    oldest is replaced when the limit is exceeded."""
    import contextlib
    store = SessionStore(tmp_path)
    ids = [store.create().id for _ in range(5)]
    # backdate updated_at so ordering is deterministic (oldest = lowest ts)
    base = 1000.0
    with contextlib.closing(store._connect()) as conn, conn:
        for i, sid in enumerate(ids):
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (base + i, sid))
    # keep 3 newest -> drop ids[0] and ids[1]
    removed = store.prune(keep=3)
    remaining = {s["id"] for s in store.list()}
    assert removed >= 2
    assert ids[0] not in remaining and ids[1] not in remaining
    assert {ids[2], ids[3], ids[4]} <= remaining
    assert len(remaining) == 3


def test_schema_validation():
    from xu_brain.features.tools.schema import validation_error
    schema = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
    assert validation_error({"name": "x"}, schema) is None
    assert "required property missing" in validation_error({}, schema)


async def _noop(*args, **kwargs):
    return None


def test_tool_output_schema_rejects_invalid_value():
    import asyncio
    from types import SimpleNamespace
    from xu_brain.features.tools.registry import ToolRegistry
    from xu_brain.features.tools.base import ToolResult
    from xu_brain.core.governance import ApprovalLevel
    class Structured:
        name = "structured"
        toolset = "test"
        description = "structured"
        approval = ApprovalLevel.NEVER
        schema = {"type": "object"}
        output_schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
        async def run(self, args, ctx):
            return ToolResult.ok("not an object", raw={"wrong": True})
    registry = ToolRegistry(None)
    registry.register(Structured())
    result = asyncio.run(registry.run("structured", {}, SimpleNamespace(session_id="s"), emit=_noop))
    assert result.error is True
    assert "invalid output" in result.output

