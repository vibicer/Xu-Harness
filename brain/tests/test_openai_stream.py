"""Regression tests: OpenAI-compatible stream tool-call buffering.

Root cause guarded here: some upstreams (twohub-style relays behind
api-v2.efroxie.com) emit *parallel* tool calls at the same ``index: 0``.
Keying the arg buffer by index alone merged the two calls into one whose
``arguments`` is two concatenated JSON objects -- invalid JSON. That
string then lives in the conversation history, and the relay answers
every subsequent request with a 200 + immediate empty stream (no
``[DONE]``), poisoning the whole turn.

The parser must (1) buffer by call id when present, (2) never emit a
tool call whose arguments are not valid JSON.
"""

from __future__ import annotations

import json

import httpx

from xu_brain.features.agent.provider import Provider, StopReason, _openai_stream

OBJ = "chat.completion.chunk"
STOP_TC = {"object": OBJ, "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]}


def _sse(chunks):
    lines = ["data: " + json.dumps(c) for c in chunks]
    lines.append("data: [DONE]")
    return ("\n\n".join(lines) + "\n\n").encode()


def _tc(index, tc_id, name, args):
    fn = {}
    if name is not None:
        fn["name"] = name
    if args:
        fn["arguments"] = args
    tc = {"index": index, "function": fn}
    if tc_id is not None:
        tc["id"] = tc_id
        tc["type"] = "function"
    return tc


def _chunk(delta):
    return {"object": OBJ, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}


def _calls(events):
    return [ev.tool_call for ev in events if ev.tool_call is not None]


def _expect_stop(events, expected):
    stops = [ev.stop_reason for ev in events if ev.stop_reason is not None]
    assert stops and stops[-1] is expected


async def _run(sse_body):
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    class FakeMgr:
        async def get_key(self, provider_id):
            return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = Provider(id="test", type="openai-compatible", base_url="http://up.test/v1")
    try:
        return [
            ev
            async for ev in _openai_stream(
                provider, "m", [{"role": "user", "content": "hi"}], None, None,
                FakeMgr(), None, client=client,
            )
        ]
    finally:
        await client.aclose()


async def test_parallel_calls_same_index_stay_separate():
    """Exact failure shape: two calls, both index 0, distinct ids."""
    ch_a = []
    ch_a.append(_chunk({"tool_calls": [_tc(0, "call_1", "github", "")]}))
    ch_a.append(_chunk({"tool_calls": [_tc(0, None, None, '{"endpoint": "')]}))
    ch_a.append(_chunk({"tool_calls": [_tc(0, None, None, '/repos/x/y"}')]}))
    ch_a.append(_chunk({"tool_calls": [_tc(0, "call_2", "web_extract", "")]}))
    ch_a.append(_chunk({"tool_calls": [_tc(0, None, None, '{"url": "https://')]}))
    ch_a.append(_chunk({"tool_calls": [_tc(0, None, None, 'x"}')]}))
    events_a = await _run(_sse(ch_a + [STOP_TC]))
    calls_a = _calls(events_a)
    assert [c.id for c in calls_a] == ["call_1", "call_2"]
    assert [c.name for c in calls_a] == ["github", "web_extract"]
    for c in calls_a:
        json.loads(c.arguments)
    assert json.loads(calls_a[0].arguments) == {"endpoint": "/repos/x/y"}
    assert json.loads(calls_a[1].arguments) == {"url": "https://x"}
    _expect_stop(events_a, StopReason.TOOL)


async def test_corrupt_merged_arguments_are_dropped():
    """A single call with concatenated-JSON args must not be emitted;
    the stream must still terminate cleanly."""
    ch_b = []
    ch_b.append(_chunk({"tool_calls": [_tc(0, "call_1", "web_extract", "")]}))
    ch_b.append(_chunk({"tool_calls": [_tc(0, None, None, '{"endpoint": "/a"}{"url": "/b"}')]}))
    events_b = await _run(_sse(ch_b + [STOP_TC]))
    assert _calls(events_b) == []
    _expect_stop(events_b, StopReason.TOOL)


async def test_single_compliant_call_still_works():
    """Normal single-call stream behaves exactly as before."""
    ch_c = []
    ch_c.append(_chunk({"tool_calls": [_tc(0, "call_9", "bash", "")]}))
    ch_c.append(_chunk({"tool_calls": [_tc(0, None, None, '{"cmd": "ls"}')]}))
    events_c = await _run(_sse(ch_c + [STOP_TC]))
    calls_c = _calls(events_c)
    assert len(calls_c) == 1 and calls_c[0].name == "bash"
    assert json.loads(calls_c[0].arguments) == {"cmd": "ls"}
    _expect_stop(events_c, StopReason.TOOL)


def _sse_no_done(chunks):
    """Same wire shape, but the stream ends on EOF (no ``[DONE]`` sentinel)."""
    lines = ["data: " + json.dumps(c) for c in chunks]
    return ("\n\n".join(lines) + "\n\n").encode()


async def test_tool_call_flushed_on_done_without_finish_reason():
    """A relay that ends with ``[DONE]`` and never sends a finish_reason
    chunk must still surface the buffered call. Before the fix the buffer was
    flushed only inside the finish_reason branch, so the call vanished: the
    loop saw no tool calls and no stop reason and ended the turn with
    "[error] model returned no text this turn" -- and the model, seeing its
    call unanswered, repeated the request."""
    ch = []
    ch.append(_chunk({"tool_calls": [_tc(0, "call_1", "bash", "")]}))
    ch.append(_chunk({"tool_calls": [_tc(0, None, None, '{"cmd": "ls"}')]}))
    events = await _run(_sse(ch))  # [DONE], but no STOP_TC
    calls = _calls(events)
    assert [c.id for c in calls] == ["call_1"]
    assert calls[0].name == "bash"
    assert json.loads(calls[0].arguments) == {"cmd": "ls"}
    # The stream claimed no stop reason; none is invented for it either.
    assert [ev.stop_reason for ev in events if ev.stop_reason] == []


async def test_tool_call_flushed_on_eof_without_done():
    """Same, for a relay that drops the connection without ``[DONE]``."""
    ch = []
    ch.append(_chunk({"tool_calls": [_tc(0, "call_2", "web_extract", "")]}))
    ch.append(_chunk({"tool_calls": [_tc(0, None, None, '{"url": "https://x"}')]}))
    events = await _run(_sse_no_done(ch))
    calls = _calls(events)
    assert [c.id for c in calls] == ["call_2"]
    assert json.loads(calls[0].arguments) == {"url": "https://x"}


async def test_repeated_finish_reason_does_not_duplicate_tool_call():
    """A stream carrying a repeated finish_reason chunk must emit the call
    exactly once. Emitting it twice declares the same ``tool_call_id`` on two
    entries of the assistant row, and the loop appends two ``tool`` messages
    carrying that id."""
    ch = []
    ch.append(_chunk({"tool_calls": [_tc(0, "call_1", "bash", "")]}))
    ch.append(_chunk({"tool_calls": [_tc(0, None, None, '{"cmd": "ls"}')]}))
    events = await _run(_sse(ch + [STOP_TC, STOP_TC]))
    calls = _calls(events)
    assert [c.id for c in calls] == ["call_1"]
    assert json.loads(calls[0].arguments) == {"cmd": "ls"}
    _expect_stop(events, StopReason.TOOL)
