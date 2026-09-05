"""Session transcript compaction — summary engine + checkpoint rows.

A mixin aspect of :class:`~xu_brain.features.agent.loop.Agent`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any

from ...core.activity import activity
from ...core.notify import notify
from ..session import Message, Session
from .attachments import dereference as _dereference_images
from .checkpoint import (
    _BENIGN_COMPACTION_ERRORS,
    _CHECKPOINT_MARKER,
    _CHECKPOINT_PREAMBLE,
    _COMPACTION_INSTRUCTION,
    _COMPRESS_AT,
    _FAILURE_MARKER,
    _SUMMARY_CLOSE,
    _SUMMARY_OPEN,
)
from .provider import StopReason


def _prune_content(content: Any, *, head: int = 4096, tail: int = 1024, threshold: int = 8192) -> Any:
    """Model-free tool-result pruning.

    If ``str(content)`` exceeds ``threshold`` characters, keep ``head`` chars
    from the start and ``tail`` from the end, joined by a fixed omission
    marker. Any other content shape (lists/dicts/images) is returned unchanged
    — pruning is for the common text-heavy tool result, never multimodal parts.
    Head/marker/tail policy.
    """
    if isinstance(content, str) and len(content) > threshold:
        return content[:head] + "\n\n[... tool result middle pruned ...]\n\n" + content[-tail:]
    return content


class CompactionMixin:
    async def _compact_engine(
        self, session_id: str, msgs: list, *, stop: asyncio.Event | None = None,
        require_shrink: bool = True,
    ) -> dict[str, Any]:
        """Shared compaction core for auto (_maybe_compress) and manual
        (compress_session): one summarization path, one cut-point rule, one
        result contract.

        ksgs is the current message list (live dicts or persisted Message
        rows). Returns {ok, before, after, dropped, error?, summary, recent}
        where recent is the tail kept verbatim and summary the generated text.
        Callers assemble their own rewritten list from those two -- the live
        path keeps transient tool rows in place, the persisted path drops them.

        Retention is a fraction of the configured context window rather than a
        fixed row count, and summarization is a
        KV-cache-aware direct call: the *real* system prompt is replayed with
        the shadowed region verbatim and a compaction instruction appended as
        the final user message, so the provider reuses the warm prefix instead
        of treating it as a fresh prompt. When ``require_shrink`` (auto), a
        summary that does not reduce the surface is rejected and retried
        (rejected until it converges); manual ``compress_session`` passes False and always
        lands a balanced cut.
        """
        entries = [m.to_dict() if not isinstance(m, dict) else m for m in msgs]
        # Attachments are paths in the model context, never base64 (see
        # agent/attachments.py). The manual path hands us raw session rows, which
        # can still hold a data URL — summarizing that would ship the whole image
        # to the summarizer and write it straight back into the rewritten
        # history. No-op on the auto path, whose rows are already dereferenced.
        for e in entries:
            e["content"] = _dereference_images(e["content"], getattr(self, "data_home", None), session_id)
        # Model-free pruning of oversized tool results BEFORE range selection,
        # so a bloated result can relieve pressure without a summary round.
        pruned = [_prune_content(e["content"]) for e in entries]
        for e, c in zip(entries, pruned):
            e["content"] = c
        # `before` measures the SHRINKABLE surface only, same set `after`
        # prices below: prior <compacted-summary> checkpoints and failure
        # notes are transcript markers, not conversation the summary can
        # shrink. Counting them here (and not in `after`) made repeated
        # compactions structurally converge — by cycle 2-3 the metric reads
        # "no shrink" forever and compaction abandons.
        # Shrinkable surface: user/assistant/tool rows, excluding transcript
        # markers (checkpoints + failure notes). `before` and `after` MUST
        # measure this same set or repeated compactions structurally converge
        # to a false "no shrink" (see the `after` side below).
        def _surface_chars(rows):
            return sum(len(json.dumps(m["content"], ensure_ascii=False))
                       for m in rows
                       if m.get("role") in ("user", "assistant", "tool")
                       and not self._is_compaction_row(m))
        before = _surface_chars(entries)

        retention = self.config.get("retain_ratio", 0.16)
        ctx_len = int(self.config.get("context_length", 128_000))
        target_chars = int(ctx_len * float(retention))

        def cut_point() -> int:
            # Keep a recent tail that (a) fits the retention fraction of the
            # context window and (b) keeps at most a
            # third of the rows — so a small/char-light surface still compacts
            # a useful older span instead of never shrinking. Then snap forward
            # to a clean user-turn boundary so a tool round is never split (an
            # orphaned assistant ``tool_calls`` would 400 at the provider).
            budget = target_chars if target_chars > 0 else float("inf")
            max_kept = max(1, len(entries) // 3)
            start = 0
            acc = 0
            for i, m in enumerate(reversed(entries)):
                acc += len(json.dumps(m["content"], ensure_ascii=False))
                kept = i + 1
                if acc > budget or kept > max_kept:
                    start = len(entries) - kept
                    break
            # Snap forward to the next user boundary so the dropped prefix ends
            # cleanly before a fresh user turn.
            while start < len(entries) and entries[start].get("role") != "user":
                start += 1
            return start

        start = cut_point()
        if start >= len(entries):
            # The forward snap ran off the end: no user boundary exists in the
            # tail. That's the normal shape of a live tool-heavy turn (the auto
            # path passes the in-flight list, whose tail is all
            # assistant/tool rows) and taking `start` verbatim would leave an
            # empty `recent` — wiping the whole conversation instead of
            # compacting it. Snap backward to the last user row instead.
            start = next(
                (i for i in range(len(entries) - 1, 0, -1) if entries[i].get("role") == "user"),
                0,
            )
        if start == 0 or start >= len(entries) or len(entries) <= 4:
            return {"ok": False, "error": "not enough history to compress", "before": before, "after": before, "dropped": 0}
        older = entries[:start]
        recent = entries[start:]

        model = self._session_model(session_id)
        resolved = self.providers.resolve(model)
        if resolved is None:
            return {"ok": False, "error": "no provider/model for compression", "before": before, "after": before, "dropped": 0}
        cprovider, cmodel = resolved

        # KV-cache-aware summarization. Build a real context the way the loop
        # does (system prompt + shadowed messages verbatim, minus transient
        # rows), then append the compaction instruction as the final user turn.
        system = next((m["content"] for m in msgs if isinstance(m, dict) and m.get("role") == "system"), None)
        if not isinstance(system, str):
            # Fall back to the persona when the live list didn't carry it yet.
            try:
                persona = self.config.resolve_persona_text(session_id)
            except (AttributeError, TypeError):
                persona = None
            system = persona or "You are Xu, a concise personal AI."
        shadowed = [
            {"role": m["role"], "content": _prune_content(m["content"], head=4096, tail=1024, threshold=16384),
             **({"tool_call_id": m["tool_call_id"]} if m.get("tool_call_id") else {}),
             **({"tool_calls": m["tool_calls"]} if m.get("tool_calls") else {})}
            for m in older
            if m["role"] in ("user", "assistant", "tool")
        ]
        if not shadowed:
            return {"ok": False, "error": "not enough conversation to summarize", "before": before, "after": before, "dropped": 0}
        summary_msgs = [
            {"role": "system", "content": system},
            *shadowed,
            {"role": "user", "content": _COMPACTION_INSTRUCTION},
        ]

        retries = int(self.config.get("compaction_retries", 1))
        # Cap the summarizer so a runaway checkpoint can't itself blow the
        # window, and so a `length` finish is a detectable truncation instead of
        # a silently half-written checkpoint.
        summary_cap = int(self.config.get("compaction_max_tokens", 8192))
        text = ""
        attempt = 0
        while True:
            summary: list[str] = []
            truncated = False
            try:
                async for ev in self.providers.chat_stream(
                    cprovider, cmodel, summary_msgs, max_tokens=summary_cap, signal=stop
                ):
                    if ev.delta:
                        summary.append(ev.delta)
                    # getattr: a partial/duck-typed event (or a provider that
                    # never sets a finish reason) must not abort the stream.
                    if getattr(ev, "stop_reason", None) is StopReason.LENGTH:
                        truncated = True
            except Exception:  # noqa: BLE001 -- fail-soft compression
                return {"ok": False, "error": "compression failed", "before": before, "after": before, "dropped": 0}
            text = "".join(summary).strip()
            if not text:
                return {"ok": False, "error": "compression returned empty summary", "before": before, "after": before, "dropped": 0}
            # Fail closed on a truncated checkpoint: landing one makes the cut
            # permanent, so a summary that ran out of tokens mid-section would
            # silently lose the resume action forever. Keyed on the provider's
            # own `length` finish rather than on section presence — a weaker
            # model that ignores the schema still produces a usable summary,
            # and hard-failing on it would break compaction outright.
            if truncated:
                attempt += 1
                if attempt > retries:
                    return {"ok": False,
                            "error": "compaction summary truncated at the token cap (incomplete checkpoint)",
                            "before": before, "after": before, "dropped": 0}
                continue
            # Shrink validation: a summary that doesn't reduce the surface is
            # useless — retry a bounded number of times.
            # Manual compaction always lands its cut (require_shrink=False).
            after = _surface_chars(recent) + len(text)
            if not require_shrink or after < before:
                break
            attempt += 1
            if attempt > retries:
                return {"ok": False, "error": "compaction did not shrink; abandoning", "before": before, "after": after, "dropped": 0}

        return {"ok": True, "before": before, "after": after, "dropped": len(older), "summary": text, "recent": recent}

    @staticmethod
    def _is_checkpoint(msg: Any) -> bool:
        """Whether a message row is a landed compaction checkpoint."""
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        return isinstance(content, str) and _SUMMARY_OPEN in content

    @staticmethod
    def _is_failure_note(msg: Any) -> bool:
        """Whether a message row is a compaction-failure transcript note."""
        content = msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", None)
        return isinstance(content, str) and content.startswith(_FAILURE_MARKER)

    @classmethod
    def _is_compaction_row(cls, msg: Any) -> bool:
        """Either transcript marker — the rows the chat renders as dividers."""
        return cls._is_checkpoint(msg) or cls._is_failure_note(msg)

    @staticmethod
    def _frame_summary(summary: str) -> str:
        """Wrap a raw summary in the durable checkpoint framing: a preamble that
        tells the *resuming* model to treat this as established background, then
        the tagged body."""
        return (
            f"{_CHECKPOINT_MARKER}\n{_CHECKPOINT_PREAMBLE}\n\n"
            f"{_SUMMARY_OPEN}\n{summary}\n{_SUMMARY_CLOSE}"
        )

    @staticmethod
    def _compaction_step(**fields: Any) -> dict[str, Any]:
        """The `steps` payload the shell reads to render a compaction divider.
        Carried on the row itself so it survives a reload and a session switch —
        an event would only reach a client that happened to be watching."""
        return {"kind": "compaction", "ts": time.time(), **fields}

    def _note_compaction_failure(self, session_id: str, mode: str, error: str) -> None:
        """Append (or coalesce) a transcript note for a compaction that did not
        land. Auto-compaction retries every turn while over threshold, so a
        persistent provider error is collapsed onto one row with a count instead
        of burying the conversation under one row per turn.

        Best-effort by construction: this row is display-only, so a store that
        can't take it must never turn a benign compaction miss into a failed
        turn. The caller's real signal is the activity log."""
        try:
            session = self.sessions.get(session_id)
            if session is None:
                return
            last = session.messages[-1] if session.messages else None
            if last is not None and self._is_failure_note(last):
                step = next((s for s in (last.steps or []) if s.get("kind") == "compaction"), None)
                if step is not None and step.get("error") == error:
                    step["count"] = int(step.get("count") or 1) + 1
                    step["ts"] = time.time()
                    self.sessions.replace_messages(session_id, list(session.messages))
                    return
            self.sessions.append(session_id, Message(
                role="system",
                content=f"{_FAILURE_MARKER} {error}",
                steps=[self._compaction_step(ok=False, mode=mode, error=error, count=1)],
            ))
        except Exception as exc:  # noqa: BLE001 -- transcript note is best-effort
            activity.record("warn", "brain",
                            f"could not record compaction failure · session {session_id}",
                            f"{type(exc).__name__}: {exc}")

    async def _report_compaction(self, session_id: str, mode: str, r: dict[str, Any]) -> None:
        """One outcome sink for both compaction paths: activity log always, plus
        a durable transcript note on real failure."""
        if r.get("ok"):
            activity.record("info", "brain",
                            f"context compacted · session {session_id}",
                            f"mode={mode} {r.get('before')}→{r.get('after')} chars, "
                            f"{r.get('dropped')} message(s) merged")
            return
        error = str(r.get("error") or "compaction failed")
        if error in _BENIGN_COMPACTION_ERRORS:
            activity.record("debug", "brain",
                            f"compaction skipped · session {session_id}", f"mode={mode} {error}")
            return
        activity.record("error", "brain",
                        f"compaction failed · session {session_id}", f"mode={mode} {error}")
        self._note_compaction_failure(session_id, mode, error)
        with contextlib.suppress(Exception):
            await notify.emit("session.updated", session_id=session_id)

    @classmethod
    def _persisted_msgs(cls, recent: list[dict], summary: str,
                        stats: dict[str, Any] | None = None) -> list[Message]:
        """Rebuild persisted rows from a compacted tail + summary.
        Tool rows are dropped (they can't be rebuilt without their assistant
        `tool_calls`, which would 400 as orphan tool messages). A prior
        checkpoint surviving in the tail is dropped too: the new summary already
        merged it, so keeping both is how a session accumulated several
        overlapping summary rows. Stale failure notes go with them: the
        compaction they reported on has now succeeded."""
        return [
            # ts/steps are coalesced because the auto path passes *live* dicts,
            # which carry neither key — `m.get("ts")` returning None would
            # override Message's 0.0 default and blow the NOT NULL constraint
            # on messages.ts inside replace_messages (manual compaction never
            # hit it: persisted rows always have a ts).
            Message(role=m["role"], content=m["content"], tool_call_id=m.get("tool_call_id"),
                    tool_name=m.get("tool_name"),
                    reasoning=m.get("reasoning") or m.get("reasoning_content") or "",
                    reasoning_signature=m.get("reasoning_signature") or "",
                    steps=m.get("steps") or [], ts=m.get("ts") or 0.0)
            for m in recent
            if m.get("role") in ("user", "assistant") and not cls._is_compaction_row(m)
        ] + [
            # Checkpoint lands AFTER the retained tail: chronologically the
            # compaction happened *now*, so the divider enters as the latest
            # transcript row and ages upward like any other message instead of
            # sitting pinned above the whole chat.
            Message(role="system", content=cls._frame_summary(summary),
                    steps=[stats] if stats else [])
        ]

    async def _maybe_compress(
        self, session: Session, messages: list[dict[str, Any]], provider, model, stop: asyncio.Event
    ) -> None:
        threshold = float(self.config.get("compress_threshold", _COMPRESS_AT))
        # The same calibrated number the meter shows: the provider's real
        # prompt_tokens + surface growth since the sample when an API sample
        # exists, the surface heuristic before the first call. Gating on the
        # heuristic alone is what made the threshold blind to tool schemas
        # and tokenizer quirks.
        est_pct = self.context_usage(session.id, messages=messages)["context"]
        if est_pct < threshold * 100:
            return
        await notify.emit("compaction.started", session_id=session.id)
        r = await self._compact_engine(session.id, messages, stop=stop)
        await notify.emit("compaction.done", session_id=session.id, **r)
        await self._report_compaction(session.id, "auto", r)
        if not r.get("ok"):
            return
        # Compact the current turn in place for its remaining tool rounds.
        # Prior checkpoints are excluded from both the kept system rows and the
        # tail: a landed checkpoint is itself a system row, so keeping it would
        # stack the new summary beside the old one (the new summary already
        # merged it, per the compaction instruction).
        system = [m for m in messages
                  if m.get("role") == "system" and not self._is_compaction_row(m)]
        summary_msg = {"role": "system", "content": self._frame_summary(r["summary"])}
        tail = [m for m in r["recent"] if not self._is_compaction_row(m)]
        messages[:] = system + tail + [summary_msg]
        # Persist so a crash still keeps the compaction AND the next turn starts
        # from it; _commit_history overwrites with the full post-turn list at
        # end of turn, so this write can't duplicate rows.
        retained = len([m for m in r["recent"]
                        if m.get("role") in ("user", "assistant")])
        stats = self._compaction_step(ok=True, mode="auto", before=r["before"],
                                      after=r["after"], dropped=r["dropped"],
                                      retained=retained)
        rows = self._persisted_msgs(r["recent"], r["summary"], stats)
        self.sessions.replace_messages(session.id, rows)
        # The divider is a new row the frontend has never seen: append it to the
        # display transcript (the retained tail is already there).
        self.sessions.add_display(session.id, rows[-1:])
        await notify.emit("session.updated", session_id=session.id)
        # Refresh the meter now — otherwise the UI keeps showing the
        # pre-compaction percentage until the next provider call lands.
        await self._emit_context(session, messages)

    async def compress_session(self, session_id: str) -> dict[str, Any]:
        """Manual context compression (Config/Agent-State "compress now").

        Shares the auto-compaction engine: same cut-point rule, same summarizer,
        same ok/error contract returned to the frontend. No-op on a too-small
        session or provider failure.
        """
        session = self.sessions.get(session_id)
        if session is None:
            return {"ok": False, "error": "session not found"}
        await notify.emit("compaction.started", session_id=session_id)
        r = await self._compact_engine(session_id, session.messages, require_shrink=False)
        await notify.emit("compaction.done", session_id=session_id, **r)
        await self._report_compaction(session_id, "manual", r)
        if not r.get("ok"):
            return r
        retained = len([m for m in r["recent"]
                        if m.get("role") in ("user", "assistant")])
        stats = self._compaction_step(ok=True, mode="manual", before=r["before"],
                                      after=r["after"], dropped=r["dropped"],
                                      retained=retained)
        rows = self._persisted_msgs(r["recent"], r["summary"], stats)
        self.sessions.replace_messages(session_id, rows)
        self.sessions.add_display(session_id, rows[-1:])  # divider row only
        await notify.emit("session.updated", session_id=session_id)
        # refresh the context meter through the same calibrated emitter
        # (anchor + post-compaction surface delta), not a third ruler —
        # the meter must actually show the shrink.
        await self._emit_context(session, [m.to_dict() for m in rows])
        return {"ok": True, "before": r["before"], "after": r["after"], "dropped": r["dropped"]}
