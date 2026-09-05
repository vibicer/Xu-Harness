"""The contract doc is the source of truth, so it must not drift from the code.

`contract/methods.md` claims to be authoritative (see its Versioning section),
but nothing enforced that: 17 registered methods had silently gone undocumented.
This check makes the claim true — add an RPC without a row and it fails here.
Registration is discovered across the core RPC modules, not just runtime.py.

Run: python -m pytest tests/test_contract_doc.py
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "contract" / "methods.md"
RUNTIME = REPO / "brain" / "xu_brain" / "core" / "runtime.py"

# Discover concern-specific registrars automatically; runtime.py retains its
# own registrations for compatibility with the current and future layouts.
def _registration_files() -> list[Path]:
    root = REPO / "brain" / "xu_brain"
    return sorted(root.glob("**/rpc.py")) + [RUNTIME]


# A dotted name in the first cell of a markdown table row.
_ROW = re.compile(r"^\s*\| `([a-z_]+(?:\.[a-z_]+)+)`", re.M)
_REGISTER = re.compile(r'app\.register\("([^"]+)"')


def _documented() -> list[str]:
    return _ROW.findall(DOC.read_text("utf-8"))


def _plugin_provided() -> set[str]:
    """Rows under `### Plugin-provided methods`.

    Derived from the doc rather than hardcoded: these are served by whatever
    plugin fills the `rpc` slot, so they are never in this repo's registrars,
    and every new plugin RPC would otherwise have to be added to a list here.
    """
    doc = DOC.read_text("utf-8")
    start = doc.index("### Plugin-provided methods")
    end = doc.index("## Server → client events", start)
    return set(_ROW.findall(doc[start:end]))


def _registered() -> list[tuple[str, Path]]:
    return [
        (method, path)
        for path in _registration_files()
        for method in _REGISTER.findall(path.read_text("utf-8"))
    ]


def _registered_names() -> set[str]:
    return {method for method, _ in _registered()}


def test_rpc_registration_scan_is_nonempty_and_complete_enough() -> None:
    files = _registration_files()
    registered = _registered()
    # The floor prevents a broken/empty glob from making the reverse drift
    # check vacuously green; it allows legitimate new RPCs without exactness.
    assert files, "RPC registration scan found no files"
    assert len(registered) >= 60, (
        f"RPC registration scan found only {len(registered)} methods in "
        f"{[str(path) for path in files]}; expected at least 60"
    )


def test_every_registered_method_is_documented() -> None:
    registered = _registered()
    missing = sorted({method for method, _ in registered} - set(_documented()))
    locations = {
        method: str(path) for method, path in registered if method in missing
    }
    assert not missing, (
        "RPC method(s) registered with no row in contract/methods.md: "
        f"{[f'{method} ({locations[method]})' for method in missing]}"
    )


def test_doc_has_no_duplicate_rows() -> None:
    """Duplicates are checked per section: `subagent.activity` is legitimately
    both an RPC (fetch a run) and an event (a run changed)."""
    doc = DOC.read_text("utf-8")
    events_at = doc.index("## Server → client events")
    for label, chunk in (("methods", doc[:events_at]), ("events", doc[events_at:])):
        rows = _ROW.findall(chunk)
        dupes = sorted({r for r in rows if rows.count(r) > 1})
        assert not dupes, f"duplicated {label} rows in contract/methods.md: {dupes}"


def test_documented_core_methods_still_exist() -> None:
    """A row for a Core method that no longer exists is just as much drift.

    Events and plugin-provided methods are excluded: events are never dispatched
    as RPCs, and plugin methods live outside this repo by design.
    """
    plugin_provided = _plugin_provided()
    events = {
        "turn.started", "turn.delta", "turn.reasoning", "turn.tool", "turn.notice",
        "turn.queue", "turn.dequeue", "queue.cancelled", "turn.approval",
        "turn.approval_resolved", "turn.finished", "turn.failed",
        "context.updated", "state.updated", "memory.updated", "status.updated",
        "session.updated", "compaction.started", "compaction.done",
        "subagent.activity", "subagent.delta", "todo.updated",
    }
    stale = sorted(set(_documented()) - _registered_names() - plugin_provided - events)
    files = ", ".join(str(path) for path in _registration_files())
    assert not stale, (
        "contract/methods.md documents method(s) not registered in scanned RPC "
        f"files ({files}): {stale}"
    )
