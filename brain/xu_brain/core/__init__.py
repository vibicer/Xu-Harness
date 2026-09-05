"""Frozen Core spine.

Exactly four pieces, none replaceable by an plugin:

- :mod:`xu_brain.core.runtime`    — boot → load plugins → serve WS → run turns.
- :mod:`xu_brain.core.governance` — ApprovalManager, breaker, caps, ALWAYS-gate.
- :mod:`xu_brain.core.contract`   — JSON-RPC registry, message schema, contexts.
- :mod:`xu_brain.core.host`       — loader + context factory (the hook bus
  itself lives in :mod:`xu_brain.core.bus`).

This package must never import from ``xu_brain.plugins`` — upstream stays
out. The old compatibility shims (``xu_brain.hooks``, ``xu_brain.approvals``,
``xu_brain.server``) are gone; their importers point straight at the frozen
modules here.
"""
from __future__ import annotations

from .bus import HookBus, HookHandle
from .contract import RpcError, _build_payload, _log_rpc_ok
from .governance import ApprovalManager, ApprovalPolicy
from .host import PluginHost

# Note: ``runtime`` (App / build_app / serve) is intentionally NOT re-exported
# here. Importing it eagerly would pull in ``agent.loop`` → ``core.governance``,
# and that cycle breaks the frozen-core boundary. The runtime is the *top* of
# the spine; callers import it directly from :mod:`xu_brain.core.runtime`.

__all__ = [
    "ApprovalManager",
    "ApprovalPolicy",
    "PluginHost",
    "HookBus",
    "HookHandle",
    "RpcError",
    "_build_payload",
    "_log_rpc_ok",
]
