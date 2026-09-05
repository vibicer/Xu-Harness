"""Runnable check: exercise the mnemosyne plugin through the real PluginHost.

Covers: activation + tool registration, the before_llm injection (digest into
the system prompt, capped), empty-recall passthrough, inject=off passthrough,
fail-soft when mnemosyne-memory is absent, and tool round-trips.

Run: `cd brain && uv run python xu_brain/plugins/builtin/mnemosyne/_check.py`
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

from xu_brain.core.bus import HookBus
from xu_brain.core.host import PluginHost


class FakeConfig:
    def __init__(self) -> None:
        self._d: dict = {}

    def all(self) -> dict:
        return dict(self._d)

    def set(self, k, v) -> None:
        self._d[k] = v


class FakeRegistry:
    def __init__(self) -> None:
        self.tools: dict = {}

    def register(self, tool):
        self.tools[tool.name] = tool
        return lambda: self.tools.pop(tool.name, None)


class FakeApp:
    def __init__(self, data_home: Path) -> None:
        self.data_home = data_home
        self.config = FakeConfig()
        self.registry = FakeRegistry()


def check(label: str, got, want) -> bool:
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}: {got!r}" + ("" if ok else f" != {want!r}"))
    return ok


async def main() -> int:
    results: list[bool] = []
    data_home = Path(tempfile.mkdtemp(prefix="mnem-check-"))
    pkg_src = Path(__file__).resolve().parent
    os.environ.pop("MNEMOSYNE_DATA_DIR", None)

    # ---- 1. full path: activate + hook + tools ------------------------ #
    app = FakeApp(data_home)
    host = PluginHost(HookBus())
    host.load_dir(pkg_src.parent, app)
    results.append(check("plugin loaded", "mnemosyne" in [p["name"] for p in host.list()], True))
    results.append(check("3 tools registered", sorted(app.registry.tools), [
        "mnemosyne_forget", "mnemosyne_recall", "mnemosyne_remember"]))

    # remember via tool, recall via hook injection
    r = await app.registry.tools["mnemosyne_remember"].run(
        {"content": "User's project codename is BLUE-HERON", "importance": 0.9}, None)
    results.append(check("remember tool ok", r.error, False))
    results.append(check("data dir honored",
                         (data_home / "mnemosyne" / "data" / "mnemosyne.db").exists(), True))

    msgs = [{"role": "system", "content": "You are Xu."},
            {"role": "user", "content": "what is the project codename?"}]
    bus_msgs = await host.bus.apply("before_llm", msgs)
    sys_text = bus_msgs[0]["content"]
    results.append(check("digest injected into system prompt",
                         "BLUE-HERON" in sys_text and "long-term memory" in sys_text, True))
    results.append(check("user message untouched", bus_msgs[1]["content"], "what is the project codename?"))

    # recall tool
    r = await app.registry.tools["mnemosyne_recall"].run({"query": "codename"}, None)
    results.append(check("recall tool finds it", "BLUE-HERON" in r.output, True))

    # forget tool + digest falls back to empty
    mem_id = r.raw[0]["id"] if isinstance(r.raw, list) and r.raw else None
    if mem_id:
        r = await app.registry.tools["mnemosyne_forget"].run({"memory_id": mem_id}, None)
        results.append(check("forget tool ok", r.error, False))

    # ---- 2. cap + empty passthrough ----------------------------------- #
    for i in range(30):
        await app.registry.tools["mnemosyne_remember"].run({"content": f"filler fact number {i} " * 10}, None)
    cap = 300
    app.config.set("plugin.mnemosyne.max_chars", cap)
    msgs2 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "filler fact"}]
    out2 = await host.bus.apply("before_llm", msgs2)
    block = out2[0]["content"].split("long-term memory (mnemosyne)", 1)[-1]
    results.append(check("digest capped", len(block) <= cap + 50, True))

    msgs3 = [{"role": "system", "content": "sys"}, {"role": "user", "content": "zzz-no-such-fact-qqq"}]
    out3 = await host.bus.apply("before_llm", msgs3)
    results.append(check("empty recall passes through unchanged", out3, msgs3))

    # ---- 3. inject off ------------------------------------------------- #
    app.config.set("plugin.mnemosyne.inject", False)
    out4 = await host.bus.apply("before_llm", msgs)
    results.append(check("inject=off passthrough", out4[0]["content"], "You are Xu."))
    app.config.set("plugin.mnemosyne.inject", True)

    # ---- 4. fail-soft without the dependency --------------------------- #
    import builtins
    real_import = builtins.__import__

    def block_mnemosyne(name, *a, **kw):
        if name == "mnemosyne" or name.startswith("mnemosyne."):
            raise ImportError("blocked for test")
        return real_import(name, *a, **kw)

    app2 = FakeApp(Path(tempfile.mkdtemp(prefix="mnem-check2-")))
    host2 = PluginHost(HookBus())
    builtins.__import__ = block_mnemosyne
    try:
        host2.load_dir(pkg_src.parent, app2)
    finally:
        builtins.__import__ = real_import
    results.append(check("fail-soft: no tools, no hook", (not app2.registry.tools, host2.bus.subscribed("before_llm")), (True, 0)))

    host.deactivate_all()
    results.append(check("tools disposed on unload", app.registry.tools, {}))

    shutil.rmtree(data_home, ignore_errors=True)
    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))