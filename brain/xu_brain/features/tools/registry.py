"""Tool registry + governance.

A :class:`ToolRegistry` owns every registered tool, grouped by toolset. The
agent loop calls :meth:`run` with a tool name + args + :class:`ToolContext`;
the registry enforces, in order:

1. **circuit breaker** — a tool tripped this turn is short-circuited;
2. **approval gate** — risky/always tools block on the user;
3. **concurrency limit** — terminal/web tools are serialized per
   class (a global sem per concurrency-class);
4. **execution** — the tool runs, output is capped before hitting the model.

Disabled toolsets are never imported (startup stays fast).
"""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass

from typing import Any, Callable

from ...core.bus import HookBus
from .schema import validation_error

from ...core.governance import ApprovalLevel, ApprovalManager
from .base import Tool, ToolContext, ToolResult

# Default output cap (chars) before truncation hits the model.
DEFAULT_OUTPUT_CAP = 16_000
# Per-toolset concurrency classes: tools in the same class serialize.
_CONCURRENCY_CLASSES: dict[str, str] = {
    "bash": "terminal",
    "web_search": "web",
    "web_extract": "web",
    "github": "net",
    "debug": "io",
    "lsp": "io",
    "ast_edit": "io",
    "ast_grep": "io",
    "browse": "browser",
    "screenshot": "browser",
    "eval": "cpu",
}


@dataclass
class _BreakerState:
    tripped: bool = False
    failures: int = 0
    reason: str = ""


class ToolRegistry:
    """Holds tools + per-toolset enable state + governance primitives."""

    def __init__(self, approvals: ApprovalManager, *, output_cap: int = DEFAULT_OUTPUT_CAP,
                 hooks: HookBus | None = None) -> None:
        self._tools: dict[str, Tool] = {}
        self._by_toolset: dict[str, list[str]] = {}
        self._enabled: dict[str, bool] = {}
        self._tool_enabled: dict[str, bool] = {}
        self._breakers: dict[str, _BreakerState] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self.approvals = approvals
        self.output_cap = output_cap
        self._failure_threshold = 3
        self.hooks = hooks or HookBus()

    # ---- registration ----

    def register(self, tool: Tool) -> Callable[[], None]:
        if not hasattr(tool, "name") or not tool.name:
            raise ValueError(f"tool missing name: {tool!r}")
        previous = self._tools.get(tool.name)
        if previous is not None:
            self.unregister(tool.name)
        self._tools[tool.name] = tool
        self._by_toolset.setdefault(tool.toolset, []).append(tool.name)
        self._enabled.setdefault(tool.toolset, True)
        disposed = False

        def dispose() -> None:
            nonlocal disposed
            if not disposed and self._tools.get(tool.name) is tool:
                disposed = True
                self.unregister(tool.name)

        return dispose

    def unregister(self, name: str) -> Tool | None:
        """Remove a tool from the registry (drop-in removal). Returns the tool
        if it was present, else ``None``."""
        tool = self._tools.pop(name, None)
        if tool is not None:
            names = self._by_toolset.get(tool.toolset)
            if names and name in names:
                names.remove(name)
        return tool

    def approval_of(self, name: str) -> str | None:
        """Declared approval level of a tool (``.value``), or ``None``."""
        tool = self._tools.get(name)
        if tool is not None:
            val = getattr(tool, "approval", None)
            return val.value if val is not None else None
        return None

    def enable(self, toolset: str, enabled: bool) -> None:
        self._enabled[toolset] = enabled

    def is_enabled(self, toolset: str) -> bool:
        return self._enabled.get(toolset, False)

    def enable_tool(self, name: str, enabled: bool) -> None:
        """Per-tool enable override (used for drop-ins). Overrides the toolset."""
        self._tool_enabled[name] = enabled

    def tool_enabled(self, name: str) -> bool:
        """Effective on/off for a single tool: per-tool override if set, else its
        toolset's enable state. Missing tools are off."""
        tool = self._tools.get(name)
        if tool is None:
            return False
        if name in self._tool_enabled:
            return self._tool_enabled[name]
        return self.is_enabled(tool.toolset)

    def names(self) -> list[str]:
        return list(self._tools)

    def schemas_for_model(self) -> list[dict[str, Any]]:
        """OpenAI-style tool schemas: ``{type:"function", function:{name,description,parameters}}``.

        A tool's ``schema`` field is the *parameters* JSON-Schema only; this wraps it
        in the OpenAI/Anthropic function envelope (Anthropic translation happens in
        the provider client)."""
        out: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if not self.tool_enabled(name):
                continue
            params = dict(tool.schema)
            params.setdefault("type", "object")
            params.setdefault("properties", {})
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": getattr(tool, "description", ""),
                        "parameters": params,
                    },
                }
            )
        return out

    def toolset_of(self, name: str) -> str | None:
        """The toolset a registered tool belongs to, or None."""
        for ts, names in self._by_toolset.items():
            if name in names:
                return ts
        return None

    def toolsets(self) -> list[dict[str, Any]]:
        """State-panel shape: [{toolset, enabled, tools:[...]}]."""
        return [
            {
                "toolset": ts,
                "enabled": self._enabled.get(ts, True),
                "tools": [
                    {"name": n, "description": getattr(self._tools[n], "description", "")}
                    for n in names
                ],
            }
            for ts, names in sorted(self._by_toolset.items())
        ]

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    # ---- governance ----

    def _sem(self, klass: str) -> asyncio.Semaphore:
        if klass not in self._sems:
            self._sems[klass] = asyncio.Semaphore(1)
        return self._sems[klass]

    def _cap(self, text: str) -> str:
        if len(text) <= self.output_cap:
            return text
        keep = self.output_cap - 64
        return f"{text[:keep]}\n…[truncated {len(text) - keep} chars]"

    def trip_breaker(self, tool_name: str, reason: str) -> None:
        st = self._breakers.setdefault(tool_name, _BreakerState())
        st.tripped = True
        st.reason = reason

    def reset_breakers(self) -> None:
        """New turn → clear per-turn breaker state (tripped tools are disabled for the turn only)."""
        self._breakers.clear()

    # ---- execution ----

    async def run(
        self, tool_name: str, args: dict[str, Any], ctx: ToolContext, *, emit
    ) -> ToolResult:
        """Run a tool under full governance. ``emit`` is the event emitter
        (``turn.tool`` updates: running → ok/error)."""
        tool = self._tools.get(tool_name)
        if tool is None:
            return ToolResult.err(f"unknown tool: {tool_name}")
        pre = await self.hooks.waterfall("tool.pre_execute", {
            "tool": tool_name, "args": args, "ctx": ctx,
        })
        if pre.get("handled"):
            result = pre.get("result")
            return result if isinstance(result, ToolResult) else ToolResult.err(
                str(pre.get("reason", "tool execution denied"))
            )
        if not self.tool_enabled(tool_name):
            why = (
                f"tool disabled: {tool_name}"
                if tool_name in self._tool_enabled
                else f"toolset disabled: {tool.toolset}"
            )
            return ToolResult.err(why)

        breaker = self._breakers.get(tool_name)
        if breaker is not None and breaker.tripped:
            return ToolResult.err(f"circuit breaker tripped: {breaker.reason}")

        # Approval gate.
        if tool.approval is not ApprovalLevel.NEVER:
            approved, reason = await self.approvals.request(
                tool_name, args, tool.approval, session_id=ctx.session_id
            )
            if not approved:
                # turn.approval_resolved is emitted by the shell RPC path
                # (approval.resolve) so every open tab learns the outcome.
                return ToolResult.err(f"denied: {reason or 'not approved'}")

        klass = _CONCURRENCY_CLASSES.get(tool_name)
        sem = self._sem(klass) if klass else None
        started = time.perf_counter()
        await emit(
            "turn.tool", tool=tool_name, args=_summarize_args(args), status="running"
        )
        try:
            if sem is not None:
                async with sem:
                    raw = await tool.run(args, ctx)
            else:
                raw = await tool.run(args, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — breaker + chip
            st = self._breakers.setdefault(tool_name, _BreakerState())
            st.failures += 1
            if st.failures >= self._failure_threshold:
                st.tripped = True
                st.reason = str(exc)
            elapsed = time.perf_counter() - started
            await emit(
                "turn.tool",
                tool=tool_name,
                args=_summarize_args(args),
                status="error",
                elapsed=round(elapsed, 3),
                output=self._cap(str(exc)),
            )
            return ToolResult.err(str(exc), raw=str(exc))

        elapsed = time.perf_counter() - started
        out = raw if isinstance(raw, ToolResult) else ToolResult.ok(str(raw))
        chip_output = out.raw if isinstance(out.raw, str) else None
        # A tool may tag its chip with extra fields (e.g. delegate's
        # subagent_run, so the shell can open that sub-agent's activity).
        extra = {k: v for k, v in out.meta.items()
                 if k in _CHIP_META and v is not None}
        await emit(
            "turn.tool",
            tool=tool_name,
            args=_summarize_args(args),
            status="ok",
            elapsed=round(elapsed, 3),
            output=self._cap(chip_output)[:2000] if chip_output else None,
            **extra,
        )
        output_schema = getattr(tool, "output_schema", None)
        if output_schema is not None:
            validation = validation_error(out.raw, output_schema)
            if validation:
                result = ToolResult.err(
                    f"invalid output from {tool_name}: {validation}",
                    raw={"validation_error": validation, "value": out.raw},
                )
                return result
        # Model-facing output is capped; chip keeps the capped slice too.
        result = ToolResult(
            output=self._cap(out.output),
            error=out.error,
            raw=out.raw,
            meta={**out.meta, "elapsed": round(elapsed, 3)},
        )
        post = await self.hooks.waterfall("tool.post_execute", {
            "tool": tool_name, "args": args, "ctx": ctx, "result": result,
        })
        candidate = post.get("result", result)
        return candidate if isinstance(candidate, ToolResult) else result


# Tool meta keys allowed to ride along on the turn.tool chip. `image` carries a
# data URL (show_image) — the shell renders it under the chip; the model only
# ever sees the tool's short text output.
_CHIP_META = {"subagent_run", "subagent", "image", "image_alt"}


# Args whose full value is the headline of a tool chip (file path, command
# line) — the shell renders these, so they get a longer leash.
_HEADLINE_ARGS = {"path", "command", "file", "program", "url", "pattern"}


def summarize_args(args: dict[str, Any]) -> str:
    """Compact one-line arg summary for the chip header.

    Also the only form of a tool call that gets persisted on a turn's step
    timeline — never the raw arguments, which for `write`/`edit` carry whole
    file bodies and would re-enter the prompt on the next session load.
    """
    parts = []
    for k, v in args.items():
        if k == "self":
            continue
        # `edit` takes only a patch, whose first line is `[path#TAG]` — surface
        # the path so the chip can render a file tab instead of patch guts.
        if k == "patch" and isinstance(v, str):
            head = _patch_path(v)
            if head:
                parts.append(f"path={_short(head, 120)}")
                continue
        parts.append(f"{k}={_short(v, 120 if k in _HEADLINE_ARGS else 40)}")
    return " ".join(parts)[:320]


def _patch_path(patch: str) -> str | None:
    """The file path from an edit patch's `[path#TAG]` header line."""
    m = re.match(r"\s*\[([^\]#]+)(?:#[^\]]*)?\]", patch)
    return m.group(1).strip() if m else None


# Back-compat alias for in-module callers.
_summarize_args = summarize_args


def _short(v: Any, limit: int = 40) -> str:
    s = repr(v) if not isinstance(v, str) else v
    if len(s) > limit:
        return s[: limit - 3] + "…"
    return s
