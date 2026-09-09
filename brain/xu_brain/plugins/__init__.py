"""Plugin bus — loads user plugins and wires them onto the unified event bus.

A plugin is a small Python module dropped into ``data_home/plugins/*.py`` that
exposes any subset of hook callables:

- ``on_start(ctx)``                       — fire-and-forget (per turn, despite the name)
- ``before_llm(messages: list) -> list``  — transform messages before the model
- ``after_tool(tool_name, result) -> result`` — transform a :class:`ToolResult`
- ``on_message_out(content: str) -> str``  — transform final assistant content

Missing hooks are no-ops. Every hook call is wrapped in try/except so a
misbehaving plugin is logged and skipped — it never crashes the brain. Plugins
may be toggled on/off; the enabled-stem list is persisted to
``data_home/plugins/enabled.json``. Disabled plugins are imported but not called.

Architecture: the *old* PluginBus kept its own
4-hook chain; now it is a thin loader that registers each plugin's hooks onto a
single shared :class:`~xu_brain.core.bus.HookBus` — the same bus that serves
``HookBus`` (``on``/``emit``/``waterfall``). ``on_start`` becomes an ``on``
(fire-and-forget) handler; the three return-replacing hooks become ``transform``
handlers. The loop-facing methods (``on_start``/``before_llm``/``after_tool``/
``on_message_out``) remain for backward compatibility and simply delegate to the
bus.
"""
from __future__ import annotations

import importlib.util
import inspect
import json
import logging
from pathlib import Path
from typing import Any

from ..core.bus import HookBus, HookHandle
from ..features.tools.base import ToolResult

log = logging.getLogger(__name__)

HOOK_NAMES = ("on_start", "before_llm", "after_tool", "on_message_out")

# Example plugin written into an empty plugins dir on first load so users see
# the pattern. It is a no-op live plugin (no leading underscore) so the loader
# picks it up — harmless because every hook is optional.
_EXAMPLE_PLUGIN = '''"""Example Xu plugin — a no-op starting template.

Copy this file, rename it, and implement the hooks you need. Every hook is
optional: delete the ones you don't use. Hooks may be sync or async.
"""


def on_start(ctx):
    """Called once per turn. ``ctx`` is the ToolContext."""
    return None


def before_llm(messages):
    """Transform the OpenAI-style message list before it hits the model."""
    return messages


def after_tool(tool_name, result):
    """Transform a ToolResult after a tool runs. Return the (possibly new) result."""
    return result


def on_message_out(content):
    """Transform the final assistant message shown to the user."""
    return content
'''


class _Plugin:
    """A loaded plugin module + its discovered hooks."""

    __slots__ = ("name", "module", "hooks")

    def __init__(self, name: str, module: Any) -> None:
        self.name = name
        self.module = module
        self.hooks: dict[str, Any] = {
            h: attr
            for h in HOOK_NAMES
            if (attr := getattr(module, h, None)) is not None and callable(attr)
        }

    def has(self, hook: str) -> bool:
        return hook in self.hooks


class PluginBus:
    """Loads plugins from ``data_home/plugins`` and registers their hooks onto a
    shared :class:`HookBus`. Fail-soft: import errors and hook exceptions are
    logged and the offending plugin (or hook) is skipped.
    """

    def __init__(self, data_home: Path, bus: HookBus | None = None) -> None:
        self.data_home = Path(data_home)
        self.plugins_dir = self.data_home / "plugins"
        self.enabled_file = self.plugins_dir / "enabled.json"
        # The unified bus this loader feeds. Defaults to a fresh instance so a
        # standalone PluginBus still works; the App shares one across HookBus.
        self.bus = bus if bus is not None else HookBus()
        # name -> _Plugin; insertion order = load order (hook chain order).
        self._plugins: dict[str, _Plugin] = {}
        # set of enabled stems; defaults to "everything loaded" until a user
        # explicitly toggles, matching the "enabled plugins listed in Config"
        # behavior (absent list = all on).
        self._enabled: set[str] | None = None
        # Bus subscriptions owned by this loader, disposed on re-load.
        self._handles: list[HookHandle] = []

    # ------------------------------------------------------------------ #
    # loading
    # ------------------------------------------------------------------ #

    def load_dir(self, path: Path | None = None) -> int:
        """Scan ``path`` (default ``data_home/plugins``) for ``*.py`` plugins.

        Imports each (module name ``xu_plugin_<stem>``), discovers hooks, and
        records the enabled set from ``enabled.json``. Registers each hook onto
        the shared bus. Returns the number of plugins loaded.

        .. deprecated::
           Flat single-file plugins are the *legacy* extension path. Plugins
           (``data_home/plugins/<name>/`` with a manifest, loaded by
           :class:`~xu_brain.core.host.PluginHost`) are a strict superset: the
           same four hooks plus tools, RPC, providers, layouts, panels,
           policies and settings. New extensions should be plugins; this
           loader stays only so existing flat plugins keep working. Note both
           loaders scan the same directory — they are kept apart by state file
           (``enabled.json`` here, ``plugins.enabled.json`` there) and by
           shape (files vs. directories).
        """
        plugins_dir = Path(path) if path is not None else self.plugins_dir
        plugins_dir.mkdir(parents=True, exist_ok=True)
        self.plugins_dir = plugins_dir
        self.enabled_file = plugins_dir / "enabled.json"

        files = sorted(
            p
            for p in plugins_dir.glob("*.py")
            if not p.name.startswith("_") and p.stem != "__pycache__"
        )
        if files:
            log.info(
                "loaded %d legacy flat plugin(s): %s — consider migrating to "
                "plugins (data_home/plugins/<name>/manifest.json)",
                len(files),
                ", ".join(f.stem for f in files),
            )
        # Drop prior registrations (safe on re-load) and re-import fresh.
        for h in self._handles:
            h.dispose()
        self._handles.clear()
        self._plugins.clear()

        for f in files:
            stem = f.stem
            mod_name = f"xu_plugin_{stem}"
            try:
                spec = importlib.util.spec_from_file_location(mod_name, f)
                if spec is None or spec.loader is None:  # pragma: no cover
                    raise ImportError(f"cannot create module spec for {f}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception as exc:
                log.warning("plugin %s failed to import: %s", stem, exc)
                continue
            self._plugins[stem] = _Plugin(stem, module)

        self._load_enabled()
        self._register_on_bus()
        return len(self._plugins)

    def _register_on_bus(self) -> None:
        """Register every loaded plugin's hooks onto the shared bus as
        ``on``/``transform`` handlers. The wrappers enforce enable/disable at
        dispatch time and preserve the legacy type-guarded passthrough."""
        for plugin in self._plugins.values():
            if plugin.has("on_start"):
                self._handles.append(self.bus.on("plugin.start", self._wrap_on_start(plugin)))
            if plugin.has("before_llm"):
                self._handles.append(
                    self.bus.transform("before_llm", self._wrap_before_llm(plugin))
                )
            if plugin.has("after_tool"):
                self._handles.append(
                    self.bus.transform("after_tool", self._wrap_after_tool(plugin))
                )
            if plugin.has("on_message_out"):
                self._handles.append(
                    self.bus.transform("on_message_out", self._wrap_on_message_out(plugin))
                )

    # ------------------------------------------------------------------ #
    # hook wrappers (enable/disable + fail-soft + type-guarded passthrough)
    # ------------------------------------------------------------------ #

    @staticmethod
    async def _call(fn: Any, *args: Any) -> Any:
        """Invoke ``fn`` (sync or async) and await if needed. The bus already
        catches + logs exceptions, so this just normalizes the return."""
        ret = fn(*args)
        if inspect.isawaitable(ret):
            ret = await ret
        return ret

    def _wrap_on_start(self, plugin: _Plugin):
        async def handler(ctx):
            if not self._is_enabled(plugin.name):
                return None
            await self._call(plugin.hooks["on_start"], ctx)
            return None

        return handler

    def _wrap_before_llm(self, plugin: _Plugin):
        async def handler(messages):
            if not self._is_enabled(plugin.name):
                return None
            out = await self._call(plugin.hooks["before_llm"], messages)
            return out if out is not None else None

        return handler

    def _wrap_after_tool(self, plugin: _Plugin):
        async def handler(tool_name, result):
            if not self._is_enabled(plugin.name):
                return None
            out = await self._call(plugin.hooks["after_tool"], tool_name, result)
            return out if isinstance(out, ToolResult) else None

        return handler

    def _wrap_on_message_out(self, plugin: _Plugin):
        async def handler(content):
            if not self._is_enabled(plugin.name):
                return None
            out = await self._call(plugin.hooks["on_message_out"], content)
            return out if isinstance(out, str) else None

        return handler

    # ------------------------------------------------------------------ #
    # enable / disable persistence
    # ------------------------------------------------------------------ #

    def _load_enabled(self) -> None:
        """Read the enabled-stem list. Absent file → None (= all on)."""
        if self.enabled_file.exists():
            try:
                data = json.loads(self.enabled_file.read_text("utf-8"))
                self._enabled = set(data) if isinstance(data, list) else None
            except Exception as exc:
                log.warning("enabled.json unreadable (%s); defaulting to all on", exc)
                self._enabled = None
        else:
            self._enabled = None

    def _save_enabled(self) -> None:
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        stems = sorted(self._enabled) if self._enabled is not None else []
        self.enabled_file.write_text(
            json.dumps(stems, indent=2, ensure_ascii=False), "utf-8"
        )

    def _is_enabled(self, name: str) -> bool:
        # None = no explicit list → every loaded plugin is on.
        return self._enabled is None or name in self._enabled

    def enable(self, name: str) -> bool:
        """Enable a loaded plugin by stem. Returns True if it exists."""
        if name not in self._plugins:
            return False
        if self._enabled is None:
            # First explicit toggle: start from the full loaded set, then add.
            self._enabled = set(self._plugins)
        self._enabled.add(name)
        self._save_enabled()
        return True

    def disable(self, name: str) -> bool:
        """Disable a loaded plugin by stem. Returns True if it exists."""
        if name not in self._plugins:
            return False
        if self._enabled is None:
            self._enabled = set(self._plugins)
        self._enabled.discard(name)
        self._save_enabled()
        return True

    def set_enabled(self, names: list[str]) -> None:
        """Replace the entire enabled set."""
        self._enabled = {n for n in names if n in self._plugins}
        self._save_enabled()

    # ------------------------------------------------------------------ #
    # introspection
    # ------------------------------------------------------------------ #

    def __len__(self) -> int:
        return len(self._plugins)

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._plugins

    def get(self, name: str) -> _Plugin | None:
        return self._plugins.get(name)

    # ------------------------------------------------------------------ #
    # loop-facing hooks (delegate to the shared bus)
    # ------------------------------------------------------------------ #

    async def on_start(self, ctx: Any) -> None:
        """Fire-and-forget ``on_start`` chain (per turn)."""
        await self.bus.emit("plugin.start", ctx)

    async def before_llm(self, messages: list[Any]) -> list[Any]:
        """Chain ``before_llm`` transforms; returns the (possibly replaced) list."""
        return await self.bus.apply("before_llm", messages)

    async def after_tool(self, tool_name: str, result: ToolResult) -> ToolResult:
        """Chain ``after_tool`` transforms; returns the (possibly replaced) result."""
        return await self.bus.apply("after_tool", tool_name, result)

    async def on_message_out(self, content: str) -> str:
        """Chain ``on_message_out`` transforms; returns the (possibly replaced) text."""
        return await self.bus.apply("on_message_out", content)


__all__ = ["PluginBus", "HOOK_NAMES"]
