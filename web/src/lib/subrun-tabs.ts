/** Sibling tabs for the sub-agent activity view.
 *
 *  A fan-out spawns several sub-agents at once, but the transcript may carry a
 *  single delegate chip for all of them (one tool call with a `tasks` batch).
 *  Opening that chip must therefore reach every sibling, not just the run the
 *  chip bound to — the siblings are the records sharing its `group` (the parent
 *  turn id the brain stamps in `begin_delegation`).
 *
 *  Runnable check: `node checks/subrun-tabs.test.ts`
 */
import type { SubagentRun } from "./types";

/** The runs to tab over, in launch order, given the opened run.
 *
 *  `pool` is every run the shell knows (live pushes merged with whatever the
 *  server returned). Records predating `group`, or a genuinely solitary run,
 *  yield just that run — one tab, which callers render as no tab strip. */
export function siblingRuns(pool: SubagentRun[], openId: string): SubagentRun[] {
  const open = pool.find((r) => r.id === openId);
  if (!open) return [];
  const group = open.group;
  if (!group) return [open];
  const siblings = pool.filter((r) => r.group === group);
  return siblings.sort(byLaunch);
}

/** Launch order, id as tie-break so equal timestamps still sort stably —
 *  siblings of one fan-out are born in the same millisecond. */
function byLaunch(a: SubagentRun, b: SubagentRun): number {
  return (a.started ?? 0) - (b.started ?? 0) || a.id.localeCompare(b.id);
}

/** Merge server-loaded runs with live-pushed ones, newest record per id wins.
 *  Live pushes carry streamed steps the server snapshot may lack, so they take
 *  precedence when both exist. */
export function mergeRuns(loaded: SubagentRun[], live: Record<string, SubagentRun>): SubagentRun[] {
  const byId = new Map<string, SubagentRun>();
  for (const run of loaded) byId.set(run.id, run);
  for (const run of Object.values(live)) {
    const prev = byId.get(run.id);
    // A finished server record outranks a stale running push.
    if (prev && prev.status !== "running" && run.status === "running") continue;
    byId.set(run.id, run);
  }
  return [...byId.values()].sort(byLaunch);
}

/** Short tab label: the squad-member name, else a shortened run id. */
export function runLabel(run: SubagentRun): string {
  return run.child?.trim() || run.id.slice(0, 5);
}

/** Elapsed seconds of a run, 1 decimal. A running run counts up to `now`; a
 *  finished one is fixed. Clamped, so clock skew never renders "-0.3s". */
export function runDuration(run: SubagentRun, now: number = Date.now() / 1000): string {
  const end = run.finished ?? now;
  return `${Math.max(end - run.started, 0).toFixed(1)}s`;
}

/** Whether the activity view still has anything to fetch: an open panel with at
 *  least one sibling still streaming. */
export function needsPoll(openId: string | null, tabs: SubagentRun[]): boolean {
  return Boolean(openId) && tabs.some((r) => r.status === "running");
}
