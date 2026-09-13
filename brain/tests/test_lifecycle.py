"""End-to-end lifecycle: `app.shutdown` / `app.restart` over a real socket.

The About panel's power controls are only safe if two things hold, and neither
is visible from a unit test of `dispatch`:

  1. the RPC *reply* reaches the caller before the socket closes — otherwise the
     shell shows "could not shut down" for a shutdown that worked;
  2. `serve()` returns the requested intent, which is what `__main__` keys the
     re-exec off.

Both need the real `serve()` loop and a real WebSocket, so this starts the
brain on an ephemeral port and drives it with `websockets.connect`.
"""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from xu_brain.core import runtime


@pytest.fixture
def data_home(tmp_path: Path) -> Path:
    """`build_app` writes into data_home; a per-test tmp dir keeps the real
    ~/.xu out of the loop (test_brain.py's fixture is module-local, so this
    file carries its own rather than importing across test modules)."""
    home = tmp_path / "xu-data"
    home.mkdir()
    return home


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _call(port: int, method: str) -> dict:
    """One request/response round trip against a freshly-started brain."""
    import json
    import websockets

    async with websockets.connect(f"ws://127.0.0.1:{port}") as ws:
        await ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": method}))
        return json.loads(await ws.recv())


async def _serve_once(data_home: Path) -> tuple[int, asyncio.Task]:
    port = _free_port()
    task = asyncio.ensure_future(runtime.serve(host="127.0.0.1", port=port, data_home=data_home))
    # Wait for the bind rather than sleeping blind: serve() prints readiness
    # only after the socket is up, and the port is ours, so a short poll is
    # deterministic enough without reaching into serve's internals.
    for _ in range(100):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            await asyncio.sleep(0.05)
    return port, task


@pytest.mark.parametrize(
    "method, intent",
    [("app.shutdown", "shutdown"), ("app.restart", "restart")],
)
def test_power_rpc_replies_then_stops(data_home, method, intent):
    async def scenario():
        port, task = await _serve_once(data_home)
        reply = await _call(port, method)
        # (1) The reply must arrive intact — not an error, not a dropped socket.
        assert reply["result"] == {"ok": True, "action": intent}
        # (2) serve() unwinds and reports the intent the entrypoint acts on.
        assert await asyncio.wait_for(task, timeout=5) == intent

    asyncio.run(scenario())


def test_bind_failure_returns_none_not_restart(data_home):
    """A port clash must not read as "restart": `__main__` re-execs only on the
    literal "restart", so a brain that never came up just exits."""

    async def scenario():
        port = _free_port()
        # Squat the port so serve() cannot bind it.
        squatter = await asyncio.start_server(lambda r, w: None, "127.0.0.1", port)
        try:
            intent = await asyncio.wait_for(
                runtime.serve(host="127.0.0.1", port=port, data_home=data_home), timeout=5
            )
        finally:
            squatter.close()
            await squatter.wait_closed()
        assert intent is None

    asyncio.run(scenario())


def test_stop_intent_defaults_to_none(data_home):
    app = runtime.build_app(data_home)
    assert app.stop_intent is None
    # flush_stop before any request must not arm a teardown.
    app.flush_stop()
    assert not app._stop_event.is_set()
    app.request_stop("shutdown")
    assert app.stop_intent == "shutdown"
    app.flush_stop()
    assert app._stop_event.is_set()


@pytest.mark.parametrize(
    "method, keeps_pid",
    [("app.shutdown", False), ("app.restart", True)],
)
def test_supervisor_handshake_pid_file(data_home, method, keeps_pid):
    """`xu --supervise` relaunches a brain for as long as brain.pid still
    names the launch — so an *intentional* stop has to drop that file, or the
    About panel's shut-down is undone a second later. A restart keeps the pid
    (POSIX execv) and therefore keeps the file."""

    async def scenario():
        port, task = await _serve_once(data_home)
        (data_home / "brain.pid").write_text("999\n")  # as `xu start` writes it
        await _call(port, method)
        await asyncio.wait_for(task, timeout=5)
        assert (data_home / "brain.pid").exists() is keeps_pid

    asyncio.run(scenario())


def test_shutdown_pid_file_clear_is_symlink_safe(data_home):
    """`_clear_pid_file` must not delete through a symlinked data home."""
    (data_home / "brain.pid").write_text("42\n")
    runtime._clear_pid_file(data_home)
    assert not (data_home / "brain.pid").exists()
