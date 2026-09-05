"""clock plugin — runnable check for the API-input time stamp.

Loads the real activate.py from the installed plugin dir (~/.xu/plugins/clock)
and drives it through the real HookBus ``before_llm`` transform point:

- system message gets a fresh "Now:" stamp with today's date
- user messages untouched, input list not mutated
- no system message -> one is prepended
"""
import asyncio
import importlib.util
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from xu_brain.core.bus import HookBus

PLUGIN = Path.home() / ".xu" / "plugins" / "clock" / "activate.py"

pytestmark = pytest.mark.skipif(
    not PLUGIN.exists(), reason="clock plugin not installed in data_home"
)


def _load_activate():
    spec = importlib.util.spec_from_file_location("clock_activate", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.activate


def _wire():
    bus = HookBus()
    ctx_stub = SimpleNamespace(
        transform=lambda point, handler: bus.transform(point, handler)
    )
    _load_activate()(ctx_stub)
    return bus


def test_stamps_today_into_system_message():
    bus = _wire()
    msgs = [
        {"role": "system", "content": "You are Xu."},
        {"role": "user", "content": "hi"},
    ]
    out = asyncio.run(bus.apply("before_llm", msgs))
    assert out is not msgs
    assert out[0]["content"].startswith("You are Xu.")
    assert "# time" in out[0]["content"]
    assert datetime.now().strftime("%d %b %Y") in out[0]["content"]
    assert out[1] == {"role": "user", "content": "hi"}
    # input list not mutated
    assert msgs[0]["content"] == "You are Xu."


def test_prepends_system_message_when_absent():
    bus = _wire()
    msgs = [{"role": "user", "content": "hi"}]
    out = asyncio.run(bus.apply("before_llm", msgs))
    assert out[0]["role"] == "system"
    assert "# time" in out[0]["content"]
    assert out[1] == {"role": "user", "content": "hi"}