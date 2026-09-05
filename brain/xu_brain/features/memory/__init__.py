"""Memory store — single MEMORY.md, §-delimited entries.

The Xu memory is a human-editable flat file at ``data_home/MEMORY.md``. Each
entry is a block:

```
§ <id>
# <badge>
<text body, possibly multiline>

§ <next-id>
...
```

The badge is one of ``from-session`` (captured automatically) or
``user-edited`` (touched in the state panel / via the ``memory_edit`` tool).
Writes are atomic (tmp file + ``os.replace``) and the store safe-loads on a
missing or corrupt file, treating it as empty.

The store itself only persists — approval gating for durable writes is the
tool layer's responsibility (the ``memory_edit`` / ``retain`` tools declare
``ApprovalLevel.ALWAYS``); the registry and approval manager handle the
actual prompt. This keeps the store usable from non-tool callers (the state
panel RPC, the reflect prompt builder) without an approval dependency.
"""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path
from typing import Any

__all__ = ["MemoryStore", "MemoryEntry"]

# Badge vocabulary (contract/methods.md memory.list).
BADGE_FROM_SESSION = "from-session"
BADGE_USER_EDITED = "user-edited"
_BADGES = frozenset({BADGE_FROM_SESSION, BADGE_USER_EDITED})

# Entry delimiter line: "§ <id>". The id is alphanumeric (m<hex>).
_ENTRY_RE = re.compile(r"^§\s+(\S+)\s*$")
_BADGE_RE = re.compile(r"^#\s+(\S+)\s*$")


def _new_id() -> str:
    """``m`` + 6 hex chars — short, collision-resistant for a personal file."""
    return "m" + secrets.token_hex(3)


class MemoryStore:
    """§-delimited MEMORY.md backed store.

    All mutating methods flush to disk atomically; reads parse the file fresh
    each call (the file is small and shared with the user's editor, so an
    in-memory cache would just go stale). Corrupt / missing files are treated
    as empty — the store never raises on a bad file, it just starts clean.
    """

    def __init__(self, data_home: Path) -> None:
        self.data_home = Path(data_home)
        self.file = self.data_home / "MEMORY.md"

    # ---- low-level parse / serialize -------------------------------------

    def _read(self) -> list[dict[str, str]]:
        """Parse MEMORY.md into ordered entries; safe-loads on any failure.

        Returns a list of ``{id, text, badge}`` dicts, oldest first. A missing
        or unparseable file yields ``[]``.
        """
        if not self.file.exists():
            return []
        try:
            raw = self.file.read_text("utf-8")
        except OSError:
            return []

        entries: list[dict[str, str]] = []
        cur: dict[str, str] | None = None
        body_lines: list[str] = []

        def _flush() -> None:
            nonlocal cur, body_lines
            if cur is not None:
                # Collapse a single trailing blank-line separator we write
                # ourselves; keep internal blank lines verbatim.
                text = "\n".join(body_lines).rstrip("\n")
                cur["text"] = text
                entries.append(cur)
                cur = None
                body_lines = []

        for line in raw.splitlines():
            m = _ENTRY_RE.match(line)
            if m:
                _flush()
                cur = {"id": m.group(1), "badge": BADGE_FROM_SESSION, "text": ""}
                body_lines = []
                continue
            if cur is not None:
                bm = _BADGE_RE.match(line)
                if bm and not body_lines and bm.group(1) in _BADGES:
                    # The badge line is the first body line of the block — and
                    # only when it carries a known badge. Any other leading
                    # `# …` line is entry text and must be kept, not consumed.
                    cur["badge"] = bm.group(1)
                    continue
                body_lines.append(line)
        _flush()
        return entries

    def _serialize(self, entries: list[dict[str, str]]) -> str:
        out: list[str] = []
        for e in entries:
            out.append(f"§ {e['id']}")
            out.append(f"# {e['badge']}")
            text = e.get("text", "")
            if text:
                out.append(text)
            out.append("")  # blank-line separator between entries
        return "\n".join(out)

    def _write(self, entries: list[dict[str, str]]) -> None:
        """Atomic write: tmp in the same dir, then ``os.replace``."""
        self.data_home.mkdir(parents=True, exist_ok=True)
        content = self._serialize(entries)
        tmp = self.file.with_name(self.file.name + ".tmp")
        tmp.write_text(content, "utf-8")
        os.replace(tmp, self.file)

    # ---- public API (contract: memory/__init__.py) ----------------------

    def list(self) -> list[dict[str, Any]]:
        """All entries as ``{id, text, badge}``, newest last."""
        return [
            {"id": e["id"], "text": e["text"], "badge": e["badge"]}
            for e in self._read()
        ]

    def get(self, id: str) -> dict[str, Any] | None:
        """Return one entry by id, or ``None`` if absent."""
        for e in self._read():
            if e["id"] == id:
                return {"id": e["id"], "text": e["text"], "badge": e["badge"]}
        return None

    def append(self, text: str, badge: str = BADGE_FROM_SESSION) -> str:
        """Append a new entry; returns its generated id.

        ``badge`` is normalized to the allowed vocabulary; an unknown badge
        falls back to ``from-session``.
        """
        if badge not in _BADGES:
            badge = BADGE_FROM_SESSION
        entries = self._read()
        eid = _new_id()
        entries.append({"id": eid, "text": text, "badge": badge})
        self._write(entries)
        return eid

    def update(self, id: str, text: str) -> bool:
        """Replace an entry's text, marking it ``user-edited``.

        Returns ``True`` if the id was found and updated, ``False`` otherwise.
        The badge flips to ``user-edited`` to reflect a deliberate edit.
        """
        entries = self._read()
        for e in entries:
            if e["id"] == id:
                e["text"] = text
                e["badge"] = BADGE_USER_EDITED
                self._write(entries)
                return True
        return False

    def delete(self, id: str) -> bool:
        """Remove an entry by id; returns whether it existed."""
        entries = self._read()
        filtered = [e for e in entries if e["id"] != id]
        if len(filtered) == len(entries):
            return False
        self._write(filtered)
        return True

    def retain(self, id: str) -> bool:
        """Mark an entry durable.

        In v1 the MEMORY.md file is the durable store — every persisted entry
        is already retained. This confirms the entry exists and rewrites it in
        place (no badge change: a retained session note keeps its badge). The
        ALWAYS-gated ``retain`` tool calls this so the user's explicit
        approval is captured even though the on-disk effect is idempotent.
        Returns ``True`` if the entry exists.
        """
        entries = self._read()
        if not any(e["id"] == id for e in entries):
            return False
        # Idempotent rewrite — confirms persistence and keeps the file tidy.
        self._write(entries)
        return True

    def recall(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Case-insensitive substring search over entry text.

        Returns matching entries (newest last) up to ``limit`` (default 20).
        An empty query matches nothing — recall is a search, not a list-all.
        """
        if not query:
            return []
        needle = query.lower()
        out: list[dict[str, Any]] = []
        for e in self._read():
            if needle in e["text"].lower():
                out.append({"id": e["id"], "text": e["text"], "badge": e["badge"]})
                if len(out) >= limit:
                    break
        return out

    def reflect(self, only: list[str] | None = None) -> str:
        """Compact digest of (optionally namespace-scoped) entries for the
        system prompt.

        Each entry is prefixed with its id so the model can cite/edit it
        precisely. ``only`` restricts to entries whose id starts with any of
        the given prefixes (``None`` = all, ``[]`` = none). Empty memory or an
        empty scope yields an empty string.
        """
        entries = self._read()
        if not entries:
            return ""
        if only is not None:
            entries = [e for e in entries if any(e["id"].startswith(p) for p in only)]
        if not entries:
            return ""
        lines: list[str] = []
        for e in entries:
            body = e["text"].strip()
            # Collapse internal newlines to a single line for a compact digest.
            body = " ".join(body.split())
            lines.append(f"[{e['id']}] {body}")
        return "\n".join(lines)


# Typing alias mirroring the TS ``MemoryEntry`` shape for callers that want it.
MemoryEntry = dict[str, Any]
