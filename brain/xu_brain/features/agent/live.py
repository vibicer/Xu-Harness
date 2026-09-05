"""Live turn streaming — in-flight step chips + draft persistence.

A mixin aspect of :class:`~xu_brain.features.agent.loop.Agent`.
"""
from __future__ import annotations

import re
from typing import Any

from ...core.notify import notify
from ..session import Message, Session
from ..tools.registry import summarize_args as _summarize_args


class LiveMixin:
    _MAX_RUN_STEPS = 200

    def _capture_tool(self, name: str, args: dict[str, Any],
                      session_id: str | None = None,
                      call_id: str | None = None) -> dict[str, Any]:
        """Start a tool step, including delegate identity before execution.

        `args` is summarized to the same one-line form the registry emits on the
        chip. Persisting the raw tool-call JSON here used to leak whole `edit`
        patches and `write` bodies back into the transcript (and from there into
        the next prompt) once the session reloaded.

        `call_id` is the provider's tool-call id. Concurrent siblings share a
        tool name, so it is the only stable way to settle the right chip.
        """
        step = {"kind": "tool", "tool": name, "args": _summarize_args(args),
                "status": "running", "elapsed": None, "output": None}
        if call_id:
            step["call_id"] = call_id
        if name == "delegate":
            # How many sub-agents this one call spawns: a `tasks` batch fans out
            # in a single call, so the chip is the only place that number can be
            # read off later.
            tasks = args.get("tasks")
            step["subagent_count"] = len(tasks) if isinstance(tasks, list) and tasks else 1
        if name == "delegate" and session_id:
            run = self._claim_delegation(session_id, args.get("label") or args.get("child"))
            if run is not None:
                step["subagent_run"] = run["id"]
                step["subagent"] = run.get("child")
        return step

    def _steps_emit(self, turn_id: str, step: dict[str, Any], session_id: str):
        base = self._emit_tool(turn_id, session_id)

        async def emit(event: str, **kw: Any) -> None:
            # Tag every chip event with the provider's tool-call id: concurrent
            # siblings share a tool name, and matching on name alone made each
            # settled call overwrite whichever chip was still running.
            if event == "turn.tool" and step.get("call_id"):
                kw = {**kw, "call_id": step["call_id"]}
            # The delegate tool creates its run record before execution, but its
            # ToolResult (which used to carry the id) arrives only at the end.
            # Attach the id to the running step immediately. The step's args are
            # the one-line chip summary ("label=researcher prompt=…"), so read
            # the name out of that rather than parsing JSON. `child` is the
            # tool's accepted alias for `label`, so match either spelling.
            if (event == "turn.tool" and kw.get("status") == "running"
                    and step.get("tool") == "delegate" and not step.get("subagent_run")):
                m = re.search(r"(?:^|\s)(?:child|label)=(\S+)", str(step.get("args") or ""))
                run = self._claim_delegation(session_id, m.group(1) if m else None)
                if run is not None:
                    step["subagent_run"] = run["id"]
                    step["subagent"] = run.get("child")
            if step.get("subagent_count") and event == "turn.tool":
                kw = {**kw, "subagent_count": step["subagent_count"]}
            if step.get("subagent_run") and event == "turn.tool":
                kw = {**kw, "subagent_run": step["subagent_run"], "subagent": step.get("subagent")}
            await base(event, **kw)
            if event == "turn.tool" and kw.get("status") in ("ok", "error"):
                step["status"] = kw["status"]
                step["elapsed"] = kw.get("elapsed")
                step["output"] = kw.get("output")
                if kw.get("subagent_run"):
                    step["subagent_run"] = kw["subagent_run"]
                    step["subagent"] = kw.get("subagent")
                # show_image chips carry the picture itself; without this the
                # image showed live and then vanished on the next session load.
                if kw.get("image"):
                    step["image"] = kw["image"]
                    step["image_alt"] = kw.get("image_alt")

        return emit

    def _emit_tool(self, turn_id: str, session_id: str):
        async def emit(event: str, **kw: Any) -> None:
            if event == "turn.tool":
                await self._emit_for(session_id, turn_id, "turn.tool", **kw)
            elif event == "turn.approval_resolved":
                await notify.emit("turn.approval_resolved", **kw)
        return emit

    def _persist_live_turn(self, session: Session, status: str = "done") -> bool:
        """Commit an in-flight draft so refresh/stop cannot erase its context."""
        live = self._live_turns.get(session.id)
        if not live or live.get("persisted"):
            return False
        steps = [dict(step) for step in live.get("steps", [])]
        text = "".join(str(step.get("text") or "") for step in steps if step.get("kind") == "text")
        reasoning = str(live.get("reasoning") or "")
        if not text and not reasoning and not steps:
            return False
        # Mark any still-running tools as failed; completed ones keep their ok/error status.
        for step in steps:
            if step.get("kind") == "tool" and step.get("status") == "running":
                step["status"] = "error"
        message = Message(role="assistant", content=text, reasoning=reasoning, steps=steps)
        message.status = status
        self.sessions.append(session.id, message)
        live["persisted"] = True
        return True

    def live_draft(self, session_id: str) -> dict[str, Any] | None:
        """Snapshot of the currently streaming turn, if any."""
        live = self._live_turns.get(session_id)
        if live is None or live.get("persisted"):
            return None
        return live

    def _merge_live(self, live: dict[str, Any], event: str, kw: dict[str, Any]) -> None:
        """Mirror one turn.* emission into the live snapshot (same shapes as
        the shell's TurnDraft: steps[] + reasoning)."""
        steps = live.setdefault("steps", [])
        if event == "turn.delta":
            delta = str(kw.get("delta") or "")
            if delta:
                if steps and steps[-1].get("kind") == "text":
                    steps[-1]["text"] = (steps[-1].get("text") or "") + delta
                else:
                    steps.append({"kind": "text", "text": delta})
        elif event == "turn.reasoning":
            delta = str(kw.get("delta") or "")
            live["reasoning"] = (live.get("reasoning") or "") + delta
            if delta:
                if steps and steps[-1].get("kind") == "reasoning":
                    steps[-1]["text"] = (steps[-1].get("text") or "") + delta
                else:
                    steps.append({"kind": "reasoning", "text": delta})
        elif event == "turn.tool":
            tool = str(kw.get("tool") or "")
            call_id = kw.get("call_id")
            chip: dict[str, Any] = {
                "kind": "tool",
                "tool": tool,
                "args": str(kw.get("args") or ""),
                "status": kw.get("status", "running"),
                "elapsed": kw.get("elapsed"),
                "output": kw.get("output"),
            }
            if call_id:
                chip["call_id"] = call_id
            # delegate chips carry the sub-agent run so the shell can open its
            # activity; keep it out of the dict when absent.
            if kw.get("subagent_run"):
                chip["subagent_run"] = kw["subagent_run"]
                chip["subagent"] = kw.get("subagent")
            if kw.get("subagent_count"):
                chip["subagent_count"] = kw["subagent_count"]
            # show_image: the data URL is part of the chip, so a shell that
            # resyncs mid-turn (refresh) still gets the picture.
            if kw.get("image"):
                chip["image"] = kw["image"]
                chip["image_alt"] = kw.get("image_alt")
            # Settle by tool-call id when the provider gave one: concurrent
            # siblings share a name, so a name match settled the wrong chip and
            # left the other sub-agents' chips running forever.
            if call_id:
                idx = next((i for i, s in enumerate(steps)
                            if s.get("kind") == "tool" and s.get("call_id") == call_id), None)
            else:
                idx = next(
                    (i for i, s in enumerate(steps)
                     if s.get("kind") == "tool" and s.get("tool") == tool
                     and s.get("status") == "running" and not s.get("call_id")),
                    None,
                )
            if idx is not None:
                steps[idx] = {**steps[idx], **chip}
            else:
                steps.append(chip)

    async def _emit_for(self, session_id: str, turn_id: str, event: str, **kw: Any) -> None:
        """Emit a turn.* event tagged with its session so multi-tab shells can
        route it, and mirror it into the live draft snapshot."""
        live = self._live_turns.get(session_id)
        if live is not None:
            self._merge_live(live, event, kw)
        # A delegated child's events also feed its activity record so the
        # parent's shell can show what the sub-agent is doing.
        run_id = self._delegation_by_session.get(session_id)
        if run_id is not None:
            rec = self._delegations.get(run_id)
            if rec is not None:
                self._merge_live(rec, event, kw)
                if len(rec["steps"]) > self._MAX_RUN_STEPS:
                    del rec["steps"][: len(rec["steps"]) - self._MAX_RUN_STEPS]
                # Token streams get a delta event instead of the whole record:
                # re-serializing a 100KB+ run per token saturated the socket and
                # stalled the parent's own stream. Structural events (tool
                # chips) still ship a full snapshot, which resyncs any drift.
                stream = event in ("turn.delta", "turn.reasoning")
                delta = str(kw.get("delta") or "") if stream else ""
                self._save_delegations(force=not stream)
                if stream:
                    if delta:
                        await notify.emit("subagent.delta", session_id=rec.get("parent_session"),
                                       run_id=run_id,
                                       kind="reasoning" if event == "turn.reasoning" else "text",
                                       delta=delta, status=rec.get("status"))
                else:
                    await notify.emit("subagent.activity", session_id=rec.get("parent_session"), run=rec)
        payload = {**kw, "session_id": session_id}
        payload.setdefault("turn_id", turn_id)
        await notify.emit(event, **payload)
