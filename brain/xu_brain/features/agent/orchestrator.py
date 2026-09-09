"""Orchestration layer — manageable subagents (the preset/orchestrator
feature's runtime engine).

This module keeps the long-running/parallel primitive the base tools never
reached:

- **Subagents** — a spawned child turn keeps a durable id so the parent can
  ``subagent_list``/``subagent_message``/``subagent_interrupt`` instead of
  fire-and-forget ``task``.

Every record is stored in a JSON file under ``data_home`` so a brain restart
retains in-flight subagent bookkeeping where it survives.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def _sid() -> str:
    return "s" + uuid.uuid4().hex[:10]


@dataclass
class Subagent:
    """A managed child turn.

    The child itself runs through the same agent loop on an ephemeral session;
    this record keeps the durable id, the parent link, its result, and enough
    state to answer ``subagent_list``/``subagent_message``.
    """
    id: str
    parent_session: str | None
    prompt: str
    status: str = "running"  # running | done | error | interrupted
    created: float = 0.0
    finished: float | None = None
    result: str = ""
    error: str | None = None
    session_id: str | None = None  # the ephemeral child session id
    label: str | None = None       # free-form display label (default agent delegates)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class Orchestrator:
    """Holds subagents; the server and tools share one."""

    def __init__(self, data_home: Path) -> None:
        self.data_home = Path(data_home)
        self.file = self.data_home / "orchestrator.json"
        self._subagents: dict[str, Subagent] = {}
        self._load()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #

    def _load(self) -> None:
        if not self.file.exists():
            return
        try:
            raw = json.loads(self.file.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for s in raw.get("subagents", []):
            try:
                rec = Subagent(**s)
            except (TypeError, KeyError):
                continue  # schema drift from an older build: skip, never crash boot
            if rec.status == "running":
                # Child turns live in memory; a brain restart killed them.
                rec.status = "interrupted"
                rec.finished = time.time()
            self._subagents[rec.id] = rec

    def _save(self) -> None:
        self.data_home.mkdir(parents=True, exist_ok=True)
        tmp = self.file.with_suffix(".json.tmp")
        payload = {
            "subagents": [s.to_dict() for s in self._subagents.values()],
        }
        tmp.write_text(json.dumps(payload, indent=2), "utf-8")
        tmp.replace(self.file)

    # ------------------------------------------------------------------ #
    # subagents
    # ------------------------------------------------------------------ #

    def register_subagent(self, parent_session: str | None, prompt: str, session_id: str | None,
                          label: str | None = None) -> Subagent:
        sub = Subagent(id=_sid(), parent_session=parent_session, prompt=prompt,
                       session_id=session_id, created=time.time(), label=label)
        self._subagents[sub.id] = sub
        self._save()
        return sub

    def get_subagent(self, sub_id: str) -> Subagent | None:
        return self._subagents.get(sub_id)

    def list_subagents(self, parent_session: str | None = None) -> list[dict[str, Any]]:
        """List subagents, optionally scoped to one parent session.

        ``parent_session`` is the isolation boundary: a caller passes its own
        session id to see only the subagents it spawned. Callers that omit it
        (e.g. an admin surface) get the full list.
        """
        subs = self._subagents.values()
        if parent_session is not None:
            subs = [s for s in subs if s.parent_session == parent_session]
        return [s.to_dict() for s in sorted(subs, key=lambda s: s.created, reverse=True)]

    def owned_subagent(self, sub_id: str, parent_session: str | None) -> Subagent | None:
        """Return the subagent, scoped to its owner when one is asserted.

        Session isolation: model-facing tools pass their own session id and see
        only subagents they spawned. ``parent_session=None`` is the admin/global
        path (the human subagent panel), which is unrestricted.
        """
        sub = self._subagents.get(sub_id)
        if sub is None:
            return None
        if parent_session is not None and sub.parent_session != parent_session:
            return None
        return sub
    def finish_subagent(self, sub_id: str, result: str | None = None, *, error: str | None = None,
                        interrupted: bool = False) -> None:
        sub = self._subagents.get(sub_id)
        if sub is None or sub.status != "running":
            return
        if error is not None:
            sub.status = "error"
            sub.error = error
        elif interrupted:
            sub.status = "interrupted"
        else:
            sub.status = "done"
            sub.result = result or ""
        sub.finished = time.time()
        self._save()


# Shared module-level orchestrator (the server injects the real one).
orchestrator: Orchestrator | None = None
