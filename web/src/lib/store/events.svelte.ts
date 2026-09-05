import type { ApprovalCard, ChatMessage, MemoryEntry, Step, SubagentRun, StatePanel, TodoInfo } from "../types";
import { EMPTY_DRAFT, type CompactionResult, type SessionStatus, type TabState, pushReasoning, pushText } from "./shared";
import { bindSubRun } from "../subrun-bind";
import type { Ctor, StoreCoreBase } from "./core.svelte";


export function EventsMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Events extends Base {
    protected declare tabs: Record<string, TabState>;
    declare openSessionIds: string[];
    declare state: StatePanel;
    declare approvals: ApprovalCard[];
    declare memories: MemoryEntry[];
    declare compressingSessions: string[];
    declare compactionResult: CompactionResult | null;
    declare subagentActivityUpdate: SubagentRun | null;
    declare subRuns: Record<string, SubagentRun>;
    declare todos: TodoInfo;
    declare status: SessionStatus;
    declare refreshSubagents: () => Promise<void>;
    declare refreshTodo: () => Promise<void>;
  /** Session every turn.* event belongs to (events are tagged server-side). */
  private eventSessionId(params: Record<string, unknown>): string | null {
    return typeof params.session_id === "string" ? params.session_id : this.activeSessionId;
  }


  protected onEvent(event: string, params: Record<string, unknown>): void {
    switch (event) {
      case "turn.started": {
        const sid = this.eventSessionId(params);
        if (!sid || !this.isOpenTab(sid)) return;
        const tab = this.ensureTab(sid);
        tab.draft = EMPTY_DRAFT;
        tab.busy = true;
        tab.queued = []; // queued sends become real history rows on reload
        break;
      }
      case "turn.delta": {
        const sid = this.eventSessionId(params);
        if (!sid || !this.isOpenTab(sid)) return;
        this.applyDraftFor(sid, (d) => ({ ...d, notice: undefined, steps: pushText(d.steps, String(params.delta ?? "")) }));
        break;
      }
      case "turn.reasoning": {
        // interleave into the timeline at arrival position (kind:"reasoning")
        const sid = this.eventSessionId(params);
        if (!sid || !this.isOpenTab(sid)) return;
        this.applyDraftFor(sid, (d) => ({
          ...d,
          notice: undefined, // the model is answering — any retry notice is spent
          reasoning: (d.reasoning ?? "") + String(params.delta ?? ""),
          steps: pushReasoning(d.steps, String(params.delta ?? "")),
        }));
        break;
      }
      case "turn.queue": {
        const sid = this.eventSessionId(params);
        const qid = typeof params.queued_id === "string" ? params.queued_id : "";
        if (!sid || !this.isOpenTab(sid) || !qid) return;
        const tab = this.ensureTab(sid);
        const text = String(params.text ?? "");
        const pending = tab.queued.find((q) => !q.id && q.text === text);
        if (pending) {
          tab.queued = tab.queued.map((q) => (q === pending ? { ...q, id: qid } : q));
        } else if (!tab.queued.some((q) => q.id === qid)) {
          tab.queued = [...tab.queued, { id: qid, text }];
        }
        break;
      }
      case "turn.notice": {
        const sid = this.eventSessionId(params);
        if (!sid || !this.isOpenTab(sid)) return;
        // Empty text retracts the notice (the brain sends one when a retry
        // countdown is spent) — don't render a blank line.
        const text = String(params.text ?? "");
        this.applyDraftFor(sid, (d) => ({ ...d, notice: text || undefined }));
        break;
      }
      case "queue.cancelled": {
        // Server dropped a queued message (user cancelled it — possibly in
        // another tab): remove just that bubble, keep the rest.
        const sid = this.eventSessionId(params);
        const qid = typeof params.queued_id === "string" ? params.queued_id : "";
        if (!sid || !qid || !this.isOpenTab(sid)) return;
        const tab = this.ensureTab(sid);
        tab.queued = tab.queued.filter((q) => q.id !== qid);
        break;
      }
      case "turn.dequeue": {
        // Server spliced queued user messages into the live turn at a tool
        // round. Move those bubbles from the queued tray into the timeline:
        // commit the assistant segment streamed so far as a finished row, drop
        // the drained bubbles, append each dequeued message as a user row, then
        // reset the draft so the rest of the turn streams fresh below.
        const sid = this.eventSessionId(params);
        const msgs = (Array.isArray(params.messages) ? params.messages : []) as Array<{
          queued_id?: unknown;
          text?: unknown;
          images?: unknown;
        }>;
        if (!sid || !this.isOpenTab(sid) || !msgs.length) return;
        const tab = this.ensureTab(sid);
        const drainedIds = new Set(
          msgs.map((m) => (typeof m.queued_id === "string" ? m.queued_id : "")).filter(Boolean),
        );
        // Commit the current streaming draft as a finished assistant row.
        const draft = tab.draft;
        const hasDraft = !!(draft && (draft.steps.length || draft.reasoning));
        const assistantRows: ChatMessage[] = hasDraft
          ? [
              {
                role: "assistant",
                content: draft!.steps
                  .filter((s) => s.kind === "text")
                  .map((s) => s.text ?? "")
                  .join(""),
                reasoning: draft!.reasoning || undefined,
                steps: draft!.steps,
              },
            ]
          : [];
        const userRows: ChatMessage[] = msgs.map((m) => {
          const text = String(m.text ?? "");
          const images = Array.isArray(m.images) ? (m.images as string[]) : [];
          // Mirror session.send / session.get content shape.
          const content = images.length
            ? [
                { type: "text", text },
                ...images.map((u) => ({ type: "image_url", image_url: { url: u } })),
              ]
            : text;
          return { role: "user", content };
        });
        tab.messages = [...tab.messages, ...assistantRows, ...userRows];
        tab.queued = tab.queued.filter((q) => !q.id || !drainedIds.has(q.id));
        tab.draft = EMPTY_DRAFT;
        break;
      }
      case "turn.tool": {
        const sid = this.eventSessionId(params);
        if (!sid || !this.isOpenTab(sid)) return;
        const tool = String(params.tool ?? "");
        const status = (params.status as Step["status"]) ?? "running";
        const callId = typeof params.call_id === "string" ? params.call_id : undefined;
        this.applyDraftFor(sid, (d) => {
          const steps = [...d.steps];
          // Settle by tool-call id when present. Concurrent delegate siblings
          // share a tool name, so matching on name settled whichever chip was
          // running first and the remaining sub-agents never showed activity.
          const idx = callId
            ? steps.findIndex((s) => s.kind === "tool" && s.call_id === callId)
            : steps.findIndex(
                (s) => s.kind === "tool" && s.tool === tool && s.status === "running" && !s.call_id,
              );
          const chip: Step = {
            kind: "tool",
            tool,
            args: String(params.args ?? ""),
            status,
            elapsed: (params.elapsed as number | undefined) ?? null,
            output: (params.output as string | undefined) ?? null,
            call_id: callId,
            subagent_run: typeof params.subagent_run === "string" ? params.subagent_run : undefined,
            subagent: typeof params.subagent === "string" ? params.subagent : undefined,
            subagent_count:
              typeof params.subagent_count === "number" ? params.subagent_count : undefined,
            // show_image: a data URL rides on the settling chip. Spread it in
            // only when present — the running chip has none, and merging an
            // `undefined` back over a settled step would erase the picture.
            ...(typeof params.image === "string" && params.image
              ? {
                  image: params.image,
                  image_alt: typeof params.image_alt === "string" ? params.image_alt : null,
                }
              : {}),
          };
          if (idx >= 0) steps[idx] = { ...steps[idx], ...chip };
          else steps.push(chip);
          // a tool call means the model answered — any retry notice is spent
          return { ...d, notice: undefined, steps };
        });
        break;
      }
      case "turn.approval": {
        const card: ApprovalCard = {
          request_id: String(params.request_id ?? ""),
          turn_id: String(params.turn_id ?? ""),
          session_id: typeof params.session_id === "string" ? params.session_id : undefined,
          tool: String(params.tool ?? ""),
          args: params.args as string | Record<string, unknown>,
          reason: String(params.reason ?? ""),
          level: typeof params.level === "string" ? params.level : undefined,
          resolved: null,
        };
        this.approvals = [...this.approvals.filter((a) => a.request_id !== card.request_id), card];
        break;
      }
      case "turn.approval_resolved": {
        const rid = String(params.request_id ?? "");
        const approved = Boolean(params.approved);
        const answer = params.answer ? String(params.answer) : undefined;
        this.approvals = this.approvals.map((a) =>
          a.request_id === rid ? { ...a, resolved: approved, ...(answer ? { answer } : {}) } : a,
        );
        // Persist a resolved ask into the turn's timeline so Q&A survives the overlay.
        const acard = this.approvals.find((a) => a.request_id === rid);
        const asid = acard?.session_id;
        if (acard?.tool === "ask" && asid && this.isOpenTab(asid)) {
          const q = String(acard.args && typeof acard.args === "object" && "question" in acard.args ? (acard.args as Record<string, unknown>).question : acard.args ?? "");
          const step: Step = { kind: "tool", tool: "ask", args: q, status: "ok", elapsed: null, output: answer ? `\u2192 ${answer}` : (approved ? "answered" : "dismissed") };
          this.applyDraftFor(asid, (d) => ({ ...d, steps: [...d.steps, step] }));
        }
        // auto-remove settled cards after 2.5s
        setTimeout(() => {
          this.approvals = this.approvals.filter((a) => a.request_id !== rid);
        }, 2500);
        break;
      }
      case "turn.finished": {
        // Server persisted the assistant message; reload authoritative history
        // for the SESSION the turn belonged to (multi-tab: may not be active),
        // then drop the streaming draft (finalizeDraft would duplicate it).
        const sid = this.eventSessionId(params);
        // A sub-agent's ephemeral session finishes mid-parent-turn. It is not an
        // open tab, so the old fallback branch cleared the ACTIVE tab's draft
        // and reloaded its history instead — wiping the orchestrator's live
        // response and every delegate chip each time a child completed.
        if (!sid || !this.tabs[sid]) {
          if (sid) this.dismissSessionApprovals(sid);
          break;
        }
        this.dismissSessionApprovals(sid);
        this.notifyTurn(sid, false);
        this.finalizeTurn(sid, false); // background refresh — don't steal focus
        break;
      }
      case "turn.failed": {
        // The server commits any partial draft before emitting this event.
        // Drop the live view and reload authoritative history so stop/error
        // survives refresh without duplicating the assistant message locally.
        const sid = this.eventSessionId(params);
        this.notifyTurn(sid, true, params.error);
        if (!sid || !this.tabs[sid]) {
          if (sid) this.dismissSessionApprovals(sid);
          return;
        }
        this.dismissSessionApprovals(sid);
        this.finalizeTurn(sid, true);
        break;
      }
      case "context.updated": {
        // The context meter tracks the ACTIVE session; subagent
        // sessions emit their own updates that must not hijack it.
        const sid = this.eventSessionId(params);
        if (sid && sid !== this.activeSessionId) break;
        this.state = {
          ...this.state,
          context: Number(params.usage_pct ?? this.state.context),
          compress: Boolean(params.compress),
          tokens: params.tokens == null ? this.state.tokens : Number(params.tokens),
          // null means "no provider sample" — clear the chip instead of
          // showing a stale value across compaction or session switches.
          pressure: params.pressure == null ? undefined : Number(params.pressure),
          projected: params.projected == null ? undefined : Number(params.projected),
          calibrated: Boolean(params.calibrated),
        };
        break;
      }
      case "compaction.started": {
        const sid = this.eventSessionId(params);
        if (sid && !this.compressingSessions.includes(sid)) {
          this.compressingSessions = [...this.compressingSessions, sid];
        }
        break;
      }
      case "compaction.done": {
        const sid = this.eventSessionId(params);
        if (sid && this.compressingSessions.includes(sid)) {
          this.compressingSessions = this.compressingSessions.filter((s) => s !== sid);
        }
        if (sid && sid !== this.activeSessionId) break;
        const { session_id: _sid, ...rest } = params;
        this.compactionResult = rest as { ok: boolean; error?: string; before?: number; after?: number; dropped?: number };
        break;
      }
      case "state.updated": {
        const sid = this.eventSessionId(params);
        if (sid && sid !== this.activeSessionId) break;
        const { session_id: _sid, ...rest } = params;
        this.state = { ...this.state, ...rest };
        break;
      }
      case "status.updated":
        this.status = { ...this.status, ...(params as Partial<SessionStatus>) };
        break;
      case "session.updated": {
        const sid = this.eventSessionId(params);
        // A parent turn can emit session.updated while delegate calls are still
        // running. Reloading here replaces the visible live transcript with
        // stale persisted messages; the authoritative reload happens on the
        // parent turn's terminal event below.
        if (sid && this.tabs[sid]?.busy) break;
        if (sid && this.tabs[sid]) void this.loadSession(sid, false);
        else if (this.activeSessionId && this.isOpenTab(this.activeSessionId) && !this.tabs[this.activeSessionId]?.busy) {
          void this.loadSession(this.activeSessionId, false);
        }
        break;
      }
      case "subagent.activity": {
        const sid = this.eventSessionId(params);
        if (!sid) return;
        const run = params.run as SubagentRun | undefined;
        // The chip's "turn.tool running" event went out before the run record
        // existed, so the chip never learned its id. This push IS the run's
        // birth: bind it onto the live delegate chip now so "↳ activity" is
        // clickable while the sub-agent is still running, not only at "ok".
        if (run && run.status === "running" && this.isOpenTab(sid)) {
          this.applyDraftFor(sid, (d) => {
            const steps = bindSubRun(d.steps, run);
            return steps === d.steps ? d : { ...d, steps };
          });
        }
        if (sid !== this.activeSessionId) return;
        if (run) {
          this.subagentActivityUpdate = run;
          // Merge, never replace: four siblings stream at once and each needs
          // its own record kept so the activity view can tab between them.
          this.subRuns = { ...this.subRuns, [run.id]: run };
        }
        // The subagent list only reaches the UI through pushes; re-fetch
        // (in-flight deduped) so running/finished counts stay current.
        void this.refreshSubagents();
        break;
      }
      // Token-level growth for a delegated run. The brain sends only the
      // appended text (a full record per token stalled its own event loop);
      // apply it to the run we already hold, mirroring the brain's own
      // step-merge rule so the two stay in sync between snapshots.
      case "subagent.delta": {
        const sid = this.eventSessionId(params);
        if (!sid || sid !== this.activeSessionId) return;
        const runId = String(params.run_id ?? "");
        const delta = String(params.delta ?? "");
        const kind = params.kind === "reasoning" ? "reasoning" : "text";
        // Route by run id: with concurrent siblings the delta rarely belongs to
        // whichever run happens to be on screen.
        const cur = this.subRuns[runId];
        if (!runId || !delta || !cur) break;
        const steps = [...cur.steps];
        const last = steps[steps.length - 1];
        if (last?.kind === kind) {
          steps[steps.length - 1] = { ...last, text: (last.text ?? "") + delta };
        } else {
          steps.push({ kind, text: delta } as Step);
        }
        const grown = {
          ...cur,
          steps,
          reasoning: kind === "reasoning" ? (cur.reasoning ?? "") + delta : cur.reasoning,
        };
        this.subRuns = { ...this.subRuns, [runId]: grown };
        if (this.subagentActivityUpdate?.id === runId) this.subagentActivityUpdate = grown;
        break;
      }
      case "memory.updated":
        this.memories = (params.entries as MemoryEntry[]) ?? this.memories;
        break;
      case "todo.updated": {
        const sid = this.eventSessionId(params);
        if (sid && sid === this.activeSessionId) void this.refreshTodo();
        break;
      }
    }
  }
  };
}
