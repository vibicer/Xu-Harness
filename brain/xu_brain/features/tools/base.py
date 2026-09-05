"""Tool protocol + shared context — the contract every toolset and the agent
loop build against.

Internal message format is OpenAI tool-calling style (Anthropic-compatible is
normalized to the same loop in ``agent/provider.py``). A tool receives parsed
args and a :class:`ToolContext`, and returns a :class:`ToolResult` whose
``output`` is the model-facing text (governed: capped, full payload kept for
the chip). Approval gating, output caps, concurrency limits and the circuit
breaker live in :mod:`tools.registry`; tools only declare their needs here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, TYPE_CHECKING, runtime_checkable

from ...core.governance import ApprovalLevel

if TYPE_CHECKING:
    from pathlib import Path

    from ...core.governance import ApprovalManager
    from ..agent.loop import Agent
    from ..agent.provider import ProviderManager
    from ...core.notify import ShellNotifier
    from ..memory import MemoryStore
    from ..skills import SkillsEngine
    from ...plugins import PluginBus
    from ..presets import PresetStore


@dataclass
class ToolResult:
    """Normalized tool output. ``output`` is what the model sees; ``raw`` the
    untruncated payload for the chat chip (may be identical when small)."""

    output: str = ""
    error: bool = False
    raw: Any = None
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def ok(cls, output: str, *, raw: Any = None, **meta: Any) -> ToolResult:
        return cls(output=output, raw=raw if raw is not None else output, meta=meta)

    @classmethod
    def err(cls, message: str, *, raw: Any = None, **meta: Any) -> ToolResult:
        return cls(output=message, error=True, raw=raw if raw is not None else message, meta=meta)

    def to_model(self) -> str:
        """Model-facing content string (already capped by governance)."""
        return self.output


@runtime_checkable
class Tool(Protocol):
    """Every tool implements this surface. Registered in :class:`ToolRegistry`."""

    name: str
    toolset: str
    description: str
    approval: ApprovalLevel
    schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    """Optional JSON Schema for the structured raw output.

    JSON Schema for ``args``; the agent loop sends conforming dicts."""

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        """Execute the tool. Pure, no direct event emission — the loop emits
        ``turn.tool`` chips around the call. Raise to surface an error."""
        ...


@dataclass
class ToolContext:
    """Per-turn context handed to every tool invocation.

    Holds the live brain subsystems so a tool can reach the provider manager
    (vision, subagents), memory, skills, flat plugins and the approval manager
    without import cycles. ``cwd`` is the session working directory tools
    resolve relative paths against.
    """

    data_home: Path
    session_id: str
    turn_id: str
    cwd: str
    agent: Agent
    events: ShellNotifier
    approvals: ApprovalManager
    providers: ProviderManager
    memory: MemoryStore
    skills: SkillsEngine
    flat_plugins: PluginBus
    config: dict[str, Any]
    """Frozen-at-turn-start settings (context_length, approval_mode, …)."""

    # Per-turn breaker state — shared across all tools in this turn so a
    # tripped breaker disables the tool for the remainder of the turn.
    breakers: dict[str, bool] = field(default_factory=dict)
    # Shared orchestration-preset store (None if not wired); used by
    # orchestration tools like ``preset_create``.
    presets: PresetStore | None = None
