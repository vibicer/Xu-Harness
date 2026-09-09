"""Manifest schema + validation.

An plugin is a package: a ``manifest.json`` describing what it provides /
requires / configures, and an ``activate(ctx)`` callable. The manifest is the
declarative contract; :func:`validate_manifest` enforces its shape so the host
can reject a malformed plugin **before** executing any of its code.

The schema is deliberately permissive about the ``provides`` slot names — the
fixed slot set is owned by the Core, but the host checks membership and reports
unknown slots as warnings, not hard failures (an plugin that adds a slot name
the Core doesn't yet understand must not brick boot).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The fixed slot set the Core understands. Everything in
# ``provides`` must be one of these (or a forward-compatible unknown, warn-only).
KNOWN_SLOTS: frozenset[str] = frozenset({
    "provider", "tool", "rpc", "hook", "memory",
    "layout", "panel", "policy",
    # data-driven slots — already first-class in the product
    "skill", "persona", "theme",
})

_SETTING_TYPES = {"string", "boolean", "integer", "number", "list"}

# A theme's ``colors`` key becomes a CSS custom property (``--<key>``) written on
# the document root, so it must be a plain custom-property name. A value must be
# a 6- or 3-digit hex colour: the shell writes these straight into an inline
# style, and anything looser (``url(...)``, ``expression``) would be an injection
# point. A theme needing non-colour values ships a ``css`` file instead.
_VAR_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_HEX = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


@dataclass
class Manifest:
    """Validated manifest record. ``raw`` keeps the original dict verbatim."""

    name: str
    version: str = "0.0.0"
    description: str = ""
    provides: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    settings: list[dict[str, Any]] = field(default_factory=list)
    # Colour schemes this plugin contributes (the ``theme`` slot). Each entry is
    # ``{id, name, layout?, colors{}, css?}`` — see :func:`validate_manifest`.
    # A theme plugin needs no ``activate.py``: it is pure data, so the shell can
    # offer it in Config → Themes with nothing executing.
    themes: list[dict[str, Any]] = field(default_factory=list)
    # Frontend contribution (the ``panel`` slot): a plain-JS, single-file
    # module ``{module, element, mount, label?, icon?, assets?}`` defining a
    # custom element that the shell mounts at a named spot, plus optional
    # extra servable files. Empty = no UI.
    ui: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


# Mounts a plugin's custom element may land on. The set is OPEN: any
# well-formed lowercase-dashed name validates, because a layout plugin may
# offer spots the built-in shell doesn't know — validation checks the shape,
# and the *host* warns when no built-in layout offers the name. So this set
# is documentation plus that warning's source, NOT a validation gate.
#
# ``statusbar`` is a chip slot *inside* a built-in layout. ``config`` is a pane
# in Config, reached by a tab the shell renders from ``ui.label`` / ``ui.icon``.
# ``layout`` is the whole shell: a plugin mounting there replaces the layout
# entirely and owns everything under the app root, so at most one can be
# showing — the shell selects it via ``brain.layout === "plugin:<name>"``.
BUILTIN_UI_MOUNTS: frozenset[str] = frozenset({"statusbar", "config", "layout", "view"})

# A config tab is a word or two in a narrow strip; longer would just truncate.
_MAX_UI_LABEL = 24
_ICON_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _theme_errors(themes: Any) -> list[str]:
    """Validate ``manifest.themes``: a list of colour schemes.

    Each entry is ``{id, name, layout?, colors{}, css?}``. ``colors`` maps a
    palette key to a hex colour and is the whole scheme for most themes;
    ``css`` names an extra stylesheet in the plugin dir for what custom
    properties can't express (fonts, radii, textures).

    Strict on purpose: these values are written into a live ``style`` attribute
    and a ``<link>`` href, so a loose key or value would be an injection point.
    """
    errors: list[str] = []
    if not isinstance(themes, list):
        return ["manifest.themes must be a list"]
    seen: set[str] = set()
    for i, t in enumerate(themes):
        at = f"manifest.themes[{i}]"
        if not isinstance(t, dict):
            errors.append(f"{at} must be an object")
            continue
        tid = t.get("id")
        if not isinstance(tid, str) or not _VAR_NAME.match(tid):
            # The id becomes part of a DOM id and a localStorage key, so keep it
            # to the same shape as a palette key rather than free text.
            errors.append(f"{at}.id is required and must be lowercase-dashed")
        elif tid in seen:
            errors.append(f"{at}.id {tid!r} is duplicated in this manifest")
        else:
            seen.add(tid)
        if not isinstance(t.get("name"), str) or not str(t.get("name")).strip():
            errors.append(f"{at}.name is required")
        layout = t.get("layout")
        if layout is not None and (not isinstance(layout, str) or not layout.strip()):
            errors.append(f"{at}.layout must be a layout id when present")
        colors = t.get("colors", {})
        if not isinstance(colors, dict):
            errors.append(f"{at}.colors must be an object")
        else:
            for key, value in colors.items():
                if not isinstance(key, str) or not _VAR_NAME.match(key):
                    errors.append(f"{at}.colors key {key!r} must be lowercase-dashed")
                elif not isinstance(value, str) or not _HEX.match(value):
                    errors.append(f"{at}.colors[{key!r}] must be a hex colour")
        css = t.get("css")
        if css is not None:
            # Served straight off disk like ``ui.module``, so the same rule: a
            # bare filename, no traversal, no absolute paths.
            if not isinstance(css, str) or not css.strip():
                errors.append(f"{at}.css must be a filename when present")
            elif "/" in css or "\\" in css or ".." in css:
                errors.append(f"{at}.css must be a bare filename in the plugin dir")
            elif not css.endswith(".css"):
                errors.append(f"{at}.css must end in .css")
    return errors


def validate_manifest(data: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems; empty means valid."""
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["manifest must be a JSON object"]

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("manifest.name is required and must be a non-empty string")
    elif not name.strip().replace("-", "_").replace(".", "_").isidentifier():
        errors.append(f"manifest.name {name!r} is not a safe package name")

    version = data.get("version")
    if version is not None and not isinstance(version, str):
        errors.append("manifest.version must be a string")

    description = data.get("description")
    if description is not None and not isinstance(description, str):
        errors.append("manifest.description must be a string")

    for key in ("provides", "requires"):
        val = data.get(key, [])
        if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
            errors.append(f"manifest.{key} must be a list of strings")

    settings = data.get("settings", [])
    if not isinstance(settings, list):
        errors.append("manifest.settings must be a list")
    else:
        for i, s in enumerate(settings):
            if not isinstance(s, dict):
                errors.append(f"manifest.settings[{i}] must be an object")
                continue
            if not isinstance(s.get("key"), str) or not s["key"]:
                errors.append(f"manifest.settings[{i}].key is required")
            t = s.get("type")
            if t is not None and t not in _SETTING_TYPES:
                errors.append(
                    f"manifest.settings[{i}].type {t!r} is not one of {sorted(_SETTING_TYPES)}"
                )

    errors.extend(_theme_errors(data.get("themes", [])))

    ui = data.get("ui")
    if ui is not None:
        if not isinstance(ui, dict):
            errors.append("manifest.ui must be an object")
        else:
            for key in ("module", "element"):
                if not isinstance(ui.get(key), str) or not str(ui.get(key)).strip():
                    errors.append(f"manifest.ui.{key} is required when ui is present")
            module = str(ui.get("module", ""))
            # The module is served straight off disk from the plugin dir, so it
            # must be a plain relative filename — no traversal, no absolutes.
            if "/" in module or "\\" in module or ".." in module:
                errors.append("manifest.ui.module must be a bare filename in the plugin dir")
            element = str(ui.get("element", ""))
            # Custom elements are required by spec to contain a dash.
            if element and "-" not in element:
                errors.append(f"manifest.ui.element {element!r} must contain a dash")
            mount = ui.get("mount", "statusbar")
            # Mounts are open (see BUILTIN_UI_MOUNTS): a layout plugin may
            # offer spots the built-in shell doesn't know, so membership is
            # NOT checked here — only the shape, since a malformed name can
            # never match a slot any layout offers. The host warns about the
            # unfamiliar-but-legal ones.
            if not (isinstance(mount, str) and _ICON_NAME.match(mount)):
                errors.append("manifest.ui.mount must be a lowercase-dashed mount name")
            # A `config` pane needs a tab to open it, and the shell renders that
            # tab from data — so the label/icon live here beside the mount.
            label = ui.get("label")
            if label is not None and (not isinstance(label, str) or not label.strip()):
                errors.append("manifest.ui.label must be a non-empty string")
            elif isinstance(label, str) and len(label) > _MAX_UI_LABEL:
                errors.append(f"manifest.ui.label must be at most {_MAX_UI_LABEL} chars")
            icon = ui.get("icon")
            # The shell looks the name up in its own icon set and falls back
            # when it misses; a plugin cannot ship an SVG, which would be an
            # injection point in a slot the shell renders itself.
            if icon is not None and not (isinstance(icon, str) and _ICON_NAME.match(icon)):
                errors.append("manifest.ui.icon must be a lowercase-dashed icon name")
            assets = ui.get("assets")
            if assets is not None:
                # Extra files the frontend may fetch beyond ``ui.module`` (a
                # data file, a worker, a chart library...). Served straight
                # off disk like ``ui.module``, so each must be a bare
                # filename — no traversal, no absolute paths. Duplicates are
                # harmless (the server checks membership in a set), so they
                # stay legal rather than being policed here.
                if not isinstance(assets, list):
                    errors.append("manifest.ui.assets must be a list of filenames")
                else:
                    for i, a in enumerate(assets):
                        if (
                            not isinstance(a, str) or not a.strip()
                            or "/" in a or "\\" in a or ".." in a
                        ):
                            errors.append(
                                f"manifest.ui.assets[{i}] must be a bare filename in the plugin dir"
                            )

    return errors


def load_manifest(path: Path) -> Manifest | None:
    """Load + validate a ``manifest.json``. Returns ``None`` on a missing or
    invalid file (never raises), so a broken plugin is skipped, not fatal."""
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    if validate_manifest(raw):
        return None
    ui_raw = raw.get("ui")
    ui: dict[str, Any] = {}
    if isinstance(ui_raw, dict):
        ui = {
            "module": str(ui_raw["module"]).strip(),
            "element": str(ui_raw["element"]).strip(),
            "mount": str(ui_raw.get("mount", "statusbar")),
        }
        # Tab chrome for a `config` pane. Absent keys stay absent rather than
        # defaulting here: the shell decides the fallbacks (plugin name, and an
        # icon it actually has).
        for key in ("label", "icon"):
            if isinstance(ui_raw.get(key), str) and ui_raw[key].strip():
                ui[key] = ui_raw[key].strip()
        # Extra servable frontend files, same passthrough rule as label/icon:
        # validated above, passed through stripped; absent stays absent.
        assets = ui_raw.get("assets")
        if isinstance(assets, list):
            ui["assets"] = [str(a).strip() for a in assets]
    return Manifest(
        name=raw["name"].strip(),
        version=str(raw.get("version", "0.0.0")),
        description=str(raw.get("description", "")).strip(),
        provides=[str(v) for v in raw.get("provides", [])],
        requires=[str(v) for v in raw.get("requires", [])],
        settings=[dict(s) for s in raw.get("settings", [])],
        themes=[dict(t) for t in raw.get("themes", [])],
        ui=ui,
        raw=raw,
    )


__all__ = [
    "KNOWN_SLOTS",
    "BUILTIN_UI_MOUNTS",
    "Manifest",
    "load_manifest",
    "validate_manifest",
]
