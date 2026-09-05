"""LSP pool lifecycle — idle reaping, LRU cap, and bounded teardown."""
from __future__ import annotations

import asyncio
import sys
import time

from xu_brain.features.tools import lsp_debug as L


class _FakeServer:
    def __init__(self, last_used: float) -> None:
        self.last_used = last_used
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def test_reap_closes_idle_servers_only():
    L._LSP_SERVERS.clear()
    now = time.monotonic()
    idle = _FakeServer(now - L._LSP_IDLE_TTL - 1.0)
    fresh = _FakeServer(now)
    L._LSP_SERVERS["idle"] = idle
    L._LSP_SERVERS["fresh"] = fresh

    asyncio.run(L._reap_idle_lsp())

    assert "idle" not in L._LSP_SERVERS and idle.closed
    assert "fresh" in L._LSP_SERVERS and not fresh.closed


def test_reap_loop_waits_for_idle_ttl(monkeypatch):
    L._LSP_SERVERS.clear()
    events: list[str] = []
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        events.append(f"sleep:{seconds}")
        await real_sleep(0)
        L._LSP_SERVERS.clear()

    async def main() -> None:
        server = _FakeServer(time.monotonic())
        L._LSP_SERVERS["server"] = server
        monkeypatch.setattr(L.asyncio, "sleep", fake_sleep)
        await L._reap_loop()
        assert events == [f"sleep:{L._LSP_IDLE_TTL}"]

    asyncio.run(main())
    L._LSP_SERVERS.clear()


def test_reap_evicts_lru_beyond_cap():
    L._LSP_SERVERS.clear()
    now = time.monotonic()
    # All within TTL so only the LRU cap fires; k0 is the least-recently-used.
    for i in range(L._LSP_MAX_SERVERS + 1):
        L._LSP_SERVERS[f"k{i}"] = _FakeServer(now - (L._LSP_MAX_SERVERS - i))

    asyncio.run(L._reap_idle_lsp())

    assert len(L._LSP_SERVERS) == L._LSP_MAX_SERVERS
    assert "k0" not in L._LSP_SERVERS  # oldest -> evicted
    L._LSP_SERVERS.clear()


def test_close_bounded_teardown_kills_process(monkeypatch):
    monkeypatch.setattr(L, "_SHUTDOWN_TIMEOUT", 0.2)
    monkeypatch.setattr(L, "_KILL_GRACE", 0.2)
    srv = L._JsonRpcStdio([sys.executable, "-c", "import time; time.sleep(60)"])

    async def main() -> None:
        await srv._ensure()
        assert srv._proc is not None and srv._proc.returncode is None
        await srv.close()
        assert srv._proc.returncode is not None

    asyncio.run(main())


def test_close_is_idempotent(monkeypatch):
    monkeypatch.setattr(L, "_SHUTDOWN_TIMEOUT", 0.2)
    monkeypatch.setattr(L, "_KILL_GRACE", 0.2)
    srv = L._JsonRpcStdio([sys.executable, "-c", "import time; time.sleep(60)"])

    async def main() -> None:
        await srv._ensure()
        await srv.close()
        await srv.close()  # second call must be a no-op, not a crash

    asyncio.run(main())
