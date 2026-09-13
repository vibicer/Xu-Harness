"""repo_map toolset — structural repository map and symbol index.

Provides fast, high-density codebase orientation for coding agents:
- 'map': High-density outline of files and their declared symbols (classes, methods,
  functions, types, interfaces). Scoped by path, depth, or query filter.
- 'symbols': Search across all symbols in the repository matching a query/pattern.
- 'tree': Fast directory tree of tracked source files.
- 'overview': High-level statistics on languages, file counts, and top modules.
"""
from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
from pathlib import Path
from typing import Any

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult

# File extensions recognized as source code
_CODE_EXTENSIONS: frozenset[str] = frozenset({
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".svelte",
    ".vue",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".cc",
    ".h",
    ".hpp",
    ".cs",
    ".rb",
    ".php",
    ".swift",
    ".kt",
    ".dart",
    ".scala",
    ".lua",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".md",
})

# Directories to ignore during walking
_IGNORE_DIRS: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    ".venv",
    "venv",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "target",
    "bin",
    "obj",
    ".next",
    ".nuxt",
    ".turbo",
    "vendor",
    ".idea",
    ".vscode",
    ".svelte-kit",
    ".coverage",
    "coverage",
})


def _resolve_path(cwd: str, path_arg: str) -> Path:
    p = Path(path_arg.strip() if path_arg else ".")
    if not p.is_absolute():
        p = (Path(cwd) / p).resolve()
    return p


async def _get_tracked_files(root: Path, cwd: Path) -> list[Path]:
    """Return all git-tracked and untracked non-ignored files, falling back to os.walk."""
    git_bin = shutil.which("git")
    if git_bin and (root / ".git").exists() or (cwd / ".git").exists():
        try:
            rel_target = os.path.relpath(root, cwd)
            cmd = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
            if rel_target not in (".", ""):
                cmd.extend(["--", rel_target])
            proc = await asyncio.create_subprocess_exec(
                git_bin,
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd),
            )
            stdout_b, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if proc.returncode == 0 and stdout_b:
                files = []
                for line in stdout_b.decode("utf-8", errors="replace").splitlines():
                    clean = line.strip().strip('"')
                    if clean:
                        fpath = (cwd / clean).resolve()
                        if fpath.is_file() and fpath.suffix.lower() in _CODE_EXTENSIONS:
                            files.append(fpath)
                if files:
                    return sorted(files)
        except Exception:  # noqa: BLE001
            pass

    # Fallback: manual os.walk
    files = []
    if root.is_file():
        return [root] if root.suffix.lower() in _CODE_EXTENSIONS else []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _IGNORE_DIRS and not d.startswith(".")]
        for fn in filenames:
            ext = Path(fn).suffix.lower()
            if ext in _CODE_EXTENSIONS:
                files.append(Path(dirpath) / fn)
    return sorted(files)


def _extract_py_symbols(code: str) -> list[tuple[str, str, int, list[str]]]:
    """Extract (kind, signature, lineno, methods) for Python."""
    symbols: list[tuple[str, str, int, list[str]]] = []
    try:
        tree = ast.parse(code)
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                methods = []
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        params = [a.arg for a in item.args.args if a.arg not in ("self", "cls")]
                        prefix = "async def" if isinstance(item, ast.AsyncFunctionDef) else "def"
                        methods.append(f"{prefix} {item.name}({', '.join(params[:5])})")
                symbols.append(("class", node.name, node.lineno, methods))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                params = [a.arg for a in node.args.args]
                prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
                sig = f"{prefix} {node.name}({', '.join(params[:5])})"
                symbols.append(("func", sig, node.lineno, []))
    except Exception:
        pass
    return symbols


def _extract_regex_symbols(ext: str, code: str) -> list[tuple[str, str, int, list[str]]]:
    """Extract symbols using fast structural regex matching."""
    symbols: list[tuple[str, str, int, list[str]]] = []
    lines = code.splitlines()

    if ext in (".ts", ".tsx", ".js", ".jsx", ".svelte", ".vue"):
        for idx, line in enumerate(lines, 1):
            s = line.strip()
            # export function / function
            m = re.match(
                r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s+([a-zA-Z0-9_$]+)\s*\(([^)]*)\)",
                s,
            )
            if m:
                args = ", ".join(
                    a.strip().split(":")[0].strip() for a in m.group(2).split(",") if a.strip()
                )
                symbols.append(("func", f"{m.group(1)}({args})", idx, []))
                continue
            # export class / class
            m = re.match(r"^(?:export\s+)?(?:default\s+)?class\s+([a-zA-Z0-9_$]+)", s)
            if m:
                symbols.append(("class", m.group(1), idx, []))
                continue
            # interface
            m = re.match(r"^(?:export\s+)?interface\s+([a-zA-Z0-9_$]+)", s)
            if m:
                symbols.append(("interface", m.group(1), idx, []))
                continue
            # type
            m = re.match(r"^(?:export\s+)?type\s+([a-zA-Z0-9_$]+)\b", s)
            if m:
                symbols.append(("type", m.group(1), idx, []))
                continue
            # export const arrow function / value
            m = re.match(r"^export\s+const\s+([a-zA-Z0-9_$]+)\s*=", s)
            if m:
                symbols.append(("const", m.group(1), idx, []))
                continue
    elif ext == ".rs":
        for idx, line in enumerate(lines, 1):
            s = line.strip()
            m = re.match(r"^(?:pub\s+)?(?:async\s+)?fn\s+([a-zA-Z0-9_]+)\s*\(([^)]*)\)", s)
            if m:
                symbols.append(("fn", f"{m.group(1)}({m.group(2).strip()})", idx, []))
                continue
            m = re.match(r"^(?:pub\s+)?(?:struct|enum|trait|type)\s+([a-zA-Z0-9_]+)", s)
            if m:
                symbols.append(("type", m.group(1), idx, []))
                continue
    elif ext == ".go":
        for idx, line in enumerate(lines, 1):
            s = line.strip()
            m = re.match(r"^func\s+(?:\([a-zA-Z0-9_ *]+\)\s+)?([a-zA-Z0-9_]+)\s*\(([^)]*)\)", s)
            if m:
                symbols.append(("func", f"{m.group(1)}({m.group(2).strip()})", idx, []))
                continue
            m = re.match(r"^type\s+([a-zA-Z0-9_]+)\s+(?:struct|interface)", s)
            if m:
                symbols.append(("type", m.group(1), idx, []))
                continue
    elif ext in (
        ".c", ".cpp", ".cc", ".h", ".hpp", ".cs", ".java",
        ".kt", ".swift", ".dart", ".rb", ".php",
    ):
        for idx, line in enumerate(lines, 1):
            s = line.strip()
            m = re.match(
                r"^(?:public\s+|private\s+|protected\s+|static\s+|async\s+)*"
                r"(?:class|interface|struct|enum)\s+([a-zA-Z0-9_]+)",
                s,
            )
            if m:
                symbols.append(("class", m.group(1), idx, []))
                continue
            m = re.match(r"^(?:def|fun|function)\s+([a-zA-Z0-9_]+)\s*\(([^)]*)\)", s)
            if m:
                symbols.append(("func", f"{m.group(1)}({m.group(2).strip()})", idx, []))
                continue

    return symbols


def extract_symbols(path: Path) -> list[tuple[str, str, int, list[str]]]:
    """Extract symbols from path based on file type."""
    try:
        text = path.read_text("utf-8", errors="replace")
    except Exception:
        return []
    ext = path.suffix.lower()
    if ext == ".py":
        return _extract_py_symbols(text)
    return _extract_regex_symbols(ext, text)


class RepoMapTool(Tool):
    name = "repo_map"
    toolset = "search"
    description = (
        "Generate a structural repository map or search symbols across the codebase. "
        "Extracts classes, functions, methods, types, and interfaces from source files. "
        "Actions: 'map' (structural outline), 'symbols' (search symbols by query), "
        "'tree' (file hierarchy), 'overview' (language summary)."
    )
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["map", "symbols", "tree", "overview"],
"default": "map",
"description": (
"Operation mode: 'map' (symbol outline), 'symbols' (search symbols), "
"'tree' (file layout), 'overview' (module summary)."
),
},
            "path": {
                "type": "string",
                "default": ".",
                "description": "Root directory or file to map/inspect relative to cwd.",
            },
            "query": {
"type": "string",
"description": (
"Symbol name or pattern filter (for 'symbols' mode or to filter 'map')."
),
},
            "depth": {
                "type": "integer",
                "default": 4,
                "description": "Maximum directory depth to explore.",
            },
            "max_files": {
                "type": "integer",
"default": 60,
"description": (
"Maximum number of files to map symbols from (default: 60, max: 200)."
),
},
        },
    }
    output_schema = None

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = str(args.get("action") or args.get("mode") or "map").lower().strip()
        path_arg = str(args.get("path") or ".").strip()
        query = str(args.get("query") or "").strip()
        max_depth = max(1, min(10, int(args.get("depth", 4) or 4)))
        max_files = max(1, min(200, int(args.get("max_files", 60) or 60)))

        cwd_path = Path(ctx.cwd).resolve()
        target = _resolve_path(ctx.cwd, path_arg)

        if not target.exists():
            return ToolResult.err(f"path not found: {path_arg}")

        files = await _get_tracked_files(target, cwd_path)
        if not files:
            return ToolResult.ok(f"No source code files found in {path_arg}", raw={"count": 0})

        # Depth filtering
        filtered_files: list[Path] = []
        for f in files:
            try:
                rel = f.relative_to(target)
                if len(rel.parts) <= max_depth:
                    filtered_files.append(f)
            except ValueError:
                filtered_files.append(f)

        if action == "symbols" or query:
            search_limit = max(300, max_files * 5)
            return self._search_symbols(filtered_files[:search_limit], cwd_path, query)

        files = filtered_files[:max_files]

        if action == "tree":
            return self._format_tree(files, cwd_path, target)
        if action == "overview":
            return self._format_overview(filtered_files, cwd_path)

        return self._format_map(files, cwd_path, query)

    def _format_map(self, files: list[Path], cwd: Path, query: str = "") -> ToolResult:
        """Format high-density symbol outline grouped by directories."""
        lines: list[str] = []
        total_symbols = 0
        file_count = 0

        # Group by directory
        dir_groups: dict[str, list[Path]] = {}
        for f in files:
            try:
                rel = f.relative_to(cwd)
                dir_part = str(rel.parent)
                if dir_part == ".":
                    dir_part = ""
            except ValueError:
                dir_part = str(f.parent)
            dir_groups.setdefault(dir_part, []).append(f)

        for dir_name in sorted(dir_groups):
            prefix_dir = f"{dir_name}/" if dir_name else "./"
            dir_lines: list[str] = [prefix_dir]

            for f in sorted(dir_groups[dir_name]):
                symbols = extract_symbols(f)
                if query:
                    symbols = [
                        s for s in symbols
                        if query.lower() in s[1].lower()
                        or any(query.lower() in m.lower() for m in s[3])
                    ]
                if not symbols and f.suffix.lower() not in (".md", ".json", ".yaml", ".yml"):
                    continue

                file_count += 1
                total_symbols += len(symbols)
                dir_lines.append(f"  {f.name}:")

                for kind, sig, _ln, methods in symbols[:15]:
                    if kind == "class":
                        dir_lines.append(f"    class {sig}:")
                        for m in methods[:6]:
                            dir_lines.append(f"      {m}")
                        if len(methods) > 6:
                            dir_lines.append(f"      …(+{len(methods) - 6} methods)")
                    elif kind == "interface":
                        dir_lines.append(f"    interface {sig}")
                    elif kind == "type":
                        dir_lines.append(f"    type {sig}")
                    else:
                        dir_lines.append(f"    {sig}")

                if len(symbols) > 15:
                    dir_lines.append(f"    …(+{len(symbols) - 15} more symbols)")

            if len(dir_lines) > 1:
                lines.extend(dir_lines)

        output = "\n".join(lines)
        if len(output) > 14000:
            output = output[:14000] + f"\n…[truncated {len(output) - 14000} chars]"

        header = f"# Repository Map ({file_count} files, {total_symbols} symbols)\n\n"
        return ToolResult.ok(header + output, raw={"files": file_count, "symbols": total_symbols})

    def _search_symbols(self, files: list[Path], cwd: Path, query: str) -> ToolResult:
        """Search and return symbols matching query across all files."""
        matches: list[str] = []
        pattern = None
        if query:
            try:
                pattern = re.compile(query, re.IGNORECASE)
            except re.error:
                pattern = None

        for f in files:
            symbols = extract_symbols(f)
            try:
                rel_path = str(f.relative_to(cwd))
            except ValueError:
                rel_path = str(f)

            for kind, sig, ln, methods in symbols:
                # Test top-level symbol
                match_top = False
                if not query:
                    match_top = True
                elif pattern:
                    match_top = bool(pattern.search(sig))
                else:
                    match_top = query.lower() in sig.lower()

                if match_top:
                    matches.append(f"{rel_path}:{ln}: [{kind}] {sig}")

                # Test methods
                for m in methods:
                    match_m = False
                    if not query:
                        match_m = False
                    elif pattern:
                        match_m = bool(pattern.search(m))
                    else:
                        match_m = query.lower() in m.lower()
                    if match_m:
                        matches.append(f"{rel_path}:{ln}: [method in {sig}] {m}")

        if not matches:
            return ToolResult.ok(f"No symbols found matching '{query}'", raw={"matches": 0})

        body = "\n".join(matches[:150])
        if len(matches) > 150:
            body += f"\n…[{len(matches) - 150} more matches]"

        header = f"# Symbols matching '{query}' ({len(matches)} matches)\n\n"
        return ToolResult.ok(header + body, raw={"matches": len(matches)})

    def _format_tree(self, files: list[Path], cwd: Path, root: Path) -> ToolResult:
        """Format directory file tree."""
        lines = [f"{root.name}/"]
        for f in sorted(files):
            try:
                rel = f.relative_to(root)
                indent = "  " * len(rel.parts)
                lines.append(f"{indent}{rel.parts[-1]}")
            except ValueError:
                lines.append(f"  {f.name}")
        out = "\n".join(lines[:200])
        if len(lines) > 200:
            out += f"\n…[{len(lines) - 200} more files]"
        return ToolResult.ok(out, raw={"files": len(files)})

    def _format_overview(self, files: list[Path], cwd: Path) -> ToolResult:
        """Summary statistics of codebase by language and top directories."""
        by_ext: dict[str, int] = {}
        by_dir: dict[str, int] = {}
        total_lines = 0

        for f in files:
            ext = f.suffix.lower() or "other"
            by_ext[ext] = by_ext.get(ext, 0) + 1
            try:
                rel = f.relative_to(cwd)
                top = rel.parts[0] if len(rel.parts) > 1 else "."
            except ValueError:
                top = "."
            by_dir[top] = by_dir.get(top, 0) + 1
            try:
                total_lines += sum(1 for _ in f.open("rb"))
            except Exception:
                pass

        lines = [
            f"# Codebase Overview ({len(files)} files, ~{total_lines} lines)",
            "\n## By Language/Extension:",
        ]
        for ext, count in sorted(by_ext.items(), key=lambda x: -x[1]):
            lines.append(f"- `{ext}`: {count} file(s)")

        lines.append("\n## Top Directories:")
        for d, count in sorted(by_dir.items(), key=lambda x: -x[1]):
            lines.append(f"- `{d}/`: {count} file(s)")

        return ToolResult.ok("\n".join(lines), raw={"files": len(files), "lines": total_lines})


repo_map = RepoMapTool()
