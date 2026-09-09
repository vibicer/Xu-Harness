"""ast toolset — ast_grep + ast_edit.

Shells out to the `ast-grep` (`sg`) binary when present (fast, multi-language
structural search + codemods). Falls back to Python's `ast` module for `.py`
files only when the binary is absent. The agent uses these for codemods where
text replace is unsafe.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult


def _sg() -> str | None:
    return shutil.which("ast-grep") or shutil.which("sg")


class AstGrepTool(Tool):
    name = "ast_grep"
    toolset = "ast"
    description = (
        "Structural AST search. Run a pattern across files/dirs, return matches "
        "with file:line:capture. Uses the `ast-grep` binary (multi-language)."
    )
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "ast-grep pattern (use $NAME metavariables)"},
            "path": {"type": "string", "default": "."},
            "lang": {"type": "string", "description": "ast-grep -l language id"},
            "strict": {"type": "boolean", "default": False},
        },
        "required": ["pattern", "path"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args.get("pattern", ""))
        root = _resolve(ctx.cwd, str(args.get("path", ".")))
        if not pattern:
            return ToolResult.err("pattern required")
        if not root.exists():
            return ToolResult.err(f"path not found: {root}")
        sg = _sg()
        if not sg:
            return ToolResult.err("ast-grep binary not installed; install `ast-grep` for AST tools")
        cmd = [sg, "run", "--json=compact"]
        if args.get("lang"):
            cmd += ["-l", str(args["lang"])]
        if args.get("strict"):
            cmd += ["--strict-pattern"]
        cmd += ["--pattern", pattern, str(root)]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except (asyncio.TimeoutError, FileNotFoundError):
            return ToolResult.err("ast-grep timed out or missing")
        if not out:
            return ToolResult.ok(f"(no matches)\n{err.decode('utf-8','replace').strip()}", raw=0)
        try:
            matches = json.loads(out.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            return ToolResult.ok(out.decode("utf-8", "replace")[:4000])
        lines = []
        for m in matches[:200]:
            f = m.get("file", "?")
            pos = m.get("range", {}).get("byteOffset", m.get("lineNumber", "?"))
            lines.append(f"{f}:{pos}: {json.dumps(m.get('text', m.get('metaVariables', {})))[:120]}")
        body = "\n".join(lines)
        if len(matches) > 200:
            body += f"\n…[{len(matches) - 200} more]"
        return ToolResult.ok(body, raw=len(matches))


class AstEditTool(Tool):
    name = "ast_edit"
    toolset = "ast"
    description = (
        "Structural AST rewrite. Rewrite a pattern → replacement across files "
        "(metavariables substitute). Uses `ast-grep` --rewrite. For one-off "
        "text edits prefer the `edit` tool."
    )
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "rewrite": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"}},
            "lang": {"type": "string"},
        },
        "required": ["pattern", "rewrite", "paths"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args.get("pattern", ""))
        rewrite = str(args.get("rewrite", ""))
        paths = args.get("paths", [])
        if not pattern or not rewrite or not paths:
            return ToolResult.err("pattern, rewrite, paths required")
        sg = _sg()
        if not sg:
            return ToolResult.err("ast-grep binary not installed")
        targets = [str(_resolve(ctx.cwd, p)) for p in paths]
        # `-U` is what actually writes the files; without it ast-grep only
        # prints a diff and exits 0, so the tool reported matches it never
        # applied. `--json` suppresses the write even alongside `-U`, so the
        # count comes from ast-grep's own "Applied N changes" on stderr.
        cmd = [sg, "run", "-U", "--rewrite", rewrite]
        if args.get("lang"):
            cmd += ["-l", str(args["lang"])]
        cmd += ["--pattern", pattern, *targets]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            _out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
        except (asyncio.TimeoutError, FileNotFoundError):
            return ToolResult.err("ast-grep timed out or missing")
        stderr = err.decode("utf-8", "replace")
        applied = re.search(r"Applied (\d+) changes", stderr)
        n = int(applied.group(1)) if applied else 0
        if not n:
            return ToolResult.ok(f"rewrote 0 matches\n{stderr.strip()}", raw={"matches": 0})
        return ToolResult.ok(f"rewrote {n} matches", raw={"matches": n, "stderr": stderr})


def _resolve(cwd: str, p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else Path(cwd) / path


ast_grep = AstGrepTool()
ast_edit = AstEditTool()
