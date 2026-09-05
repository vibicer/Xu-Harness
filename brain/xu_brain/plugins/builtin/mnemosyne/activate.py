"""mnemosyne — long-term memory for Xu, backed by mnemosyne-memory.

What it does (only when enabled in Config → Plugins):

- ``before_llm`` hook: queries Mnemosyne with the latest user message and
  injects a capped digest block into the system prompt, so remembered facts
  reach the model without any tool call.
- ``mnemosyne_remember`` / ``mnemosyne_recall`` / ``mnemosyne_forget`` tools:
  the write path and explicit read path for the agent.

Dependency: ``pip install mnemosyne-memory`` (pure Python + PyYAML).
Without it the plugin logs one warning and stays inert — the brain boots fine.
Semantic (embedding) recall needs the optional ``[embeddings]`` extra
(fastembed, local ONNX); without it recall degrades to keyword/FTS only.

Storage: one SQLite file at ``<data_home>/mnemosyne/data`` (MNEMOSYNE_DATA_DIR).
Zero cloud, zero telemetry.
"""
from __future__ import annotations

from typing import Any

PLUGIN_DIR_KEY = "MNEMOSYNE_DATA_DIR"

INJECT_HEADER = "# long-term memory (mnemosyne)\nRelevant memories for this turn:"

# ---- tools ------------------------------------------------------------ #


class _MnemosyneTool:
    """remember/recall/forget wearing the local Tool protocol."""

    toolset = "mnemosyne"
    output_schema = None

    def __init__(self, name: str, description: str, schema: dict[str, Any],
                 fn: Any, approval: Any) -> None:
        self.name = name
        self.description = description
        self.schema = schema
        self.approval = approval
        self._fn = fn

    async def run(self, args: dict[str, Any], ctx: Any) -> Any:
        from xu_brain.features.tools.base import ToolResult

        try:
            out = self._fn(**(args or {}))
            return ToolResult.ok(str(out))
        except Exception as exc:  # noqa: BLE001 — tool errors are model-facing
            return ToolResult.err(f"mnemosyne failed: {exc}")


# ---- activation ------------------------------------------------------- #


def activate(ctx: Any) -> None:
    try:
        import mnemosyne  # noqa: F401
    except ImportError:
        ctx.log(
            "mnemosyne-memory is not installed in the brain env "
            "(pip install mnemosyne-memory) — plugin is inert",
            level="warning",
        )
        return

    from xu_brain.core.governance import ApprovalLevel

    data_home = getattr(ctx.app, "data_home", None)
    if data_home is not None:
        # Point Mnemosyne at Xu's data dir before anything imports it.
        import os

        os.environ.setdefault(PLUGIN_DIR_KEY, str(data_home / "mnemosyne" / "data"))

    from mnemosyne import forget, recall, remember

    def _conf(key: str, default: Any) -> Any:
        """Read live — Config → Plugins changes apply on the next turn."""
        return ctx.settings().get(key, default)

    def _digest(query: str) -> str | None:
        """Top-k recall rendered as a compact block, or None when empty."""
        try:
            hits = recall(query, top_k=max(1, int(_conf("top_k", 5))))
        except Exception as exc:  # noqa: BLE001 — never break the turn
            ctx.log(f"recall failed: {exc}", level="warning")
            return None
        if not hits:
            return None
        try:
            cap = max(200, int(_conf("max_chars", 2000)))
        except (TypeError, ValueError):
            cap = 2000
        lines = []
        used = 0
        for h in hits:
            line = f"- {str(h.get('content', ''))[:300]}"
            if used + len(line) > cap:
                break
            lines.append(line)
            used += len(line)
        return f"{INJECT_HEADER}\n" + "\n".join(lines) if lines else None

    def _latest_user_text(messages: list[dict[str, Any]]) -> str | None:
        for m in reversed(messages):
            if m.get("role") == "user":
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    return c
                if isinstance(c, list):  # multimodal parts; keep the text ones
                    text = " ".join(
                        p.get("text", "") for p in c
                        if isinstance(p, dict) and p.get("type") == "text"
                    ).strip()
                    if text:
                        return text
        return None

    def before_llm(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not _conf("inject", True) or not messages:
            return messages
            return messages
        query = _latest_user_text(messages)
        digest = _digest(query) if query else None
        if digest is None:
            return messages
        out = list(messages)
        for i, m in enumerate(out):
            if m.get("role") == "system":
                out[i] = {**m, "content": f"{m['content']}\n\n{digest}"}
                return out
        # No system row (shouldn't happen) — prepend one.
        return [{"role": "system", "content": digest}, *out]

    # The hook — enable/disable is enforced by the plugin bus wrapper.
    ctx.transform("before_llm", before_llm)

    # Tools — the write path and explicit read path.
    ctx.register_tool(_MnemosyneTool(
        "mnemosyne_remember",
        "Store a fact in long-term memory (Mnemosyne). Use for durable user "
        "preferences, decisions, project facts — not transient chatter.",
        {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "The fact to remember, self-contained."},
                "importance": {"type": "number", "description": "0.0-1.0, default 0.5."},
            },
            "required": ["content"],
        },
        lambda content, importance=0.5: (
            remember(content, importance=float(importance)) or "stored (filtered as noise)"
        ),
        ApprovalLevel.RISKY,
    ))
    ctx.register_tool(_MnemosyneTool(
        "mnemosyne_recall",
        "Search long-term memory (Mnemosyne) by query. Returns ranked memories.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "top_k": {"type": "integer", "description": "Default 5."},
            },
            "required": ["query"],
        },
        lambda query, top_k=5: recall(query, top_k=int(top_k)),
        ApprovalLevel.NEVER,
    ))
    ctx.register_tool(_MnemosyneTool(
        "mnemosyne_forget",
        "Delete one memory by its id (get ids from mnemosyne_recall).",
        {
            "type": "object",
            "properties": {"memory_id": {"type": "string"}},
            "required": ["memory_id"],
        },
        lambda memory_id: forget(memory_id),
        ApprovalLevel.ALWAYS,
    ))


def deactivate(ctx: Any) -> None:
    # Registrations are host-tracked and disposed automatically; nothing owns
    # a live connection (Mnemosyne opens per-call SQLite handles).
    pass
