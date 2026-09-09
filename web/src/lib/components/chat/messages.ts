/** Transcript helpers shared by the Chat shell and its sub-components.
 *  Pure functions only — no runes, no markup. Bodies are verbatim from the
 *  original Chat.svelte (see git history before the Phase E1a split). */
import type { ApprovalCard, ChatMessage } from "../../types";
import { compactionDivider } from "../../compaction.ts";

// Replace raw approval JSON with one short, human-readable action.
const APPROVAL_SUMMARY_LIMIT = 160;

function compactApprovalValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  try { return JSON.stringify(value); } catch { return String(value); }
}

export function approvalSummary(card: ApprovalCard): string {
  let args: Record<string, unknown> | null = null;
  if (typeof card.args === "string") {
    try {
      const parsed = JSON.parse(card.args) as unknown;
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
        args = parsed as Record<string, unknown>;
      }
    } catch { /* keep the tool name when args are not JSON */ }
  } else {
    args = card.args;
  }
  const short = (text: string): string =>
    text.length > APPROVAL_SUMMARY_LIMIT ? `${text.slice(0, APPROVAL_SUMMARY_LIMIT)}…` : text;
  if (args) {
    if (typeof args.command === "string") return short(`Run command: ${args.command}`);
    if (typeof args.question === "string") return short(`Ask: ${args.question}`);
    if (typeof args.prompt === "string") return short(`Ask: ${args.prompt}`);
    for (const key of ["path", "file", "url", "query", "name", "id", "text"]) {
      const value = args[key];
      if (typeof value === "string") {
        const label = key === "query" ? "Search" : key === "text" ? "Write" : key;
        return short(`${label}: ${value}`);
      }
    }
    const first = Object.entries(args)[0];
    if (first) return short(`${first[0]}: ${compactApprovalValue(first[1])}`);
  }
  return humanizeTool(card.tool);
}

export function approvalReason(reason: string): string | null {
  const text = reason.trim();
  if (!text || /^(Risky action requested|Always-gated action):|^clarification requested by agent$/.test(text)) return null;
  if (text.startsWith("Destructive shell command:")) return null;
  return text.length > APPROVAL_SUMMARY_LIMIT ? `${text.slice(0, APPROVAL_SUMMARY_LIMIT)}…` : text;
}

export function humanizeTool(tool: string): string {
  return tool
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, (char) => char.toUpperCase());
}

export function askQuestion(card: ApprovalCard): string {
  const args = card.args;
  if (typeof args === "object" && args) {
    const q = (args as Record<string, unknown>).question;
    if (typeof q === "string") return q;
  }
  return "";
}

export function askOptions(card: ApprovalCard): string[] {
  if (typeof card.args === "object") {
    const opts = (card.args as Record<string, unknown>).options;
    if (Array.isArray(opts)) return opts.map(String);
  }
  return [];
}

export interface Segment {
  kind: "md" | "error";
  text: string;
}

/** Split assistant text into prose and [error] segments; errors render as
 * framed blocks instead of raw JSON dumps.
 * Memoized: the transcript calls this for every message on every history
 * reload (the whole array is replaced after each turn), and a stable array
 * identity lets Svelte skip re-running the inner each-block effects too. */
const SEGMENT_CACHE = new Map<string, Segment[]>();
const SEGMENT_CACHE_CAP = 4000;

export function segments(text: string): Segment[] {
  const hit = SEGMENT_CACHE.get(text);
  if (hit !== undefined) return hit;
  const out = splitSegments(text);
  if (SEGMENT_CACHE.size >= SEGMENT_CACHE_CAP) SEGMENT_CACHE.clear();
  SEGMENT_CACHE.set(text, out);
  return out;
}

function splitSegments(text: string): Segment[] {
  const out: Segment[] = [];
  const re = /(?:^|\n)\[error\]\s([\s\S]*?)(?=\n\[error\]|\s*$)/g;
  let last = 0;
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    const prose = text.slice(last, m.index).trim();
    if (prose) out.push({ kind: "md", text: prose });
    out.push({ kind: "error", text: tidyError(m[1]) });
    last = m.index + m[0].length;
  }
  const tail = text.slice(last).trim();
  if (tail) out.push({ kind: "md", text: tail });
  return out.length ? out : [{ kind: "md", text }];
}

/** Extract the text from a message content that may be a plain string or an
 * [OI] content-parts list carrying text + image_url blocks. */
export function contentText(content: unknown): string {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    const parts = content as Array<Record<string, unknown>>;
    return parts
      .filter((p) => typeof p?.text === "string")
      .map((p) => p.text as string)
      .join("\n");
  }
  return typeof content === "string" ? content : "";
}

/** Pull the image URLs (data: or http(s):) out of a content-parts list. */
export function contentImages(content: unknown): string[] {
  if (!Array.isArray(content)) return [];
  const urls: string[] = [];
  for (const p of content as Array<Record<string, unknown>>) {
    const img = (p?.image_url as { url?: string } | undefined)?.url ||
      (p?.image as { url?: string } | undefined)?.url;
    if (typeof img === "string" && img) urls.push(img);
  }
  return urls;
}

/** A message is "empty" when it has nothing visible to render: whitespace-only
 * text, no tool/step timeline, no reasoning. Tool rounds whose model emitted
 * only newlines (e.g. content "\n\n") persist as such and must not become a
 * blank bubble. That rule also decides whether the row is draggable. */
export function isEmptyMessage(msg: ChatMessage): boolean {
  if (msg.reasoning || (msg.steps && msg.steps.length > 0)) return false;
  if (contentImages(msg.content).length) return false;
  return contentText(msg.content).trim() === "";
}

/** One visual "turn" = an optional user bubble + the assistant bubble that
 * carries that turn's display timeline. The brain persists every protocol
 * round separately (intermediate whitespace assistant rows + tool rows), but
 * the FINAL assistant row of a turn carries the whole `steps` list
 * (reasoning + tool + text in order). So we collapse each user → assistant
 * exchange into a single bubble, matching the streaming draft, and hide the
 * intermediate protocol rows. */
export interface Turn {
  user: ChatMessage | null;
  assistant: ChatMessage | null;
  assistantIdx: number;
  /** compaction divider: a `system` marker row, rendered instead of a bubble */
  divider?: ChatMessage;
  /** subagent report: a background child's result delivered to the orchestrator, rendered as a notice (not a user bubble) */
  subagent?: ChatMessage;
}

const SUBAGENT_RESULT_MARKER = "[subagent result]";

/** A `system` row the transcript renders as a compaction divider. Every other
 * system row (persona, skills) is rebuilt per turn and never persisted.
 * Label/detail formatting is shared with the other shells — see ../../compaction. */
function isCompactionRow(msg: ChatMessage): boolean {
  return compactionDivider(msg) !== null;
}

/** A background subagent's completion, pushed into the parent session by the
 * brain with this content prefix. Rendered as a notice, never a user bubble. */
function isSubagentResult(msg: ChatMessage): boolean {
  return (
    msg.role === "user" &&
    typeof msg.content === "string" &&
    msg.content.startsWith(SUBAGENT_RESULT_MARKER)
  );
}

export function subagentResultParts(msg: ChatMessage): { label: string; body: string } {
  const text = typeof msg.content === "string" ? msg.content : "";
  const rest = text.slice(SUBAGENT_RESULT_MARKER.length).trim();
  const idx = rest.indexOf(":");
  if (idx < 0) return { label: "subagent", body: rest };
  return { label: rest.slice(0, idx).trim() || "subagent", body: rest.slice(idx + 1).trim() };
}

/** The divider label + body, formatted the same way in every shell. `d` is
 * only ever called on a row `isCompactionRow` accepted, so the null branch
 * is unreachable — it satisfies the type without a non-null assertion. */
export function compactionLabel(msg: ChatMessage): string {
  return compactionDivider(msg)?.label ?? "context compacted";
}

export function compactionDetail(msg: ChatMessage): string {
  return compactionDivider(msg)?.detail ?? "";
}

export function isCompactionFailure(msg: ChatMessage): boolean {
  return compactionDivider(msg)?.failed ?? false;
}

/** The assistant row that ENDS a turn is the one not immediately followed by
 * a `tool` row. Intermediate tool-round assistant rows (which announced the
 * following tool call) carry only their own short text and are folded into
 * the terminal row's full `steps` timeline, so they are hidden. */
function isTurnEnd(messages: ChatMessage[], i: number): boolean {
  const next = messages[i + 1];
  return !next || next.role !== "tool";
}

export function displayTurns(messages: ChatMessage[]): Turn[] {
  const turns: Turn[] = [];
  let i = 0;
  const pushDividers = (from: number, to: number) => {
    for (let k = from; k < to && k < messages.length; k++) {
      if (isCompactionRow(messages[k])) {
        turns.push({ user: null, assistant: null, assistantIdx: -1, divider: messages[k] });
      }
    }
  };
  while (i < messages.length) {
    const msg = messages[i];
    if (isCompactionRow(msg)) {
      // A compaction marker — its own full-width divider, in transcript order.
      turns.push({ user: null, assistant: null, assistantIdx: -1, divider: msg });
      i++;
    } else if (isSubagentResult(msg)) {
      turns.push({ user: null, assistant: null, assistantIdx: -1, subagent: msg });
      i++;
    } else if (msg.role === "user") {
      // find the terminal assistant of this turn: first assistant row that is
      // not followed by a tool result
      let j = i + 1;
      let lastAssistant = -1;
      while (j < messages.length && messages[j].role !== "user") {
        if (messages[j].role === "assistant") lastAssistant = j;
        if (messages[j].role === "assistant" && isTurnEnd(messages, j)) break;
        j++;
      }
      if (j < messages.length && messages[j].role === "assistant" && isTurnEnd(messages, j)) {
        turns.push({ user: msg, assistant: messages[j], assistantIdx: j });
        pushDividers(i + 1, j);
        i = j + 1;
      } else if (lastAssistant >= 0 && j < messages.length && messages[j].role === "user") {
        // A queued user message broke the turn: the preceding assistant
        // (with its tool rounds) is this turn's terminal assistant.
        turns.push({ user: msg, assistant: messages[lastAssistant], assistantIdx: lastAssistant });
        pushDividers(i + 1, lastAssistant);
        i = j; // skip to the queued user message
      } else {
        // reply still pending or no assistant yet — show just the user bubble
        turns.push({ user: msg, assistant: null, assistantIdx: -1 });
        pushDividers(i + 1, j);
        i = j >= messages.length ? messages.length : j;
      }
    } else if (msg.role === "assistant" && isTurnEnd(messages, i) && !isEmptyMessage(msg)) {
      // a lone final reply with no preceding user in this window (e.g. after
      // compaction) — one bubble on its own; skip pure-whitespace assistants
      turns.push({ user: null, assistant: msg, assistantIdx: i });
      i++;
    } else {
      i++; // tool rows and intermediate tool-round assistant rows — hidden
    }
  }
  return turns;
}

/** Tool-step count of a turn — divider/subagent turns carry none. */
function turnSteps(turn: Turn): number {
  return turn.assistant?.steps?.length ?? 0;
}

/** Size one transcript mount batch walking back from index `end`, bounded by
 * BOTH turn count and total tool steps. The mount window counts turns, but a
 * single turn can carry 100+ tool steps — with a turn-only bound, "20 turns"
 * can mean 600 ToolChips, and the synchronous mount is the session-switch
 * freeze the window was built to avoid. Always returns >= 1 when end > 0 so
 * one oversized turn can't stall backfill. */
export function mountBatch(all: Turn[], end: number, maxTurns: number, stepBudget: number): number {
  let steps = 0;
  let count = 0;
  while (count < end && count < maxTurns) {
    const s = turnSteps(all[end - 1 - count]);
    if (count > 0 && steps + s > stepBudget) break;
    steps += s;
    count++;
  }
  return count;
}

/** Every tool step in the history, across all turns. The mount window budgets
 *  per batch, but the decision to mount eagerly is made on the TOTAL: a
 *  step-dense turn in the middle must not clamp a small session to one turn. */
export function totalSteps(all: Turn[]): number {
  let n = 0;
  for (const t of all) n += turnSteps(t);
  return n;
}

/** Steps cheap enough to mount in one synchronous pass when a session opens.
 *  The window exists to defuse histories carrying ~2000 tool steps, where the
 *  synchronous ToolChip mount froze the shell for seconds; 600 stays well clear
 *  of that while covering ordinary sessions whole. Anything above windows. */
export const EAGER_STEPS = 600;

/** Turns to mount on a session's first paint: the whole history when it fits
 *  the eager budget, otherwise one step-aware batch off the tail.
 *
 *  Getting this wrong in the timid direction is worse than the freeze it
 *  prevents — a 4-turn session whose tail turn carries 171 steps armed to ONE
 *  turn under a per-batch budget, and the other three turns then looked
 *  deleted: "when I sent a message all my old responses disappeared". */
export function armMount(all: Turn[], maxTurns: number, stepBudget: number): number {
  const len = all.length;
  if (len === 0) return 0;
  if (totalSteps(all) <= EAGER_STEPS) return len;
  return mountBatch(all, len, maxTurns, stepBudget);
}

/** Where the reader is, and so what a prepend must do to keep their view still.
 *
 *  A prepend adds height ABOVE the viewport, which silently moves content
 *  under a reader unless the scroll offset is corrected — and which correction
 *  is right depends entirely on where they are:
 *
 *  - `"edge"`: on the newest response. This is where opening a session or
 *    refreshing leaves them. Re-pin to the bottom; the earlier one-shot
 *    reference correction left scrollTop alone while scrollHeight grew, so
 *    batch after batch the viewport marched to the top of the transcript.
 *  - `"hold"`: scrolled back into the mounted window and about to run out of
 *    history. Hold the row under the top edge instead — pinning them to the
 *    bottom from here would be a yank.
 *  - `null`: mid-window, reading. Mount nothing until they move.
 *
 *  A session that fits on screen is always `"edge"` (both gaps are 0 or
 *  negative), which is honest: there is nothing above to correct for. */
export type BackfillMode = "edge" | "hold" | null;

export function backfillMode(
  box: { scrollTop: number; scrollHeight: number; clientHeight: number },
  opts: { edgeSlack?: number; triggerViews?: number } = {},
): BackfillMode {
  const view = box.clientHeight;
  if (view <= 0) return null;
  const edgeSlack = opts.edgeSlack ?? 80;
  const triggerViews = opts.triggerViews ?? 1;
  const fromBottom = box.scrollHeight - box.scrollTop - view;
  if (fromBottom <= edgeSlack) return "edge";
  if (box.scrollTop <= view * triggerViews && fromBottom > view) return "hold";
  return null;
}

/** Compress a provider error payload into one readable line.
 * Handles "502 {json}" shapes and nested {"error":{"message":…}} chains. */
export function tidyError(raw: string): string {
  let status = "";
  let body = raw.trim();
  const statusMatch = body.match(/^(\d{3})\s+(\{[\s\S]*)$/);
  if (statusMatch) {
    status = statusMatch[1];
    body = statusMatch[2];
  }
  // walk nested {"error": ...} / {"message": ...} to the human sentence
  for (let depth = 0; depth < 6; depth++) {
    let candidate = body;
    try {
      let parsed: Record<string, unknown>;
      try {
        parsed = JSON.parse(candidate) as Record<string, unknown>;
      } catch {
        // maybe "prefix text (NNN): {json}" — try the trailing object
        const brace = candidate.indexOf("{");
        if (brace < 0) break;
        parsed = JSON.parse(candidate.slice(brace)) as Record<string, unknown>;
      }
      if (typeof parsed.status === "number" && !status) status = String(parsed.status);
      const err = parsed.error;
      if (typeof err === "string") { body = err; continue; }
      if (err && typeof err === "object") {
        const inner = err as Record<string, unknown>;
        if (typeof inner.message === "string") { body = inner.message; continue; }
      }
      if (typeof parsed.message === "string") { body = parsed.message; continue; }
      break;
    } catch {
      break;
    }
  }
  // truncated payload (brain caps at ~400 chars) — dig out the deepest
  // "message":"…" with a regex instead of full parses.
  if (body.startsWith("{") || body.includes('"message"')) {
    const msgs = [...body.matchAll(/"message"\s*:\s*"((?:[^"\\]|\\.)*)"?/g)];
    if (msgs.length) {
      const last = msgs[msgs.length - 1][1];
      try {
        body = JSON.parse(`"${last}"`) as string;
      } catch {
        body = last;
      }
    }
  }
  const text = body.length > 280 ? body.slice(0, 280) + "…" : body;
  return status ? `HTTP ${status} — ${text}` : text;
}
