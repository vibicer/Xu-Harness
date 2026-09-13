"""Tool logs carry a human-readable action note and a working directory.

The chip line the shell renders is `tool · where · what the agent is doing`.
Two pieces of that come from the brain:

1. every tool schema offers an optional `note` — one short line the model
   writes per call ("Check frontend types"). It is chip-only: it never reaches
   the tool's own `run`, so a tool with `additionalProperties: false` cannot
   choke on it;
2. every `turn.tool` event carries `cwd`, the session working directory, so a
   shell chip can say *where* the work happened without guessing.

Runnable: `python -m pytest tests/test_tool_notes.py`
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from xu_brain.core.governance import ApprovalLevel, ApprovalManager
from xu_brain.features.tools.base import ToolResult
from xu_brain.features.tools.registry import NOTE_ARG, ToolRegistry, summarize_args


class _Echo:
    """A tool with a closed schema — the strictest shape `note` must survive."""

    name = "echo"
    toolset = "test"
    description = "echo"
    approval = ApprovalLevel.NEVER
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
        "additionalProperties": False,
    }
    output_schema = None

    def __init__(self) -> None:
        self.seen: dict[str, Any] | None = None

    async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
        self.seen = dict(args)
        return ToolResult.ok(str(args.get("text", "")))


def _registry(tool: Any) -> ToolRegistry:
    reg = ToolRegistry(ApprovalManager("yolo"))
    reg.register(tool)
    reg.enable(tool.toolset, True)
    return reg


def _ctx(cwd: str = "/home/vibi/ProjectAI/Xu") -> Any:
    return SimpleNamespace(session_id="s1", turn_id="t1", cwd=cwd, config={})


def test_every_tool_schema_offers_the_note_arg() -> None:
    reg = _registry(_Echo())
    fn = reg.schemas_for_model()[0]["function"]
    props = fn["parameters"]["properties"]
    assert NOTE_ARG in props
    assert props[NOTE_ARG]["type"] == "string"
    # optional: a model that ignores it still makes a valid call
    assert NOTE_ARG not in fn["parameters"].get("required", [])


def test_note_is_not_leaked_into_the_tools_own_args() -> None:
    tool = _Echo()
    reg = _registry(tool)
    events: list[dict[str, Any]] = []

    async def emit(event: str, **kw: Any) -> None:
        events.append({"event": event, **kw})

    import asyncio

    asyncio.run(
        reg.run("echo", {"text": "hi", NOTE_ARG: "Check frontend types"}, _ctx(), emit=emit)
    )
    assert tool.seen == {"text": "hi"}, "the note is chip metadata, not a tool argument"


def test_running_and_settled_chips_carry_note_and_cwd() -> None:
    reg = _registry(_Echo())
    events: list[dict[str, Any]] = []

    async def emit(event: str, **kw: Any) -> None:
        events.append({"event": event, **kw})

    import asyncio

    asyncio.run(
        reg.run("echo", {"text": "hi", NOTE_ARG: "Check frontend types"}, _ctx(), emit=emit)
    )
    chips = [e for e in events if e["event"] == "turn.tool"]
    assert [c["status"] for c in chips] == ["running", "ok"]
    for chip in chips:
        assert chip["note"] == "Check frontend types"
        assert chip["cwd"] == "/home/vibi/ProjectAI/Xu"


def test_a_failing_tool_still_reports_where_and_why_it_ran() -> None:
    class Boom(_Echo):
        async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
            raise RuntimeError("nope")

    reg = _registry(Boom())
    events: list[dict[str, Any]] = []

    async def emit(event: str, **kw: Any) -> None:
        events.append({"event": event, **kw})

    import asyncio

    asyncio.run(reg.run("echo", {"text": "hi", NOTE_ARG: "Try a thing"}, _ctx(), emit=emit))
    err = [e for e in events if e["event"] == "turn.tool" and e["status"] == "error"][0]
    assert err["note"] == "Try a thing"
    assert err["cwd"] == "/home/vibi/ProjectAI/Xu"


@pytest.mark.parametrize("note", ["", "   ", None])
def test_a_missing_note_is_omitted_rather_than_rendered_empty(note) -> None:
    reg = _registry(_Echo())
    events: list[dict[str, Any]] = []

    async def emit(event: str, **kw: Any) -> None:
        events.append({"event": event, **kw})

    import asyncio

    args = {"text": "hi"} if note is None else {"text": "hi", NOTE_ARG: note}
    asyncio.run(reg.run("echo", args, _ctx(), emit=emit))
    chips = [e for e in events if e["event"] == "turn.tool"]
    assert all(c.get("note") is None for c in chips)


def test_the_note_never_reaches_the_persisted_arg_summary() -> None:
    """`args` is what re-enters the prompt; the note is already on the chip."""
    summary = summarize_args({"path": "web/src/App.svelte", NOTE_ARG: "Read the shell"})
    assert "path=web/src/App.svelte" in summary
    assert "Read the shell" not in summary


def test_model_schema_does_not_mutate_the_tool_schema() -> None:
    tool = _Echo()
    reg = _registry(tool)
    reg.schemas_for_model()
    assert NOTE_ARG not in tool.schema["properties"]


@pytest.mark.parametrize("note", [17, True, {"text": "no"}, ["no"]])
async def test_invalid_note_metadata_cannot_break_a_valid_tool_call(note: Any) -> None:
    tool = _Echo()
    events: list[dict[str, Any]] = []

    async def emit(event: str, **kw: Any) -> None:
        events.append(kw)

    result = await _registry(tool).run("echo", {"text": "hi", NOTE_ARG: note}, _ctx(), emit=emit)
    assert not result.error
    assert all("note" not in event for event in events)
    assert tool.seen == {"text": "hi"}


async def test_note_is_bounded_and_one_line() -> None:
    events: list[dict[str, Any]] = []

    async def emit(event: str, **kw: Any) -> None:
        events.append(kw)

    await _registry(_Echo()).run("echo", {NOTE_ARG: "check\n\t" + "x" * 500}, _ctx(), emit=emit)
    assert events[0]["note"].startswith("check x")
    assert len(events[0]["note"]) == 160
    assert "\n" not in events[0]["note"]


async def test_returned_tool_errors_and_denials_keep_metadata() -> None:
    class Failed(_Echo):
        async def run(self, args: dict[str, Any], ctx: Any) -> ToolResult:
            return ToolResult.err("not found")

    reg = _registry(Failed())
    events: list[dict[str, Any]] = []

    async def emit(event: str, **kw: Any) -> None:
        events.append(kw)

    await reg.run("echo", {NOTE_ARG: "Find file"}, _ctx(), emit=emit)
    assert events[-1]["status"] == "error"
    assert events[-1]["note"] == "Find file"
    reg.enable("test", False)
    events.clear()
    result = await reg.run("echo", {NOTE_ARG: "Find file"}, _ctx(), emit=emit)
    assert result.error
    assert events[-1]["status"] == "error"
    assert events[-1]["note"] == "Find file"


async def test_steps_and_live_snapshots_preserve_metadata_on_reload(tmp_path: Any) -> None:
    from xu_brain.features.agent.live import LiveMixin
    from xu_brain.features.session import Message, SessionStore

    class Harness(LiveMixin):
        async def _emit_for(self, session_id: str, turn_id: str, event: str, **kw: Any) -> None:
            self._merge_live(live, event, kw)

    live: dict[str, Any] = {}
    harness = Harness()
    step = harness._capture_tool("echo", {NOTE_ARG: "Inspect file"}, call_id="one")
    assert step["note"] == "Inspect file"
    emit = harness._steps_emit("t", step, "s")
    await emit("turn.tool", tool="echo", status="running", note="Inspect file", cwd="/work")
    assert step["note"] == "Inspect file"
    assert step["cwd"] == "/work"
    # Partial completion should retain rather than erase the metadata.
    await emit("turn.tool", tool="echo", status="ok", output="done")
    assert live["steps"][0]["note"] == "Inspect file"
    assert live["steps"][0]["cwd"] == "/work"
    store = SessionStore(tmp_path)
    session = store.create(cwd="/work")
    store.append(session.id, Message(role="assistant", content="", steps=[step]))
    stored = store.display(session.id)[-1]["steps"][0]
    assert stored["note"] == "Inspect file"
    assert stored["cwd"] == "/work"


async def test_bash_uses_persistent_start_directory_and_session_override(tmp_path: Any) -> None:
    from xu_brain.features.tools import terminal

    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    ctx = _ctx(str(first))

    class TestBash(terminal.BashTool):
        output_schema = None

    tool = TestBash()
    # Use the real shell with a registry requiring no approval for these harmless commands.
    tool.approval = ApprovalLevel.NEVER
    reg = _registry(tool)
    events: list[dict[str, Any]] = []

    async def emit(event: str, **kw: Any) -> None:
        events.append(kw)

    import shlex

    try:
        await reg.run("bash", {"command": f"cd {shlex.quote(str(second))}"}, ctx, emit=emit)
        assert events[-1]["cwd"] == str(first), "log records the start cwd, not the final cwd"
        events.clear()
        await reg.run("bash", {"command": "pwd"}, ctx, emit=emit)
        assert all(event["cwd"] == str(second) for event in events)
        events.clear()
        # User changes the session directory; that overrides the persistent shell.
        ctx.cwd = str(tmp_path)
        await reg.run("bash", {"command": "pwd"}, ctx, emit=emit)
        assert all(event["cwd"] == str(tmp_path) for event in events)
    finally:
        shell = terminal._SHELLS.pop(ctx.session_id, None)
        if shell is not None:
            await shell.close()


def test_provider_history_strips_the_note_from_a_tool_call() -> None:
    import json

    from xu_brain.features.tools.logmeta import strip_note_arguments

    call = {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": "echo",
            "arguments": json.dumps({"text": "hi", NOTE_ARG: "Explain the call"}),
        },
    }
    clean = strip_note_arguments(call)
    assert json.loads(clean["function"]["arguments"]) == {"text": "hi"}
    assert NOTE_ARG in json.loads(call["function"]["arguments"]), "input remains untouched"


def test_bash_requires_an_action_note_without_changing_other_tool_schemas() -> None:
    class Shell(_Echo):
        name = "bash"

    tool = Shell()
    reg = _registry(tool)
    schema = reg.schemas_for_model()[0]["function"]["parameters"]
    assert NOTE_ARG in schema["required"]
    assert schema["properties"][NOTE_ARG]["minLength"] == 1
    assert schema["required"].count(NOTE_ARG) == 1
    assert NOTE_ARG not in tool.schema["required"]
    assert NOTE_ARG not in tool.schema["properties"]
    echo_schema = _registry(_Echo()).schemas_for_model()[0]["function"]["parameters"]
    assert NOTE_ARG not in echo_schema["required"]
