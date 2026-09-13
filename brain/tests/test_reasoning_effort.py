"""Reasoning effort: the per-session pick, and how it reaches the wire.

The ladder is the union of what the providers behind a relay accept, so Xu
sends the picked level verbatim rather than translating it — the relay may
choose a different upstream per attempt, and guessing which one will serve the
request would mean sending a spelling that upstream may not read.

Two behaviours are load-bearing and pinned here:

* **absent is not "off"**. `None` sends no field at all, which leaves the
  provider's (or a relay's) own default in force. Sending nothing is the only
  way to say "you decide", so it must stay reachable and must not be confused
  with a level.
* **Anthropic has no `reasoning_effort`**. It takes an explicit thinking token
  budget, so the level is translated there instead of being passed through.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from xu_brain.core.config import Config
from xu_brain.features.agent.provider import (
    REASONING_EFFORTS,
    Provider,
    StopReason,
    _anthropic_stream,
    _openai_stream,
)


@pytest.fixture()
def data_home(tmp_path: Path) -> Path:
    return tmp_path / "xu"


SSE_DONE = b"data: [DONE]\n\n"


class _FakeMgr:
    async def get_key(self, provider_id: str) -> None:
        return None


def _sse_client(captured: list[dict]) -> httpx.AsyncClient:
    """A client that records the request body and answers with an empty stream."""

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(
            200, content=SSE_DONE, headers={"content-type": "text/event-stream"}
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def _drain(stream) -> list:
    return [ev async for ev in stream]


# --------------------------------------------------------------------------
# the ladder
# --------------------------------------------------------------------------

def test_ladder_is_the_full_union() -> None:
    """Seven levels: Mooxy's five plus DeepSeek's `off` and `max`."""
    assert REASONING_EFFORTS == (
        "off", "minimal", "low", "medium", "high", "xhigh", "max",
    )


# --------------------------------------------------------------------------
# [OI] wire: verbatim
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_openai_sends_the_level_verbatim() -> None:
    captured: list[dict] = []
    provider = Provider(id="p", type="openai-compatible", base_url="http://up.test/v1")
    async with _sse_client(captured) as client:
        await _drain(_openai_stream(
            provider, "m", [{"role": "user", "content": "hi"}], None, None,
            _FakeMgr(), None, reasoning_effort="xhigh", client=client,
        ))
    assert captured[0]["reasoning_effort"] == "xhigh"


@pytest.mark.asyncio
async def test_openai_omits_the_field_when_unset() -> None:
    """`None` must send nothing — that is what lets a relay default apply."""
    captured: list[dict] = []
    provider = Provider(id="p", type="openai-compatible", base_url="http://up.test/v1")
    async with _sse_client(captured) as client:
        await _drain(_openai_stream(
            provider, "m", [{"role": "user", "content": "hi"}], None, None,
            _FakeMgr(), None, client=client,
        ))
    assert "reasoning_effort" not in captured[0]


@pytest.mark.asyncio
async def test_openai_sends_off_as_a_value_not_an_omission() -> None:
    """`off` is a level, not "unset": it must cross the wire as a value.

    Omitting the field would let a relay's default (e.g. xhigh) turn thinking
    back on, which is the opposite of what the picker says.
    """
    captured: list[dict] = []
    provider = Provider(id="p", type="openai-compatible", base_url="http://up.test/v1")
    async with _sse_client(captured) as client:
        await _drain(_openai_stream(
            provider, "m", [{"role": "user", "content": "hi"}], None, None,
            _FakeMgr(), None, reasoning_effort="off", client=client,
        ))
    assert captured[0]["reasoning_effort"] == "off"


# --------------------------------------------------------------------------
# Anthropic wire: translated to a thinking budget
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_anthropic_translates_a_level_to_a_thinking_budget() -> None:
    captured: list[dict] = []
    provider = Provider(
        id="a", type="anthropic-compatible", base_url="http://a.test", models=["m"]
    )
    async with _sse_client(captured) as client:
        await _drain(_anthropic_stream(
            provider, "m", [{"role": "user", "content": "hi"}], None, None,
            _FakeMgr(), None, reasoning_effort="high", client=client,
        ))
    body = captured[0]
    # No `reasoning_effort` on this protocol — the budget is the control.
    assert "reasoning_effort" not in body
    assert body["thinking"] == {"type": "enabled", "budget_tokens": 16384}
    # Thinking must leave room for an answer beside the budget.
    assert body["max_tokens"] >= 16384 + 4096


@pytest.mark.asyncio
async def test_anthropic_leaves_thinking_off_for_off() -> None:
    captured: list[dict] = []
    provider = Provider(
        id="a", type="anthropic-compatible", base_url="http://a.test", models=["m"]
    )
    async with _sse_client(captured) as client:
        await _drain(_anthropic_stream(
            provider, "m", [{"role": "user", "content": "hi"}], None, None,
            _FakeMgr(), None, reasoning_effort="off", client=client,
        ))
    assert "thinking" not in captured[0]


@pytest.mark.asyncio
async def test_anthropic_omits_thinking_when_unset() -> None:
    captured: list[dict] = []
    provider = Provider(
        id="a", type="anthropic-compatible", base_url="http://a.test", models=["m"]
    )
    async with _sse_client(captured) as client:
        await _drain(_anthropic_stream(
            provider, "m", [{"role": "user", "content": "hi"}], None, None,
            _FakeMgr(), None, client=client,
        ))
    assert "thinking" not in captured[0]


# --------------------------------------------------------------------------
# config resolution
# --------------------------------------------------------------------------

def test_effort_defaults_to_none(data_home) -> None:
    """Unset everywhere → None, so no field is sent and the relay decides."""
    assert Config(data_home).session_reasoning_effort("s1") is None


def test_session_override_wins_over_the_global_default(data_home) -> None:
    cfg = Config(data_home)
    cfg.set("reasoning_effort", "low")
    assert cfg.session_reasoning_effort("s1") == "low"
    cfg.set_session_reasoning_effort("s1", "max")
    assert cfg.session_reasoning_effort("s1") == "max"
    # A sibling session still follows the global default.
    assert cfg.session_reasoning_effort("s2") == "low"


def test_clearing_the_session_override_falls_back(data_home) -> None:
    cfg = Config(data_home)
    cfg.set("reasoning_effort", "low")
    cfg.set_session_reasoning_effort("s1", "max")
    cfg.set_session_reasoning_effort("s1", None)
    assert cfg.session_reasoning_effort("s1") == "low"


def test_delete_session_forgets_the_effort(data_home) -> None:
    """A deleted session must not leak its pick into a future session id."""
    cfg = Config(data_home)
    cfg.set_session_reasoning_effort("s1", "max")
    cfg.delete_session("s1")
    assert cfg.session_reasoning_effort("s1") is None
    assert "s1" not in cfg.all().get("session_reasoning_effort", {})


def test_effort_survives_a_reload(data_home) -> None:
    cfg = Config(data_home)
    cfg.set_session_reasoning_effort("s1", "medium")
    assert Config(data_home).session_reasoning_effort("s1") == "medium"


# --------------------------------------------------------------------------
# the stream still behaves when an effort rides along
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_effort_does_not_disturb_the_stream_contract() -> None:
    body = (
        b'data: {"object":"chat.completion.chunk","choices":'
        b'[{"index":0,"delta":{"content":"hi"},"finish_reason":null}]}\n\n'
        b'data: {"object":"chat.completion.chunk","choices":'
        b'[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        + SSE_DONE
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    provider = Provider(id="p", type="openai-compatible", base_url="http://up.test/v1")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        events = await _drain(_openai_stream(
            provider, "m", [{"role": "user", "content": "hi"}], None, None,
            _FakeMgr(), None, reasoning_effort="max", client=client,
        ))
    assert "".join(ev.delta or "" for ev in events) == "hi"
    assert events[-1].stop_reason is StopReason.STOP


# --------------------------------------------------------------------------
# the RPC surface the picker drives
# --------------------------------------------------------------------------

def _app(data_home):
    from xu_brain.core.runtime import build_app

    return build_app(data_home)


def test_set_effort_round_trips_through_state(data_home) -> None:
    app = _app(data_home)

    async def exercise() -> None:
        sid = (await app.dispatch("session.create", {}))["session"]["id"]
        result = await app.dispatch("state.set_effort", {"session_id": sid, "effort": "xhigh"})
        assert result["reasoning_effort"] == "xhigh"
        assert (await app.dispatch("state.get", {"session_id": sid}))["reasoning_effort"] == "xhigh"

    asyncio.run(exercise())


def test_set_effort_with_null_clears_it(data_home) -> None:
    """Null is the "let the provider decide" option, so it must round-trip."""
    app = _app(data_home)

    async def exercise() -> None:
        sid = (await app.dispatch("session.create", {}))["session"]["id"]
        await app.dispatch("state.set_effort", {"session_id": sid, "effort": "max"})
        result = await app.dispatch("state.set_effort", {"session_id": sid, "effort": None})
        assert result["reasoning_effort"] is None
        assert (await app.dispatch("state.get", {"session_id": sid}))["reasoning_effort"] is None

    asyncio.run(exercise())


def test_set_effort_rejects_a_level_outside_the_ladder(data_home) -> None:
    """An unknown level would reach the wire and be silently ignored."""
    from xu_brain.core.contract import RpcError

    app = _app(data_home)

    async def exercise() -> None:
        sid = (await app.dispatch("session.create", {}))["session"]["id"]
        with pytest.raises(RpcError):
            await app.dispatch("state.set_effort", {"session_id": sid, "effort": "ultra"})

    asyncio.run(exercise())


def test_app_info_publishes_the_ladder(data_home) -> None:
    """The picker reads its options from here instead of keeping its own copy."""
    app = _app(data_home)

    async def exercise() -> None:
        info = await app.dispatch("app.info", {})
        assert info["reasoning_efforts"] == list(REASONING_EFFORTS)

    asyncio.run(exercise())
