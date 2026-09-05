from __future__ import annotations

import asyncio

from xu_brain.features.agent.scheduler import run_bounded


def test_run_bounded_overlaps_and_preserves_input_order():
    async def scenario():
        active = 0
        peak = 0

        async def worker(item: tuple[int, float]) -> str:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(item[1])
            active -= 1
            return str(item[0])

        result = await run_bounded([(0, .03), (1, .01), (2, .02)], worker, 2)
        return result, peak

    result, peak = asyncio.run(scenario())
    assert result == ["0", "1", "2"]
    assert peak == 2


def test_run_bounded_is_all_settled():
    async def scenario():
        async def worker(item: int) -> str:
            if item == 1:
                raise ValueError("broken shard")
            await asyncio.sleep(0)
            return str(item)

        return await run_bounded([0, 1, 2], worker, 3)

    result = asyncio.run(scenario())
    assert result[0] == "0"
    assert isinstance(result[1], ValueError)
    assert result[2] == "2"


def test_delegate_batch_runs_tasks_concurrently_and_keeps_failures():
    from types import SimpleNamespace
    from xu_brain.features.tools import agents as A
    from xu_brain.features.tools.base import ToolResult

    class Agent:
        async def run_subtask(self, prompt, ctx, **kwargs):
            if prompt == "bad":
                raise RuntimeError("shard failed")
            await asyncio.sleep(.01 if prompt == "slow" else 0)
            return prompt + " result"

        def begin_delegation(self, label, prompt, session):
            return label + "-run"

    ctx = SimpleNamespace(
        agent=Agent(), session_id="parent",
        config={"preset_depth": 0, "max_parallel_subagents": 2},
    )
    result = asyncio.run(A.delegate.run({"tasks": [
        {"label": "one", "prompt": "slow"},
        {"label": "two", "prompt": "bad"},
        {"label": "three", "prompt": "fast"},
    ]}, ctx))

    assert not result.error
    assert result.raw["failed"] == 1
    assert [item["index"] for item in result.raw["results"]] == [1, 2, 3]
    assert result.raw["results"][0]["output"] == "slow result"
    assert result.raw["results"][1]["status"] == "error"
    assert result.raw["results"][2]["output"] == "fast result"


def test_run_bounded_cancellation_stops_workers():
    async def scenario():
        started = asyncio.Event()
        finished = False

        async def worker(_item: int) -> str:
            nonlocal finished
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                finished = True
            return "done"

        task = asyncio.create_task(run_bounded([1], worker, 1))
        await started.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        return finished

    assert asyncio.run(scenario())
