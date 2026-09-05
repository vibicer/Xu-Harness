#!/usr/bin/env python3
"""Runnable check for the append-only display transcript.

    cd brain && python tests/check_display_transcript.py

Asserts the four rules the frontend transcript must obey:
  1. a shown response survives repeated compaction
  2. tool logs (`steps`) survive with it
  3. the cap drops the OLDEST rows only
  4. delete/prune are the only other way rows leave

Uses a throwaway DB in a temp dir; never touches ~/.xu.
"""
from __future__ import annotations

import contextlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xu_brain.features.session import Message, SessionStore  # noqa: E402


def check(name: str, cond: bool) -> bool:
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    return cond


def main() -> int:
    ok = True
    with tempfile.TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp))

        print("1. survives 10 compactions")
        s = store.create()
        store.append(s.id, Message(role="user", content="the first question"))
        store.append(s.id, Message(
            role="assistant", content="the first answer",
            steps=[{"kind": "tool", "name": "read", "status": "ok"}]))
        for i in range(10):
            store.append(s.id, Message(role="user", content=f"q{i}"))
            store.append(s.id, Message(role="assistant", content=f"a{i}"))
            # what compaction does: rewrite the model context down to a summary
            store.replace_messages(s.id, [Message(role="system", content=f"summary {i}")])
        shown = store.display(s.id)
        texts = [m["content"] for m in shown]
        ok &= check("first question still shown", texts[0] == "the first question")
        ok &= check("first answer still shown", texts[1] == "the first answer")
        ok &= check("newest turn shown", "a9" in texts)
        ok &= check("all 22 rows kept", len(shown) == 22)
        ok &= check("tool log survived", shown[1]["steps"][0]["name"] == "read")
        ok &= check("model context did shrink",
                    len(store.get(s.id).messages) == 1)

        print("2. cap drops oldest first")
        store._display_cap = 5
        c = store.create()
        for i in range(8):
            store.append(c.id, Message(role="user", content=f"m{i}"))
        kept = [m["content"] for m in store.display(c.id)]
        ok &= check(f"kept newest 5, got {kept}", kept == ["m3", "m4", "m5", "m6", "m7"])
        store._display_cap = 1000

        print("3. delete clears the transcript")
        d = store.create()
        store.append(d.id, Message(role="user", content="x"))
        store.delete(d.id)
        ok &= check("display empty after delete", store.display(d.id) == [])

        print("4. prune clears orphaned rows")
        p = store.create()
        store.append(p.id, Message(role="user", content="y"))
        store._sessions.clear()
        with contextlib.closing(store._connect()) as conn, conn:
            conn.execute("DELETE FROM sessions WHERE id = ?", (p.id,))
        store.prune(keep=30)
        with contextlib.closing(store._connect()) as conn, conn:
            for table in ("messages", "display", "session_events"):
                n = conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE session_id NOT IN"
                    " (SELECT id FROM sessions) AND session_id <> '_meta'"
                ).fetchone()[0]
                ok &= check(f"no orphan rows in {table}", n == 0)

        print("5. event log stays small (no full-history snapshots)")
        with contextlib.closing(store._connect()) as conn, conn:
            biggest = conn.execute(
                "SELECT COALESCE(MAX(LENGTH(payload)), 0) FROM session_events"
                " WHERE kind = 'messages.replaced'"
            ).fetchone()[0]
        ok &= check(f"replaced-event payload is metadata ({biggest}B)", biggest < 500)

    print("\nPASS" if ok else "\nFAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
