"""Small bounded async fan-out helpers and dependency wave scheduler."""
from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")
R = TypeVar("R")

async def run_bounded(  # noqa: UP047
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


# Tools with broad environment/state effects that act as sequential barriers.
_BARRIER_TOOLS: frozenset[str] = frozenset({
    "bash",
    "ask",
    "eval",
    "debug",
    "todo",
    "tool_create",
    "tool_remove",
    "preset_create",
    "manage_skill",
    "memory_edit",
    "retain",
    "mnemosyne_remember",
    "mnemosyne_forget",
    "subagent_interrupt",
    "subagent_message",
    "browse",
    "screenshot",
})

# Read actions for the git tool that are safe to run concurrently.
_GIT_READ_ACTIONS: frozenset[str] = frozenset({
    "status",
    "diff",
    "log",
    "show",
    "blame",
})


def _norm_path(p: str, cwd: str = "") -> str:
    """Normalize file path for conflict comparison."""
    if not p:
        return ""
    clean = p.partition(":")[0].strip()
    if not clean or clean.startswith(("http://", "https://")):
        return clean
    try:
        path_obj = Path(clean)
        if not path_obj.is_absolute() and cwd:
            path_obj = Path(cwd) / path_obj
        return str(path_obj.resolve())
    except Exception:
        return clean


def _paths_conflict(p1: str, p2: str) -> bool:
    """True if p1 and p2 refer to the same file, or one is an ancestor of the other."""
    if not p1 or not p2:
        return False
    if p1 == p2:
        return True
    try:
        path1 = Path(p1)
        path2 = Path(p2)
        return path1 in path2.parents or path2 in path1.parents
    except Exception:
        return False


def _tool_io(
    name: str, args: dict[str, Any], cwd: str = "", tool: Any = None
) -> tuple[frozenset[str], frozenset[str], bool]:
    """Classify tool call into (read_paths, write_paths, is_barrier)."""
    tool_name = str(name).strip().lower()

    if tool_name in _BARRIER_TOOLS:
        return frozenset(), frozenset(), True

    if (
        getattr(tool, "read_only", False)
        or tool_name == "read"
        or tool_name.endswith("_read")
        or tool_name.startswith("read_")
    ):
        p = _norm_path(str(args.get("path") or args.get("file") or ""), cwd)
        return frozenset({p}) if p else frozenset(), frozenset(), False

    if (
        getattr(tool, "write_only", False)
        or tool_name == "write"
        or tool_name.endswith("_write")
        or tool_name.startswith("write_")
    ):
        p = _norm_path(str(args.get("path") or args.get("file") or ""), cwd)
        return frozenset(), frozenset({p}) if p else frozenset(), False

    if tool_name == "edit":
        patch = str(args.get("patch") or "")
        m = re.search(r"\[(?P<p>[^#\]\r\n]+)", patch)
        p_raw = m.group("p") if m else str(args.get("path") or args.get("file") or "")
        p = _norm_path(p_raw, cwd)
        return frozenset(), frozenset({p}) if p else frozenset(), False

    if tool_name == "ast_edit":
        paths = args.get("paths") or []
        if isinstance(paths, list):
            w_paths = frozenset(_norm_path(str(p), cwd) for p in paths if p)
            return frozenset(), w_paths, False
        return frozenset(), frozenset(), False
    if tool_name == "undo":
        p_raw = str(args.get("path") or args.get("file") or "")
        if p_raw:
            p = _norm_path(p_raw, cwd)
            return frozenset(), frozenset({p}) if p else frozenset(), False
        return frozenset(), frozenset(), True


    if tool_name in ("grep", "glob", "ast_grep"):
        raw_p = str(args.get("path") or "")
        p = _norm_path(raw_p, cwd) if raw_p else (_norm_path(cwd, cwd) if cwd else "")
        return frozenset({p}) if p else frozenset(), frozenset(), False

    if tool_name == "lsp":
        action = str(args.get("action") or "hover").lower()
        p = _norm_path(str(args.get("file") or ""), cwd)
        if action == "rename":
            return frozenset(), frozenset({p}) if p else frozenset(), False
        return frozenset({p}) if p else frozenset(), frozenset(), False

    if tool_name == "git":
        action = str(args.get("action") or "status").lower()
        if action in _GIT_READ_ACTIONS:
            raw_p = str(args.get("path") or "")
            p = _norm_path(raw_p, cwd) if raw_p else ""
            return frozenset({p}) if p else frozenset(), frozenset(), False
        if action in ("branch", "stash"):
            subaction = str(args.get("subaction") or "").lower()
            is_branch_mod = bool(args.get("branch_name") or args.get("name"))
            if subaction in ("", "list", "show") and not is_branch_mod:
                return frozenset(), frozenset(), False
        return frozenset(), frozenset(), True

    if tool_name == "github":
        method = str(args.get("method") or "GET").upper()
        if method == "GET":
            return frozenset(), frozenset(), False
        return frozenset(), frozenset(), True

    if tool_name in ("inspect_image", "show_image"):
        p = _norm_path(str(args.get("path") or ""), cwd)
        return frozenset({p}) if p else frozenset(), frozenset(), False

    if tool_name in (
        "web_search",
        "web_extract",
        "delegate",
        "recall",
        "reflect",
        "mnemosyne_recall",
        "skill_list",
        "skill_load",
        "tool_list",
        "subagent_list",
    ):
        return frozenset(), frozenset(), False

    # Default unknown tools to barrier for safety
    return frozenset(), frozenset(), True


def calls_conflict(
    spec_a: tuple[frozenset[str], frozenset[str], bool],
    spec_b: tuple[frozenset[str], frozenset[str], bool],
) -> bool:
    """Return True if two tool call specs cannot run concurrently."""
    reads_a, writes_a, barrier_a = spec_a
    reads_b, writes_b, barrier_b = spec_b

    if barrier_a or barrier_b:
        return True

    for w_a in writes_a:
        for w_b in writes_b:
            if _paths_conflict(w_a, w_b):
                return True
        for r_b in reads_b:
            if _paths_conflict(w_a, r_b):
                return True

    for r_a in reads_a:
        for w_b in writes_b:
            if _paths_conflict(r_a, w_b):
                return True

    return False


def _default_extractor(call: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(call, (tuple, list)) and len(call) >= 4:
        return str(call[2]), dict(call[3]) if isinstance(call[3], dict) else {}
    if isinstance(call, dict):
        return str(call.get("name", "")), dict(call.get("args", {}))
    return getattr(call, "name", ""), getattr(call, "args", {})


def partition_tool_waves(  # noqa: UP047
    calls: Sequence[T],
    *,
    cwd: str = "",
get_name_and_args: Callable[[T], tuple[str, dict[str, Any]]] | None = None,
tool_lookup: Callable[[str], Any] | None = None,
) -> list[list[T]]:
    """Partition tool calls into waves of mutually non-conflicting calls.

    Each wave contains calls that can execute concurrently. Consecutive waves
    execute sequentially, preserving ordering and dependencies across barriers
    or conflicting file I/O.
    """
    if not calls:
        return []
    if len(calls) == 1:
        return [list(calls)]

    extractor = get_name_and_args or _default_extractor

    specs = [
        _tool_io(
            *extractor(call),
            cwd=cwd,
            tool=tool_lookup(extractor(call)[0]) if tool_lookup else None,
        )
        for call in calls
    ]
    waves: list[list[T]] = []
    current_wave: list[T] = []
    current_specs: list[tuple[frozenset[str], frozenset[str], bool]] = []

    for call, spec in zip(calls, specs, strict=False):
        conflict = any(calls_conflict(existing_spec, spec) for existing_spec in current_specs)
        if conflict and current_wave:
            waves.append(current_wave)
            current_wave = [call]
            current_specs = [spec]
        else:
            current_wave.append(call)
            current_specs.append(spec)

    if current_wave:
        waves.append(current_wave)

    return waves


__all__ = ["run_bounded", "cancel_and_wait", "partition_tool_waves", "calls_conflict"]
