"""Context-window metering — usage estimate + ``context.updated`` emit.

A mixin aspect of :class:`~xu_brain.features.agent.loop.Agent`.
"""
from __future__ import annotations

import json
from typing import Any

from ...core.notify import notify
from ..session import Session
from .checkpoint import _COMPRESS_AT


class ContextMixin:
    def context_usage(
        self,
        session_id: str,
        *,
        messages: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Context-window estimate ``{context: pct, tokens, calibrated}``.

        Calibrated against the real API whenever possible: every provider
        call reports the actual ``prompt_tokens`` of the last request, so the
        estimate anchors on it and uses the ~4-chars/token surface ruler only
        for the *growth since that sample*. The anchor carries everything the
        heuristic cannot see (tool schemas, persona/skills envelope,
        tokenizer quirks) — the meter (and the compaction gate) read the real
        API state instead of a guess. Falls back to the surface heuristic,
        marked ``calibrated=False``, only before the session's first API
        sample.
        """
        if messages is None:
            session = self.sessions.get(session_id)
            messages = [m.to_dict() for m in session.messages] if session else []
        surface = self._surface_tokens(messages)
        ctx_len = max(int(self.config.get("context_length", 128_000)), 1)
        last = self._last_usage.get(session_id) or {}
        pressure = last.get("prompt_tokens")
        if pressure is not None:
            # `snapshot` is the surface measured with the same ruler at anchor
            # time, so the delta prices only growth since the sample. A
            # negative delta is legitimate (compaction landed after the
            # sample) and must shrink the estimate — clamp only at zero.
            snapshot = int(last.get("surface_at_sample", surface))
            est = max(int(pressure) + (surface - snapshot), 0)
            calibrated = True
        else:
            est = surface
            calibrated = False
        pct = min(int(est / ctx_len * 100), 100)
        return {"context": pct, "tokens": int(est), "calibrated": calibrated}

    async def _emit_context(
        self, session: Session, messages: list[dict[str, Any]], usage: dict[str, int] | None = None
    ) -> None:
        """Publish the context meter plus the provider-anchored companions.

        Three numbers, one of them the real one:

        - ``usage_pct`` / ``tokens`` — the meter and the compaction gate:
          the calibrated estimate (the provider's last real ``prompt_tokens``
          plus surface growth since it was sampled), or the surface heuristic
          before the first sample.
        - ``pressure`` — the last provider-reported ``prompt_tokens``: the
          real cost of the previous request, for display.
        - ``projected`` — the calibrated estimate again; kept for the
          pre-calibration UI chip, which hides it once ``calibrated`` is set
          (the meter already *is* the projected next-request cost).
        """
        # Anchor the provider sample together with the surface size at that
        # moment, so the delta below measures growth *since* the sample.
        if usage and "prompt_tokens" in usage:
            self._last_usage[session.id] = {
                **usage, "surface_at_sample": self._surface_tokens(messages),
            }
        est = self.context_usage(session.id, messages=messages)
        pressure = (self._last_usage.get(session.id) or {}).get("prompt_tokens")
        gate = float(self.config.get("compress_threshold", _COMPRESS_AT)) * 100
        await notify.emit(
            "context.updated",
            session_id=session.id,
            usage_pct=est["context"],
            compress=est["context"] >= gate,
            tokens=est["tokens"],
            calibrated=est["calibrated"],
            pressure=int(pressure) if pressure is not None else None,
            projected=est["tokens"] if est["calibrated"] else None,
        )

    @classmethod
    def _surface_tokens(cls, messages: list[dict[str, Any]]) -> int:
        """~4-chars/token estimate of the transcript surface — the *relative*
        ruler. It counts user/assistant/tool rows plus retained compaction
        checkpoints and skips the transient system block (persona/skills/
        memory): that block is rebuilt from scratch every turn, so between
        two samples of the same conversation it cancels and only the growth
        is measured. The ruler is identical for the live in-flight list and
        the persisted rows, so an estimate derived from ``session.messages``
        (state.get / Agent State panel) matches the one built mid-turn.
        Never used bare as the meter once a provider anchor exists — see
        :meth:`context_usage`."""
        return sum(len(json.dumps(m.get("content"), ensure_ascii=False)) // 4
                   for m in messages
                   if m.get("role") != "system" or cls._is_compaction_row(m))
