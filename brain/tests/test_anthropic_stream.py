"""Regression tests: Anthropic-compatible stream tool-call flushing.

Root cause guarded here: ``cur_tool`` was flushed only on a
``content_block_stop`` event. A gateway that ends the stream with ``[DONE]``
(or just EOF) before closing the block therefore dropped the tool call the
model made -- the loop saw no tool calls and no stop reason, ended the turn
with "[error] model returned no text this turn", and the model repeated the
request. The post-loop flush must emit it, and must not double-emit it when a
``content_block_stop`` *did* arrive.
"""

from __future__ import annotations

import json

import httpx

from xu_brain.features.agent.provider import Provider, _anthropic_stream


def _event(event_type: str, payload: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload)}"


def _sse(events: list[str], *, done: bool = True) -> bytes:
    body = "\n\n".join(events)
    if done:
        body += "\n\ndata: [DONE]"
    return (body + "\n\n").encode()


def _tool_start(call_id: str, name: str) -> str:
    return _event(
        "content_block_start",
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": call_id, "name": name},
        },
    )


def _arg_delta(partial: str) -> str:
    return _event(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": partial},
        },
    )


def _block_stop() -> str:
    return _event("content_block_stop", {"type": "content_block_stop", "index": 0})


async def _run(sse_body: bytes):
    def handler(request):
        assert request.method == "POST"
        return httpx.Response(
            200, content=sse_body, headers={"content-type": "text/event-stream"}
        )

    class FakeMgr:
        async def get_key(self, provider_id):
            return None

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = Provider(id="test", type="anthropic-compatible", base_url="http://up.test")
    try:
        return [
            ev
            async for ev in _anthropic_stream(
                provider, "m", [{"role": "user", "content": "hi"}], None, None,
                FakeMgr(), None, client=client,
            )
        ]
    finally:
        await client.aclose()


def _calls(events):
    return [ev.tool_call for ev in events if ev.tool_call is not None]


async def test_tool_call_flushed_on_done_without_content_block_stop():
    """[DONE] arrives before the block was closed: the call must survive."""
    events = await _run(_sse([
        _tool_start("toolu_1", "bash"),
        _arg_delta('{"cmd"'),
        _arg_delta(': "ls"}'),
    ]))
    calls = _calls(events)
    assert [c.id for c in calls] == ["toolu_1"]
    assert calls[0].name == "bash"
    assert json.loads(calls[0].arguments) == {"cmd": "ls"}


async def test_tool_call_flushed_on_eof_without_done():
    """Same, for a gateway that drops the connection with no sentinel."""
    events = await _run(_sse([
        _tool_start("toolu_2", "web_extract"),
        _arg_delta('{"url": "https://x"}'),
    ], done=False))
    calls = _calls(events)
    assert [c.id for c in calls] == ["toolu_2"]
    assert json.loads(calls[0].arguments) == {"url": "https://x"}


async def test_closed_block_is_not_flushed_twice():
    """A well-behaved stream (content_block_stop, then [DONE]) emits once:
    the post-loop flush must not re-emit an already-closed block, or the
    assistant row declares the same tool_use id twice."""
    events = await _run(_sse([
        _tool_start("toolu_3", "bash"),
        _arg_delta('{"cmd": "ls"}'),
        _block_stop(),
    ]))
    assert [c.id for c in _calls(events)] == ["toolu_3"]
