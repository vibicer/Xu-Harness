"""Session registry + SQLite store (`~/.xu/sessions.db`)."""
from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

# Sessions default their working directory to the repo root, not $HOME
# (tools resolve relative edit/grep paths against session.cwd). Deriving from
# this file keeps it correct regardless of where the brain is launched from.
DEFAULT_CWD = str(Path(__file__).resolve().parents[4])

# Display transcript cap: the frontend keeps at most this many rows per
# session; the oldest fall off. Independent of the model-visible context,
# which compaction shrinks on its own schedule.
_DISPLAY_CAP = 1000


def _normalize_cwd(cwd: str) -> str:
    """Remap stale/legacy session cwds to the working default so relative
    edit/grep/tool paths resolve against the repo root, not $HOME."""
    if not cwd or cwd in (os.path.expanduser("~"), str(Path.home())):
        return DEFAULT_CWD
    return cwd

def _new_session_id() -> str:
    # Compact id matching the mockup (`u7f2a1c` style).
    return "u" + uuid4().hex[:6]


@dataclass
class Message:
    role: str  # "user" | "assistant" | "tool" | "system"
    content: Any
    tool_call_id: str | None = None
    tool_name: str | None = None
    tool_calls: list = field(default_factory=list)  # OpenAI tool_calls array (assistant rows)
    reasoning: str = ""
    reasoning_signature: str = ""  # Anthropic thinking-block signature (multi-turn continuity)
    steps: list = field(default_factory=list)  # interleaved [{kind:text|tool,...}]
    ts: float = 0.0
    status: str = "done"  # "done" | "interrupted" (user stopped) | "failed" (turn crashed)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_call_id": self.tool_call_id,
            "tool_name": self.tool_name,
            "tool_calls": self.tool_calls,
            "reasoning": self.reasoning,
            "reasoning_signature": self.reasoning_signature,
            "steps": self.steps,
            "status": self.status,
            "ts": self.ts,
        }


@dataclass
class Session:
    id: str
    title: str
    cwd: str
    created_at: float = 0.0
    updated_at: float = 0.0
    messages: list[Message] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "message_count": len(self.messages),
        }


class SessionStore:
    """SQLite persistence. In-memory registry mirrors rows for hot access."""

    def __init__(self, data_home: Path) -> None:
        self._data_home = data_home
        self._db_path = data_home / "sessions.db"
        self._sessions: dict[str, Session] = {}
        self._display_cap = _DISPLAY_CAP
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        # WAL: readers (session.get) never block on the writer (turn commits).
        # Persistent once set, but cheap to re-assert per connection.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _init_db(self) -> None:
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_call_id TEXT,
                    tool_name TEXT,
                    ts REAL NOT NULL,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_events (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    ts REAL NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (session_id, seq),
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_session_events ON session_events(session_id, seq)"
            )
            # Display transcript: append-only, never rewritten by compaction.
            # `messages` is the model-visible context (rewritten every turn);
            # this is what the frontend renders, so a compacted turn keeps its
            # text and tool log. Capped at _DISPLAY_CAP, oldest first.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS display (
                    session_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    reasoning TEXT NOT NULL DEFAULT '',
                    steps TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'done',
                    ts REAL NOT NULL,
                    PRIMARY KEY (session_id, seq),
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_display_session ON display(session_id, seq)"
            )
            # migrate: reasoning column (added later) — tolerate existing DBs
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
            if "reasoning" not in cols:
                conn.execute("ALTER TABLE messages ADD COLUMN reasoning TEXT NOT NULL DEFAULT ''")
            if "steps" not in cols:
                conn.execute("ALTER TABLE messages ADD COLUMN steps TEXT NOT NULL DEFAULT '[]'")
            if "reasoning_signature" not in cols:
                conn.execute("ALTER TABLE messages ADD COLUMN reasoning_signature TEXT NOT NULL DEFAULT ''")
            if "status" not in cols:
                conn.execute("ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT 'done'")
            if "tool_calls" not in cols:
                conn.execute("ALTER TABLE messages ADD COLUMN tool_calls TEXT NOT NULL DEFAULT '[]'")
            self._backfill_display(conn)
        self._gc_storage()

    def _gc_storage(self) -> None:
        """One-shot reclaim of the legacy full-history event payloads.

        Old `messages.replaced` events stored the entire transcript on every
        turn, so the file grew O(n²) (339 MB for 3 MB of messages). Those rows
        are redundant now that `display` holds the durable transcript, so shrink
        each payload to metadata, drop orphans, and VACUUM once. Guarded by a
        marker row: reruns are a no-op."""
        with contextlib.closing(self._connect()) as conn, conn:
            done = conn.execute(
                "SELECT 1 FROM session_events WHERE session_id = '_meta' AND kind = 'storage.gc'"
            ).fetchone()
            if done:
                return
            for table in ("messages", "display", "session_events"):
                conn.execute(
                    f"DELETE FROM {table} WHERE session_id NOT IN (SELECT id FROM sessions)"
                    " AND session_id <> '_meta'"
                )
            fat = conn.execute(
                "SELECT session_id, seq, payload FROM session_events"
                " WHERE kind = 'messages.replaced' AND length(payload) > 200"
            ).fetchall()
            for r in fat:
                payload = json_loads(r["payload"])
                msgs = payload.get("messages") if isinstance(payload, dict) else None
                if not isinstance(msgs, list):
                    continue
                conn.execute(
                    "UPDATE session_events SET payload = ? WHERE session_id = ? AND seq = ?",
                    (json_dumps({"count": len(msgs),
                                 "roles": [m.get("role") for m in msgs if isinstance(m, dict)]}),
                     r["session_id"], r["seq"]),
                )
            conn.execute(
                "INSERT OR REPLACE INTO session_events (session_id, seq, ts, kind, payload)"
                " VALUES ('_meta', 1, ?, 'storage.gc', ?)",
                (time.time(), json_dumps({"shrunk": len(fat)})),
            )
        # VACUUM cannot run inside a transaction; isolation_level=None gives
        # autocommit. Best-effort: a locked file must not block startup.
        with contextlib.suppress(sqlite3.Error), contextlib.closing(
            sqlite3.connect(self._db_path, isolation_level=None)
        ) as conn:
            conn.execute("PRAGMA auto_vacuum=INCREMENTAL")
            conn.execute("VACUUM")

    def _backfill_display(self, conn: sqlite3.Connection) -> None:
        """One-shot migration: seed `display` for sessions that predate it.

        The richest surviving history is in `session_events`: `message.appended`
        for every row ever written, plus `messages.replaced` snapshots. Merging
        appended rows in order recovers turns that compaction later dropped from
        `messages`. Only touches sessions with no display rows yet, so a rerun
        is a no-op and a live session is never disturbed."""
        rows = conn.execute(
            "SELECT id FROM sessions WHERE id NOT IN (SELECT DISTINCT session_id FROM display)"
        ).fetchall()
        for row in rows:
            sid = row["id"]
            events = conn.execute(
                "SELECT kind, payload FROM session_events WHERE session_id = ? ORDER BY seq",
                (sid,),
            ).fetchall()
            seen: list[dict[str, Any]] = []
            keys: set[tuple] = set()

            def _take(m: dict[str, Any]) -> None:
                if m.get("role") not in ("user", "assistant", "system"):
                    return
                # (role, content) dedupes a row that appears in both an
                # `appended` event and a later snapshot; ts is unreliable
                # because replace_messages restamps ts==0 rows.
                key = (m.get("role"), json_dumps(m.get("content")))
                if key in keys:
                    return
                keys.add(key)
                seen.append(m)

            for ev in events:
                payload = json_loads(ev["payload"])
                if ev["kind"] == "message.appended" and isinstance(payload, dict):
                    _take(payload)
                elif ev["kind"] == "messages.replaced" and isinstance(payload, dict):
                    for m in payload.get("messages") or []:
                        if isinstance(m, dict):
                            _take(m)
            if not seen:
                # No event stream (legacy session): fall back to `messages`.
                seen = [
                    {"role": r["role"], "content": json_loads(r["content"]),
                     "reasoning": r["reasoning"] or "", "steps": json_loads(r["steps"]) or [],
                     "status": r["status"], "ts": r["ts"]}
                    for r in conn.execute(
                        "SELECT role, content, reasoning, steps, status, ts FROM messages"
                        " WHERE session_id = ? ORDER BY id", (sid,)).fetchall()
                    if r["role"] in ("user", "assistant", "system")
                ]
            if not seen:
                continue
            seen = seen[-_DISPLAY_CAP:]
            for seq, m in enumerate(seen, start=1):
                conn.execute(
                    "INSERT INTO display (session_id, seq, role, content, reasoning, steps, status, ts)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (sid, seq, m.get("role"), json_dumps(m.get("content")),
                     m.get("reasoning") or "", json_dumps(m.get("steps") or []),
                     m.get("status") or "done", float(m.get("ts") or 0.0)),
                )

    # ---- lifecycle ----

    def create(self, cwd: str | None = None) -> Session:
        import time

        now = time.time()
        session = Session(
            id=_new_session_id(),
            title="new session",
            cwd=str(cwd or DEFAULT_CWD),
            created_at=now,
            updated_at=now,
        )
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO sessions (id, title, cwd, created_at, updated_at) VALUES (?,?,?,?,?)",
                (session.id, session.title, session.cwd, session.created_at, session.updated_at),
            )
        self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session is not None:
            return session
        with contextlib.closing(self._connect()) as conn, conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        # Legacy sessions defaulted to $HOME before DEFAULT_CWD existed; that
        # cwd makes relative edit/grep resolve under the wrong root. Remap to
        # the repo root and persist so the stale value never resurfaces.
        cwd = _normalize_cwd(row["cwd"])
        if cwd != row["cwd"]:
            with contextlib.closing(self._connect()) as conn, conn:
                conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (cwd, session_id))
        session = Session(
            id=row["id"], title=row["title"], cwd=cwd,
            created_at=row["created_at"], updated_at=row["updated_at"],
        )
        # Model-visible context comes straight from `messages` — the table
        # `replace_messages` keeps authoritative. (It used to be replayed from
        # the event stream, which only worked while every rewrite stored a full
        # snapshot.) The frontend transcript is `display()`, not this list.
        for m in self._messages(session_id):
            session.messages.append(m)
        self._sessions[session_id] = session
        return session

    def list(self, limit: int | None = None) -> list[dict[str, Any]]:
        rows_sql = "SELECT * FROM sessions ORDER BY updated_at DESC"
        params: tuple = ()
        if limit is not None:
            rows_sql += " LIMIT ?"
            params = (limit,)
        with contextlib.closing(self._connect()) as conn, conn:
            rows = conn.execute(rows_sql, params).fetchall()
            out = []
            for row in rows:
                count = conn.execute(
                    "SELECT COUNT(*) AS c FROM messages WHERE session_id = ?", (row["id"],)
                ).fetchone()["c"]
                out.append(
                    {
                        "id": row["id"],
                        "title": row["title"],
                        "cwd": row["cwd"],
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "message_count": count,
                    }
                )
        return out

    def delete(self, session_id: str) -> None:
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM display WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM session_events WHERE session_id = ?", (session_id,))
            conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        self._sessions.pop(session_id, None)
        # Attached images are stored per session under data_home; without this
        # they outlive every session that ever carried one.
        shutil.rmtree(self._data_home / "attachments" / session_id, ignore_errors=True)

    def set_cwd(self, session_id: str, cwd: str) -> str:
        session = self.get(session_id)
        if session is None:
            raise KeyError(session_id)
        session.cwd = cwd
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute("UPDATE sessions SET cwd = ? WHERE id = ?", (cwd, session_id))
        return cwd

    def set_title(self, session_id: str, title: str) -> str:
        session = self.get(session_id)
        if session is None:
            raise KeyError(session_id)
        session.title = title
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))
        return title

    def touch(self, session_id: str) -> None:
        import time

        session = self._sessions.get(session_id)
        if session is not None:
            session.updated_at = time.time()
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (time.time(), session_id))

    # ---- messages ----

    def append(self, session_id: str, message: Message) -> None:
        import time

        if message.ts == 0.0:
            message.ts = time.time()
        session = self._sessions.get(session_id)
        if session is not None:
            session.messages.append(message)
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, tool_calls, reasoning, reasoning_signature, steps, status, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    message.role,
                    json_dumps(message.content),
                    message.tool_call_id,
                    message.tool_name,
                    json_dumps(message.tool_calls),
                    message.reasoning,
                    message.reasoning_signature,
                    json_dumps(message.steps),
                    message.status,
                    message.ts,
                ),
            )
        with contextlib.closing(self._connect()) as conn, conn:
            self._record_event(conn, session_id, "message.appended", message.to_dict())
        # Mirror into the append-only display transcript so the frontend keeps
        # this row even after compaction rewrites the model-visible context.
        self.add_display(session_id, [message])
        self.touch(session_id)

    # ---- display transcript (append-only) ----

    def add_display(self, session_id: str, messages: list[Message]) -> int:
        """Append rows to the display transcript and enforce the cap.

        Append-only by contract: compaction rewrites `messages` (the
        model-visible context) but must never remove a row the frontend has
        already shown. The only eviction is the _DISPLAY_CAP window, oldest
        first."""
        rows = [m for m in messages if m.role in ("user", "assistant", "system")]
        if not rows:
            return 0
        now = time.time()
        with contextlib.closing(self._connect()) as conn, conn:
            seq = int(conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM display WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0])
            for m in rows:
                seq += 1
                conn.execute(
                    "INSERT INTO display (session_id, seq, role, content, reasoning, steps, status, ts)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (session_id, seq, m.role, json_dumps(m.content), m.reasoning,
                     json_dumps(m.steps), m.status, m.ts or now),
                )
            # Cap: keep the newest _DISPLAY_CAP rows, drop the oldest.
            conn.execute(
                "DELETE FROM display WHERE session_id = ? AND seq <= ?",
                (session_id, seq - self._display_cap),
            )
        return len(rows)

    def display(self, session_id: str) -> list[dict[str, Any]]:
        """The frontend transcript: every row ever shown, within the cap."""
        with contextlib.closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT role, content, reasoning, steps, status, ts FROM display"
                " WHERE session_id = ? ORDER BY seq", (session_id,)
            ).fetchall()
        return [
            {"role": r["role"], "content": json_loads(r["content"]),
             "tool_call_id": None, "tool_name": None, "tool_calls": [],
             "reasoning": r["reasoning"] or "", "reasoning_signature": "",
             "steps": json_loads(r["steps"]) or [], "status": r["status"],
             "ts": r["ts"]}
            for r in rows
        ]

    def replace_messages(self, session_id: str, messages: list[Message]) -> None:
        """Rewrite a session's whole history (used by manual context compression)."""
        import time

        now = time.time()
        session = self._sessions.get(session_id)
        if session is not None:
            session.messages = list(messages)
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            for m in messages:
                if m.ts == 0.0:
                    m.ts = now
                conn.execute(
                    "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name, tool_calls, reasoning, reasoning_signature, steps, ts)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        m.role,
                        json_dumps(m.content),
                        m.tool_call_id,
                        m.tool_name,
                        json_dumps(m.tool_calls),
                        m.reasoning,
                        m.reasoning_signature,
                        json_dumps(m.steps),
                        m.ts,
                    ),
                )
        with contextlib.closing(self._connect()) as conn, conn:
            # Metadata only. This used to store every message on every rewrite —
            # once per turn — which grew the event log to O(n²) of the
            # transcript (339 MB for 3 MB of messages). The display table is now
            # the durable transcript, so the snapshot is redundant.
            self._record_event(conn, session_id, "messages.replaced", {
                "count": len(messages),
                "roles": [m.role for m in messages],
            })
        self.touch(session_id)

    @staticmethod
    def _record_event(conn: sqlite3.Connection, session_id: str, kind: str, payload: Any) -> None:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM session_events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        conn.execute(
            "INSERT INTO session_events (session_id, seq, ts, kind, payload) VALUES (?,?,?,?,?)",
            (session_id, row["next_seq"], time.time(), kind, json_dumps(payload)),
        )

    def events(self, session_id: str, *, after: int = 0) -> list[dict[str, Any]]:
        """Return the append-only audit stream for a session."""
        with contextlib.closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT seq, ts, kind, payload FROM session_events "
                "WHERE session_id = ? AND seq > ? ORDER BY seq", (session_id, after)
            ).fetchall()
        return [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"],
                 "payload": json_loads(r["payload"])} for r in rows]

    # (replay_messages removed: `messages.replaced` no longer carries a message
    # snapshot, so the event stream can't reconstruct history. `display()` is
    # the durable transcript and `_messages()` the model context.)


    def _messages(self, session_id: str) -> list[Message]:
        with contextlib.closing(self._connect()) as conn, conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,)
            ).fetchall()
        return [
            Message(
                role=row["role"],
                content=json_loads(row["content"]),
                tool_call_id=row["tool_call_id"],
                tool_name=row["tool_name"],
                tool_calls=json_loads(row["tool_calls"]) or [],
                reasoning=row["reasoning"] or "",
                reasoning_signature=row["reasoning_signature"] or "",
                steps=json_loads(row["steps"]) or [],
                status=row["status"],
                ts=row["ts"],
            )
            for row in rows
        ]

    def prune(self, keep: int = 30) -> int:
        """GC: cap total sessions at ``keep`` (newest by activity) — no age-based pruning."""
        with contextlib.closing(self._connect()) as conn, conn:
            conn.execute(
                "DELETE FROM sessions WHERE id NOT IN"
                " (SELECT id FROM sessions ORDER BY updated_at DESC LIMIT ?)",
                (keep,),
            )
            # Every child table, or an orphan keeps its rows forever: this is
            # how session_events grew to hundreds of MB of dead payloads.
            for table in ("messages", "display", "session_events"):
                conn.execute(
                    f"DELETE FROM {table} WHERE session_id NOT IN (SELECT id FROM sessions)"
                    " AND session_id <> '_meta'"  # storage.gc marker is not a session
                )
            return conn.total_changes


def json_dumps(content: Any) -> str:
    import json

    return json.dumps(content, ensure_ascii=False)


def json_loads(raw: str) -> Any:
    import json

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw
