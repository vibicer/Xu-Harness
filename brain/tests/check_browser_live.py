"""Live check for the browser toolset — needs network + a Chrome binary.

Not part of the offline suite (see ``TestBrowserToolset`` in test_brain.py for
that). Run it when the browser tools misbehave against real sites:

    cd brain && python tests/check_browser_live.py

Covers:
  1. a Cloudflare-fronted page returns real text (UA no longer says headless)
  2. a plain static page still works (no regression)
  3. shutdown() leaves no Chrome process and no profile dir behind
  4. the orphan sweep reaps a profile whose owning brain is gone
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xu_brain.features.tools.browser import (  # noqa: E402
    _OWNER_FILE,
    _PROFILE_PREFIX,
    BrowserManager,
    _client,
)

# openai.com sits behind Cloudflare and served "Just a moment..." to the old UA.
CASES = (
    ("https://openai.com/index/gpt-6-astra/", 5_000, "GPT-6 Astra"),
    ("https://example.com", 50, "Example Domain"),
)


def _tree_size(pgid: int) -> int:
    """Processes still alive in the spawned Chrome's process group."""
    out = subprocess.run(
        ["ps", "-eo", "pgid="], capture_output=True, text=True, check=False
    ).stdout
    return sum(1 for line in out.split() if line.strip() == str(pgid))


def _report(name: str, ok: bool, detail: str) -> bool:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}\n        {detail}")
    return ok


async def check_page(url: str, min_chars: int, needle: str) -> bool:
    client = await _client()
    sid = await client.new_page()
    try:
        await client.navigate(url, sid)
        text = await client.evaluate_text(sid, html=False)
        title = await client.page_title(sid)
    finally:
        await client.detach_page(sid)
        await client.close()
    ok = len(text) >= min_chars and needle.lower() in (title + text).lower()
    return _report(url, ok, f"title={title!r} text={len(text)} chars (>= {min_chars})")


async def check_no_leak() -> bool:
    """The whole Chrome tree and its profile must be gone after shutdown()."""
    await BrowserManager.ensure()
    pgid, udd = BrowserManager._pgid, BrowserManager._udd
    before = _tree_size(pgid)
    assert udd is not None and (udd / _OWNER_FILE).exists(), "owner marker missing"
    await BrowserManager.shutdown()
    await asyncio.sleep(0.5)  # let the kernel finish reaping
    after = _tree_size(pgid)
    ok = before > 1 and after == 0 and not udd.exists()
    return _report(
        "shutdown leaves nothing behind",
        ok,
        f"tree {before} procs -> {after}; profile {udd.name} exists={udd.exists()}",
    )


async def check_sweep_reaps_orphan() -> bool:
    """A profile whose owning brain is gone gets reaped on the next spawn."""
    root = BrowserManager._profiles_root()
    orphan = root / f"{_PROFILE_PREFIX}liveorphan"
    orphan.mkdir(exist_ok=True)
    # PID 2**22 is above the default pid_max, so it cannot be a live process.
    (orphan / _OWNER_FILE).write_text(json.dumps({"brain_pid": 2**22, "pgid": None}))
    try:
        await BrowserManager.ensure()
        return _report(
            "orphan profile reaped on spawn",
            not orphan.exists(),
            f"{orphan.name} exists={orphan.exists()} (expected False)",
        )
    finally:
        await BrowserManager.shutdown()
        if orphan.exists():
            orphan.rmdir()


async def main() -> int:
    results: list[bool] = []
    try:
        print(f"UA override: {BrowserManager.ua()!r} (None until first spawn)")
        for case in CASES:
            results.append(await check_page(*case))
        print(f"UA in use: {BrowserManager.ua()!r}\n")
        results.append(await check_no_leak())
        results.append(await check_sweep_reaps_orphan())
        return 0 if all(results) else 1
    finally:
        await BrowserManager.shutdown()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
