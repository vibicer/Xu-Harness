"""File toolset — read/write/edit.

`read`: line selectors, raw mode, summarized output, SQLite/archives/URLs.
`write`: create/overwrite, archive member write, SQLite row upsert.
`edit`: hash-anchored line edits (`PUT N.=M:` / `CUT` / `PUT <N` …) — lands
first try by anchoring on original `#TAG` snapshots.

Output is capped by the registry; tools return the full payload as `raw`.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult


def _resolve(cwd: str, path: str) -> Path:
    p = Path(path)
    if not p.is_absolute():
        p = Path(cwd) / p
    return p.expanduser()



def _snapshot_tag(text: str) -> str:
    """Short stable content tag for the ``[path#TAG]`` edit anchor. Mirrors
    the exact file bytes (as read/written) so drift is detected, not guessed."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:6].upper()


class FileReadTool(Tool):
    name = "read"
    toolset = "file"
    description = (
        "Read a file, directory, SQLite DB, archive member, or URL. Supports "
        "line selectors (`path:N`, `path:N-M`, `path:raw`) and structural "
        "summaries for code. Directories return a depth-limited listing."
    )
    approval = ApprovalLevel.NEVER
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "file/dir/sqlite/url path (supports :selector)"},
            "offset": {"type": "integer", "description": "1-indexed start line", "default": 1},
            "limit": {"type": "integer", "description": "max lines to return"},
        },
        "required": ["path"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        raw_path = str(args.get("path", ""))
        if not raw_path:
            return ToolResult.err("path required")
        if raw_path.startswith(("http://", "https://")):
            return await _read_url(raw_path)
        # split optional `:selector` suffix (but not Windows drive letters or url :port)
        path_str, selector = _split_selector(raw_path)
        target = _resolve(ctx.cwd, path_str)
        if not target.exists():
            return ToolResult.err(f"not found: {target}")
        if target.is_dir():
            return ToolResult.ok(_dir_listing(target), raw=str(target))
        if target.is_file() and target.suffix in (".sqlite", ".sqlite3", ".db", ".db3"):
            return _read_sqlite(target, selector, args)
        if target.is_file() and target.suffix in (".tar", ".tar.gz", ".tgz", ".zip"):
            return ToolResult.ok(f"(archive: {target.name}) — extract via terminal for full access")
        try:
            text = target.read_text("utf-8")
        except UnicodeDecodeError:
            data = target.read_bytes()
            return ToolResult.ok(f"(binary, {len(data)} bytes)", raw=len(data))
        lines = text.splitlines()
        offset = int(args.get("offset", 1) or 1)
        limit = args.get("limit")
        start = max(1, offset) - 1
        end = start + int(limit) if limit else len(lines)
        sel_lines = _apply_selector(lines, selector, start, end)
        rendered = _render_lines(sel_lines)
        # snapshot anchor: `edit` copies `[path#TAG]` and verifies it,
        # so a stale read can't silently edit the wrong lines.
        rendered = f"[{path_str}#{_snapshot_tag(text)}]\n" + rendered
        return ToolResult.ok(rendered, raw=text)


class FileWriteTool(Tool):
    name = "write"
    toolset = "file"
    description = (
        "Create or overwrite a file. Creates parent dirs. For SQLite, upsert "
        "a row (db.sqlite:table:key). For archives, write a member."
    )
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "content": {"type": "string", "description": "full file content"},
            "append": {"type": "boolean", "description": "append instead of overwrite", "default": False},
        },
        "required": ["path", "content"],
    }

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path_str = str(args.get("path", ""))
        content = str(args.get("content", ""))
        append = bool(args.get("append", False))
        if not path_str:
            return ToolResult.err("path required")
        # SQLite row upsert: the suffix sits on the FILE part (`db.sqlite:t:k`),
        # so test the head before the colon — not the whole string.
        head, _, _tail = path_str.partition(":")
        if ":" in path_str and head.endswith((".sqlite", ".sqlite3", ".db", ".db3")):
            return _sqlite_write(ctx, path_str, content)
        target = _resolve(ctx.cwd, path_str)
        target.parent.mkdir(parents=True, exist_ok=True)
        mode = "a" if append else "w"
        with open(target, mode, encoding="utf-8") as f:
            f.write(content)
        note = await _diagnostics_note(target, ctx)
        return ToolResult.ok(f"wrote {len(content)} bytes → {target}{note}", raw=str(target))


class FileEditTool(Tool):
    name = "edit"
    toolset = "file"
    description = (
        "Hash-anchored line edit. Provide a `[path#TAG]` header line, then "
        "`PUT N.=M:` (replace lines N-M), `PUT <N:` (insert before), `PUT >N:` "
        "(insert after), or `CUT N.=M` (delete). Body rows start with `+`. "
        "Anchors on the original snapshot tag so edits land first try."
    )
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {
            "patch": {"type": "string", "description": "the edit patch (header + ops)"},
        },
        "required": ["patch"],
    }

    # `[path]` or `[path#tag]`; the tag is a real snapshot hex only for
    # verification — models often pass an arbitrary label (e.g. `#pickModel`),
    # which must not be rejected, just not drift-checked.
    _HEADER = re.compile(r"^\[(?P<path>[^#\]]+)(?:#(?P<tag>[^#\]\s]+))?\]")

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        patch = str(args.get("patch", ""))
        if not patch.strip():
            return ToolResult.err("empty patch")
        lines = patch.splitlines()
        # tolerate leading junk before the header: blank lines, markdown code
        # fences (```python …), and indentation — models emit these constantly
        # and a bare "must start with header" error makes them retry the exact
        # same malformed patch forever.
        while lines and (
            not lines[0].strip() or lines[0].strip().startswith("```")
        ):
            lines.pop(0)
        # a trailing closing fence (``` alone) is patch wrapper, not content
        while lines and lines[-1].strip().startswith("```"):
            lines.pop()
        if not lines:
            return ToolResult.err("empty patch")
        lines[0] = lines[0].strip()
        if not self._HEADER.match(lines[0]):
            return ToolResult.err(
                "patch must start with [path#TAG] header — first line was: "
                f"{lines[0][:80]!r}"
            )
        hdr = self._HEADER.match(lines[0])
        target = _resolve(ctx.cwd, hdr.group("path"))
        if not target.exists():
            return ToolResult.err(f"not found: {target}")
        raw_text = target.read_text("utf-8")
        tag = hdr.group("tag")
        # drift-check only when the tag is a real 6-hex snapshot id; arbitrary
        # labels can't be verified, so just edit the current content
        if (
            tag
            and re.fullmatch(r"[0-9A-Fa-f]{6}", tag)
            and tag.upper() != _snapshot_tag(raw_text)
        ):
            return ToolResult.err(
                f"file {target.name} changed since the #{tag} snapshot — "
                f"re-read it and retry the edit"
            )
        original = raw_text.splitlines()
        ops = _parse_ops(lines[1:])
        if not ops:
            return ToolResult.err("no ops parsed")
        try:
            new_lines, touched = _apply_ops(original, ops)
        except ValueError as exc:
            return ToolResult.err(str(exc))

        new_text = "\n".join(new_lines) + "\n"
        if new_text == raw_text:
            # Keep repeated model attempts from spinning forever on an edit that
            # parses successfully but cannot change the file. The state lives
            # in the per-turn config, not on this shared tool instance.
            attempts = ctx.config.setdefault("_edit_noop_attempts", {})
            key = hashlib.sha256(
                f"{_snapshot_tag(raw_text)}\0{patch}".encode("utf-8")
            ).hexdigest()
            count = int(attempts.get(key, 0)) + 1
            attempts[key] = count
            if count >= 3:
                return ToolResult.err(
                    f"repeated no-op edit blocked after {count} attempts; "
                    "re-read the file and change the edit instead of retrying it"
                )
            return ToolResult.err(
                "edit produced no changes; re-read the file and verify the "
                "target lines instead of retrying the same patch"
            )

        target.write_text(new_text, "utf-8")
        note = await _diagnostics_note(target, ctx)
        return ToolResult.ok(
            f"edited {target.name}: {touched} line(s) changed{note}", raw=str(target)
        )


async def _diagnostics_note(target: Path, ctx: ToolContext) -> str:
    """Edit/write feedback loop: surface LSP/syntax diagnostics for the file
    just written. Best-effort — a diagnostics failure never fails the write."""
    if not ctx.config.get("toolsets", {}).get("lsp", True):
        return ""
    try:
        from .lsp_debug import diagnostics_for

        return await diagnostics_for(target, ctx)
    except Exception:  # noqa: BLE001
        return ""


read = FileReadTool()
write = FileWriteTool()
edit = FileEditTool()


# ---------------------------------------------------------------------------
# selectors / rendering
# ---------------------------------------------------------------------------


async def _read_url(url: str) -> ToolResult:
    """Fetch an http(s) URL and return its body (the description advertises
    URL reads; the registry caps the model-facing size)."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as c:
            r = await c.get(url, headers={"User-Agent": "Mozilla/5.0 XuBrain"})
        r.raise_for_status()
    except httpx.HTTPError as e:
        return ToolResult.err(f"fetch failed: {e}")
    return ToolResult.ok(f"[{url}]\n{r.text}", raw=r.text)


def _split_selector(raw: str) -> tuple[str, str | None]:
    # SQLite table selector first: `db.sqlite:table` / `db.sqlite:table:pk`.
    # Anchored on the file suffix, so it can't capture plain text paths; and
    # it must run before the numeric check or `db.sqlite:t:5` would split as
    # path `db.sqlite:t` + line-selector `5`.
    m2 = re.match(r"^(.*\.(?:sqlite|sqlite3|db|db3))(?::(\w+(?::\S+)?))$", raw)
    if m2 and not raw.startswith(("http://", "https://", "ssh://", "file://")):
        return m2.group(1), m2.group(2)
    # `:selector` only when not a bare URL/port. Simple heuristic: last `:`
    # followed by digits/raw/conflicts and preceded by a non-colon.
    m = re.search(r"^(.*?)(?::(\d+(?:-\d+)?|raw|conflicts))$", raw)
    if m and not raw.startswith(("http://", "https://", "ssh://", "file://")):
        return m.group(1), m.group(2)
    return raw, None


def _apply_selector(lines: list[str], selector: str | None, start: int, end: int) -> list[tuple[int, str]]:
    if selector is None:
        rng = range(start, min(end, len(lines)))
        return [(i + 1, lines[i]) for i in rng]
    if selector == "raw":
        return [(i + 1, lines[i]) for i in range(len(lines))]
    if "-" in selector:
        a, b = selector.split("-", 1)
        rng = range(int(a) - 1, min(int(b), len(lines)))
    else:
        n = int(selector)
        rng = range(n - 1, min(n, len(lines)))
    return [(i + 1, lines[i]) for i in rng]


def _render_lines(rows: list[tuple[int, str]]) -> str:
    if len(rows) > 60:
        head = rows[:30]
        tail = rows[-15:]
        body = "\n".join(f"{n}:{t}" for n, t in head)
        body += f"\n…[{len(rows) - 45} more lines elided]…\n" + "\n".join(
            f"{n}:{t}" for n, t in tail
        )
        return body
    return "\n".join(f"{n}:{t}" for n, t in rows)


def _dir_listing(p: Path) -> str:
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    out = [f"[{p}/]"]
    for e in entries[:200]:
        if e.is_dir():
            out.append(f"  {e.name}/")
        else:
            try:
                sz = e.stat().st_size
            except OSError:
                sz = 0
            out.append(f"  {e.name}  ({_human(sz)})")
    if len(entries) > 200:
        out.append(f"…[{len(entries) - 200} more entries]")
    return "\n".join(out)


def _human(n: int) -> str:
    for unit in ("B", "K", "M", "G"):
        if n < 1024:
            return f"{n}{unit}"
        n //= 1024
    return f"{n}T"


# ---------------------------------------------------------------------------
# SQLite
# ---------------------------------------------------------------------------


def _read_sqlite(target: Path, selector: str | None, args: dict[str, Any]) -> ToolResult:
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    try:
        if selector and selector not in ("raw",):
            # path:table or path:table:pk
            parts = selector.split(":")
            table = parts[0]
            pk = parts[1] if len(parts) > 1 else None
            return _sqlite_read_table(conn, table, pk)
        # list tables
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        return ToolResult.ok(f"[{target.name}] tables: {', '.join(tables) or '(none)'}")
    finally:
        conn.close()


def _sqlite_read_table(conn: sqlite3.Connection, table: str, pk: str | None) -> ToolResult:
    if not re.match(r"^\w+$", table):
        return ToolResult.err("invalid table name")
    if pk:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        pkcol = cols[0] if cols else "rowid"
        row = conn.execute(f"SELECT * FROM {table} WHERE {pkcol} = ?", (pk,)).fetchone()
        if row is None:
            return ToolResult.err(f"no row {pk} in {table}")
        return ToolResult.ok(json.dumps(dict(row), ensure_ascii=False, indent=2))
    rows = conn.execute(f"SELECT * FROM {table} LIMIT 50").fetchall()
    return ToolResult.ok(
        json.dumps([dict(r) for r in rows], ensure_ascii=False, indent=2),
        raw=f"{len(rows)} rows",
    )


def _sqlite_write(ctx: ToolContext, path_str: str, content: str) -> ToolResult:
    # db.sqlite:table:key → upsert row (JSON content)
    file_part, sel = path_str.split(":", 1)
    parts = sel.split(":")
    table = parts[0]
    key = parts[1] if len(parts) > 1 else None
    target = _resolve(ctx.cwd, file_part)
    conn = sqlite3.connect(str(target))
    try:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
        if not cols:
            return ToolResult.err(f"no such table: {table}")
        pkcol = cols[0]
        if not content.strip():
            conn.execute(f"DELETE FROM {table} WHERE {pkcol} = ?", (key,))
            conn.commit()
            return ToolResult.ok(f"deleted {key}")
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = {"value": content}
        cols_set = [c for c in cols if c in data]
        if not cols_set:
            return ToolResult.err("no matching columns")
        placeholders = ", ".join("?" for _ in cols_set)
        colnames = ", ".join(cols_set)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({colnames}) VALUES ({placeholders})",
            [data[c] for c in cols_set],
        )
        conn.commit()
        return ToolResult.ok(f"upserted {table}:{key}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# edit patch parsing + application (hash-anchored)
# ---------------------------------------------------------------------------


def _parse_ops(lines: list[str]) -> list[dict[str, Any]]:
    """Parse a patch body into ops. Tolerant: header already stripped."""
    ops: list[dict[str, Any]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        m_put = re.match(r"^PUT\s+(.+?)\s*(?::|@(\w+))?\s*$", line)
        m_cut = re.match(r"^CUT\s+(.+?)\s*(?:@(\w+))?\s*$", line)
        if m_put and line.rstrip().endswith(":"):
            # `@reg` buffer names are accepted for grammar tolerance but carry
            # no semantics here — strip them off the spec.
            spec = re.sub(r"\s*@\w+$", "", m_put.group(1).strip())
            reg = m_put.group(2)
            body: list[str] = []
            i += 1
            # Body lines may arrive with a leading indent before `+` (fenced
            # blocks) or with no `+` at all. Take every non-op line as body
            # content; stop at the next PUT/CUT op or EOF.
            while i < len(lines) and not re.match(r"^\s*(?:PUT|CUT)\b", lines[i]):
                s = lines[i].lstrip()
                body.append(s[1:] if s.startswith("+") else s)
                i += 1
            ops.append({"op": "PUT", "spec": spec, "body": body, "reg": reg})
        elif m_cut:
            spec = re.sub(r"\s*@\w+$", "", m_cut.group(1).strip())
            reg = m_cut.group(2)
            ops.append({"op": "CUT", "spec": spec, "reg": reg})
            i += 1
        else:
            i += 1
    return ops


def _apply_ops(original: list[str], ops: list[dict[str, Any]]) -> tuple[list[str], int]:
    """Apply ops to a copy of original lines. Returns (new_lines, touched_count)."""
    out = list(original)
    offset = 0
    touched = 0
    for op in ops:
        spec = op["spec"]
        body = op.get("body", []) if op["op"] == "PUT" else []
        # Specs reference ORIGINAL line numbers (applied via `offset`); bounds
        # must be checked against the original length, not the live buffer —
        # after a growth op the live buffer is longer and would wave through
        # out-of-range line numbers that then land at the wrong position.
        n_lines = len(original)
        # resolve line range from spec, bounds-checked against the file
        # (out-of-range PUT/CUT used to silently append at EOF or report phantom
        # changes — both now raise so the model gets a real error).
        def _raise(what: str) -> None:
            raise ValueError(
                f"{what} out of range ({n_lines} line{'s' if n_lines != 1 else ''} in file)"
            )

        # tolerate read-style ranges (`N-M`) and whole-file (`*`) specs; the
        # latter normalizes to the full range (or a top insert on an empty file)
        m_range = re.fullmatch(r"(\d+)-(\d+)", spec)
        if m_range:
            spec = f"{m_range.group(1)}.={m_range.group(2)}"
        if spec == "*":
            spec = f"1.={n_lines}" if n_lines else ">0"
        if spec.startswith("<"):
            n = int(spec[1:])
            if not (1 <= n <= n_lines + 1):
                _raise(f"insert before {n}")
            idx = n - 1 + offset
            # Live-bounds guard: prior CUTs shrink the buffer, so an original-
            # length-valid line can land on a negative index. Python wraps
            # negative slice indices, silently corrupting the file — raise.
            if not (0 <= idx <= len(out)):
                _raise(f"insert before {n}")
            out[idx:idx] = body
            offset += len(body)
            touched += len(body)
        elif spec.startswith(">"):
            n = int(spec[1:])
            if not (0 <= n <= n_lines):
                _raise(f"insert after {n}")
            idx = n + offset
            if not (0 <= idx <= len(out)):
                _raise(f"insert after {n}")
            out[idx:idx] = body
            offset += len(body)
            touched += len(body)
        elif ".=" in spec:
            if not re.fullmatch(r"\d+\.=\d+", spec):
                raise ValueError(
                    f"unrecognized line spec: {spec!r} "
                    "(use N, N.=M, N-M, <N, >N, or *)"
                )
            a, b = spec.split(".=", 1)
            a, b = int(a), int(b)
            if a > b:
                # Inverted range: the value after ".=" is an absolute original
                # line number, not a line count or replacement length. Guide
                # the model to the correct form instead of a bare "out of
                # range" (the line numbers are valid — the endpoints are not).
                count = b
                counted_end = a + count - 1
                raise ValueError(
                    f"invalid absolute range: start {a}, end {b}. The value "
                    'after ".=" is an absolute source line, not a line count. '
                    f"For {count} lines starting at {a}, use "
                    f"`PUT {a}.={counted_end}:`; for one line use `PUT {a}:`."
                )
            if not (1 <= a <= b <= n_lines):
                _raise(f"lines {a}.={b}")
            i0 = a - 1 + offset
            i1 = b + offset
            if not (0 <= i0 <= i1 <= len(out)):
                _raise(f"lines {a}.={b}")
            out[i0:i1] = body
            offset += len(body) - (i1 - i0)
            touched += i1 - i0
        elif spec.isdigit():
            n = int(spec)
            if not (1 <= n <= n_lines):
                _raise(f"line {n}")
            idx = n - 1 + offset
            if not (0 <= idx < len(out)):
                _raise(f"line {n}")
            out[idx : idx + 1] = body
            offset += len(body) - 1
            touched += 1
        else:
            # unknown specs used to be a silent no-op — surface them so the
            # model can fix the patch instead of believing the edit landed
            raise ValueError(
                f"unrecognized line spec: {spec!r} (use N, N.=M, N-M, <N, >N, or *)"
            )
    return out, touched
