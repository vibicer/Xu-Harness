"""Seed shipped plugin packages into ``data_home/plugins``.

The repo ships plugin packages under ``xu_brain/plugins/builtin/<name>/``. The
plugin host only ever scans one root (``data_home/plugins``) — that single root
is what makes ``ui_asset`` path containment, ``plugins.enabled.json`` and
``enable()``'s re-load path unambiguous — so shipped packages are *copied* in
rather than scanned from a second location.

Mirrors :meth:`xu_brain.features.skills.SkillsEngine._seed_builtin`: runs on
every boot, not just a fresh one, so a package added by a later version reaches
an install that already has a plugins dir.

Refresh policy: an installed copy is left alone unless the shipped
``manifest.version`` differs from the installed one, in which case the shipped
files are written *over* the top. Nothing is ever deleted, so a file the user
added next to ours survives — but our own files are ours, and editing them in
place is not supported. Copy the package under a new name to customize it.
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = ["BUILTIN_PLUGIN_DIR", "seed_builtin_plugins"]

BUILTIN_PLUGIN_DIR = Path(__file__).parent / "builtin"


def _version(manifest: Path) -> str | None:
    """``manifest.version`` as a string, or None when unreadable."""
    try:
        data = json.loads(manifest.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    version = data.get("version") if isinstance(data, dict) else None
    return str(version) if version is not None else None


def seed_builtin_plugins(data_home: Path, source: Path | None = None) -> list[str]:
    """Copy shipped packages missing from (or outdated in) ``data_home/plugins``.

    Returns the package names written, so a caller can log the delta. Fail-soft
    per package: an unreadable or unwritable one is logged and skipped, because
    a bad seed must not stop the brain from booting.
    """
    root = Path(source) if source is not None else BUILTIN_PLUGIN_DIR
    if not root.is_dir():
        return []
    dest_root = Path(data_home) / "plugins"
    written: list[str] = []
    for pkg in sorted(p for p in root.iterdir() if p.is_dir()):
        if not (pkg / "manifest.json").is_file():
            continue
        dest = dest_root / pkg.name
        if dest.exists():
            shipped = _version(pkg / "manifest.json")
            if shipped is None or shipped == _version(dest / "manifest.json"):
                continue
        try:
            dest_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(
                pkg, dest, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        except OSError as exc:
            log.warning("could not seed built-in plugin %s: %s", pkg.name, exc)
            continue
        written.append(pkg.name)
    if written:
        log.info("seeded built-in plugins: %s", ", ".join(written))
    return written
