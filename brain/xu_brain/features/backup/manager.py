"""Filesystem backup and undo manager for Xu Harness.

Tracks file states before and after mutating tool executions (write, edit, ast_edit).
Maintains content-addressable blob storage under `<data_home>/backups/blobs` and an
append-only SQLite journal in `<data_home>/backups/undo.db`. Supports turn-by-turn
rollback, path-targeted undo, redo, diff generation, and history inspection.
"""
from __future__ import annotations

import contextlib
import difflib
import hashlib
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class SnapshotToken:
    id: str
    session_id: str
    turn_id: str
    path: str
    rel_path: str
    existed: bool
    original_hash: str | None
    mode: int | None
    tool: str
    created_at: float


@dataclass
class FileSnapshot:
    id: str
    session_id: str
    turn_id: str
    path: str
    rel_path: str
    existed: bool
    original_hash: str | None
    new_hash: str | None
    tool: str
    created_at: float
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "path": self.path,
            "rel_path": self.rel_path,
            "existed": self.existed,
            "original_hash": self.original_hash,
            "new_hash": self.new_hash,
            "tool": self.tool,
            "created_at": self.created_at,
            "status": self.status,
        }


@dataclass
class UndoResult:
    success: bool
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    turns: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.success,
            "restored": self.restored,
            "deleted": self.deleted,
            "turns": self.turns,
            "message": self.message,
        }


@dataclass
class RedoResult:
    success: bool
    restored: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    turns: list[str] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.success,
            "restored": self.restored,
            "deleted": self.deleted,
            "turns": self.turns,
            "message": self.message,
        }


class BackupManager:
    """Manages file backups, turn-based rollback, and redo stacks."""

    def __init__(self, data_home: Path | str) -> None:
        self.data_home = Path(data_home).expanduser()
        self.backups_dir = self.data_home / "backups"
        self.blobs_dir = self.backups_dir / "blobs"
        self.db_path = self.backups_dir / "undo.db"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS snapshots (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    rel_path TEXT NOT NULL,
                    existed INTEGER NOT NULL,
                    original_hash TEXT,
                    new_hash TEXT,
                    tool TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_session_status "
                "ON snapshots(session_id, status, created_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_turn "
                "ON snapshots(session_id, turn_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_snapshots_path "
                "ON snapshots(session_id, path)"
            )

    def _blob_path(self, sha: str) -> Path:
        return self.blobs_dir / sha[:2] / sha

    def _save_blob(self, data: bytes) -> str:
        sha = hashlib.sha256(data).hexdigest()
        dest = self._blob_path(sha)
        if dest.exists():
            return sha
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via temp file in the same directory
        with tempfile.NamedTemporaryFile(dir=dest.parent, delete=False) as tf:
            tf.write(data)
            tmp_name = tf.name
        os.replace(tmp_name, dest)
        return sha

    def _get_blob_bytes(self, sha: str) -> bytes | None:
        if not sha:
            return None
        dest = self._blob_path(sha)
        if dest.exists():
            return dest.read_bytes()
        # Fallback to flat directory if present
        flat = self.blobs_dir / sha
        if flat.exists():
            return flat.read_bytes()
        return None

    def record_pre_state(
        self,
        session_id: str,
        turn_id: str,
        path: Path | str,
        tool: str,
        cwd: str = "",
    ) -> SnapshotToken | None:
        """Capture pre-mutation file state. Safe: returns None on any failure."""
        try:
            p = Path(path).expanduser()
            if not p.is_absolute() and cwd:
                p = Path(cwd) / p
            p = p.resolve()

            try:
                rel_path = str(p.relative_to(Path(cwd).resolve())) if cwd else p.name
            except Exception:
                rel_path = str(p)

            if p.is_file():
                existed = True
                data = p.read_bytes()
                orig_hash = self._save_blob(data)
                mode = p.stat().st_mode
            else:
                existed = False
                orig_hash = None
                mode = None

            return SnapshotToken(
                id=uuid.uuid4().hex[:12],
                session_id=session_id or "default",
                turn_id=turn_id or "initial",
                path=str(p),
                rel_path=rel_path,
                existed=existed,
                original_hash=orig_hash,
                mode=mode,
                tool=tool,
                created_at=time.time(),
            )
        except Exception:
            return None

    def commit_post_state(self, token: SnapshotToken | None) -> FileSnapshot | None:
        """Commit post-mutation file state to journal. Safe: returns None on no-op or error."""
        if token is None:
            return None
        try:
            p = Path(token.path)
            if not p.exists():
                if not token.existed:
                    return None  # Created nothing, existed nothing
                new_hash = None
            else:
                if not p.is_file():
                    return None
                data = p.read_bytes()
                new_hash = self._save_blob(data)
                if token.existed and new_hash == token.original_hash:
                    return None  # No content changes

            with contextlib.closing(self._connect()) as conn, conn:
                # Invalidate redo stack for this session on new mutations
                conn.execute(
                    "DELETE FROM snapshots WHERE session_id = ? AND status = 'undone'",
                    (token.session_id,),
                )
                conn.execute(
                    """
                    INSERT INTO snapshots (
                        id, session_id, turn_id, path, rel_path, existed,
                        original_hash, new_hash, tool, created_at, status
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        token.id,
                        token.session_id,
                        token.turn_id,
                        token.path,
                        token.rel_path,
                        1 if token.existed else 0,
                        token.original_hash,
                        new_hash,
                        token.tool,
                        token.created_at,
                    ),
                )
            return FileSnapshot(
                id=token.id,
                session_id=token.session_id,
                turn_id=token.turn_id,
                path=token.path,
                rel_path=token.rel_path,
                existed=token.existed,
                original_hash=token.original_hash,
                new_hash=new_hash,
                tool=token.tool,
                created_at=token.created_at,
                status="active",
            )
        except Exception:
            return None

    def undo(
        self,
        session_id: str,
        steps: int = 1,
        path: str | None = None,
        cwd: str = "",
    ) -> UndoResult:
        """Undo file changes. If path is given, undoes that file; otherwise undoes `steps` turns."""
        try:
            with contextlib.closing(self._connect()) as conn:
                if path:
                    norm = Path(path).expanduser()
                    if not norm.is_absolute() and cwd:
                        norm = Path(cwd) / norm
                    target_abs = str(norm.resolve())
                    rows = conn.execute(
                        """
                        SELECT * FROM snapshots
                        WHERE session_id = ? AND status = 'active'
                          AND (path = ? OR rel_path = ? OR path LIKE ?)
                        ORDER BY created_at DESC, id DESC
                        LIMIT ?
                        """,
                        (session_id, target_abs, path, f"%{path}", steps),
                    ).fetchall()
                else:
                    turns = conn.execute(
                        """
                        SELECT DISTINCT turn_id FROM snapshots
                        WHERE session_id = ? AND status = 'active'
                        ORDER BY created_at DESC
                        LIMIT ?
                        """,
                        (session_id, max(1, steps)),
                    ).fetchall()
                    if not turns:
                        return UndoResult(
                            success=True,
                            restored=[],
                            deleted=[],
                            turns=[],
                            message="Nothing to undo for this session.",
                        )
                    turn_ids = [t["turn_id"] for t in turns]
                    placeholders = ",".join("?" for _ in turn_ids)
                    rows = conn.execute(
                        f"""
                        SELECT * FROM snapshots
                        WHERE session_id = ? AND status = 'active'
                          AND turn_id IN ({placeholders})
                        ORDER BY created_at DESC, id DESC
                        """,
                        (session_id, *turn_ids),
                    ).fetchall()

            if not rows:
                return UndoResult(
                    success=True,
                    restored=[],
                    deleted=[],
                    turns=[],
                    message="Nothing to undo for this session.",
                )

            restored: list[str] = []
            deleted: list[str] = []
            undone_ids: list[str] = []
            turn_names: list[str] = []

            for row in rows:
                target = Path(row["path"])
                if row["existed"] == 0:
                    # File was newly created by the tool; delete it
                    if target.exists():
                        target.unlink()
                        deleted.append(row["rel_path"])
                else:
                    # File existed before; restore original bytes from blob
                    orig_hash = row["original_hash"]
                    if orig_hash:
                        content = self._get_blob_bytes(orig_hash)
                        if content is not None:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_bytes(content)
                            restored.append(row["rel_path"])

                undone_ids.append(row["id"])
                if row["turn_id"] not in turn_names:
                    turn_names.append(row["turn_id"])

            with contextlib.closing(self._connect()) as conn, conn:
                id_placeholders = ",".join("?" for _ in undone_ids)
                conn.execute(
                    f"UPDATE snapshots SET status = 'undone' WHERE id IN ({id_placeholders})",
                    undone_ids,
                )

            summary_parts = []
            if restored:
                summary_parts.append(f"restored {len(restored)} file(s): {', '.join(restored)}")
            if deleted:
                summary_parts.append(f"deleted {len(deleted)} newly created file(s): {', '.join(deleted)}")
            msg = f"Undid changes from turn(s) {', '.join(turn_names)}: " + "; ".join(summary_parts)
            return UndoResult(
                success=True,
                restored=restored,
                deleted=deleted,
                turns=turn_names,
                message=msg,
            )
        except Exception as exc:
            return UndoResult(
                success=False,
                restored=[],
                deleted=[],
                turns=[],
                message=f"Undo failed: {exc}",
            )

    def redo(self, session_id: str, steps: int = 1, cwd: str = "") -> RedoResult:
        """Redo previously undone file changes."""
        try:
            with contextlib.closing(self._connect()) as conn:
                turns = conn.execute(
                    """
                    SELECT DISTINCT turn_id FROM snapshots
                    WHERE session_id = ? AND status = 'undone'
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (session_id, max(1, steps)),
                ).fetchall()
                if not turns:
                    return RedoResult(
                        success=True,
                        restored=[],
                        deleted=[],
                        turns=[],
                        message="Nothing to redo for this session.",
                    )
                turn_ids = [t["turn_id"] for t in turns]
                placeholders = ",".join("?" for _ in turn_ids)
                rows = conn.execute(
                    f"""
                    SELECT * FROM snapshots
                    WHERE session_id = ? AND status = 'undone'
                      AND turn_id IN ({placeholders})
                    ORDER BY created_at ASC, id ASC
                    """,
                    (session_id, *turn_ids),
                ).fetchall()

            restored: list[str] = []
            deleted: list[str] = []
            redone_ids: list[str] = []
            turn_names: list[str] = []

            for row in rows:
                target = Path(row["path"])
                new_hash = row["new_hash"]
                if new_hash:
                    content = self._get_blob_bytes(new_hash)
                    if content is not None:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(content)
                        restored.append(row["rel_path"])
                else:
                    if target.exists():
                        target.unlink()
                        deleted.append(row["rel_path"])

                redone_ids.append(row["id"])
                if row["turn_id"] not in turn_names:
                    turn_names.append(row["turn_id"])

            with contextlib.closing(self._connect()) as conn, conn:
                id_placeholders = ",".join("?" for _ in redone_ids)
                conn.execute(
                    f"UPDATE snapshots SET status = 'active' WHERE id IN ({id_placeholders})",
                    redone_ids,
                )

            summary_parts = []
            if restored:
                summary_parts.append(f"re-applied {len(restored)} file(s): {', '.join(restored)}")
            if deleted:
                summary_parts.append(f"deleted {len(deleted)} file(s): {', '.join(deleted)}")
            msg = f"Redid changes from turn(s) {', '.join(turn_names)}: " + "; ".join(summary_parts)
            return RedoResult(
                success=True,
                restored=restored,
                deleted=deleted,
                turns=turn_names,
                message=msg,
            )
        except Exception as exc:
            return RedoResult(
                success=False,
                restored=[],
                deleted=[],
                turns=[],
                message=f"Redo failed: {exc}",
            )

    def diff(
        self,
        session_id: str,
        turn_id: str | None = None,
        backup_id: str | None = None,
    ) -> str:
        """Generate unified diff for snapshots in a session or turn."""
        try:
            with contextlib.closing(self._connect()) as conn:
                if backup_id:
                    rows = conn.execute(
                        "SELECT * FROM snapshots WHERE id = ?", (backup_id,)
                    ).fetchall()
                elif turn_id:
                    rows = conn.execute(
                        "SELECT * FROM snapshots WHERE session_id = ? AND turn_id = ? ORDER BY created_at ASC",
                        (session_id, turn_id),
                    ).fetchall()
                else:
                    # Latest turn with active snapshots
                    latest = conn.execute(
                        "SELECT turn_id FROM snapshots WHERE session_id = ? AND status = 'active' ORDER BY created_at DESC LIMIT 1",
                        (session_id,),
                    ).fetchone()
                    if not latest:
                        return ""
                    rows = conn.execute(
                        "SELECT * FROM snapshots WHERE session_id = ? AND turn_id = ? ORDER BY created_at ASC",
                        (session_id, latest["turn_id"]),
                    ).fetchall()

            diff_parts: list[str] = []
            for row in rows:
                orig_bytes = self._get_blob_bytes(row["original_hash"]) if row["original_hash"] else b""
                new_bytes = self._get_blob_bytes(row["new_hash"]) if row["new_hash"] else b""
                orig_lines = (orig_bytes.decode("utf-8", "replace")).splitlines(keepends=True)
                new_lines = (new_bytes.decode("utf-8", "replace")).splitlines(keepends=True)

                rel = row["rel_path"]
                part = "".join(
                    difflib.unified_diff(
                        orig_lines,
                        new_lines,
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                    )
                )
                if part:
                    diff_parts.append(part)

            return "\n".join(diff_parts)
        except Exception:
            return ""

    def history(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Return history of file modifications in this session."""
        try:
            with contextlib.closing(self._connect()) as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM snapshots
                    WHERE session_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                    """,
                    (session_id, limit),
                ).fetchall()
                return [FileSnapshot(**dict(r)).to_dict() for r in rows]
        except Exception:
            return []
