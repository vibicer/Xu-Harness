"""Small bounded async fan-out helpers."""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

T = TypeVar("T")
R = TypeVar("R")


async def run_bounded(
    items: Sequence[T],
    worker: Callable[[T], Awaitable[R]],
    limit: int,
) -> list[R | BaseException]:
    """Run workers with a concurrency limit and return results in input order."""
    if not items:
        return []
    limit = max(1, min(int(limit), len(items)))
    semaphore = asyncio.Semaphore(limit)

    async def guarded(item: T) -> R:
        async with semaphore:
            return await worker(item)

    return await asyncio.gather(
        *(guarded(item) for item in items), return_exceptions=True
    )


async def cancel_and_wait(tasks: Sequence[asyncio.Task[object]]) -> None:
    """Cancel unfinished workers and consume their cancellation exceptions."""
    pending = [task for task in tasks if not task.done()]
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


__all__ = ["run_bounded", "cancel_and_wait"]
