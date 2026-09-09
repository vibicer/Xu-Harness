"""Built-in Mnemosyne long-term memory (no plugin).

One SQLite file at ``<data_home>/mnemosyne/data`` (``MNEMOSYNE_DATA_DIR``),
set by :func:`init` before the first import so every consumer (tools, the
``before_llm`` digest, auto-capture) shares one store.

Everything here is fail-soft: with the package absent ``available()`` is
``False`` and the accessors raise a clear error that tools surface to the
model, so the brain boots and runs fine without ``mnemosyne-memory``.

Dependency: ``pip install mnemosyne-memory`` (pure Python + PyYAML). Semantic
(embedding) recall needs the optional ``[embeddings]`` extra (fastembed, local
ONNX); without it recall degrades to keyword/FTS.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_DATA_DIR_KEY = "MNEMOSYNE_DATA_DIR"
_EMBED_OFF_KEY = "MNEMOSYNE_EMBEDDINGS_OFF"  # Mnemosyne re-reads this per call

INJECT_HEADER = "# long-term memory (mnemosyne)\nRelevant memories for this turn:"

_avail: bool | None = None


def init(data_home: Path | str | None) -> None:
    """Point Mnemosyne at Xu's data dir *before* its first import."""
    if data_home is None:
        return
    os.environ.setdefault(_DATA_DIR_KEY, str(Path(data_home) / "mnemosyne" / "data"))


def available() -> bool:
    """True when the mnemosyne package imports (cached)."""
    global _avail
    if _avail is None:
        try:
            import mnemosyne  # noqa: F401
            _avail = True
        except ImportError:
            _avail = False
    return _avail

_embed_off_applied = False  # only the toggle's own OFF may be undone by it


def apply_embeddings(enabled: bool) -> None:
    """Switch dense (embedding) recall on/off at runtime — no restart needed.

    OFF sets Mnemosyne's own kill-flag; ON removes it again *only if the
    toggle set it*, so a flag the user exported before boot is respected.
    """
    global _embed_off_applied
    if enabled:
        if _embed_off_applied:
            os.environ.pop(_EMBED_OFF_KEY, None)
            _embed_off_applied = False
    else:
        os.environ[_EMBED_OFF_KEY] = "1"
        _embed_off_applied = True


def embeddings_enabled() -> bool:
    """Env truth — mirrors Mnemosyne's own _is_disabled() check."""
    return not os.environ.get(_EMBED_OFF_KEY)


def embeddings_installed() -> bool:
    """True when the optional [embeddings] extra (fastembed + sqlite-vec) imports."""
    try:
        import fastembed  # noqa: F401
        import sqlite_vec  # noqa: F401
        return True
    except ImportError:
        return False


def _require() -> None:
    if not available():
        raise RuntimeError(
            "mnemosyne-memory is not installed in the brain env "
            "(pip install mnemosyne-memory)"
        )


def remember(content: str, *, importance: float = 0.5,
             metadata: dict[str, Any] | None = None) -> Any:
    _require()
    from mnemosyne import remember as _remember

    return _remember(content, importance=float(importance), metadata=metadata or {})


def recall(query: str, *, top_k: int = 5) -> list[Any]:
    _require()
    from mnemosyne import recall as _recall

    return _recall(query, top_k=int(top_k))


def forget(memory_id: str) -> Any:
    _require()
    from mnemosyne import forget as _forget

    return _forget(memory_id)


def _latest_user_text(messages: list[dict[str, Any]]) -> str | None:
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
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


def before_llm(messages: list[dict[str, Any]], config: Any = None) -> list[dict[str, Any]]:
    """``before_llm`` transform: inject a capped recall digest into the system prompt.

    ``config`` is the live Config object (``.get(key, default)``). Unavailable
    store, empty recall, and ``memory_mnemosyne_inject=False`` all pass the
    messages through unchanged. Fail-soft by design.
    """
    get = getattr(config, "get", None)
    if get is None or not get("memory_mnemosyne_inject", True) or not messages:
        return messages
    query = _latest_user_text(messages)
    if not query or not available():
        return messages
    try:
        hits = recall(query, top_k=max(1, int(get("memory_mnemosyne_top_k", 5))))
    except Exception:  # noqa: BLE001 — never break the turn
        return messages
    if not hits:
        return messages
    try:
        cap = max(200, int(get("memory_mnemosyne_max_chars", 2000)))
    except (TypeError, ValueError):
        cap = 2000
    lines: list[str] = []
    used = 0
    for h in hits:
        line = f"- {str(h.get('content', ''))[:300]}"
        if used + len(line) > cap:
            break
        lines.append(line)
        used += len(line)
    if not lines:
        return messages
    digest = INJECT_HEADER + "\n" + "\n".join(lines)
    out = list(messages)
    for i, m in enumerate(out):
        if m.get("role") == "system":
            out[i] = {**m, "content": f"{m['content']}\n\n{digest}"}
            return out
    # No system row (shouldn't happen) — prepend one.
    return [{"role": "system", "content": digest}, *out]


__all__ = [
"available", "init", "remember", "recall", "forget", "before_llm",
"INJECT_HEADER", "apply_embeddings", "embeddings_enabled",
"embeddings_installed",
]
