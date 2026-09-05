/** Sub-agent activity view — behaviour only, no markup.
 *
 *  Four shells each draw this panel their own way (a centred modal, a docked
 *  aside, a radial orb-field), but they all need the same *decisions*: which
 *  run is open, which siblings to tab over, whether to poll, when to give up.
 *  Those lived in three near-identical copies, and every fix had to land three
 *  times — the live-edge scroll bug was fixed wrong in all three at once, and
 *  a `min-height: 0` present in one copy was missing from another.
 *
 *  Same split as `tab-drag.svelte.ts`: logic here, look in the layout.
 *
 *  Runnable check: `node checks/subrun-view.test.ts` — the rune-driven parts
 *  need a component to host them, so the check covers the pure decision rules
 *  in `subrun-tabs.ts` (`needsPoll`, `runDuration`, the pool/tab reducers) that
 *  this module wires together.
 */
import { brain } from "./store.svelte";
import { mergeRuns, needsPoll, runDuration, siblingRuns } from "./subrun-tabs";
import { followEdge } from "./follow-edge";
import type { SubagentRun } from "./types";

/** How often to re-fetch while a sibling is still running. */
const POLL_MS = 1000;

export interface SubRunView {
  /** The open run's id, or null when the panel is closed. */
  readonly id: string | null;
  /** Siblings of the open run, in launch order. One entry = no tab strip. */
  readonly tabs: SubagentRun[];
  /** The open run itself. */
  readonly run: SubagentRun | null;
  /** True only for the first fetch of a newly opened run. */
  readonly loading: boolean;
  /** Human-readable failure, or "" — render it instead of the body. */
  readonly error: string;
  /** Open a run (fetches it and its group). */
  open(runId: string): Promise<void>;
  /** Same, but clicking the already-open run closes the panel. */
  toggle(runId: string): void;
  /** Switch tabs within the open group — no fetch, the pool already has it. */
  select(runId: string): void;
  close(): void;
  /** `{@attach view.follow}` on the scroll box to pin it to the live edge. */
  follow(el: HTMLElement): () => void;
  /** Elapsed seconds, 1 decimal — "4.2s". Running runs count up. */
  duration(run: SubagentRun): string;
}

/** Wire up one activity panel. Call during component init; its effects are
 *  owned by that component, so the poller dies with it. */
export function createSubRunView(): SubRunView {
  let id = $state<string | null>(null);
  let pool = $state<SubagentRun[]>([]);
  let loading = $state(false);
  let error = $state("");
  // The panel belongs to the session that opened it. Without this a background
  // session's run kept polling behind a switched-away tab, and its steps landed
  // in the new session's panel.
  let owner = $state<string | null>(null);

  // Live pushes carry streamed steps the fetched snapshot lacks, so they are
  // merged on read rather than written into the pool.
  const tabs = $derived(id ? siblingRuns(mergeRuns(pool, brain.subRuns), id) : []);
  const run = $derived(tabs.find((r) => r.id === id) ?? null);
  // Read as a boolean, not off the pool: the poller must restart when the group
  // settles, not every time a token grows a step.
  const polling = $derived(needsPoll(id, tabs));

  const mine = (): boolean => owner === (brain.session?.id ?? null);

  /** Fetch the run, then its whole sibling group. Live pushes cover the rest,
   *  so this only has to catch what streaming missed: a reopened chip, or a
   *  sibling born before this shell connected. */
  async function load(runId: string): Promise<SubagentRun | null> {
    if (!mine()) return null;
    try {
      const one = await brain.subagentActivity({ run_id: runId });
      // An await is a chance for the user to switch sessions or close the
      // panel; re-check before publishing anything.
      if (!mine() || id !== runId) return null;
      const opened = one[0] ?? brain.subRuns[runId] ?? null;
      error = opened ? "" : "activity for this run is no longer available";
      if (!opened) {
        pool = [];
        return null;
      }
      const group = opened.group ? await brain.subagentActivity({ group: opened.group }) : [];
      if (!mine() || id !== runId) return null;
      pool = mergeRuns(group.length ? group : [opened], { [opened.id]: opened });
      return opened;
    } catch (e) {
      error = e instanceof Error ? e.message : String(e);
      return null;
    }
  }

  // Poll only while something is still streaming; the effect stops itself once
  // the group settles, so there is no timer to clear by hand.
  $effect(() => {
    const open = id;
    if (!open || !polling) return;
    const timer = setInterval(() => void load(open), POLL_MS);
    return () => clearInterval(timer);
  });

  // A session switch is not a reason to keep someone else's panel on screen.
  $effect(() => {
    const active = brain.session?.id ?? null;
    if (owner !== null && owner !== active) close();
  });

  // Layout → panel handshake: a layout that does not own the panel (Simple's
  // topbar chip) asks for a run by id and whoever owns the panel opens it.
  $effect(() => {
    const wanted = brain.requestSubRunId;
    if (!wanted) return;
    brain.requestSubRunId = null;
    void open(wanted);
  });

  async function open(runId: string): Promise<void> {
    owner = brain.session?.id ?? null;
    id = runId;
    pool = [];
    error = "";
    loading = true;
    try {
      await load(runId);
    } finally {
      // Only the run we were opening may clear the spinner — a fast reopen of
      // another run would otherwise unblank the wrong panel.
      if (id === runId) loading = false;
    }
  }

  function close(): void {
    id = null;
    owner = null;
    pool = [];
    error = "";
    loading = false;
  }

  return {
    get id() { return id; },
    get tabs() { return tabs; },
    get run() { return run; },
    get loading() { return loading; },
    get error() { return error; },
    open,
    toggle(runId: string): void {
      if (id === runId) close();
      else void open(runId);
    },
    select(runId: string): void {
      if (tabs.some((r) => r.id === runId)) id = runId;
    },
    close,
    follow: (el: HTMLElement) => followEdge(el),
    duration: runDuration,
  };
}
