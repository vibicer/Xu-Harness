"""ITEM 13: the WS server must accept the shell's image-carrying frames.

``web/src/lib/attach.ts`` reads an attached image into a data URL under an
8 MiB guard and ``web/src/lib/rpc.ts`` sends the whole ``session.send`` payload
in one frame; base64 inflates that ~4/3 (~10.7 MiB) before the JSON envelope.
That is well past the ``websockets`` default ``max_size`` (1 MiB), which closed
the frame with 1009 — the send silently never got a reply.

A live echo server would bind a port, which this task forbids; per the brief we
instead pin the value at the server-construction site and assert it clears the
library default.
"""
from __future__ import annotations

import inspect

from websockets.asyncio.server import serve as ws_serve

from xu_brain.core import runtime
from xu_brain.core.runtime import _MAX_WS_FRAME_BYTES


def _library_default_max_size() -> int | None:
    return inspect.signature(ws_serve).parameters["max_size"].default


def test_limit_clears_the_library_default_and_covers_one_attachment():
    default = _library_default_max_size()
    assert default is not None
    assert _MAX_WS_FRAME_BYTES > default
    # Covers one full-size attachment after base64 inflation (8 MiB -> ~10.7 MiB)...
    assert _MAX_WS_FRAME_BYTES >= 8 * 1024 * 1024 * 4 // 3
    # ...while staying bounded, so a single frame can't exhaust memory.
    assert _MAX_WS_FRAME_BYTES <= 64 * 1024 * 1024


class _FakeServer:
    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.closed = False

    async def __aenter__(self) -> _FakeServer:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def serve_forever(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


class _FakeApp:
    def __init__(self, data_home: object) -> None:
        self.data_home = data_home
        self.stop_intent = "shutdown"

    async def startup(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def wait_for_stop(self) -> None:
        return None


async def test_serve_passes_max_size_to_ws_serve(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    async def fake_ws_serve(handler, host, port, **kwargs):
        captured.update(host=host, port=port, **kwargs)
        return _FakeServer(**kwargs)
    monkeypatch.setattr(runtime, "ws_serve", fake_ws_serve)
    monkeypatch.setattr(runtime, "build_app", lambda dh=None: _FakeApp(tmp_path / "xu"))
    monkeypatch.delenv("XU_RESTARTED", raising=False)

    # No real port is bound: the stubbed ws_serve never touches a socket.
    intent = await runtime.serve(port=0)

    assert intent == "shutdown"
    assert captured["max_size"] == _MAX_WS_FRAME_BYTES
    assert captured["process_request"] is not None
