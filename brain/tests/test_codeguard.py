"""codeguard plugin — runnable check for the pre-write syntax gate.

Loads the real activate.py from the installed plugin dir (~/.xu/plugins/
codeguard) and drives it through the real ToolRegistry + write tool:

- broken .py  -> write blocked with the exact syntax error, file NOT on disk
- clean .py   -> passes, file written
- non-.py / sqlite / edit  -> not gated (guard stays out of their way)
"""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from xu_brain.core.bus import HookBus
from xu_brain.features.tools import file as file_tools
from xu_brain.features.tools.registry import ToolRegistry

PLUGIN = Path.home() / ".xu" / "plugins" / "codeguard" / "activate.py"

pytestmark = pytest.mark.skipif(
    not PLUGIN.exists(), reason="codeguard plugin not installed in data_home"
)


def _load_activate():
    spec = importlib.util.spec_from_file_location("codeguard_activate", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.activate


class AutoApprove:
    async def request(self, tool_name, args, level, session_id=None):
        return True, "auto"


def _wire():
    hooks = HookBus()
    ctx_stub = SimpleNamespace(
        on=lambda event, handler: hooks.on(event, handler)
    )
    _load_activate()(ctx_stub)
    registry = ToolRegistry(AutoApprove(), hooks=hooks)
    registry.register(file_tools.write)
    return registry


def _tool_ctx(tmp_path):
    return SimpleNamespace(
        cwd=str(tmp_path), session_id="s", config={"toolsets": {"lsp": False}}
    )


async def _noop(*args, **kwargs):
    return None


def test_broken_py_blocked_before_disk(tmp_path):
    registry = _wire()
    res = asyncio.run(registry.run("write", {
        "path": "bad.py",
        "content": "def f(:\n    pass\n",
    }, _tool_ctx(tmp_path), emit=_noop))
    assert res.error
    assert "write blocked" in res.output
    assert "line 1" in res.output
    assert not (tmp_path / "bad.py").exists()


def test_clean_py_passes(tmp_path):
    registry = _wire()
    res = asyncio.run(registry.run("write", {
        "path": "good.py",
        "content": "def f(a):\n    return a\n",
    }, _tool_ctx(tmp_path), emit=_noop))
    assert not res.error
    assert (tmp_path / "good.py").read_text() == "def f(a):\n    return a\n"


def test_non_py_and_sqlite_not_gated(tmp_path):
    registry = _wire()
    res = asyncio.run(registry.run("write", {
        "path": "notes.txt",
        "content": "not python at all ((((",
    }, _tool_ctx(tmp_path), emit=_noop))
    assert not res.error, res.output

    res = asyncio.run(registry.run("write", {
        "path": "data.sqlite:t:k",
        "content": "((((",
    }, _tool_ctx(tmp_path), emit=_noop))
    # must reach the sqlite path, not be blocked by the syntax gate
    assert "write blocked" not in res.output
