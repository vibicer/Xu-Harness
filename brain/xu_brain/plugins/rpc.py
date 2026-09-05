"""RPC surface for plugin management and slot introspection."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from ..core.contract import RpcError

if TYPE_CHECKING:
    from ..core.runtime import App


def _coerce_setting(declared: Any, value: Any) -> Any:
    """Fit ``value`` to a manifest setting's declared type, or raise.

    Coercing rather than only validating: the shell sends form values, so a
    number arrives as a string and a checkbox as a bool. Raising on a genuine
    mismatch is the point — a plugin that declared ``integer`` must never read
    back ``"seven"``.

    ``bool`` is checked before ``int`` throughout because ``isinstance(True,
    int)`` is True in Python, so the obvious order silently stores ``True`` as
    an integer setting.
    """
    kind = str(declared or "string")
    if kind == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValueError(f"{value!r} is not a boolean")
    if kind == "integer":
        if isinstance(value, bool):
            raise ValueError("a boolean is not an integer")
        return int(value)
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError("a boolean is not a number")
        return float(value)
    if kind == "list":
        if isinstance(value, list):
            return list(value)
        # A textarea gives one item per line; empty lines are not items.
        if isinstance(value, str):
            return [line.strip() for line in value.splitlines() if line.strip()]
        raise ValueError(f"{value!r} is not a list")
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        raise ValueError(f"{type(value).__name__} is not a string")
    return str(value)


def register(app: App) -> None:
    @app.register("slots.list")
    async def slots_list(params: dict[str, Any]) -> dict[str, Any]:
        return {"slots": app.slots.list()}

    @app.register("layouts.list")
    async def layouts_list(params: dict[str, Any]) -> dict[str, Any]:
        """Return registered layouts plus agent-created definitions."""
        slots = app.slots.list()
        layouts = list(slots.get("layout", []))
        custom_layouts: list[dict[str, Any]] = []
        custom_themes: list[dict[str, Any]] = []
        for path in (app.data_home / "layouts").glob("*/layout.json"):
            try:
                value = json.loads(path.read_text("utf-8"))
                if isinstance(value, dict):
                    value["builtin"] = False
                    custom_layouts.append(value)
            except (OSError, json.JSONDecodeError):
                continue
        for path in (app.data_home / "themes").glob("*/theme.json"):
            try:
                value = json.loads(path.read_text("utf-8"))
                if isinstance(value, dict):
                    value["builtin"] = False
                    custom_themes.append(value)
            except (OSError, json.JSONDecodeError):
                continue
        active: dict[str, Any] = {}
        active_path = app.data_home / "active_layout.json"
        if active_path.is_file():
            try:
                loaded = json.loads(active_path.read_text("utf-8"))
                if isinstance(loaded, dict):
                    active = loaded
            except (OSError, json.JSONDecodeError):
                pass
        return {
            "layouts": layouts,
            "panels": slots.get("panel", []),
            "custom_layouts": sorted(custom_layouts, key=lambda x: str(x.get("name", ""))),
            "custom_themes": sorted(custom_themes, key=lambda x: str(x.get("name", ""))),
            "active": active,
        }

    @app.register("plugin.list")
    async def plugin_list(params: dict[str, Any]) -> dict[str, Any]:
        return {"plugins": app.plugins.list()}

    @app.register("plugin.enable")
    async def plugin_enable(params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        if not app.plugins.enable(name):
            raise RpcError(-32002, "plugin not found", {"name": name})
        return {"ok": True, "name": name}

    @app.register("plugin.disable")
    async def plugin_disable(params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "").strip()
        if not app.plugins.disable(name):
            raise RpcError(-32002, "plugin not found", {"name": name})
        return {"ok": True, "name": name}

    @app.register("plugin.reload")
    async def plugin_reload(params: dict[str, Any]) -> dict[str, Any]:
        # Fail-soft: a bad plugin is logged + skipped; the brain keeps running.
        loaded = app.plugins.load_dir(app.data_home / "plugins", app)
        await app.plugins.ready()
        return {"loaded": loaded, "plugins": app.plugins.list()}

    @app.register("plugin.set_setting")
    async def plugin_set_setting(params: dict[str, Any]) -> dict[str, Any]:
        """Write one setting a plugin's manifest declared.

        Only declared keys are writable, and the value is coerced to the
        declared type — otherwise the settings block would be an untyped
        key-value store on the app's config file, writable by any caller with a
        socket, and a plugin reading its own setting could get anything at all.
        """
        name = str(params.get("name") or "").strip()
        key = str(params.get("key") or "").strip()
        record = next((p for p in app.plugins.list() if p.get("name") == name), None)
        if record is None:
            raise RpcError(-32002, "plugin not found", {"name": name})
        spec = next(
            (s for s in record.get("settings") or [] if s.get("key") == key), None
        )
        if spec is None:
            raise RpcError(
                -32602, "plugin declares no such setting", {"name": name, "key": key}
            )
        try:
            value = _coerce_setting(spec.get("type"), params.get("value"))
        except (TypeError, ValueError) as exc:
            raise RpcError(
                -32602,
                f"value does not fit setting type {spec.get('type')!r}",
                {"name": name, "key": key, "error": str(exc)},
            ) from exc
        app.config.set(f"plugin.{name}.{key}", value)
        # Return the refreshed record so the caller does not need a second call
        # to see the effective value (defaults vs stored).
        updated = next((p for p in app.plugins.list() if p.get("name") == name), None)
        return {"ok": True, "name": name, "key": key, "value": value, "plugin": updated}
