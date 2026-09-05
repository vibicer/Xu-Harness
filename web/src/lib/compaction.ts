/** Compaction checkpoints, as the transcript renders them.
 *
 *  The brain persists a compaction as a `system` row whose content starts with
 *  a marker — either the landed checkpoint or the failure note. Every other
 *  `system` row (persona, skills, rules) is rebuilt per turn and never stored,
 *  so a marker prefix is a reliable test. Stats live in a `compaction` step;
 *  rows written before those existed render label-only.
 *
 *  Shared because three shells draw the same divider (Classic, Neo, Garden).
 *
 *  Runnable check: `node checks/compaction.test.ts`
 */
import type { ChatMessage, Step } from "./types";

export const CHECKPOINT_MARKER = "[conversation summary]";
export const FAILURE_MARKER = "[compaction failed]";
const SUMMARY_OPEN = "<compacted-summary>";
const SUMMARY_CLOSE = "</compacted-summary>";

export interface CompactionDivider {
  label: string;
  /** Checkpoint text with framing stripped, or the raw error. May be empty. */
  detail: string;
  failed: boolean;
}

/** Chars, abbreviated — 1200 → "1.2k". */
function kchars(n: number): string {
  return n >= 1000 ? `${Math.round(n / 100) / 10}k` : String(n);
}

/** The divider for a compaction row, or null when the message is not one. */
export function compactionDivider(msg: ChatMessage): CompactionDivider | null {
  if (msg.role !== "system" || typeof msg.content !== "string") return null;
  const text = msg.content;
  const failed = text.startsWith(FAILURE_MARKER);
  if (!failed && !text.startsWith(CHECKPOINT_MARKER)) return null;
  const s = (msg.steps ?? []).find((x: Step) => x.kind === "compaction");

  let label: string;
  if (failed) {
    const err = (s?.error ?? text.slice(FAILURE_MARKER.length).trim()) || "unknown error";
    // A repeated failure is recorded as one row with a count, not N rows.
    label = `compaction failed · ${err}${s?.count && s.count > 1 ? ` · ×${s.count}` : ""}`;
  } else if (s?.before != null && s?.after != null) {
    const merged = s.dropped ? ` · ${s.dropped} merged` : "";
    label = `context compacted · ${kchars(s.before)} → ${kchars(s.after)} chars${merged}`;
  } else {
    label = "context compacted";
  }

  const open = text.indexOf(SUMMARY_OPEN);
  const close = text.lastIndexOf(SUMMARY_CLOSE);
  const detail = failed
    ? text.slice(FAILURE_MARKER.length).trim()
    : open >= 0 && close > open
      ? text.slice(open + SUMMARY_OPEN.length, close).trim()
      : text.slice(CHECKPOINT_MARKER.length).trim();

  return { label, detail, failed };
}
