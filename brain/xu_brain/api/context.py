"""``PluginContext`` — the object an plugin's ``activate(ctx)`` receives.

This is the *only* mutable surface an plugin is handed. Everything the plugin
does flows through the methods below, which the Core implements behind the scenes
(:mod:`xu_brain.core.host.PluginHost`). Keeping the surface narrow here is what
lets the frozen Core guarantee an plugin can't reach internals by accident.

The method set:

- ``register_tool`` / ``register_rpc`` — fill the ``tool`` / ``rpc`` slots.
- ``register_provider`` — the ``provider`` slot.
- ``register_layout`` / ``register_panel`` — frontend ``layout`` / ``panel`` slots.
- ``register_policy`` — a named approval policy (the ``policy`` slot).
- ``register_memory_backend`` — the ``memory`` slot.
- ``on(event)`` / ``transform(point)`` — the unified ``hook`` slot.
- ``settings()`` / ``set_setting()`` — per-plugin persisted settings.
- ``log`` — the plugin's own logger (never the global one).
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class PluginContext(Protocol):
    """Structural surface handed to ``activate(ctx)``. The host passes a concrete
    object satisfying this; plugins should type against this protocol only."""

    name: str
    """Manifest name of the plugin this context belongs to."""

    app: Any
    """The live App. Needed by ``rpc``-slot plugins that must resolve real
    state (e.g. a session's cwd via ``app.sessions``)."""

    def register_tool(self, tool: Any) -> None: ...
    def register_rpc(self, name: str, handler: Callable[..., Any]) -> None: ...
    def register_provider(self, provider: Any) -> None: ...
    def register_layout(self, layout: Any) -> None: ...
    def register_panel(self, panel: Any) -> None: ...
    def register_policy(self, policy: Any) -> None: ...
    def register_memory_backend(self, backend: Any) -> None: ...

    def on(self, event: str, handler: Callable[..., Any]) -> None:
        """Subscribe ``handler`` to a fire-and-forget ``event``."""

    def transform(self, point: str, handler: Callable[..., Any]) -> None:
        """Subscribe ``handler`` to a return-replacing ``point``."""

    def settings(self) -> dict[str, Any]: ...
    def set_setting(self, key: str, value: Any) -> None: ...

    def log(self, message: str, *, level: str = "info") -> None: ...


__all__ = ["PluginContext"]
