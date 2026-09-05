"""Search toolset — glob + grep.

In-process ripgrep when available (fast, respects gitignore); Python fallback.
Glob via `pathlib`; grep via re over a bounded file walk. Both resolve against
the session cwd and respect a default ignore set (`.venv`, `node_modules`, …).
"""
from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
from pathlib import Path
from typing import Any

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult

_IGNORE_DIRS = {
    ".venv", "venv", "node_modules", "__pycache__", ".git", ".mypy_cache",
    ".ruff_cache", "dist", "build", "target", ".next", ".cache",
}
_MAX_FILES = 40_000
_MAX_MATCHES = 500


class GlobTool(Tool):
    name = "glob"
    toolset = "search"
    description = (
        "Glob files/dirs matching a pattern (e.g. `src/**/*.py`). Resolves "
        "against the session cwd. Defaults: respects gitignore-ish ignore set."
    )
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "glob pattern"},
            "path": {"type": "string", "description": "root to search (default cwd)", "default": "."},
            "limit": {"type": "integer", "default": 200},
        },
        "required": ["pattern"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args.get("pattern", ""))
        if not pattern:
            return ToolResult.err("pattern required")
        root = _resolve(ctx.cwd, str(args.get("path", ".")))
        limit = int(args.get("limit", 200))
        if not root.exists():
            return ToolResult.err(f"root not found: {root}")
        matches = _glob(root, pattern, limit)
        if not matches:
            return ToolResult.ok("(no matches)", raw=[])
        return ToolResult.ok("\n".join(matches), raw=matches)


class GrepTool(Tool):
    name = "grep"
    toolset = "search"
    description = (
        "Regex search across files. Returns file:line:match. Uses ripgrep if "
        "available, else Python fallback. Scope to a path; case-sensitive optional."
    )
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Rust/Python regex"},
            "path": {"type": "string", "description": "file/dir/glob root (default cwd)"},
            "case": {"type": "boolean", "default": False, "description": "case-sensitive"},
        },
        "required": ["pattern"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args.get("pattern", ""))
        if not pattern:
            return ToolResult.err("pattern required")
        root = _resolve(ctx.cwd, str(args.get("path", ".")))
        case = bool(args.get("case", False))
        if not root.exists():
            return ToolResult.err(f"root not found: {root}")
        rg = shutil.which("rg")
        if rg:
            return await _grep_rg(rg, pattern, root, case)
        return await _grep_py(pattern, root, case)


glob = GlobTool()
grep = GrepTool()


def _resolve(cwd: str, path: str) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (Path(cwd) / p)


def _glob(root: Path, pattern: str, limit: int) -> list[str]:
    out: list[str] = []
    # A pattern containing "/" scopes by path — never fall back to a bare
    # basename match, or `src/**/*.py` would match .py files everywhere.
    path_mode = "/" in pattern
    # recursive descent with ignore pruning
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and not d.startswith(".")]
        for name in filenames:
            if (not path_mode and fnmatch.fnmatch(name, pattern)) or _match_path(
                Path(dirpath, name), root, pattern
            ):
                rel = os.path.relpath(str(Path(dirpath, name)), str(root))
                out.append(rel)
                if len(out) >= limit:
                    return sorted(out)
    out.sort()
    return out


def _match_path(path: Path, root: Path, pattern: str) -> bool:
    rel = path.relative_to(root)
    parts = str(rel).replace(os.sep, "/")
    # translate ** globs
    rx = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", ".")
    return re.fullmatch(rx, parts) is not None


async def _grep_rg(rg: str, pattern: str, root: Path, case: bool) -> ToolResult:
    cmd = [rg, "--no-heading", "-n", "--color=never", "-M", "200"]
    cmd += [] if case else ["-i"]
    cmd += ["-g", "!.venv", "-g", "!node_modules", "-g", "!__pycache__", "-g", "!.git"]
    # -e guards patterns that start with `-` from being parsed as flags
    cmd += ["-e", pattern, str(root)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (asyncio.TimeoutError, FileNotFoundError):
        return await _grep_py(pattern, root, case)
    text = out.decode("utf-8", "replace")
    lines = text.splitlines()
    if not lines:
        return ToolResult.ok("(no matches)", raw=[])
    shown = lines[:_MAX_MATCHES]
    body = "\n".join(shown)
    if len(lines) > _MAX_MATCHES:
        body += f"\n…[{len(lines) - _MAX_MATCHES} more matches]"
    return ToolResult.ok(body, raw=len(lines))


async def _grep_py(pattern: str, root: Path, case: bool) -> ToolResult:
    flags = 0 if case else re.IGNORECASE
    try:
        rx = re.compile(pattern, flags)
    except re.error as e:
        return ToolResult.err(f"bad regex: {e}")
    matches: list[str] = []
    files = 0
    if root.is_file():
        files_to_scan = [root]
    else:
        files_to_scan = []
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in _IGNORE_DIRS]
            for fn in fns:
                files_to_scan.append(Path(dp, fn))
                files += 1
                if files > _MAX_FILES:
                    break
            if files > _MAX_FILES:
                break
    for fp in files_to_scan[:_MAX_FILES]:
        try:
            data = fp.read_bytes()
            if not _is_probably_text(data):
                continue
            for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(line):
                    matches.append(f"{fp}:{i}:{line[:200]}")
                    if len(matches) >= _MAX_MATCHES:
                        matches.append("…[truncated]")
                        return ToolResult.ok("\n".join(matches), raw=len(matches))
        except OSError:
            continue
    if not matches:
        return ToolResult.ok("(no matches)", raw=[])
    return ToolResult.ok("\n".join(matches), raw=len(matches))


def _is_probably_text(data: bytes) -> bool:
    if b"\x00" in data[:1024]:
        return False
    try:
        data[:4096].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False
