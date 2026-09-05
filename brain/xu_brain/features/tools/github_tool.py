"""github toolset — GitHub REST via httpx, token from keychain/env."""
from __future__ import annotations

import json
from typing import Any

import httpx

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult

_API = "https://api.github.com"


class GithubTool(Tool):
    name = "github"
    toolset = "github"
    description = (
        "Call the GitHub REST API. args: {endpoint: '/repos/owner/repo/issues', "
        "method?: 'GET', params?: {...}, body?: {...}}. Token from keychain "
        "(provider id 'github') or GITHUB_TOKEN env."
    )
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {
            "endpoint": {"type": "string", "description": "API path, e.g. /repos/o/r/issues"},
            "method": {"type": "string", "default": "GET"},
            "params": {"type": "object"},
            "body": {"type": "object"},
        },
        "required": ["endpoint"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        endpoint = str(args.get("endpoint", "")).strip()
        if not endpoint:
            return ToolResult.err("endpoint required")
        method = str(args.get("method", "GET")).upper()
        import os

        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            token = await ctx.providers.get_key("github")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = endpoint if endpoint.startswith("http") else _API + endpoint
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.request(method, url, params=args.get("params"), json=args.get("body"), headers=headers)
        except httpx.HTTPError as e:
            return ToolResult.err(f"github error: {e}")
        body = r.text
        try:
            parsed = r.json()
            body = json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:  # noqa: BLE001
            pass
        if len(body) > 12000:
            body = body[:12000] + f"\n…[truncated {len(body) - 12000} chars]"
        text = f"{method} {endpoint} → {r.status_code}\n\n{body}"
        return ToolResult.ok(text, raw={"status": r.status_code})


github = GithubTool()
