"""filebrowser — cwd-scoped filesystem RPCs for the shell's FILES explorer.

Registers four methods on the ``rpc`` slot:

    fs.tree    {session_id, path?}            -> {path, rel, entries[], parent}
    fs.read    {session_id, path}             -> {path, content, tag, size}
    fs.write   {session_id, path, content, tag?} -> {path, tag, created}
    fs.remove  {session_id, path}             -> {path, removed}

Security posture (the brain WebSocket is unauthenticated, so this is the only
thing standing between a stray caller and the disk):

- **Every** path is resolved and then required to sit inside the session's
  ``cwd``. Absolute paths, ``..`` traversal and symlinks that escape are all
  rejected — ``Path.resolve()`` collapses the first two and resolves the third,
  and the containment check runs on the resolved result.
- Writes and reads refuse anything over ``max_bytes`` (setting, 1 MiB default),
  and reads refuse non-UTF-8 content instead of returning mojibake.
- ``fs.write`` takes the ``tag`` the client last read. A mismatch means the file
  changed underneath (usually the agent writing the same file) and the write is
  refused rather than clobbering it. Same ``_snapshot_tag`` the ``edit`` tool
  anchors on.
- ``fs.remove`` handles files and empty directories only. Recursive delete is
  deliberately absent: the blast radius is not worth a button.

Reuses ``xu_brain.features.tools.file`` helpers (``_resolve``, ``_is_text``,
``_snapshot_tag``) so the shell and the agent's tools agree on path resolution
and content tagging.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

# Entries above this are truncated — a node_modules listing must not wedge the UI.
MAX_ENTRIES = 2000
DEFAULT_MAX_BYTES = 1024 * 1024


def activate(ctx):
    from xu_brain.core.contract import RpcError
    from xu_brain.features.session import DEFAULT_CWD
    from xu_brain.features.tools.file import _is_text, _resolve, _snapshot_tag

    app = ctx.app  # the live App: sessions + config

    def _max_bytes() -> int:
        raw = ctx.settings().get("max_bytes", DEFAULT_MAX_BYTES)
        try:
            return max(1024, int(raw))
        except (TypeError, ValueError):
            return DEFAULT_MAX_BYTES

    def _root(params: dict[str, Any]) -> Path:
        """The session cwd — the jail every path is checked against."""
        sid = params.get("session_id")
        session = app.sessions.get(sid) if sid else None
        return Path(session.cwd if session else DEFAULT_CWD).expanduser().resolve()

    def _target(params: dict[str, Any], *, required: bool = True) -> tuple[Path, Path]:
        """Resolve ``params['path']`` under the session cwd. Returns (root, target).

        Raises RpcError(-32602) when the result escapes the root, so traversal
        and absolute paths fail loudly instead of reading somewhere surprising.
        """
        root = _root(params)
        raw = str(params.get("path") or "")
        target = _resolve(str(root), raw)
        try:
            target = target.resolve()
        except OSError as exc:
            raise RpcError(-32602, f"cannot resolve path: {exc}") from exc
        if target != root and root not in target.parents:
            raise RpcError(-32602, "path escapes the session cwd")
        if required and not target.exists():
            rel = target.relative_to(root) if target != root else "."
            raise RpcError(-32602, f"not found: {rel}")
        return root, target

    def _rel(root: Path, target: Path) -> str:
        return "" if target == root else str(target.relative_to(root))

    async def fs_tree(params: dict[str, Any]) -> dict[str, Any]:
        """One directory level: dirs first, then files, each with size."""
        root, target = _target(params)
        if not target.is_dir():
            raise RpcError(-32602, "not a directory")
        entries: list[dict[str, Any]] = []
        try:
            for entry in sorted(
                target.iterdir(),
                key=lambda e: (not e.is_dir(), e.name.casefold()),
            )[:MAX_ENTRIES]:
                try:
                    is_dir = entry.is_dir()
                    size = None if is_dir else entry.stat().st_size
                except OSError:
                    continue  # vanished or unreadable mid-listing
                entries.append({"name": entry.name, "dir": is_dir, "size": size})
        except OSError as exc:
            raise RpcError(-32603, f"cannot list directory: {exc}") from exc
        rel = _rel(root, target)
        return {
            "path": str(target),
            "rel": rel,
            "root": str(root),
            "entries": entries,
            # None at the jail boundary, so the UI can disable "up".
            "parent": None if target == root else _rel(root, target.parent),
        }

    async def fs_read(params: dict[str, Any]) -> dict[str, Any]:
        root, target = _target(params)
        if target.is_dir():
            raise RpcError(-32602, "path is a directory")
        try:
            size = target.stat().st_size
        except OSError as exc:
            raise RpcError(-32603, f"cannot stat file: {exc}") from exc
        cap = _max_bytes()
        if size > cap:
            raise RpcError(-32602, f"file is {size} bytes; limit is {cap}")
        try:
            data = target.read_bytes()
        except OSError as exc:
            raise RpcError(-32603, f"cannot read file: {exc}") from exc
        if not _is_text(data):
            raise RpcError(-32602, f"binary file ({len(data)} bytes) — not editable here")
        content = data.decode("utf-8")
        return {
            "path": str(target),
            "rel": _rel(root, target),
            "content": content,
            "tag": _snapshot_tag(content),
            "size": size,
        }

    async def fs_write(params: dict[str, Any]) -> dict[str, Any]:
        """Write (or create) a file. ``tag`` guards against concurrent writes."""
        root, target = _target(params, required=False)
        if target.is_dir():
            raise RpcError(-32602, "path is a directory")
        content = params.get("content")
        if not isinstance(content, str):
            raise RpcError(-32602, "content must be a string")
        cap = _max_bytes()
        encoded = content.encode("utf-8")
        if len(encoded) > cap:
            raise RpcError(-32602, f"content is {len(encoded)} bytes; limit is {cap}")

        created = not target.exists()
        expected = params.get("tag")
        if not created and expected:
            try:
                current = target.read_text("utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise RpcError(-32603, f"cannot verify file before write: {exc}") from exc
            if _snapshot_tag(current) != str(expected):
                raise RpcError(
                    -32602,
                    "file changed on disk since it was read — reload before saving",
                )
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise RpcError(-32603, f"cannot write file: {exc}") from exc
        ctx.log(f"{'created' if created else 'wrote'} {_rel(root, target)}")
        return {
            "path": str(target),
            "rel": _rel(root, target),
            "tag": _snapshot_tag(content),
            "created": created,
        }

    async def fs_remove(params: dict[str, Any]) -> dict[str, Any]:
        """Delete one file or one empty directory. Never recursive."""
        root, target = _target(params)
        if target == root:
            raise RpcError(-32602, "refusing to remove the session cwd")
        rel = _rel(root, target)
        try:
            if target.is_dir():
                target.rmdir()  # raises OSError when not empty — that is the guard
            else:
                target.unlink()
        except OSError as exc:
            raise RpcError(-32603, f"cannot remove: {exc}") from exc
        ctx.log(f"removed {rel}", level="warning")
        return {"path": str(target), "rel": rel, "removed": True}

    ctx.register_rpc("fs.tree", fs_tree)
    ctx.register_rpc("fs.read", fs_read)
    ctx.register_rpc("fs.write", fs_write)
    ctx.register_rpc("fs.remove", fs_remove)
    return None
