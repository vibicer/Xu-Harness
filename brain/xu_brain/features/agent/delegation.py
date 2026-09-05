"""Sub-agent delegation — activity records + managed subagent runs.

A mixin aspect of :class:`~xu_brain.features.agent.loop.Agent`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from ...core.notify import notify


class DelegationMixin:

    # ---- delegation activity (sub-agent visibility) ----

    _MAX_RUNS = 60
    # Per-token persistence of the whole delegation blob was the dominant cost
    # of a delegated turn: ~5ms to serialize + ~1.5ms to write a 630KB file,
    # synchronously on the event loop, for EVERY streamed token — and the cost
    # grew with total history because all runs share one file. Deltas are
    # recoverable from the live stream, so the disk only needs the record at
    # step boundaries and terminal events; anything in between is throttled.
    _SAVE_INTERVAL = 1.0

    @property
    def _delegation_file(self) -> Path | None:
        """Resolved lazily: lightweight Agent() construction passes no data_home."""
        return self.data_home / "delegations.json" if self.data_home else None

    def _load_delegations(self) -> None:
        path = self._delegation_file
        if path is None:
            return
        try:
            raw = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        loaded = raw if isinstance(raw, dict) else {}
        for rec in loaded.values():
            # Child turns live in memory; a restart killed in-flight ones.
            if rec.get("status") == "running":
                rec["status"] = "interrupted"
        self._delegations.update(loaded)

    def _save_delegations(self, force: bool = False) -> None:
        path = self._delegation_file
        if path is None:
            return
        now = time.monotonic()
        if not force and (now - self._delegations_saved) < self._SAVE_INTERVAL:
            self._delegations_dirty = True
            return
        self._delegations_dirty = False
        self._delegations_saved = now
        self.data_home.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._delegations, ensure_ascii=False), "utf-8")
        tmp.replace(path)

    def begin_delegation(self, child: str, prompt: str, parent_session: str | None) -> str:
        """Open and persist an activity record before the child starts.

        The record carries the parent's live turn id as ``group``: siblings of
        one fan-out share it, which is what lets the shell show every concurrent
        sub-agent as a tab even when the model spent a single tool call on them.
        """
        run_id = f"d{uuid4().hex[:8]}"
        live = self._live_turns.get(parent_session or "") or {}
        self._delegations[run_id] = {
            "id": run_id, "child": child, "prompt": prompt,
            "parent_session": parent_session, "status": "running",
            "group": live.get("turn_id") or "",
            "started": time.time(), "finished": None, "result": "",
            "steps": [], "reasoning": "",
        }
        if len(self._delegations) > self._MAX_RUNS:
            for rid, rec in sorted(self._delegations.items(), key=lambda kv: kv[1]["started"]):
                if len(self._delegations) <= self._MAX_RUNS: break
                if rec["status"] != "running": self._delegations.pop(rid, None)
        self._save_delegations(force=True)
        asyncio.create_task(notify.emit("subagent.activity", session_id=parent_session,
                                     run=self._delegations[run_id]))
        return run_id

    def finish_delegation(self, run_id: str, result: str = "", status: str = "ok") -> None:
        rec = self._delegations.get(run_id)
        if rec is None:
            return
        if rec.get("status") != "running":
            if result and not rec.get("result"):
                rec["result"] = result
                self._save_delegations(force=True)
            return
        rec["status"] = status
        rec["result"] = result or ""
        rec["finished"] = time.time()
        self._save_delegations(force=True)

    def delegation_activity(self, run_id: str | None = None,
                            session_id: str | None = None,
                            group: str | None = None) -> list[dict[str, Any]]:
        """Return delegation runs owned by the requested parent session.

        ``group`` returns one fan-out's siblings in launch order — the whole
        record set the activity view tabs over. Filtering here keeps the wire
        payload to the runs actually shown; the full store spans every session.
        """
        if group:
            runs = [r for r in self._delegations.values()
                    if r.get("group") == group
                    and (session_id is None or r.get("parent_session") == session_id)]
            return sorted(runs, key=lambda r: r.get("started", 0))
        if run_id:
            rec = self._delegations.get(run_id)
            if rec is None or (session_id is not None and rec.get("parent_session") != session_id):
                return []
            return [rec]
        runs = [r for r in self._delegations.values()
                if session_id is None or r.get("parent_session") == session_id]
        return sorted(runs, key=lambda r: r.get("started", 0), reverse=True)

    def _inherit_parent_model(self, sid: str, node: Any, parent_ctx: Any) -> None:
        """Give a subtask's ephemeral session the parent's active model so
        'inherit session model' resolves to the parent turn's model, not a
        resolver fallback. No-op when the node pins its own model."""
        if node is not None and getattr(node, "model", None):
            return
        cfg = getattr(parent_ctx, "config", None) or {}
        model = cfg.get("model") if isinstance(cfg, dict) else None
        if not model:
            model = self._session_model(getattr(parent_ctx, "session_id", None))
        if model:
            self.config.set_session_model(sid, model)

    async def run_subtask(self, prompt: str, parent_ctx: Any, tools: list[str] | None = None,
                          node: Any = None, depth: int = 0, run_id: str | None = None) -> str:
        """Isolated child turn on a fresh ephemeral session; returns final text.

        ``node`` carries an orchestration-preset child config (its own persona,
        model, skills/tools scope) applied to the child's turn; ``depth`` feeds
        the delegation recursion cap and defaults the child's own roster.
        """
        ephemeral = self.sessions.create(cwd=parent_ctx.cwd)
        sid = ephemeral.id
        self._subtask_sessions.add(sid)
        self._inherit_parent_model(sid, node, parent_ctx)
        if run_id:
            # Route the child's turn events into its activity record.
            self._delegation_by_session[sid] = run_id
        fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._subtasks[sid] = fut
        try:
            await self.send(sid, prompt, node=node, depth=depth)
            text = await asyncio.shield(fut)
            if run_id:
                self.finish_delegation(run_id, text, "ok")
                rec = self._delegations.get(run_id)
                if rec is not None:
                    await notify.emit("subagent.activity", session_id=rec.get("parent_session"), run=rec)
            return text
        except BaseException as exc:
            # Salvage: if the child already resolved (it finished real work),
            # that result must not be discarded even when the parent's wait
            # times out or is cancelled. Without this, a cancelled parent eats
            # a completed child's work ("delegate stuck / subagent never ended").
            salvaged = fut.result() if (fut.done() and not fut.cancelled()) else None
            if salvaged is not None:
                if run_id:
                    self.finish_delegation(run_id, salvaged or "", "ok")
                    rec = self._delegations.get(run_id)
                    if rec is not None:
                        await notify.emit("subagent.activity", session_id=rec.get("parent_session"), run=rec)
            elif run_id:
                self.finish_delegation(run_id, "", "error")
                rec = self._delegations.get(run_id)
                if rec is not None:
                    await notify.emit("subagent.activity", session_id=rec.get("parent_session"), run=rec)
            # Hand salvaged work back only on a plain timeout, never on a hard
            # cancel (stop()/interrupt must propagate and abort the parent).
            if salvaged is not None and not isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
                return salvaged
            raise
        finally:
            self._subtasks.pop(sid, None)
            self._subtask_sessions.discard(sid)
            self._delegation_by_session.pop(sid, None)
            # On timeout/cancellation the child turn task is orphaned: cancel
            # and drain it BEFORE deleting its session, or it keeps streaming
            # from the provider and writing into a deleted session.
            task = self._turns.get(sid)
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(task, timeout=5.0)
            self.config.set_session_model(sid, None)
            self.sessions.delete(sid)

    async def spawn_managed_subagent(self, prompt: str, parent_ctx: Any, tools: list[str] | None = None,
                                     node: Any = None, depth: int = 0, label: str | None = None) -> str:
        """Spawn a subagent that keeps a durable id and returns immediately.

        The child runs on a fresh ephemeral session through ``send`` (which is
        non-blocking — it schedules the turn task and returns). The parent gets
        back a ``s…`` id immediately so it can ``subagent_list`` / message /
        interrupt instead of blocking like the fire-and-forget ``task`` tool.
        """
        if self.orchestrator is None:
            raise RuntimeError("orchestrator not wired")
        ephemeral = self.sessions.create(cwd=parent_ctx.cwd)
        cid = ephemeral.id
        self._subtask_sessions.add(cid)
        self._inherit_parent_model(cid, node, parent_ctx)
        sub = self.orchestrator.register_subagent(
            parent_session=parent_ctx.session_id, prompt=prompt, session_id=cid, label=label
        )
        # Also open a delegation record so the child's turn events stream into
        # the parent's live-activity panel (the wiring delegate children get in
        # run_subtask). Without it every subagent.activity emit for a
        # managed subagent carries run=None and the web store drops it.
        run_id = self.begin_delegation(label or "subagent", prompt, parent_ctx.session_id)
        self._delegation_by_session[cid] = run_id
        # Register the child session so the orchestrator can map id→session and
        # drive follow-ups / interrupts.
        fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._subtasks[cid] = fut
        timeout = parent_ctx.config.get("job_timeout")

        async def _track() -> None:
            # A bounded wait unless timeout is disabled.
            result = ""
            error: str | None = None
            interrupted = False
            try:
                if timeout is None:
                    await asyncio.shield(fut)
                else:
                    await asyncio.wait_for(asyncio.shield(fut), timeout=float(timeout))
                result = fut.result() if fut.done() and not fut.cancelled() else ""
            except asyncio.TimeoutError:
                if fut.done() and not fut.cancelled():
                    result = fut.result()
                else:
                    error = "subagent timed out"
            except asyncio.CancelledError:
                interrupted = True
            # Persist the child's result on the subagent record. If a parallel
            # interrupt already finished it, this is a guarded no-op.
            self.orchestrator.finish_subagent(
                sub.id, result or "", interrupted=interrupted, error=error,
            )
            # Mirror the terminal state onto the delegation record before the
            # final emit so the UI sees a finished run, not a live one.
            self.finish_delegation(
                run_id, result or "",
                "ok" if error is None and not interrupted
                else ("interrupted" if interrupted else "error"),
            )
            # Deliver the finished child's full result straight to the parent
            # session so the orchestrator reacts immediately — no subagent_list
            # poll, no timeout. An idle parent chains a fresh turn; a mid-turn
            # parent gets it queued and spliced at the next tool round. The
            # `[subagent result]` prefix marks it as a subagent report, not a
            # message the user typed.
            if sub.parent_session is not None:
                label = getattr(sub, "label", None) or sub.id
                notice = f"[subagent result] {label}: {result or error or 'interrupted'}"
                with contextlib.suppress(Exception):
                    await self.send(sub.parent_session, notice, depth=0)
            rec = self._delegations.get(self._delegation_by_session.get(cid, ""))
            if rec is not None:
                await notify.emit("subagent.activity", session_id=sub.parent_session, run=rec)
            # Clean up the ephemeral session and tracking entries.
            task = self._turns.get(cid)
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await asyncio.wait_for(task, timeout=5.0)
            self._subtasks.pop(cid, None)
            self._subtask_sessions.discard(cid)
            self._delegation_by_session.pop(cid, None)
            self.config.set_session_model(cid, None)
            self.sessions.delete(cid)

        # Fire the child turn first; if send fails the subagent is closed as an
        # error and cleaned up instead of orphaning a tracker on a dead future.
        try:
            await self.send(cid, prompt, node=node, depth=depth)
        except BaseException:
            self._subtasks.pop(cid, None)
            self._subtask_sessions.discard(cid)
            self._delegation_by_session.pop(cid, None)
            self.orchestrator.finish_subagent(sub.id, "", error="failed to start")
            self.finish_delegation(run_id, "", "error")
            self.sessions.delete(cid)
            raise
        asyncio.get_event_loop().create_task(_track())
        return sub.id

    async def subagent_message(self, sub_id: str, message: str, parent_session: str | None = None) -> bool:
        """Send a follow-up prompt to a running managed subagent the caller owns."""
        if self.orchestrator is None:
            raise RuntimeError("orchestrator not wired")
        sub = self.orchestrator.owned_subagent(sub_id, parent_session)
        if sub is None:
            raise LookupError(
                f"no managed subagent '{sub_id}' owned by this session. "
                "`delegate` children are single-turn and cannot receive "
                "follow-ups: put the full brief in the delegate prompt, or "
                "spawn with `delegate(background=true)` for a messageable id."
            )
        if sub.status != "running":
            raise RuntimeError(f"subagent '{sub_id}' is {sub.status}, not running")
        # TODO: multi-message child loop.
        raise RuntimeError(
            f"subagent '{sub_id}' is single-turn; follow-up messages are not "
            "supported yet. Re-delegate with the complete brief instead."
        )

    async def subagent_interrupt(self, sub_id: str, parent_session: str | None = None) -> bool:
        """Stop a running managed subagent the caller owns."""
        if self.orchestrator is None:
            return False
        sub = self.orchestrator.owned_subagent(sub_id, parent_session)
        if sub is None or sub.status != "running" or sub.session_id is None:
            return False
        await self.stop(sub.session_id)
        self.orchestrator.finish_subagent(sub_id, interrupted=True)
        return True

    def _claim_delegation(self, session_id: str, child: Any = None) -> dict[str, Any] | None:
        """The oldest running delegation of this parent no chip has claimed yet.

        Concurrent siblings all match on name, so picking the newest record (the
        old rule) handed every chip the same run and the rest of the sub-agents
        never appeared in the transcript.
        """
        claimed = {s.get("subagent_run") for s in self._claimed_runs(session_id)}
        name = str(child).strip() if child else ""
        runs = [r for r in self._delegations.values()
                if r.get("parent_session") == session_id
                and r.get("status") == "running"
                and r.get("id") not in claimed]
        named = [r for r in runs if r.get("child") == name] if name else []
        pool = named or ([] if name else runs)
        return min(pool, key=lambda r: r.get("started", 0)) if pool else None

    def _claimed_runs(self, session_id: str) -> list[dict[str, Any]]:
        """Live steps of the parent turn that already own a delegation run."""
        live = self._live_turns.get(session_id) or {}
        return [s for s in (live.get("steps") or [])
                if s.get("kind") == "tool" and s.get("subagent_run")]
