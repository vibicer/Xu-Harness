"""Tool-use agent loop.

One turn per ``session.send``: streams the provider response, dispatches tool
calls through the :class:`ToolRegistry` (governed), feeds results back, repeats
until the model stops with text. Emits ``turn.*`` events. Cooperative stop via
``stop()`` (cancels the turn task). Auto context compression at the configured
threshold. Skills progressive-disclosure + keyword auto-match inject loaded
skill bodies into the system prompt.

Subagent support: :meth:`run_subtask` runs an isolated child turn (no `task`
tool — prevents runaway recursion) returning the final text.
:meth:`await_user_reply` blocks on a user answer (the ``ask`` tool), surfacing
a ``turn.approval``-style prompt the shell renders.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import json
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING


from uuid import uuid4

from ...core.activity import activity
from ...core.config import Config
from ...core.bus import HookBus
from ...core.contract import RpcError
from ...core.governance import ApprovalManager
from ...core.notify import notify
from ..memory import MemoryStore
from .orchestrator import Orchestrator
from ..tools.registry import ToolRegistry
from ..session import Message, Session, SessionStore
from ..skills import SkillsEngine
from ..agent.provider import ProviderManager, StopReason
from .scheduler import run_bounded
from .attachments import _image_parts
from .attachments import dereference as _dereference_images
from .attachments import inject as _inject_images
from .attachments import strip as _strip_images
from .checkpoint import _CHECKPOINT_MARKER
from .compaction import CompactionMixin
from .context import ContextMixin
from .delegation import DelegationMixin
from .live import LiveMixin
from .prompt import PromptMixin

if TYPE_CHECKING:
    from ...plugins import PluginBus


@dataclass
class TurnResult:
    """Final assistant output plus the interleaved display timeline, and the
    turn's real assistant/tool message rows to persist for the next turn."""
    text: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    history: list[Message] = field(default_factory=list)
    queue_splits: list[int] = field(default_factory=list)


class Agent(DelegationMixin, LiveMixin, ContextMixin, PromptMixin, CompactionMixin):
    def __init__(
        self,
        sessions: SessionStore,
        data_home: Path,
        providers: ProviderManager,
        registry: ToolRegistry,
        approvals: ApprovalManager,
        memory: MemoryStore,
        skills: SkillsEngine,
        flat_plugins: PluginBus,
        config: Config,
        hooks: HookBus | None = None,
    ) -> None:
        self.sessions = sessions
        self.data_home = data_home
        self.providers = providers
        self.registry = registry
        self.approvals = approvals
        self.memory = memory
        self.skills = skills
        self.flat_plugins = flat_plugins
        self.hooks = hooks or getattr(registry, "hooks", HookBus())
        self.config = config
        self.presets = None
        self.orchestrator: Orchestrator | None = None
        self._turns: dict[str, asyncio.Task[None]] = {}
        self._stop_events: dict[str, asyncio.Event] = {}
        self._subtasks: dict[str, asyncio.Future[str]] = {}
        # Sessions spawned by run_subtask — their turns get the recursion guard.
        self._subtask_sessions: set[str] = set()
        self._replies: dict[str, asyncio.Future[str]] = {}
        # Owning session per pending `ask` reply, so stop() only settles its own.
        self._reply_sessions: dict[str, str | None] = {}
        self._reasoning: dict[str, str] = {}
        self._reasoning_sigs: dict[str, str] = {}
        # In-flight turn snapshot per session (steps + reasoning) so a
        # (re)connecting shell can resume a streaming draft via session.get.
        self._live_turns: dict[str, dict[str, Any]] = {}
        # Delegation runs: run_id -> activity record (child name, prompt,
        # status, mirrored steps) so the shell can show what a sub-agent did.
        # Throttle state for _save_delegations (see _SAVE_INTERVAL).
        self._delegations_saved = 0.0
        self._delegations_dirty = False
        self._delegations: dict[str, dict[str, Any]] = {}
        self._load_delegations()
        # child ephemeral session id -> run_id (routes the child's events).
        self._delegation_by_session: dict[str, str] = {}
        # Provenance: last-built (model-visible) system prompt fingerprint per
        # session. Lets callers check whether what the model last saw can be
        # reconstructed from the persisted store (P3: model-visible = logged).
        self._last_sent_fp: dict[str, str] = {}
        # Last provider-reported usage per session: the
        # raw OpenAI {prompt_tokens, total_tokens} anchor, reused for the next
        # context % / compaction gate until a newer usage replaces it.
        self._last_usage: dict[str, dict[str, int]] = {}
        # Queued user messages per session: sent while a turn was running,
        # flushed between tool rounds (the model sees them mid-turn) and
        # chained as a fresh turn after the current one ends. Entries carry
        # a short id so the UI can cancel a specific queued message.
        self._queued: dict[str, list[tuple[str, str, list[str] | None]]] = {}

    # ---- public ----

    async def stop(self, session_id: str) -> None:
        ev = self._stop_events.get(session_id)
        if ev:
            ev.set()
        task = self._turns.get(session_id)
        # cancelling() > 0 = a cancel is already in flight (rapid stop/steer
        # double-click): a second cancel would land inside the turn's finally
        # and corrupt the _chain_queued handoff (popped row lost, rest of the
        # queue orphaned). One cancel per turn.
        if task and not task.done() and task.cancelling() == 0:
            task.cancel()
        # Scope the cleanup to THIS session: cancelling every pending approval
        # / ask would deny or blank another session's in-flight turn.
        await self.approvals.cancel_for_session(session_id)
        # Unblock any in-flight `ask` for this session so its turn doesn't
        # hang 600s post-stop.
        for rid, fut in list(self._replies.items()):
            if self._reply_sessions.get(rid) == session_id:
                if not fut.done():
                    fut.set_result("")
                self._replies.pop(rid, None)
                self._reply_sessions.pop(rid, None)

    # Delegation, live-draft streaming, context metering, prompt assembly and
    # compaction live in the sibling mixin modules; only the turn loop and its
    # queue/reply plumbing are below.

    async def send(self, session_id: str, text: str, images: list[str] | None = None,
                   node: Any = None, depth: int = 0) -> tuple[str, str | None]:
        """Send a user message; returns ``(turn_id, queued_id)``.

        A mid-turn send is queued instead of starting a turn: it returns
        ``("", queued_id)`` where ``queued_id`` lets the UI cancel that
        specific queued message. A normal send returns ``(turn_id, None)``."""
        session = self.sessions.get(session_id)
        if session is None:
            raise RpcError(-32002, "session not found", {"session_id": session_id})
        if session_id in self._turns and not self._turns[session_id].done():
            # Queue-send: a message arriving mid-turn joins a queue instead of
            # erroring. It's flushed between tool rounds (the running model
            # sees it in the very next request) and any remainder chains as a
            # fresh turn when the current one ends. Queued sends belong to the
            # user's turn, never to a subagent child turn (depth > 0), which
            # must stay invisible to the parent conversation.
            if depth > 0:

                raise RpcError(-32004, "turn in progress", {"session_id": session_id})
            queued_id = uuid4().hex[:6]
            self._queued.setdefault(session_id, []).append((queued_id, text, images or None))
            activity.record("info", "brain", f"message queued · session {session_id}")
            await self._emit_for(session_id, "", "turn.queue",
                                 queued_id=queued_id, text=text)
            await self._emit_for(session_id, "", "turn.notice",
                                 text="message queued — it will reach Xu at the next opportunity",
                                 queued_id=queued_id)
            return "", queued_id
        # A node is the active orchestration-preset root for this session
        # unless an explicit (child) node was passed by the delegating caller.
        if node is None:
            node = self._root_node(session_id)
        # Multimodal turn: OpenAI content-parts list carrying text + images.
        content: Any = text if not images else [{"type": "text", "text": text}] + _image_parts(images)
        self.sessions.append(session_id, Message(role="user", content=content))
        turn_id = f"t{uuid4().hex[:6]}"
        stop = asyncio.Event()
        self._stop_events[session_id] = stop
        task = asyncio.create_task(self._run(session, turn_id, text, stop, images=images, node=node, depth=depth))
        self._turns[session_id] = task
        return turn_id, None

    async def await_user_reply(
        self,
        request_id: str,
        question: str,
        options: list | None,
        *,
        session_id: str | None = None,
    ) -> str:
        fut: asyncio.Future[str] = asyncio.get_event_loop().create_future()
        self._replies[request_id] = fut
        self._reply_sessions[request_id] = session_id
        payload = {
            "request_id": request_id,
            "tool": "ask",
            "args": {"question": question, "options": options or []},
            "reason": "clarification requested by agent",
        }
        if session_id is not None:
            payload["session_id"] = session_id
        await notify.emit("turn.approval", **payload)
        try:
            return await asyncio.wait_for(fut, timeout=600.0)
        except asyncio.TimeoutError:
            return ""
        finally:
            self._replies.pop(request_id, None)
            self._reply_sessions.pop(request_id, None)

    def resolve_reply(self, request_id: str, answer: str) -> bool:
        fut = self._replies.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(answer)
        return True

    def reply_session(self, request_id: str) -> str | None:
        """Session whose ask owns this pending reply (for event tagging)."""
        return self._reply_sessions.get(request_id)

    # ---- queue-send ----

    def _drain_queue(self, session_id: str, messages: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        """Splice queued user messages into the LIVE message list mid-turn.

        Called between tool rounds: the queued rows land as user messages
        right after the last tool result, so the model sees them in the next
        request without ending the turn. Returns the list of drained items as
        {queued_id, text, images} (empty list when nothing drained)."""
        queued = self._queued.get(session_id) or []
        if not queued or messages is None:
            return []
        take = queued
        self._queued[session_id] = []
        drained: list[dict[str, Any]] = []
        for _qid, text, images in take:
            # Same rule as the persisted rows (see prompt._build_messages): the
            # image goes to disk, the model gets its path. Queued rows are
            # spliced straight into the live list, so they never pass through
            # `_build_messages` and need dereferencing here.
            content: Any = text if not images else _dereference_images(
                [{"type": "text", "text": text}] + _image_parts(images), self.data_home, session_id)
            messages.append({"role": "user", "content": content})
            drained.append({"queued_id": _qid, "text": text, "images": images})
        return drained

    async def _chain_queued(self, session_id: str) -> None:
        """After a turn ends, start a fresh turn for any messages that never
        got spliced (queued after the last tool round). Depth guard: only a
        root turn chains; subagent child turns never do."""
        queued = self._queued.get(session_id) or []
        if not queued:
            return
        _qid, text, images = queued[0]
        self._queued[session_id] = queued[1:]
        try:
            await self.send(session_id, text, images=images, depth=0)
        except Exception:  # noqa: BLE001 — chaining is best-effort
            activity.record("warn", "brain", f"queued send failed · session {session_id}")

    def cancel_queued(self, session_id: str, queued_id: str) -> bool:
        """Remove a specific queued (not yet flushed) message by id.

        Returns False if the id is unknown — the message already got spliced
        into the live turn (or the session has no queue), so it can no longer
        be cancelled: it reached the model."""
        queued = self._queued.get(session_id) or []
        for i, (qid, _text, _images) in enumerate(queued):
            if qid == queued_id:
                del queued[i]
                return True
        return False

    def steer_queued(self, session_id: str, queued_id: str) -> bool:
        """Promote a queued message to the front of the queue.

        Paired with ``stop()`` by ``session.queue.steer``: cancelling the
        running turn makes the turn's ``finally`` (``_chain_queued``) start the
        steered message immediately as a fresh turn, ahead of the rest of the
        queue. Returns False if the id is unknown — it was already spliced into
        the live turn (it reached the model) and can no longer be steered."""
        queued = self._queued.get(session_id) or []
        idx = next((i for i, (qid, _t, _im) in enumerate(queued) if qid == queued_id), -1)
        if idx < 0:
            return False
        if idx > 0:
            queued.insert(0, queued.pop(idx))
        return True

    # ---- turn execution ----

    async def _run(self, session: Session, turn_id: str, text: str, stop: asyncio.Event,
                   images: list[str] | None = None, node: Any = None, depth: int = 0) -> None:
        model = node.model if (node is not None and node.model) else self._session_model(session.id)
        started = time.perf_counter()
        await notify.emit("turn.started", turn_id=turn_id, session_id=session.id, model=model)
        await self.hooks.emit("turn.start", {"turn_id": turn_id, "session_id": session.id, "model": model})
        self._live_turns[session.id] = {"turn_id": turn_id, "steps": [], "reasoning": ""}
        activity.record("debug", "brain", f"turn started · session {session.id}", f"model={model}")
        final_text = ""
        try:
            await self.flat_plugins.on_start(self._ctx(session, turn_id, model, node=node, depth=depth))
            final = await self._loop(session, turn_id, stop, images, node=node, depth=depth)
            if stop.is_set():
                self._persist_live_turn(session, status="interrupted")
            elif final is not None:
                final_text = await self.flat_plugins.on_message_out(final.text) or ""
                self._commit_history(session, final, final_text)
            await self._emit_for(session.id, turn_id, "turn.finished", stop_reason="stop")
            activity.record(
                "info", "brain",
                _turn_summary(session, model, time.perf_counter() - started),
            )
            # complete any waiting subtask future with the final text
            sub = self._subtasks.get(session.id)
            if sub and not sub.done():
                sub.set_result(final_text)
        except asyncio.CancelledError:
            self._persist_live_turn(session, status="interrupted")
            await self._emit_for(session.id, turn_id, "turn.failed", error="stopped")
            activity.record("warn", "brain", f"turn stopped · session {session.id}")
            sub = self._subtasks.get(session.id)
            if sub and not sub.done():
                sub.set_result("")
            raise
        except Exception as exc:  # noqa: BLE001
            self._persist_live_turn(session, status="failed")  # not "done": a crashed turn must not read as healthy
            await self._emit_for(session.id, turn_id, "turn.failed", error=str(exc))
            activity.record("error", "brain", f"turn failed · session {session.id}", str(exc))
            sub = self._subtasks.get(session.id)
            if sub and not sub.done():
                sub.set_result(f"subagent error: {exc}")
        finally:
            # Fallback: if the turn escaped every handler (e.g. a
            # BaseException such as SystemExit/KeyboardInterrupt skipped the
            # except clauses above), resolve any waiting subtask future so its
            # tracker is never stranded in "running" forever.
            sub = self._subtasks.get(session.id)
            if sub and not sub.done():
                sub.set_result("")
            self._live_turns.pop(session.id, None)
            self._turns.pop(session.id, None)
            self._stop_events.pop(session.id, None)
            self._reasoning.pop(turn_id, None)
            self._reasoning_sigs.pop(turn_id, None)
            # queue-send: chain any messages queued after the last tool round
            # as a fresh turn (root turns only — a subagent's child turn must
            # not hijack the parent's conversation).
            if depth == 0:
                await self._chain_queued(session.id)

    @staticmethod
    def _anchor_notes(session: Session) -> list[tuple[int, Message]]:
        """Snapshot compaction-failure notes with their position, expressed as
        the count of non-system rows preceding them.

        These notes are `system` rows that `_build_messages` deliberately does
        not replay (the model must never see them), so they are absent from
        ``result.history`` and a plain rewrite would erase them. The anchor is
        the non-system row count rather than an absolute index because the
        rebuilt list is longer — it now includes this turn's tool rounds."""
        notes: list[tuple[int, Message]] = []
        seen = 0
        for m in session.messages:
            if m.role == "system":
                if Agent._is_failure_note(m):
                    notes.append((seen, m))
                continue
            seen += 1
        return notes

    def _commit_history(self, session: Session, result: TurnResult, final_text: str) -> None:
        """Persist the turn's full message history (assistant ``tool_calls`` +
        tool results included) so the next turn sees every tool round — not a
        flattened user→answer. Rewrites the store in one shot so mid-turn
        compression can't double rows; persona/skill system rows are rebuilt
        fresh each turn and skipped. The final assistant row carries the
        display timeline and the plugin-transformed final text."""
        # Captured before the rewrite: the transcript's compaction dividers.
        notes = self._anchor_notes(session)
        # Checkpoint rows keep their stats `steps` (before/after/dropped), which
        # only exist on the persisted row — the live list carries content only.
        ckpt_steps = {m.content: m.steps for m in session.messages
                      if m.role == "system" and self._is_checkpoint(m) and m.steps}
        rows: list[Message] = []
        for m in (result.history or []):
            if not isinstance(m, dict):
                continue
            role = m.get("role")
            if role == "system":
                # Keep persisted compression summaries; drop persona/skill rows.
                content = m.get("content")
                if isinstance(content, str) and content.startswith(_CHECKPOINT_MARKER):
                    rows.append(Message(role="system", content=content,
                                        steps=ckpt_steps.get(content) or []))
                continue
            rows.append(Message(
                role=role,
                content=m.get("content"),
                tool_call_id=m.get("tool_call_id"),
                tool_name=m.get("tool_name"),
                reasoning=m.get("reasoning_content") or m.get("reasoning") or "",
                reasoning_signature=m.get("reasoning_signature") or "",
                tool_calls=m.get("tool_calls") or [],
            ))
        # Orphan guard: an assistant row may only declare tool_calls if its tool
        # rows follow it, or the next request 400s as orphan tool messages.
        for i, r in enumerate(rows):
            if r.role == "assistant" and r.tool_calls:
                if not any(x.role == "tool" for x in rows[i + 1:]):
                    r.tool_calls = []
        # Distribute steps across queue-split boundaries: the assistant row
        # before a queued user message carries the steps from its tool rounds;
        # the final assistant row gets the remainder.
        # `turn_rows` collects what this turn contributes to the append-only
        # display transcript: the user row was mirrored by `append` at turn
        # start, but mid-turn queued users and every assistant row are only
        # known here.
        splits = result.queue_splits or []
        turn_rows: list[Message] = []
        if splits:
            si = 0
            first_user = True
            for i, r in enumerate(rows):
                if r.role == "user" and not first_user and si < len(splits):
                    split = splits[si]
                    prev = splits[si - 1] if si > 0 else 0
                    for j in range(i - 1, -1, -1):
                        if rows[j].role == "assistant":
                            rows[j].steps = list(result.steps[prev:split])
                            turn_rows.append(rows[j])
                            break
                    turn_rows.append(r)  # queued user: never went through `append`
                    si += 1
                if r.role == "user":
                    first_user = False
        # The final text and the remaining steps belong to the last assistant
        # row, which is not always the tail: a queued user message or a spliced
        # compaction note can sit after it. Searching backwards keeps the tool
        # log attached instead of silently dropping it.
        final_split = splits[-1] if splits else 0
        assistant_idx = next(
            (i for i in range(len(rows) - 1, -1, -1) if rows[i].role == "assistant"),
            None,
        )
        if assistant_idx is not None:
            rows[assistant_idx].content = final_text
            rows[assistant_idx].steps = list(result.steps[final_split:])
            # Identity check: with a trailing queued user the last assistant is
            # already a split row, and adding it twice would duplicate it.
            if not any(x is rows[assistant_idx] for x in turn_rows):
                turn_rows.append(rows[assistant_idx])
        # Splice the failure notes back at their original anchors so a compaction
        # divider stays put in the transcript across every later turn.
        for seen, note in notes:
            count = 0
            at = len(rows)
            for i, r in enumerate(rows):
                if r.role == "system":
                    continue
                if count == seen:
                    at = i
                    break
                count += 1
            rows.insert(at, note)
        if rows:
            self.sessions.replace_messages(session.id, rows)
        # Display transcript is append-only: this write is what survives the
        # `replace_messages` above once compaction shrinks the model context.
        if turn_rows:
            self.sessions.add_display(session.id, turn_rows)

    async def _loop(self, session: Session, turn_id: str, stop: asyncio.Event, images: list[str] | None = None,
                    node: Any = None, depth: int = 0) -> TurnResult | None:
        # A turn carrying images routes to a dedicated vision model when one is
        # configured (`vision_model`), else it goes to the session's own model —
        # which is frequently multimodal anyway. Either way the pixels ride along
        # for this request only: history keeps the path (agent/attachments.py), so
        # a model that cannot see fails once, gets retried without the image, and
        # never poisons the next turn.
        active_model = self.config.vision_model() if images else None
        # A node's explicit ``model`` overrides the session's active model.
        node_model = node.model if (node is not None and node.model) else None
        # Optional pinned provider (per-session) disambiguates same-id models
        # across providers; tolerate configs mocks without it.
        session_provider = getattr(self.config, "session_provider", None)
        provider_pin = session_provider(session.id) if session_provider else None
        chain = self._model_chain(
            node_model or active_model or self._session_model(session.id),
            provider_pin,
            node,
        )
        if not chain:
            await self._emit_for(session.id, turn_id, "turn.delta", delta="No provider/model configured. Set one up in Config.")
            return TurnResult(text="No provider/model configured.")
        provider, model = chain.pop(0)

        # build the message history + system prompt, then let plugins
        # transform the list before it hits the model
        messages = self._build_messages(session, node)
        # Pixels for this request only — never persisted, never replayed.
        sent_pixels = _inject_images(messages, images)
        stripped_pixels = False
        # Where to land if the vision model turns out to be the broken one.
        text_target = None
        if sent_pixels and active_model:
            fallback_chain = self._model_chain(self._session_model(session.id), provider_pin, node)
            text_target = fallback_chain[0] if fallback_chain else None
        pre = await self.hooks.waterfall("request.pre", {"session": session, "turn_id": turn_id, "messages": messages})
        messages = pre.get("messages", messages)
        if self.flat_plugins is not None:
            messages = await self.flat_plugins.before_llm(messages)
        tools = self.registry.schemas_for_model()
        # Node tools: a non-None allowlist scopes which toolsets the model sees.
        if node is not None and node.tools is not None:
            allowed = set(node.tools)
            pass

        registry = self.registry
        ctx = self._ctx(session, turn_id, model, node=node, depth=depth)
        registry.reset_breakers()
        assistant_text_parts: list[str] = []
        steps: list[dict[str, Any]] = []
        queue_splits: list[int] = []
        last_assistant: dict[str, Any] | None = None

        while not stop.is_set():
            if stop.is_set():
                break
            await notify.emit("state.updated", session_id=session.id, model=model)
            # context relief: LLM summarization of old turns past the threshold
            await self._maybe_compress(session, messages, provider, model, stop)


            streamed: list[str] = []
            stop_reason = StopReason.STOP
            usage: dict[str, int] = {}
            had_error = False

            retry_max = self.config.retry_max()
            retry_base = self.config.retry_interval()
            last_error: str | None = None
            call_started = time.perf_counter()
            req_reasoning: list[str] = []
            req_reasoning_sig: str | None = None

            attempt = 0
            while True:
                streamed = []
                tool_calls = []
                stop_reason = StopReason.STOP
                had_error = False
                last_error = None
                err_retryable = False
                req_reasoning = []
                req_reasoning_sig = None
                try:
                    async for ev in self.providers.chat_stream(provider, model, messages, tools, max_tokens=self.config.session_max_tokens(session.id), signal=stop):
                        if ev.error:
                            last_error = ev.error
                            err_retryable = ev.retryable
                            had_error = True
                            break
                        if ev.reasoning:
                            self._reasoning[turn_id] = self._reasoning.get(turn_id, "") + ev.reasoning
                            req_reasoning.append(ev.reasoning)
                            # Stream thinking immediately; otherwise the UI stays on
                            # "waiting for response" until the final text arrives.
                            await self._emit_for(session.id, turn_id, "turn.reasoning", delta=ev.reasoning)
                            # interleave into the display timeline at arrival position
                            if steps and steps[-1].get("kind") == "reasoning":
                                steps[-1]["text"] = (steps[-1].get("text") or "") + ev.reasoning
                            else:
                                steps.append({"kind": "reasoning", "text": ev.reasoning})
                        if ev.reasoning_signature:
                            self._reasoning_sigs[turn_id] = ev.reasoning_signature
                            req_reasoning_sig = ev.reasoning_signature
                        if ev.delta:
                            streamed.append(ev.delta)
                            await self._emit_for(session.id, turn_id, "turn.delta", delta=ev.delta)
                        if ev.tool_call:
                            tool_calls.append(
                                {"id": ev.tool_call.id, "type": "function",
                                 "function": {"name": ev.tool_call.name, "arguments": ev.tool_call.arguments}}
                            )
                        if ev.stop_reason:
                            stop_reason = ev.stop_reason
                        if ev.usage:
                            usage.update(ev.usage)
                            # Merge, don't replace: late fragments (e.g. an
                            # output-only chunk) must not erase the known
                            # prompt anchor used by context_usage().
                            merged = {**self._last_usage.get(session.id, {}), **ev.usage}
                            if "prompt_tokens" in ev.usage:
                                # Re-anchor the surface sample together with the
                                # anchor that produced it: a stale
                                # surface_at_sample beside a fresh prompt_tokens
                                # inflates the growth delta (and the meter).
                                merged["surface_at_sample"] = self._surface_tokens(messages)
                            self._last_usage[session.id] = merged
                except Exception as exc:  # noqa: BLE001 — provider failure
                    last_error = str(exc)
                    err_retryable = True  # transport-level exceptions are transient
                    had_error = True

                # Retry only a transient, clean provider failure — nothing streamed,
                # no tool calls, error marked retryable, not stopped, attempts left.
                clean_failure = had_error and not streamed and not tool_calls and not stop.is_set()
                if clean_failure and err_retryable and attempt < retry_max:
                    delay = retry_base + 2 * attempt
                    # Surface as a transient notice with a LIVE countdown (re-emitted
                    # every second) instead of a static "in Ns" line.
                    remaining = max(1, round(delay))
                    await self._emit_for(session.id, turn_id, "turn.notice",
                                   text=f"provider error — retry {attempt + 1}/{retry_max} in {remaining}s", detail=last_error)
                    stopped = False
                    while remaining > 0:
                        try:
                            await asyncio.wait_for(stop.wait(), timeout=1.0)
                            stopped = True
                            break
                        except asyncio.TimeoutError:
                            remaining -= 1
                            if remaining > 0:
                                await self._emit_for(
                                    session.id, turn_id, "turn.notice",
                                    text=f"provider error — retry {attempt + 1}/{retry_max} in {remaining}s",
                                    detail=last_error,
                                )
                    if stopped:
                        break
                    # Retract the countdown before re-calling the provider. Nothing
                    # else retracts it: the shell only cleared a notice on the next
                    # text delta, so on a reasoning-first or tool-first reply the
                    # spent "retry 1/10 in 1s" line stayed on screen for the rest of
                    # the turn.
                    await self._emit_for(session.id, turn_id, "turn.notice", text="")
                    attempt += 1
                    continue
                # The model can't see. Retrying the same request is pointless and
                # switching models would replay the same pixels, so take the image
                # out once, say so, and let the turn finish on text. This rung is
                # what keeps a blind model from ending the turn on `[error]` — and
                # since the image never entered history, the *next* turn is clean
                # whatever happens here.
                # `not err_retryable`: a 5xx/timeout is the provider having a bad
                # day, not a blind model — that keeps its normal retry path.
                if clean_failure and not err_retryable and sent_pixels and not stripped_pixels:
                    stripped_pixels = _strip_images(messages)
                    if stripped_pixels:
                        blind = f"{getattr(provider, 'id', provider)}/{model}"
                        if text_target is not None and text_target != (provider, model):
                            provider, model = text_target
                            ctx.config["model"] = model
                            await notify.emit("state.updated", session_id=session.id, model=model)
                        configured = self.config.vision_model()
                        text = (f"vision model {configured} can't read the image — answering without it"
                                if configured else
                                f"{blind} can't read images — set a vision model in Config → Agent")
                        activity.record("warn", "brain", f"llm {blind} rejected the image", (last_error or "")[:240])
                        await self._emit_for(session.id, turn_id, "turn.notice",
                                             text=text, detail=last_error)
                        attempt = 0
                        continue
                # Retries are spent, or the error is one retries can never fix
                # (bad key, unknown model, quota). Fall back to the next model in
                # the chain; the switch is sticky for the rest of the turn.
                if clean_failure and chain:
                    failed_label = f"{getattr(provider, 'id', provider)}/{model}"
                    provider, model = chain.pop(0)
                    attempt = 0
                    # Tools read the active model off ctx (delegated children
                    # inherit it) — keep it pointing at the model actually in use.
                    ctx.config["model"] = model
                    activity.record("warn", "brain", f"llm {failed_label} exhausted — falling back to {model}",
                                    (last_error or "")[:240])
                    await self._emit_for(session.id, turn_id, "turn.notice",
                                         text=f"{failed_label} failed — falling back to {model}", detail=last_error)
                    await notify.emit("state.updated", session_id=session.id, model=model)
                    continue
                break

            # Log the provider API call: token usage + latency, or the final failure.
            provider_label = getattr(provider, "id", provider)
            tok = {k: v for k, v in usage.items() if v}
            if had_error and last_error is not None:
                activity.record("error", "brain", f"llm {provider_label}/{model} failed", last_error[:240])
            else:
                detail = f"tokens={tok}" if tok else "no usage"
                activity.record("debug", "brain", f"llm {provider_label}/{model}",
                                f"{detail} · {time.perf_counter() - call_started:.1f}s")

            if had_error and last_error is not None and not tool_calls:
                err_line = f"[error] {last_error}"
                await self._emit_for(session.id, turn_id, "turn.delta", delta=("\n" if streamed else "") + err_line)
                streamed.append(err_line)  # persist errors in the assistant message
            # A turn ending without text — e.g. a thinking model that hit its
            # token budget mid-thought — would persist a reasoning-only row
            # that reads as a vanished answer. Surface it in the transcript.
            if not had_error and not tool_calls and not "".join(streamed).strip():
                marker = (
                    "[error] stopped at token limit before producing an answer"
                    if stop_reason is StopReason.LENGTH
                    else "[error] model returned no text this turn"
                )
                await self._emit_for(session.id, turn_id, "turn.delta", delta=marker)
                streamed.append(marker)
            # context meter (rough: chars vs configured length)
            await self._emit_context(session, messages, usage)

            text = "".join(streamed)
            assistant_text_parts.append(text)
            if text.strip():
                steps.append({"kind": "text", "text": text})
            req_reasoning_text = "".join(req_reasoning)
            assistant_msg: dict[str, Any] = {"role": "assistant", "content": text}
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            # thinking-mode models need this turn's own reasoning echoed back on
            # the next request in the same turn (tool rounds), not just on the
            # persisted rows of prior turns.
            if req_reasoning_text:
                assistant_msg["reasoning_content"] = req_reasoning_text
            if req_reasoning_sig:
                assistant_msg["reasoning_signature"] = req_reasoning_sig
            messages.append(assistant_msg)
            last_assistant = assistant_msg

            if had_error and not tool_calls:
                # surface error, keep partial text
                break

            # before_llm plugins already transformed the message list at build;
            # after_tool below.
            #
            # Pending tool calls always outrank a claimed stop. Providers and
            # gateways routinely pair tool_calls with a finish_reason that maps
            # to STOP ("end_turn", "tool_use", or any value _map_stop_reason
            # folds to STOP), and returning here discarded every one of them:
            # the model asked to delegate, nothing ran, and the turn ended on
            # empty text despite a 200 from the API.


            # Delegate siblings are independent child sessions. Run them as one
            # bounded, all-settled batch while keeping tool results in model order.
            async def execute_tool_call(tc: dict[str, Any], step: dict[str, Any]) -> tuple[str, str | None]:
                fn = tc["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn["arguments"] or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                emit = self._steps_emit(turn_id, step, session.id)
                res = await registry.run(name, args, ctx, emit=emit)
                res = await self.flat_plugins.after_tool(name, res)
                if res.error:
                    activity.record("error", "brain", f"tool {name} failed", res.raw)
                else:
                    activity.record("debug", "brain", f"tool {name} ok")
                return res.output, res.error

            parsed_calls: list[tuple[dict[str, Any], dict[str, Any], str]] = []
            for tc in tool_calls:
                fn = tc["function"]
                name = fn["name"]
                try:
                    args = json.loads(fn["arguments"] or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                if stop.is_set():
                    break
                step = self._capture_tool(name, args, session.id, call_id=tc.get("id"))
                steps.append(step)
                parsed_calls.append((tc, step, name))
            # How many sub-agents this tool round spawns in total. Each chip
            # counted only its own call, so a 4-way fan-out of sibling calls read
            # "1 agent spawned" four times. Summing the round covers both shapes:
            # four sibling calls, or one call carrying a `tasks` batch.
            spawned = sum(s.get("subagent_count") or 0
                          for _, s, n in parsed_calls if n == "delegate")
            for _, s, n in parsed_calls:
                if n == "delegate":
                    s["subagent_count"] = spawned
            all_delegates = len(parsed_calls) > 1 and all(name == "delegate" for _, _, name in parsed_calls)
            if all_delegates:
                limit = int(ctx.config.get("max_parallel_subagents", len(parsed_calls)) or len(parsed_calls))
                settled = await run_bounded(
                    parsed_calls,
                    lambda item: execute_tool_call(item[0], item[1]),
                    limit,
                )
            else:
                settled = [await execute_tool_call(tc, step) for tc, step, _ in parsed_calls]

            for (tc, step, name), result in zip(parsed_calls, settled):
                if isinstance(result, BaseException):
                    result_text, error = f"tool {name} failed: {result}", str(result)
                    step["status"] = "error"
                    step["output"] = result_text
                    activity.record("error", "brain", f"tool {name} failed", result_text)
                else:
                    result_text, error = result
                messages.append({"role": "tool", "tool_call_id": tc["id"], "tool_name": name, "content": result_text})
            # Queue-send: drain after the tool round, before the next LLM request.
            drained = self._drain_queue(session.id, messages)
            if drained:
                queue_splits.append(len(steps))
                await self._emit_for(
                    session.id, turn_id, "turn.dequeue",
                    messages=[{"queued_id": d["queued_id"], "text": d["text"], "images": d["images"]} for d in drained],
                )
            if not tool_calls and not drained:
                return TurnResult(text=text, steps=steps, history=messages,
                                   queue_splits=queue_splits)
            # Continue with tool results and/or queued user messages.


        # stopped before turn completed
        final_text = "".join(p for p in assistant_text_parts if p).strip()
        return TurnResult(text=final_text, steps=steps, history=messages,
                           queue_splits=queue_splits) if final_text or steps else None

    def _session_model(self, session_id: str) -> str | None:
        return self.config.session_model(session_id)

    def _ctx(self, session: Session, turn_id: str, model: str | None, node: Any = None, depth: int = 0):
        from ..tools.base import ToolContext

        return ToolContext(
            data_home=self.data_home,
            session_id=session.id,
            turn_id=turn_id,
            cwd=session.cwd,
            agent=self,
            events=notify,
            approvals=self.approvals,
            providers=self.providers,
            memory=self.memory,
            skills=self.skills,
            flat_plugins=self.flat_plugins,
            presets=self.presets,
            config={
                **self.config.all(), "model": model,
                "subtask": session.id in self._subtask_sessions,
                "preset_depth": depth,
                "preset_node": node.to_dict() if node is not None else None,
            },
        )


def _turn_summary(session: Any, model: str, seconds: float) -> str:
    """The single user-facing info line per completed turn."""
    title = getattr(session, "title", "") or session.id
    return f"Turn finished · {title} · {model} · {seconds:.1f}s"

