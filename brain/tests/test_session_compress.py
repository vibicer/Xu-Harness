"""Regression tests: ``session.compress`` must refuse while a turn is running.

Compaction rewrites the model-visible history, but a running turn holds its
own pre-compaction ``messages`` list and rewrites the store from that list at
turn end (``_commit_history``). Compressing mid-turn was therefore silently
reverted, while the append-only ``display`` table kept the "context compacted"
divider -- the transcript claimed a compaction that never happened and the
context meter lied. The guard mirrors ``session.delete``.
"""

from __future__ import annotations

import asyncio

import pytest

from xu_brain.core.contract import RpcError
from xu_brain.core.runtime import build_app


def test_compress_refused_while_turn_running(tmp_path):
    app = build_app(tmp_path)

    async def exercise():
        sid = (await app.dispatch("session.create", {}))["session"]["id"]
        other = (await app.dispatch("session.create", {}))["session"]["id"]
        # A running turn is registered as the not-done task driving it.
        app.agent._turns[sid] = asyncio.current_task()
        app.agent._turns[other] = asyncio.current_task()

        with pytest.raises(RpcError) as exc:
            await app.dispatch("session.compress", {"id": sid})

        # ... and it reports exactly what session.delete reports in the same
        # state, so the frontend needs one branch for both.
        with pytest.raises(RpcError) as del_exc:
            await app.dispatch("session.delete", {"id": other})
        assert exc.value.code == del_exc.value.code == -32004
        assert str(exc.value) == str(del_exc.value) == "turn in progress"
        assert exc.value.data == {"session_id": sid}
        assert del_exc.value.data == {"session_id": other}
        # Nothing was handed to the summarizer: no divider, no replaced rows.
        assert [e["kind"] for e in app.sessions.events(sid)] == []
        assert app.sessions.display(sid) == []

    asyncio.run(exercise())


def test_compress_allowed_once_the_turn_is_done(tmp_path):
    """The guard keys on the turn being *in flight*, not on a turn ever
    having existed: a finished turn must not block compaction."""
    app = build_app(tmp_path)
    started: list[str] = []

    async def fake_compress(session_id: str) -> None:
        started.append(session_id)

    app.agent.compress_session = fake_compress  # never call the real summarizer

    async def exercise():
        sid = (await app.dispatch("session.create", {}))["session"]["id"]
        app.agent._turns[sid] = asyncio.ensure_future(asyncio.sleep(0))
        await app.agent._turns[sid]  # the turn has finished

        result = await app.dispatch("session.compress", {"id": sid})
        assert result == {"ok": True, "async": True}
        await asyncio.sleep(0)  # let the off-path task run
        assert started == [sid]

    asyncio.run(exercise())
