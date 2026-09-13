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

def test_partition_tool_waves_pure_reads():
    from xu_brain.features.agent.scheduler import partition_tool_waves

    calls = [
        ({"id": "c1"}, {}, "read", {"path": "a.py"}),
        ({"id": "c2"}, {}, "read", {"path": "b.py"}),
        ({"id": "c3"}, {}, "grep", {"pattern": "foo", "path": "c.py"}),
        ({"id": "c4"}, {}, "glob", {"pattern": "*.py"}),
        ({"id": "c5"}, {}, "git", {"action": "status"}),
        ({"id": "c6"}, {}, "delegate", {"prompt": "subtask"}),
    ]
    waves = partition_tool_waves(calls, cwd="/repo")
    assert len(waves) == 1
    assert len(waves[0]) == 6


def test_partition_tool_waves_disjoint_edits():
    from xu_brain.features.agent.scheduler import partition_tool_waves

    calls = [
        ({"id": "c1"}, {}, "edit", {"patch": "[a.py#TAG]\nPUT 1:\n+new"}),
        ({"id": "c2"}, {}, "edit", {"patch": "[b.py#TAG]\nPUT 1:\n+new"}),
        ({"id": "c3"}, {}, "write", {"path": "c.py", "content": "hello"}),
        ({"id": "c4"}, {}, "read", {"path": "d.py"}),
    ]
    waves = partition_tool_waves(calls, cwd="/repo")
    assert len(waves) == 1
    assert len(waves[0]) == 4


def test_partition_tool_waves_conflicting_file_rw():
    from xu_brain.features.agent.scheduler import partition_tool_waves

    calls = [
        ({"id": "c1"}, {}, "write", {"path": "a.py", "content": "1"}),
        ({"id": "c2"}, {}, "read", {"path": "a.py"}),
    ]
    waves = partition_tool_waves(calls, cwd="/repo")
    assert len(waves) == 2
    assert [c[2] for c in waves[0]] == ["write"]
    assert [c[2] for c in waves[1]] == ["read"]


def test_partition_tool_waves_conflicting_edits_same_file():
    from xu_brain.features.agent.scheduler import partition_tool_waves

    calls = [
        ({"id": "c1"}, {}, "edit", {"patch": "[a.py#111111]\nPUT 1:\n+v1"}),
        ({"id": "c2"}, {}, "edit", {"patch": "[a.py#222222]\nPUT 2:\n+v2"}),
    ]
    waves = partition_tool_waves(calls, cwd="/repo")
    assert len(waves) == 2
    assert waves[0] == [calls[0]]
    assert waves[1] == [calls[1]]


def test_partition_tool_waves_barrier_bash():
    from xu_brain.features.agent.scheduler import partition_tool_waves

    calls = [
        ({"id": "c1"}, {}, "read", {"path": "a.py"}),
        ({"id": "c2"}, {}, "bash", {"command": "pytest"}),
        ({"id": "c3"}, {}, "read", {"path": "b.py"}),
    ]
    waves = partition_tool_waves(calls, cwd="/repo")
    assert len(waves) == 3
    assert [c[2] for c in waves[0]] == ["read"]
    assert [c[2] for c in waves[1]] == ["bash"]
    assert [c[2] for c in waves[2]] == ["read"]


def test_partition_tool_waves_git_read_vs_mutation():
    from xu_brain.features.agent.scheduler import partition_tool_waves

    calls = [
        ({"id": "c1"}, {}, "git", {"action": "status"}),
        ({"id": "c2"}, {}, "git", {"action": "diff"}),
        ({"id": "c3"}, {}, "git", {"action": "commit", "message": "feat: test"}),
        ({"id": "c4"}, {}, "git", {"action": "log"}),
    ]
    waves = partition_tool_waves(calls, cwd="/repo")
    assert len(waves) == 3
    assert [c[2] for c in waves[0]] == ["git", "git"]
    assert [c[2] for c in waves[1]] == ["git"]
    assert [c[2] for c in waves[2]] == ["git"]


def test_partition_tool_waves_empty_and_single():
    from xu_brain.features.agent.scheduler import partition_tool_waves

    assert partition_tool_waves([], cwd="/repo") == []
    single = [({"id": "c1"}, {}, "read", {"path": "a.py"})]
    assert partition_tool_waves(single, cwd="/repo") == [single]
