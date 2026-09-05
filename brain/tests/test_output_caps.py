"""Output-capture bounds for bash/eval — in-memory cap + spill.

A tool's registry `_cap` (16k chars) limits what the *model* sees; these
bounds limit what the *daemon/kernel* buffers so huge command/program output
can't balloon RAM regardless of the context cap.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from xu_brain.features.tools.base import ToolContext
from xu_brain.features.tools.terminal import _BASH_MAX_SPILL, _BASH_MAX_STREAM, _StreamBuffer


class TestStreamBuffer:
    def test_keeps_head_and_does_not_truncate_within_cap(self):
        buf = _StreamBuffer(1024, _BASH_MAX_SPILL)
        buf.write("a" * 900)
        assert buf.truncated is False
        assert buf.head() == "a" * 900
        assert buf.detail() == ""
        buf.close()

    def test_overflow_spills_and_sets_truncated(self):
        buf = _StreamBuffer(1024, 4096)  # small caps for speed
        buf.write("a" * 512)
        assert buf.truncated is False
        buf.write("b" * 2048)  # exceeds head cap -> spill to temp file
        assert buf.truncated is True
        assert buf.head() == "a" * 512  # head kept
        assert buf.spill_path() is not None  # temp file created
        assert buf.detail() != ""
        buf.close()

    def test_beyond_spill_cap_counts_dropped(self):
        buf = _StreamBuffer(256, 512)
        buf.write("a" * 200)            # head
        buf.write("b" * 400)            # spills
        buf.write("c" * 10_000)         # beyond spill cap -> dropped
        assert buf.truncated is True
        assert len(buf.head()) <= 256
        assert "dropped" in buf.detail()
        buf.close()

    def test_default_stream_cap(self):
        # Default is 64_000 bytes/stream; ours is 64 KiB.
        assert _BASH_MAX_STREAM == 64 * 1024
        assert _BASH_MAX_SPILL == 64 * 1024 * 1024


def _ctx(cwd: Path) -> ToolContext:
    return ToolContext(
        data_home=cwd, session_id="s", turn_id="t", cwd=str(cwd),
        agent=None, events=None, approvals=None, providers=None,
        memory=None, skills=None, flat_plugins=None, config={},
    )


class TestEvalOutputCap:
    async def _eval(self, E, ctx, code):
        return await E.eval.run({"code": code}, ctx)

    def test_large_stdout_is_bounded_and_marked(self, tmp_path: Path):
        from xu_brain.features.tools import eval_tool as E
        ctx = _ctx(tmp_path)

        async def main():
            r = await self._eval(E, ctx, "print('x' * 100000)")
            assert r.error is False
            assert "output truncated at 64 KB" in r.output
            # kernel-level cap keeps captured stdout well under the dump
            assert len(r.output) < 70_000
            await _close(E)

        asyncio.run(main())

    def test_small_output_has_no_truncation_marker(self, tmp_path: Path):
        from xu_brain.features.tools import eval_tool as E
        ctx = _ctx(tmp_path)

        async def main():
            r = await self._eval(E, ctx, "print('hi')\n" + "q = 1")
            assert r.error is False
            assert "truncated" not in r.output
            assert "hi" in r.output
            await _close(E)

        asyncio.run(main())


async def _close(E) -> None:
    t = E.close_session("s")
    if t:
        await t
