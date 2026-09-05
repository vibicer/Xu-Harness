"""Drop-in tool loader.

Users author custom tools with the :func:`tool` decorator in a module dropped
under ``data_home/tools/*.py``. :func:`load_dropin_tools` imports each module,
extracts the :class:`Tool` objects it defines, and returns them for
registration alongside the built-ins. One bad module is logged and skipped — it
never crashes the brain (same policy as plugins).

Export contract (pick one per module):
- ``tools: list[Tool | Callable]``  — preferred; a list of decorated tools
- ``tool: Tool``                    — a single decorated tool

Every drop-in goes through the same :class:`ToolRegistry` governance as
built-ins (circuit breaker, approval gating, concurrency, output caps). Drop-ins
never collide with built-ins: the registrar rejects any name that is already
taken (so ``read``/``write``/``edit``/… stay unshadowable).
"""
from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any, Callable

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult

__all__ = ["tool", "load_dropin_tools", "active_dropins", "ApprovalLevel"]

log = logging.getLogger(__name__)


class _Dropin:
    """Adapt a plain ``async (args, ctx) -> ToolResult`` function to the
    :class:`Tool` protocol so users can author a tool as a function."""

    name: str
    toolset: str
    description: str
    approval: ApprovalLevel
    schema: dict[str, Any]

    def __init__(
        self,
        name: str,
        description: str,
        schema: dict[str, Any],
        fn: Callable[[dict[str, Any], ToolContext], Any],
        *,
        toolset: str = "dropin",
        approval: ApprovalLevel = ApprovalLevel.RISKY,
    ) -> None:
        self.name = name
        self.toolset = toolset
        self.description = description
        self.schema = schema
        self.approval = approval
        self._fn = fn

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        return await self._fn(args, ctx)


def tool(
    name: str,
    description: str = "",
    schema: dict[str, Any] | None = None,
    *,
    toolset: str = "dropin",
    approval: ApprovalLevel = ApprovalLevel.RISKY,
) -> Callable[[Callable], _Dropin]:
    """Decorate an ``async (args, ctx) -> ToolResult`` function into a drop-in
    :class:`Tool`. ``schema`` may be omitted for arg-less tools. ``approval``
    and ``toolset`` are optional overrides; drop-in tools default to ``RISKY``
    (prompt in manual mode) and the ``dropin`` toolset so all third-party tools
    can be toggled together."""

    def deco(fn: Callable) -> _Dropin:
        return _Dropin(
            name,
            description,
            schema or {},
            fn,
            toolset=toolset,
            approval=approval,
        )

    return deco


def _is_tool(x: Any) -> bool:
    return (
        x is not None
        and hasattr(x, "name")
        and hasattr(x, "toolset")
        and hasattr(x, "schema")
        and callable(getattr(x, "run", None))
    )


_ACTIVE_DROPINS: set[str] = set()
"""Source of truth for which tool names are user drop-ins (vs. built-ins).

Populated by :func:`load_dropin_tools` (boot) and appended by ``tool_create``.
``tool_remove`` deletes from it. Cleared on a fresh disk scan so the set always
mirrors ``data_home/tools/*.py``.
"""


def active_dropins() -> set[str]:
    """Live set of drop-in tool names (registered or pending)."""
    return _ACTIVE_DROPINS


def load_dropin_tools(data_home: Path | str) -> list[Tool]:
    """Discover and import ``data_home/tools/*.py``, returning the ``Tool``
    objects each module exports. A failing module is logged and skipped.

    The scan is the source of truth for :func:`active_dropins`: the set is
    cleared and repopulated from disk on every call."""
    tools_dir = Path(data_home) / "tools"
    if not tools_dir.is_dir():
        return []

    out: list[Tool] = []
    _ACTIVE_DROPINS.clear()
    for path in sorted(tools_dir.glob("*.py")):
        if path.name == "__init__.py":
            continue
        spec = importlib.util.spec_from_file_location(f"xu_dropin_{path.stem}", path)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            log.warning("tool loader: no loader for %s", path.name)
            continue
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:  # noqa: BLE001 - a bad drop-in must not take down boot
            log.exception("tool loader: failed to import %s (skipped)", path.name)
            continue

        # Contract: `tools: [...]` (preferred) or a single `tool`.
        candidates: list[Any] = getattr(mod, "tools", None)
        if candidates is None:
            single = getattr(mod, "tool", None)
            candidates = [single] if single is not None else []
        elif isinstance(candidates, (list, tuple)):
            candidates = list(candidates)
        else:  # a bare Tool bound to `tools`
            candidates = [candidates]

        for cand in candidates:
            if _is_tool(cand):
                out.append(cand)
                _ACTIVE_DROPINS.add(cand.name)
            else:
                log.warning(
                    "tool loader: %s exports a non-tool item (skipped)", path.name
                )
    return out
