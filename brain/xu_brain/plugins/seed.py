"""Seed shipped plugin packages into ``data_home/plugins``.

The repo ships plugin packages under ``xu_brain/plugins/builtin/<name>/``. The
plugin host only ever scans one root (``data_home/plugins``) — that single root
is what makes ``ui_asset`` path containment, ``plugins.enabled.json`` and
``enable()``'s re-load path unambiguous — so shipped packages are *copied* in
rather than scanned from a second location.

Mirrors :meth:`xu_brain.features.skills.SkillsEngine._seed_builtin`: runs on
every boot, not just a fresh one, so a package added by a later version reaches
an install that already has a plugins dir.

Refresh policy: an installed copy is left alone unless it declares a
``manifest.version`` that differs from the shipped one, in which case the
shipped files are written *over* the top. A copy whose manifest is missing,
unreadable, or carries no ``version`` is treated as the user's own and is never
touched. Nothing is ever deleted, so a file the user added next to ours
survives — but our own files are ours, and editing them in place is not
supported. Copy the package under a new name to customize it.
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
            # An existing copy is the user's unless it is provably one of ours
            # and provably older. A manifest without a `version` (it is optional
            # in the schema) or an unreadable/absent one is not evidence of a
            # stale built-in, so never write over it — say so instead, because
            # the user cannot otherwise tell why the built-in stopped updating.
            shipped = _version(pkg / "manifest.json")
            installed = _version(dest / "manifest.json")
            if shipped is None or installed is None:
                log.warning(
                    "not seeding built-in plugin %s: the installed copy declares "
                    "no usable version (installed=%s shipped=%s) — treating it as "
                    "the user's own",
                    pkg.name, installed or "<none>", shipped or "<none>",
                )
                continue
            if shipped == installed:
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
