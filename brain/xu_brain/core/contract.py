"""Frozen wire contract.

Owns the JSON-RPC error type, the message-schema helpers (sanitize / trim /
cap for the activity log), and the quiet-method classification. The method
*registry* itself lives on the runtime App (``register`` / ``dispatch``); the
contexts (`PluginContext`, `ToolContext`) live in :mod:`xu_brain.api` and
:mod:`xu_brain.features.tools.base` respectively.
"""
from __future__ import annotations

import json
from typing import Any

# Polled status/heartbeat RPCs — logged at warn/error only, never on every
# success, or they'd flood the activity log (web polls these every ~5s).
_QUIET_RPC = {"app.status"}

# Keys whose values must never reach the activity log (credentials/tokens).
# Matched case-insensitively against dict keys at every nesting depth.
_SECRET_KEYS = frozenset({
    "key", "api_key", "apikey", "password", "passwd",
    "token", "secret", "authorization", "x-api-key", "bearer",
})

# Methods whose params/result carry large conversation payloads. Their
# ``messages`` field is collapsed to a marker so the modal stays readable.
_HEAVY_METHODS = frozenset({"session.send", "session.get"})

# Per-entry payload size cap (after JSON serialization). Beyond this we
# store a stub so a runaway session payload can't blow the 1000-entry buffer.
_PAYLOAD_MAX_BYTES = 64 * 1024


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


def _log_rpc_ok(method: str) -> bool:
    """Whether a successful RPC call is worth an activity-log entry."""
    return bool(method) and not method.startswith("logs.") and method not in _QUIET_RPC


def _sanitize(value):
    """Deep-walk ``value`` and redact values under credential-shaped keys."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(k, str) and k.lower() in _SECRET_KEYS:
                out[k] = "[REDACTED]"
            else:
                out[k] = _sanitize(v)
        return out
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(v) for v in value)
    return value


def _trim_heavy(method, value):
    """Collapse ``messages`` on heavy methods to a length marker."""
    if method not in _HEAVY_METHODS or not isinstance(value, dict):
        return value
    out = dict(value)
    msgs = out.get("messages")
    if isinstance(msgs, list):
        out["messages"] = f"<{len(msgs)} message(s) redacted>"
    return out


def _cap_payload(payload):
    """Keep serialized JSON of ``payload`` under the per-entry byte cap."""
    try:
        size = len(json.dumps(payload, default=str, ensure_ascii=False))
    except (TypeError, ValueError):
        size = 0
    if size <= _PAYLOAD_MAX_BYTES:
        return payload
    trimmed = dict(payload)
    for key in ("messages", "result", "error", "params"):
        if key in trimmed:
            trimmed[key] = "<truncated for size>"
            try:
                if len(json.dumps(trimmed, default=str, ensure_ascii=False)) <= _PAYLOAD_MAX_BYTES:
                    return trimmed
            except (TypeError, ValueError):
                pass
    return {"_truncated": True}


def _build_payload(method, params, result=None, error=None):
    """Assemble a sanitized, size-capped entry for the details modal."""
    if not method or method.startswith("logs."):
        return {}
    payload = {"params": _sanitize(_trim_heavy(method, params))}
    if error is not None:
        if isinstance(error, RpcError):
            payload["error"] = {
                "code": error.code,
                "message": str(error),
                "data": _sanitize(error.data),
            }
        else:
            payload["error"] = {"code": -32603, "message": str(error)}
    elif result is not None:
        payload["result"] = _sanitize(_trim_heavy(method, result))
    return _cap_payload(payload)


__all__ = [
    "RpcError",
    "_build_payload",
    "_cap_payload",
    "_log_rpc_ok",
    "_sanitize",
    "_trim_heavy",
]
