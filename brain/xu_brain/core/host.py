"""Frozen plugin host.

This module owns the **plugin loader** and the **context factory**. The
unified in-process event bus lives in :mod:`xu_brain.core.bus` (one bus, two
subscription kinds — see there): :class:`~xu_brain.core.bus.HookBus` for the
fire-and-forget ``on`` hooks and the return-replacing ``transform`` points.
"""
from __future__ import annotations

import importlib.util
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..api.manifest import BUILTIN_UI_MOUNTS, Manifest, load_manifest
from .bus import HookBus, HookHandle, _run_soon, log
class PluginContextImpl:
    """Concrete :class:`PluginContext` handed to an plugin's ``activate(ctx)``.

    Wires the plugin's slot-filling calls back into the live App (tools, RPCs,
    :class:`HookBus`. Settings are persisted under ``plugin.<name>.<key>``.
    """
    def __init__(self, app: Any, bus: HookBus, manifest: Manifest) -> None:
        self._app = app
        self._bus = bus
        self._manifest = manifest
        self.name = manifest.name
        self._logger = logging.getLogger(f"xu_brain.plugin.{manifest.name}")
        # Every registration this plugin made, newest first — the host disposes
        # them on reload/shutdown so a re-activated plugin never stacks
        # duplicate hooks or leaves a dead tool in the registry.
        self._disposers: list[Callable[[], None]] = []

    @property
    def app(self) -> Any:
        """The live App, for slots that need real subsystems (sessions, config).

        Read-only on purpose: an rpc-slot plugin has to reach ``app.sessions``
        to resolve a session's cwd, and reaching through ``_app`` from plugin
        code would couple every plugin to a private name.
        """
        return self._app

    def _track(self, dispose: Callable[[], None] | None) -> None:
        if callable(dispose):
            self._disposers.append(dispose)

    def dispose(self) -> None:
        """Undo every registration, newest first. Fail-soft per disposer."""
        while self._disposers:
            try:
                self._disposers.pop()()
            except Exception:  # noqa: BLE001 — teardown must never raise
                log.exception("plugin %s disposer failed", self.name)

    # ---- slots ------------------------------------------------------- #

    def register_tool(self, tool: Any) -> None:
        if hasattr(self._app, "registry"):
            self._track(self._app.registry.register(tool))

    def register_rpc(self, name: str, handler: Callable[..., Any]) -> None:
        # NB: ``App.register`` is the *decorator* form (one arg, returns the
        # wrapper). The imperative 2-arg form is ``register_handler``.
        imperative = getattr(self._app, "register_handler", None)
        if callable(imperative):
            self._track(imperative(name, handler))
        else:
            log.warning(
                "plugin %s registered rpc %r but the app has no register_handler",
                self.name,
                name,
            )

    def register_provider(self, provider: Any) -> None:
        if hasattr(self._app, "providers"):
            self._app.providers.upsert(provider, None)

    def register_layout(self, layout: Any) -> None:
        self._slot("layout", layout)

    def register_panel(self, panel: Any) -> None:
        self._slot("panel", panel)

    def register_policy(self, policy: Any) -> None:
        """Feed a plugin's approval criteria to the live gate.

        Recording the slot is not enough: ``ApprovalManager`` is what consults
        policies, so a slot-only registration made every ``policy`` plugin a
        silent no-op — it would load clean, declare it gates tools, and change
        nothing. A safety surface must not fail quietly, so if the gate is
        missing this warns rather than pretending to have wired it up.
        """
        self._slot("policy", policy)
        gate = getattr(self._app, "approvals", None)
        if gate is None or not callable(getattr(gate, "add_policy", None)):
            log.warning(
                "plugin %s registered an approval policy but the app has no "
                "approval gate — the policy will NOT be consulted",
                self.name,
            )
            return
        gate.add_policy(policy)
        # Disposal is mandatory, not tidiness: a disabled plugin's criteria
        # must stop gating, and a reload must not stack a second copy.
        remove = getattr(gate, "remove_policy", None)
        if callable(remove):
            self._track(lambda: remove(policy))

    def register_memory_backend(self, backend: Any) -> None:
        self._slot("memory", backend)

    def _slot(self, kind: str, value: Any) -> None:
        # Slots that don't have a live subsystem yet are recorded for
        # introspection (slots.list) without breaking activation.
        store = getattr(self._app, "slots", None)
        if store is not None:
            self._track(store.register(kind, self.name, value))
        else:
            log.warning(
                "plugin %s provided slot %r but the app has no slot store",
                self.name,
                kind,
            )

    # ---- hook slot --------------------------------------------------- #

    def on(self, event: str, handler: Callable[..., Any]) -> HookHandle:
        handle = self._bus.on(event, handler)
        self._track(handle.dispose)
        return handle

    def transform(self, point: str, handler: Callable[..., Any]) -> HookHandle:
        handle = self._bus.transform(point, handler)
        self._track(handle.dispose)
        return handle

    # ---- settings ---------------------------------------------------- #

    def _prefix(self, key: str) -> str:
        return f"plugin.{self.name}.{key}"

    def settings(self) -> dict[str, Any]:
        config = getattr(self._app, "config", None)
        if config is None:
            return {}
        prefix = f"plugin.{self.name}."
        return {
            k.removeprefix(prefix): v
            for k, v in config.all().items()
            if k.startswith(prefix)
        }

    def set_setting(self, key: str, value: Any) -> None:
        config = getattr(self._app, "config", None)
        if config is not None:
            config.set(self._prefix(key), value)

    # ---- logging ----------------------------------------------------- #

    def log(self, message: str, *, level: str = "info") -> None:
        valid = ("debug", "info", "warning", "error", "critical")
        getattr(self._logger, level if level in valid else "info")(message)


class PluginHost:
    """Loads manifest-based plugin packages and calls their ``activate(ctx)``.

    A package is a directory ``<name>/`` containing ``manifest.json`` plus an
    ``activate.py`` exposing ``activate(ctx)``. Loading is fail-soft: a missing
    manifest, bad module, or raising ``activate`` is logged and the plugin is
    skipped — the brain keeps running.
    """
    def __init__(self, bus: HookBus) -> None:
        self.bus = bus
        self._plugins: dict[str, dict[str, Any]] = {}
        self._enabled: set[str] | None = None
        self._root: Path | None = None
        # The App the last load_dir ran against, so reload() can repeat it.
        self._app: Any | None = None
        # name → (context, module), so reload/shutdown can undo registrations
        # and call the plugin's own ``deactivate(ctx)``.
        self._live: dict[str, tuple[PluginContextImpl, Any]] = {}
        # Tasks from ``async def activate`` — awaited by :meth:`ready` so boot
        # can guarantee every plugin finished registering before serving.
        self._pending: list[Any] = []

    def ui_asset(self, name: str, filename: str) -> Path | None:
        """Resolve a plugin's declared frontend file, or ``None``.

        The web server's only door into the plugins dir. Returns a path only
        when the plugin is loaded, *enabled*, and ``filename`` is exactly a
        file its manifest declared — its ``ui.module``, one of its declared
        ``ui.assets``, or the ``css`` of one of its themes. Everything else
        in the package stays unreachable, and a disabled plugin serves
        nothing at all.
        """
        if self._root is None:
            return None
        record = self._plugins.get(name)
        if not record or not record.get("enabled"):
            return None
        ui = record.get("ui") or {}
        declared = {ui.get("module")} | set(ui.get("assets") or [])
        declared |= {t.get("css") for t in record.get("themes") or []}
        if filename not in declared:
            return None
        path = self._root / name / filename
        try:
            resolved = path.resolve()
            resolved.relative_to(self._root.resolve())
        except (OSError, ValueError):
            return None
        return resolved if resolved.is_file() else None

    async def ready(self) -> None:
        """Await every in-flight ``async def activate``. Fail-soft per task."""
        while self._pending:
            pending, self._pending = self._pending, []
            for task in pending:
                try:
                    await task
                except Exception as exc:  # noqa: BLE001 — fail-soft
                    log.warning("plugin activate task failed: %s", exc)

    def load_dir(self, path: Path, app: Any) -> int:
        """Scan ``path`` for plugin packages and activate them. Returns count.

        Deactivates whatever is currently loaded first, so a reload replaces
        plugins instead of stacking a second copy of every hook.
        """
        root = Path(path)
        if not root.is_dir():
            return 0
        self.deactivate_all()
        self._root = root
        self._app = app
        self._load_enabled(root)
        self._plugins.clear()
        for pkg_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            self._load_one(pkg_dir, app)
        return len(self._plugins)

    def reload(self) -> int:
        """Re-scan the directory this host last loaded, against the same App.

        The caller (the ``plugin.reload`` RPC) passes nothing — the host must
        reuse the App it booted with, or slot registrations would land on the
        wrong object.
        """
        if self._root is None or self._app is None:
            return 0
        return self.load_dir(self._root, self._app)

    # ---- teardown ----------------------------------------------------- #

    def deactivate(self, name: str) -> None:
        """Call the plugin's ``deactivate(ctx)`` then undo its registrations."""
        entry = self._live.pop(name, None)
        if entry is None:
            return
        ctx, module = entry
        hook = getattr(module, "deactivate", None)
        if callable(hook):
            try:
                _run_soon(hook(ctx))
            except Exception as exc:  # noqa: BLE001 — fail-soft
                log.warning("plugin %s deactivate() raised: %s", name, exc)
        ctx.dispose()

    def deactivate_all(self) -> None:
        for name in list(self._live):
            self.deactivate(name)

    async def shutdown(self) -> None:
        """Called from ``App.shutdown`` so plugins can release resources."""
        self.deactivate_all()

    # ---- enable / disable -------------------------------------------- #
    # Persisted to ``<root>/plugins.enabled.json``. Deliberately NOT
    # ``enabled.json``: the legacy flat-file ``PluginBus`` owns that name in
    # the same directory and keys it by module *stem*, so sharing the file
    # made the two loaders silently disable each other's entries.

    ENABLED_FILE = "plugins.enabled.json"

    def _load_enabled(self, root: Path) -> None:
        f = root / self.ENABLED_FILE
        if f.is_file():
            try:
                data = json.loads(f.read_text("utf-8"))
                self._enabled = set(data) if isinstance(data, list) else None
            except (OSError, json.JSONDecodeError):
                self._enabled = None
        else:
            self._enabled = None

    def _save_enabled(self) -> None:
        if self._root is None:
            return
        stems = sorted(self._enabled) if self._enabled is not None else []
        (self._root / self.ENABLED_FILE).write_text(
            json.dumps(stems, indent=2, ensure_ascii=False), "utf-8"
        )

    def _is_enabled(self, name: str) -> bool:
        return self._enabled is None or name in self._enabled

    def enable(self, name: str) -> bool:
        if name not in self._plugins:
            return False
        if self._enabled is None:
            self._enabled = set(self._plugins)
        self._enabled.add(name)
        self._save_enabled()
        self._plugins[name]["enabled"] = True
        # Take effect now, mirroring ``disable``. Without this an enabled plugin
        # stays inert until the next reload, so anything gated on its slots (a
        # UI surface backed by its rpc methods) would be visible but dead.
        if name not in self._live and self._root is not None and self._app is not None:
            self._load_one(self._root / name, self._app)
        return True

    def disable(self, name: str) -> bool:
        if name not in self._plugins:
            return False
        if self._enabled is None:
            self._enabled = set(self._plugins)
        self._enabled.discard(name)
        self._save_enabled()
        # Take effect now: undo its registrations instead of waiting for the
        # next reload (otherwise a "disabled" plugin keeps handling events).
        self.deactivate(name)
        self._plugins[name]["enabled"] = False
        return True

    def _load_one(self, pkg_dir: Path, app: Any) -> None:
        manifest = load_manifest(pkg_dir / "manifest.json")
        if manifest is None:
            return
        # Validation checks the mount's shape, not its membership (a layout
        # plugin may offer spots the built-in shell doesn't know), so an
        # unfamiliar-but-legal mount reaching here is fine — but it renders
        # nowhere until some layout offers it, and that is worth one warning
        # line, not a rejection. Validation stays pure; this is the host's
        # call to make.
        mount = (manifest.ui or {}).get("mount")
        if mount and mount not in BUILTIN_UI_MOUNTS:
            log.warning(
                "plugin %s: mount %r is not offered by any built-in layout; "
                "it renders only in a layout that offers it",
                manifest.name,
                mount,
            )
        # A disabled plugin is still listed, but never activated.
        if not self._is_enabled(manifest.name):
            self._plugins[manifest.name] = self._record(manifest, enabled=False)
            return
        activate_file = pkg_dir / "activate.py"
        if not activate_file.is_file():
            # A data-only plugin (themes, or a UI element with no Python behind
            # it) legitimately has no code to run. Record it as enabled so the
            # shell can offer its contributions; only a package with neither
            # code nor data is actually broken.
            if manifest.themes or manifest.ui:
                self._plugins[manifest.name] = self._record(manifest, enabled=True)
                log.info("plugin %s v%s loaded (data only)", manifest.name, manifest.version)
            else:
                log.warning("plugin %s has no activate.py and no data; skipped", manifest.name)
            return
        try:
            spec = importlib.util.spec_from_file_location(
                f"xu_plugin_{manifest.name.replace('-', '_')}", activate_file
            )
            if spec is None:  # pragma: no cover
                raise ImportError(f"cannot create module spec for {activate_file}")
            module = importlib.util.module_from_spec(spec)
            # Compile the source text ourselves rather than going through
            # ``spec.loader.exec_module``. The loader consults __pycache__, and
            # its staleness check is (mtime, size) at one-second granularity —
            # so a plugin edited twice in the same second, to the same length,
            # reloads as the *old* bytecode. That is exactly the shape of an
            # agent iterating on a plugin, and it makes plugin_reload lie.
            source = activate_file.read_text("utf-8")
            exec(compile(source, str(activate_file), "exec"), module.__dict__)  # noqa: S102
            activate = getattr(module, "activate", None)
            if activate is None or not callable(activate):
                log.warning("plugin %s exposes no activate(ctx); skipped", manifest.name)
                return
        except Exception as exc:  # noqa: BLE001 — fail-soft
            log.warning("plugin %s failed to import: %s", manifest.name, exc)
            return

        ctx = PluginContextImpl(app, self.bus, manifest)
        try:
            # ``activate`` may be ``async def``; _run_soon hands the coroutine
            # to the running loop so I/O-heavy plugins can await their setup.
            task = _run_soon(activate(ctx))
        except Exception as exc:  # noqa: BLE001 — fail-soft
            log.warning("plugin %s activate() raised: %s", manifest.name, exc)
            # Undo whatever it managed to register before it blew up.
            ctx.dispose()
            return
        if task is not None:
            self._pending.append(task)
        self._live[manifest.name] = (ctx, module)
        self._plugins[manifest.name] = self._record(manifest, enabled=True)
        log.info("plugin %s v%s activated", manifest.name, manifest.version)

    def _record(self, manifest: Manifest, *, enabled: bool) -> dict[str, Any]:
        return {
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "provides": manifest.provides,
            "requires": manifest.requires,
            # Requirements the host cannot satisfy right now. Reported rather
            # than enforced: refusing to load would strand a plugin whose
            # dependency simply loads later in the same scan, and a plugin can
            # legitimately degrade. Empty list = all good.
            "unmet": self._unmet(manifest),
            "enabled": enabled,
            # The declared setting schema plus each key's CURRENT value, so the
            # shell can render controls for any plugin without knowing it by
            # name. Without this a plugin author has to ship a bespoke RPC just
            # to expose its own settings.
            "settings": self._settings_schema(manifest),
            # Frontend contribution, so the shell can mount an enabled plugin's
            # custom element without knowing any plugin by name.
            "ui": dict(manifest.ui) if manifest.ui else None,
            # Colour schemes this plugin contributes, so Config → Themes can
            # offer them beside the built-ins. Pure data: a theme plugin needs
            # no activate.py, and the shell paints from `colors` directly.
            "themes": [dict(t) for t in manifest.themes],
        }

    def _unmet(self, manifest: Manifest) -> list[str]:
        """Slots this plugin requires that nothing currently fills.

        ``requires`` was validated and then ignored, so a plugin could declare a
        dependency, load into a host with nothing providing it, and fail in some
        unrelated way later. Naming the gap is the whole value.
        """
        required = [str(s) for s in manifest.requires if str(s).strip()]
        if not required:
            return []
        filled: set[str] = set()
        store = getattr(self._app, "slots", None) if self._app is not None else None
        if store is not None:
            try:
                filled |= {k for k, v in store.list().items() if v}
            except Exception:  # noqa: BLE001 — introspection must not break loading
                pass
        # Slots backed by a live subsystem rather than the slot store: their
        # registrations never appear there, so treat a present subsystem as the
        # provider. Otherwise every `requires: ["tool"]` reads as unmet.
        #
        # NB this answers "can I use this capability", not "can I replace its
        # implementation". `requires: ["memory"]` is met because a memory store
        # exists to write to; registering a substitute *backend* is still inert.
        app = self._app
        for slot, attr in (("tool", "registry"), ("rpc", "register_handler"),
                           ("provider", "providers"), ("hook", "bus"),
                           ("policy", "approvals"), ("memory", "memory"),
                           ("skill", "skills"), ("persona", "personas")):
            if app is not None and getattr(app, attr, None) is not None:
                filled.add(slot)
        # The shell always ships built-in colour schemes, so a plugin that needs
        # *a* theme to exist always has one. (Contributing its own is the
        # ``provides`` side, handled by the loop below.)
        filled.add("theme")
        # Anything another loaded plugin declares it provides also counts.
        for record in self._plugins.values():
            filled |= {str(s) for s in record.get("provides") or []}
        return [s for s in required if s not in filled]

    def _settings_schema(self, manifest: Manifest) -> list[dict[str, Any]]:
        """The manifest's setting declarations, each carrying its live value.

        ``value`` is the stored value when set, otherwise the declared default —
        so the shell renders the effective state, not an empty control.
        """
        if not manifest.settings:
            return []
        stored: dict[str, Any] = {}
        config = getattr(self._app, "config", None) if self._app is not None else None
        if config is not None:
            prefix = f"plugin.{manifest.name}."
            try:
                stored = {
                    k.removeprefix(prefix): v
                    for k, v in config.all().items()
                    if k.startswith(prefix)
                }
            except Exception:  # noqa: BLE001 — a bad config must not hide the plugin
                stored = {}
        out: list[dict[str, Any]] = []
        for spec in manifest.settings:
            key = str(spec.get("key", ""))
            if not key:
                continue
            entry = dict(spec)
            entry["key"] = key
            entry["value"] = stored[key] if key in stored else spec.get("default")
            out.append(entry)
        return out

    def list(self) -> list[dict[str, Any]]:
        return list(self._plugins.values())


__all__ = [
    "PluginHost",
    "PluginContextImpl",
    "load_manifest",
]
