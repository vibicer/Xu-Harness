import type { StatePanel, Step, SessionInfo, ChatMessage } from "../types";
/** A shell view. The built-ins are the shell's own; `plugin:<name>` is a full
 *  view contributed by an enabled plugin (`ui.mount: "view"`). */
export type ViewName = "workspace" | "sessions" | "config" | "logs" | "onboarding" | `plugin:${string}`;

/** The shell's own views — the fixed glyph set VIEW_ICONS is keyed by. A
 *  plugin view has no fixed glyph (its manifest names one), so it resolves
 *  through `viewIcon` instead. */
export type BuiltinViewName = Exclude<ViewName, `plugin:${string}`>;

export type CompactionResult = { ok: boolean; error?: string; before?: number; after?: number; dropped?: number };
export interface TurnDraft {
  steps: Step[]; // interleaved text chunks + tool calls, in real order
  reasoning: string; // streamed thinking (rendered in its own block)
  notice?: string; // transient status (e.g. retrying) — never persisted as text
  failed?: string;
}

export type SessionStatus = {
  brain: string;
};
export const EMPTY_DRAFT: TurnDraft = { steps: [], reasoning: "" };

/** Per-tab session state: message history, live streaming draft, and
 * messages the user queued while a turn was running (shown above the
 * composer until the server picks them up). */
export interface TabState {
  session: SessionInfo | null;
  messages: ChatMessage[];
  draft: TurnDraft | null;
  busy: boolean;
  queued: { text: string; images?: string[]; id?: string; key?: string }[];
}

export const DEFAULT_STATE: StatePanel = {
  model: null,
  persona: null,
  rules: [],
  context: 0,
  compress: false,
  tokens: null,
  cwd: "",
  git: null,
  preset: null,
};

/** Append (or extend) a text step at the tail of the timeline. */
export function pushText(steps: Step[], delta: string): Step[] {
  const last = steps[steps.length - 1];
  if (last && last.kind === "text") {
    return [...steps.slice(0, -1), { ...last, text: (last.text ?? "") + delta }];
  }
  return [...steps, { kind: "text", text: delta }];
}

/** Append (or extend) a reasoning step at the tail of the timeline. */
export function pushReasoning(steps: Step[], delta: string): Step[] {
  const last = steps[steps.length - 1];
  if (last && last.kind === "reasoning") {
    return [...steps.slice(0, -1), { ...last, text: (last.text ?? "") + delta }];
  }
  return [...steps, { kind: "reasoning", text: delta }];
}
