"""Event / transform point names — the contract shape of the ``hook`` slot.

One bus, two subscription kinds:

- ``on(event)``       — fire-and-forget, return ignored.
- ``transform(point)`` — return-replacing, chainable.

The names below are the **documented** set the Core guarantees to emit / consult.
An plugin may register any string, but only these are contractually fired by the
Core today; registering an unknown name logs a warning rather than raising, so a
forward-compatible plugin can't take the loop down.
"""
from __future__ import annotations

# Fire-and-forget lifecycle events (absorb the legacy 7-event HookBus).
EVENTS: tuple[str, ...] = (
    "turn.start",
    "step.pre",
    "request.pre",
    "tool.pre_execute",
    "tool.execute",
    "tool.post_execute",
    "turn.stopping",
    "turn.started",
    "turn.finished",
    # Legacy PluginBus.on_start(ctx) — fire-and-forget per turn (despite the name).
    "plugin.start",
)

# Legacy 7-event set: the fire-and-forget core that predates
# ``turn.started`` / ``turn.finished`` / ``plugin.start``. Kept for backward
# compatibility with readers of the old HOOK_EVENTS name.
HOOK_EVENTS: tuple[str, ...] = (
    "turn.start",
    "step.pre",
    "request.pre",
    "tool.pre_execute",
    "tool.execute",
    "tool.post_execute",
    "turn.stopping",
)

# Return-replacing interception points (absorb the legacy 4-hook PluginBus).
TRANSFORM_POINTS: tuple[str, ...] = (
    "before_llm",
    "after_tool",
    "on_message_out",
)

# Union, for validation / introspection helpers.
EVENT_POINTS: tuple[str, ...] = EVENTS + TRANSFORM_POINTS

__all__ = ["EVENTS", "EVENT_POINTS", "HOOK_EVENTS", "TRANSFORM_POINTS"]
