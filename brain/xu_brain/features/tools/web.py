"""web toolset — web_search + web_extract.

Firecrawl optional (env XU_FIRECRAWL_KEY). Without it, web_search falls back
to a DuckDuckGo HTML scrape and web_extract to a simple readability pass.
Keeps the brain dependency-free when no key is set.
"""
from __future__ import annotations

import json
import os
import re
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult


class WebSearchTool(Tool):
    name = "web_search"
    toolset = "web"
    description = "Search the web. Returns result titles + URLs + snippets."
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "default": 8}},
        "required": ["query"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        q = str(args.get("query", ""))
        if not q:
            return ToolResult.err("query required")
        limit = int(args.get("limit", 8))
        fc = _firecrawl_key(ctx)
        if fc:
            return await _firecrawl_search(fc, q, limit)
        return await _ddg_search(q, limit)


class WebExtractTool(Tool):
    name = "web_extract"
    toolset = "web"
    description = "Extract clean text/markdown from a URL (Firecrawl if configured, else reader fallback)."
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {"url": {"type": "string"}, "raw": {"type": "boolean", "default": False}},
        "required": ["url"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = str(args.get("url", ""))
        if not url:
            return ToolResult.err("url required")
        raw_html = bool(args.get("raw", False))
        fc = _firecrawl_key(ctx)
        if fc:
            return await _firecrawl_extract(fc, url, raw_html)
        res = await _reader_fallback(url, raw_html)
        # Render fallback: a failed fetch or a near-empty (JS-shell) page is
        # often worth one real-browser pass — the agent owns the browser.
        if (res.error or _looks_like_shell(res.output)):
            bres = await _browser_extract(ctx, url, raw_html)
            if bres is not None and not bres.error:
                return bres
        return res


web_search = WebSearchTool()
web_extract = WebExtractTool()


def _looks_like_shell(text: str) -> bool:
    """Heuristic: near-empty or a SPA shell that fetched no real content."""
    t = text.strip()
    return not t or len(t) < 200


def _browser_available() -> bool:
    """Agent owns the browser — available iff we can resolve a Chrome/Chromium
    binary (or an external CDP endpoint is configured)."""
    if os.environ.get("XU_BROWSER_CDP_ENDPOINT"):
        return True
    from .browser import BrowserManager
    try:
        return bool(BrowserManager.binary())
    except Exception:  # noqa: BLE001
        return False


async def _browser_extract(ctx: ToolContext, url: str, raw_html: bool) -> ToolResult | None:
    """One rendered pass through the agent's browser. None if no browser is
    available — caller keeps the reader result."""
    if not _browser_available():
        return None
    try:
        from .browser import BrowseTool
        tool = BrowseTool()
        return await tool.run({"url": url, "html": raw_html}, ctx)
    except Exception:  # noqa: BLE001
        return None


def _firecrawl_key(ctx: ToolContext) -> str | None:
    # Master toggle gates everything (Config > agent env). Off === never touch
    # Firecrawl, even if a key is present; web tools fall back to DDG/reader.
    if not bool(ctx.config.get("firecrawl_enabled", False)):
        return None
    # Config is the source of truth (Config > agent env). Accept a single key
    # or a pool ("k1,k2" / ["k1","k2"]) rotated round-robin so concurrent
    # calls spread across keys on the quota pool.
    raw = ctx.config.get("firecrawl_key")
    if not raw:
        import os
        return os.environ.get("XU_FIRECRAWL_KEY")
    if isinstance(raw, (list, tuple)):
        pool = [k.strip() for k in raw if isinstance(k, str) and k.strip()] or None
    else:
        pool = [k.strip() for k in str(raw).split(",") if k.strip()] or None
    if not pool:
        return None
    i = ctx.breakers.get("_fc_rr", 0)
    ctx.breakers["_fc_rr"] = i + 1
    return pool[i % len(pool)]


async def _firecrawl_search(key: str, query: str, limit: int) -> ToolResult:
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.post(
                "https://api.firecrawl.dev/v1/search",
                headers={"Authorization": f"Bearer {key}"},
                json={"query": query, "limit": limit},
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            lines = [f"- {d.get('title','?')}: {d.get('url')}\n  {d.get('description','')}" for d in data]
            return ToolResult.ok("\n".join(lines), raw=len(data))
    except httpx.HTTPError as e:
        return await _ddg_search(query, limit)


async def _firecrawl_extract(key: str, url: str, raw_html: bool = False) -> ToolResult:
    fmt = "html" if raw_html else "markdown"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.post(
                "https://api.firecrawl.dev/v1/scrape",
                headers={"Authorization": f"Bearer {key}"},
                json={"url": url, "formats": [fmt]},
            )
            r.raise_for_status()
            md = r.json().get("data", {}).get(fmt, "")
            return ToolResult.ok(md[:16000], raw=len(md))
    except httpx.HTTPError:
        return await _reader_fallback(url, raw_html)


_TAG = re.compile(r"<[^>]+>")


async def _ddg_search(query: str, limit: int) -> ToolResult:
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
            r = await c.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers={"User-Agent": "Mozilla/5.0 XuBrain"},
            )
            r.raise_for_status()
            html = r.text
    except httpx.HTTPError as e:
        return ToolResult.err(f"search failed: {e}")
    results = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', html, re.S)
    out: list[str] = []
    for href, title in results[:limit]:
        title = _TAG.sub("", title).strip()
        out.append(f"- {title}\n  {_clean_href(href)}")
    if not out:
        return ToolResult.ok("(no results)", raw=0)
    return ToolResult.ok("\n".join(out), raw=len(out))


def _clean_href(href: str) -> str:
    """DDG's HTML results link through a redirect wrapper — unwrap `uddg` to
    the real target URL (the regex above captures them still HTML-escaped)."""
    href = unescape(href)
    if "uddg=" in href:
        qs = parse_qs(urlparse(href).query)
        if qs.get("uddg"):
            return qs["uddg"][0]
    if href.startswith("//"):
        return "https:" + href
    return href


async def _reader_fallback(url: str, raw_html: bool = False) -> ToolResult:
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 XuBrain"})
            r.raise_for_status()
            html = r.text
    except httpx.HTTPError as e:
        return ToolResult.err(f"fetch failed: {e}")
    if raw_html:
        text = html
    else:
        # crude: strip tags, scripts, styles
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.S | re.I)
        text = _TAG.sub(" ", html)
        text = re.sub(r"\s+", " ", text).strip()
    if len(text) > 16000:
        text = text[:16000] + "\n…[truncated]"
    return ToolResult.ok(text, raw=len(text))
