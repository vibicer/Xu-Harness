"""browser toolset — an agent-owned headless Chrome.

The *agent* owns the browser. :class:`BrowserManager` lazily spawns one
headless Chrome/Chromium process with a throwaway user profile and a CDP
remote-debugging endpoint on a free port, keeps it alive across turns, and
kills it (and cleans the profile) on shutdown — the user never touches it.
No user-facing config field: the tools simply "work". The browser binary is
resolved from ``XU_BROWSER_BIN`` or PATH (``google-chrome-stable``,
``chromium``, …); unlike the old Obscura harness there is nothing to
auto-install, so a missing binary is a clear error.

Transport is the Chromium DevTools Protocol over one WebSocket to the
browser-level endpoint discovered via ``GET /json/version``
(``webSocketDebuggerUrl``). For each page the client uses the standard
browser-CDP flow:

    Target.createTarget {url: "about:blank"}  -> targetId
    Target.attachToTarget {targetId, flatten: true}  -> sessionId

then drives that page with page-domain commands (``Page.navigate``,
``Runtime.evaluate``, ``Page.captureScreenshot``) tagged with the sessionId.

Connect mode: if ``XU_BROWSER_CDP_ENDPOINT`` is set, no process is spawned —
the manager connects to that browser-level CDP WebSocket directly. This is the
seam for driving an externally-launched Chrome (e.g. one with your logged-in
profile) or, later, the omp-style relay — Phase 1 ships the spawn path, the
env seam is the hook Phase 1 (logged-in driving) plugs into.

Tools (toolset ``browser``):

- ``browse``      → rendered body text (or raw HTML) for a URL (JS has run)
- ``screenshot``  → saves a PNG to the session cwd, returns its path
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import socket
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Any

import websockets

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult

_NAV_TIMEOUT = 30.0        # seconds to wait for load after Page.navigate
_IDLE = 0.15               # poll gap while waiting for document.readyState
_START_TIMEOUT = 12.0      # seconds to wait for the CDP endpoint after spawn
_BROWSER_WS_TIMEOUT = 10.0   # per-write / per-read timeout on the CDP socket
_SETTLE_TIMEOUT = 3.0      # extra seconds to let a client-rendered page paint text
_KILL_TIMEOUT = 3.0        # seconds to wait for the Chrome tree to die per signal

# Throwaway profile dirs live in the temp dir under this prefix, each carrying an
# owner marker so a later run can tell a live brain's profile from an orphan.
_PROFILE_PREFIX = "xu-chrome-"
_OWNER_FILE = "xu-owner.json"
# An unmarked profile (written by a build that predates the marker) is only
# reaped once it is this old, so a concurrent brain is never stolen from.
_ORPHAN_GRACE = 3600.0
# Chrome's headless default is 800x600, which crops screenshots and keeps
# viewport-gated lazy loading from firing. 1280x900 is a plain laptop window.
_VIEWPORT = (1280, 900)

# Page text: ``innerText`` first, ``textContent`` as the fallback. innerText is
# the *rendered* text, so it is empty whenever the body is hidden or lays out to
# nothing — which is exactly what a bot-challenge interstitial looks like.
# textContent still carries the markup's text, and saying "Just a moment…" beats
# reporting an empty page.
_TEXT_JS = (
    "(() => { const b = document.body; if (!b) return '';"
    " const t = b.innerText;"
    " return (t && t.trim()) ? t : (b.textContent || ''); })()"
)
_TEXT_LEN_JS = f"({_TEXT_JS}).trim().length"

# Chrome/Chromium binaries searched on PATH (XU_BROWSER_BIN overrides first).
_CHROME_CANDIDATES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "chrome",
)


class BrowserError(Exception):
    pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pid_alive(pid: int) -> bool:
    """True when *pid* still exists (signal 0 delivers nothing, just probes)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    except OSError:
        return False
    return True


def _kill_group(pgid: int | None, sig: int) -> bool:
    """Signal a whole process group; ``False`` if that is not possible here.

    Chrome's renderer, GPU and zygote helpers are separate processes, so
    signalling the parent alone can leave the tree running. Spawning with
    ``start_new_session=True`` makes the parent a group leader, which lets one
    signal reach every descendant. Returns ``False`` on a platform without
    process groups (Windows) or when the group is already gone, so the caller
    can fall back to signalling the parent handle.
    """
    if pgid is None or not hasattr(os, "killpg"):
        return False
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True

def _dehead(ua: str | None) -> str | None:
    """Strip Chrome's headless marker from a User-Agent string.

    Chrome advertises ``HeadlessChrome/<ver>``, and bot filters (Cloudflare
    among them) answer that with a challenge page — the document loads,
    ``readyState`` reaches ``complete``, and ``innerText`` is empty. Everything
    else in the UA is the genuine one reported by ``/json/version``, so dropping
    just the marker stays correct across Chrome upgrades with no pinned string.

    Returns ``None`` when there is nothing to strip, meaning "no override".
    """
    if not ua:
        return None
    fixed = ua.replace("HeadlessChrome/", "Chrome/")
    return fixed if fixed != ua else None


class BrowserManager:
    """Owns the one headless Chrome the agent uses. Singleton (module-level):
    spawned on first use, reused across turns, terminated on shutdown.

    If ``XU_BROWSER_CDP_ENDPOINT`` is set, no process is spawned — the manager
    adopts that browser-level CDP WebSocket (an externally-launched Chrome, or
    the Phase 1 relay) and never owns its lifecycle.
    """

    _proc: asyncio.subprocess.Process | None = None
    _port: int | None = None
    _endpoint: str | None = None
    _udd: Path | None = None
    _lock: asyncio.Lock | None = None
    # Real UA with the headless marker stripped, learned from /json/version at
    # startup; None when no override is needed (connect mode, or a UA whose
    # string never advertised headless).
    _ua: str | None = None
    # Process-group id of the spawned Chrome tree (== the parent's pid, since it
    # is spawned as a session leader). None in connect mode or on Windows.
    _pgid: int | None = None

    @staticmethod
    def _profiles_root() -> Path:
        return Path(tempfile.gettempdir())

    @classmethod
    def _claim(cls, udd: Path) -> None:
        """Record who owns this profile, so a later run can prove it orphaned.

        Best-effort: without the marker the sweep just falls back to age.
        """
        try:
            (udd / _OWNER_FILE).write_text(
                json.dumps({"brain_pid": os.getpid(), "pgid": cls._pgid}),
                encoding="utf-8",
            )
        except OSError:
            pass

    @classmethod
    def _sweep_orphans(cls) -> int:
        """Reap Chrome trees and profiles left by a brain that never ran
        ``shutdown()`` — SIGKILL, a crash, a lost session.

        No teardown code can cover that case, so recovery has to happen on the
        way up instead. A profile is only reaped when its recorded owner is
        provably gone; an unmarked one waits out ``_ORPHAN_GRACE`` first, which
        is the one case where a still-running pre-marker Chrome could lose its
        profile dir. That profile is a throwaway, so the cost is a dead browser
        rather than lost data.
        """
        reaped = 0
        try:
            entries = list(cls._profiles_root().glob(_PROFILE_PREFIX + "*"))
        except OSError:
            return 0
        for udd in entries:
            if udd == cls._udd or not udd.is_dir():
                continue
            try:
                info = json.loads((udd / _OWNER_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                info = None
            if info is None:
                try:
                    if time.time() - udd.stat().st_mtime < _ORPHAN_GRACE:
                        continue
                except OSError:
                    continue
            elif _pid_alive(int(info.get("brain_pid") or 0)):
                continue  # a live brain still owns this one
            else:
                pgid = info.get("pgid")
                _kill_group(pgid if isinstance(pgid, int) else None, signal.SIGKILL)
            shutil.rmtree(udd, ignore_errors=True)
            reaped += 1
        return reaped
    @classmethod
    def ua(cls) -> str | None:
        """UA override to apply to new pages, or ``None`` for "leave it alone"."""
        return cls._ua

    @staticmethod
    def binary() -> str | None:
        """Resolve the Chrome/Chromium executable: ``XU_BROWSER_BIN`` env →
        PATH. Returns ``None`` if nothing is found; never installs."""
        env = os.environ.get("XU_BROWSER_BIN")
        if env:
            p = Path(env)
            if p.exists():
                return str(p)
            return None
        for name in _CHROME_CANDIDATES:
            p = shutil.which(name)
            if p:
                return p
        return None

    @classmethod
    def endpoint(cls) -> str | None:
        return cls._endpoint

    @classmethod
    async def _locker(cls) -> asyncio.Lock:
        if cls._lock is None:
            cls._lock = asyncio.Lock()
        return cls._lock

    @classmethod
    async def ensure(cls) -> str:
        """Return the browser-level CDP WebSocket endpoint, spawning a headless
        Chrome on first use (or respawning if it died). Serialized so
        concurrent tool calls share one process."""
        async with await cls._locker():
            # Connect mode: an externally-provided CDP endpoint is adopted
            # as-is — never spawned, never owned.
            env_ep = os.environ.get("XU_BROWSER_CDP_ENDPOINT")
            if env_ep:
                cls._endpoint = env_ep.strip()
                return cls._endpoint
            if cls._proc is not None and cls._proc.returncode is None:
                return cls.endpoint()
            bin_ = cls.binary()
            if not bin_:
                raise BrowserError(
                    "no Chrome/Chromium found — install one "
                    "(google-chrome-stable / chromium) or set XU_BROWSER_BIN"
                )
            # Recover from any previous brain that died without a shutdown.
            cls._sweep_orphans()
            port = _free_port()
            udd = Path(tempfile.mkdtemp(prefix=_PROFILE_PREFIX))
            cls._udd = udd
            proc = await asyncio.create_subprocess_exec(
                bin_,
                "--headless=new",
                f"--remote-debugging-port={port}",
                f"--user-data-dir={udd}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-gpu",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                f"--window-size={_VIEWPORT[0]},{_VIEWPORT[1]}",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                # Own session → the parent is its group leader, so one killpg
                # reaches every renderer/GPU/zygote helper Chrome forks.
                start_new_session=True,
            )
            cls._proc = proc
            cls._port = port
            # start_new_session makes the child its own group leader, so the
            # group id is its pid — no getpgid race against a fast exit.
            cls._pgid = proc.pid if hasattr(os, "killpg") else None
            cls._claim(udd)
            cls._endpoint = await cls._wait_ready(port)
            return cls._endpoint
            return cls._endpoint

    @classmethod
    async def _wait_ready(cls, port: int) -> str:
        """Poll ``/json/version`` until Chrome's CDP endpoint is up; return the
        browser-level ``webSocketDebuggerUrl``."""
        url = f"http://127.0.0.1:{port}/json/version"
        deadline = time.monotonic() + _START_TIMEOUT
        while time.monotonic() < deadline:
            if cls._proc is not None and cls._proc.returncode is not None:
                raise BrowserError(
                    "chrome exited during startup; check the binary "
                    f"({cls.binary()}) and XU_BROWSER_BIN"
                )
            try:
                with urllib.request.urlopen(url, timeout=2.0) as r:
                    data = json.loads(r.read())
                ws = data.get("webSocketDebuggerUrl")
                if ws:
                    cls._ua = _dehead(data.get("User-Agent"))
                    return ws
            except Exception:  # noqa: BLE001 — endpoint not up yet
                await asyncio.sleep(0.3)
        raise BrowserError("chrome CDP endpoint did not come up in time")

    @classmethod
    async def shutdown(cls) -> None:
        proc = cls._proc
        pgid = cls._pgid
        cls._proc = None
        cls._port = None
        cls._endpoint = None
        cls._ua = None
        cls._pgid = None
        udd = cls._udd
        cls._udd = None
        if proc is not None:
            await cls._reap(proc, pgid)
        # Only once the tree is dead: Chrome holds files open in its profile, so
        # deleting first is what used to leave the directory behind.
        if udd is not None:
            shutil.rmtree(udd, ignore_errors=True)

    @staticmethod
    async def _reap(proc: asyncio.subprocess.Process, pgid: int | None) -> None:
        """SIGTERM the whole Chrome tree, wait, then SIGKILL any straggler.

        The final group SIGKILL is unconditional rather than a timeout fallback:
        the parent can exit on SIGTERM while a renderer ignores it, which is how
        waiting on the parent alone used to report success over a tree that was
        still running. Killing an already-empty group is a no-op.
        """
        if not _kill_group(pgid, signal.SIGTERM):
            try:
                proc.terminate()
            except (ProcessLookupError, OSError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_TIMEOUT)
        except (asyncio.TimeoutError, OSError):
            pass
        if not _kill_group(pgid, signal.SIGKILL):
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=_KILL_TIMEOUT)
        except (asyncio.TimeoutError, OSError):
            pass


class CDPClient:
    """One browser-level CDP session. Connects to the spawned browser and
    drives pages via ``Target.createTarget`` + flattened
    ``Target.attachToTarget``."""

    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint
        self._ws: Any = None
        self._msg_id = 0

    async def connect(self) -> None:
        self._ws = await websockets.connect(
            self.endpoint, max_size=64 * 1024 * 1024, open_timeout=_BROWSER_WS_TIMEOUT
        )

    async def _send(
        self, method: str, params: dict[str, Any], session_id: str | None = None
    ) -> dict[str, Any]:
        self._msg_id += 1
        msg: dict[str, Any] = {"id": self._msg_id, "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        await asyncio.wait_for(self._ws.send(json.dumps(msg)), timeout=_BROWSER_WS_TIMEOUT)
        while True:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=_BROWSER_WS_TIMEOUT)
            try:
                msgr = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if msgr.get("id") == self._msg_id:
                if "error" in msgr:
                    raise RuntimeError(msgr["error"].get("message", "cdp error"))
                return msgr.get("result", {})

    async def new_page(self) -> str:
        """Create a page target and attach; returns its flattened sessionId.

        Applies the de-headless-ed UA before the page ever navigates, so the
        very first request carries it — a challenge page served on request one
        cannot be un-served later.
        """
        r = await self._send("Target.createTarget", {"url": "about:blank"})
        tid = r.get("targetId")
        if not tid:
            raise RuntimeError("Target.createTarget returned no targetId")
        r = await self._send("Target.attachToTarget", {"targetId": tid, "flatten": True})
        sid = r.get("sessionId")
        if not sid:
            raise RuntimeError("Target.attachToTarget returned no sessionId")
        ua = BrowserManager.ua()
        if ua:
            await self._send("Emulation.setUserAgentOverride", {"userAgent": ua}, sid)
        return sid

    async def detach_page(self, session_id: str) -> None:
        try:
            await self._send("Target.detachFromTarget", {"sessionId": session_id})
        except Exception:  # noqa: BLE001 — already gone
            pass

    async def _eval(self, expr: str, session_id: str) -> Any:
        res = await self._send(
            "Runtime.evaluate", {"expression": expr, "returnByValue": True}, session_id
        )
        return res.get("result", {}).get("value")

    async def navigate(self, url: str, session_id: str) -> None:
        await self._send("Page.enable", {}, session_id)
        await self._send("Runtime.enable", {}, session_id)
        await self._send("Page.navigate", {"url": url}, session_id)
        deadline = time.monotonic() + _NAV_TIMEOUT
        while time.monotonic() < deadline:
            await asyncio.sleep(_IDLE)
            try:
                state = await self._eval("document.readyState", session_id)
            except Exception:  # noqa: BLE001
                continue
            if state == "complete":
                await self._settle(session_id)
                return

    async def _settle(self, session_id: str) -> None:
        """Wait briefly for text to appear after load.

        ``readyState == "complete"`` only means the document and its
        subresources arrived; a client-rendered page paints its text after
        that. Poll instead of sleeping a fixed amount so a static page costs
        one round trip and an SPA costs only what it needs.
        """
        deadline = time.monotonic() + _SETTLE_TIMEOUT
        while True:
            try:
                n = await self._eval(_TEXT_LEN_JS, session_id)
            except Exception:  # noqa: BLE001
                return
            if isinstance(n, int) and n > 0:
                return
            if time.monotonic() >= deadline:
                return
            await asyncio.sleep(_IDLE)

    async def evaluate_text(self, session_id: str, *, html: bool) -> str:
        expr = "document.documentElement.outerHTML" if html else _TEXT_JS
        value = await self._eval(expr, session_id)
        return value if isinstance(value, str) else ""

    async def page_title(self, session_id: str) -> str:
        """Best-effort ``document.title`` — used to explain an empty page."""
        try:
            value = await self._eval("document.title", session_id)
        except Exception:  # noqa: BLE001
            return ""
        return value if isinstance(value, str) else ""

    async def screenshot(self, session_id: str, *, full_page: bool) -> bytes:
        res = await self._send(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": full_page},
            session_id,
        )
        b64 = res.get("data", "")
        return base64.b64decode(b64) if b64 else b""

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None


async def _client() -> CDPClient:
    endpoint = await BrowserManager.ensure()
    client = CDPClient(endpoint)
    await client.connect()
    return client


class BrowseTool(Tool):
    name = "browse"
    toolset = "browser"
    description = ("Open a URL in the agent's real (headless) browser and return "
                   "the rendered text — JS has executed. Use when a plain fetch "
                   "returns a shell or empty page. Needs a Chrome/Chromium binary "
                   "(or XU_BROWSER_CDP_ENDPOINT for an externally-launched browser).")
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "html": {"type": "boolean", "default": False},
        },
        "required": ["url"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url:
            return ToolResult.err("url required")
        title = ""
        try:
            client = await _client()
            sid = await client.new_page()
            try:
                await client.navigate(url, sid)
                text = await client.evaluate_text(sid, html=bool(args.get("html", False)))
                # Only on the failure path: name the page so the next reader
                # gets a diagnosis instead of a guess.
                if not text.strip():
                    title = await client.page_title(sid)
            finally:
                await client.detach_page(sid)
                await client.close()
        except BrowserError as e:
            return ToolResult.err(f"browser unavailable: {e}")
        except Exception as e:  # noqa: BLE001
            return ToolResult.err(f"browse failed: {e}")
        if not text.strip():
            named = f' (page title: "{title.strip()}")' if title.strip() else ""
            return ToolResult.err(
                f"page rendered no text{named} — the site may be serving a bot "
                "challenge, or gating its content behind interaction"
            )
        if len(text) > 16_000:
            text = text[:16_000] + "\n…[truncated]"
        return ToolResult.ok(text, raw=len(text))


class ScreenshotTool(Tool):
    name = "screenshot"
    toolset = "browser"
    description = ("Capture a screenshot of a URL in the agent's headless browser, "
                   "saved to the session working directory; returns the PNG path "
                   "so the model can inspect it (image toolset).")
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "full_page": {"type": "boolean", "default": False},
        },
        "required": ["url"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url", "")).strip()
        if not url:
            return ToolResult.err("url required")
        try:
            client = await _client()
            sid = await client.new_page()
            try:
                await client.navigate(url, sid)
                png = await client.screenshot(sid, full_page=bool(args.get("full_page", False)))
            finally:
                await client.detach_page(sid)
                await client.close()
        except BrowserError as e:
            return ToolResult.err(f"browser unavailable: {e}")
        except Exception as e:  # noqa: BLE001
            return ToolResult.err(f"screenshot failed: {e}")
        if not png:
            return ToolResult.err("screenshot returned no data")
        name = "screenshot-" + str(int(time.time())) + ".png"
        dest = Path(ctx.cwd) / name
        try:
            dest.write_bytes(png)
        except OSError as e:
            return ToolResult.err(f"write screenshot failed: {e}")
        return ToolResult.ok(
            f"saved {dest} ({len(png)} bytes). Inspect with inspect_image.",
            raw=str(dest),
        )


browse = BrowseTool()
screenshot = ScreenshotTool()
