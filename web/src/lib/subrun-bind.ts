import type { Step, SubagentRun } from "./types";

/**
 * Bind a just-born sub-agent run onto the delegate chip that spawned it.
 *
 * The brain emits the chip's `turn.tool running` event before the run record
 * exists (it is reserved inside the delegate tool, after validation, so a
 * rejected call never leaks a "running" record) — the chip therefore only
 * learns its run id when the record is born, via the `subagent.activity`
 * push. This is the one moment where the binding is certain.
 *
 * Returns the same array reference when nothing is bound (so callers can
 * skip re-rendering); a new array when exactly one chip was bound.
 */
export function bindSubRun(steps: Step[], run: SubagentRun): Step[] {
  // A chip already bound to THIS run: nothing to do (repeat pushes are normal).
  if (steps.some((s) => s.subagent_run === run.id)) return steps;
  const candidates: number[] = [];
  steps.forEach((s, i) => {
    if (s.kind === "tool" && s.tool === "delegate" && s.status === "running" && !s.subagent_run) {
      candidates.push(i);
    }
  });
  if (!candidates.length) return steps;
  let target = -1;
  if (run.child) {
    // Several live delegate chips: prefer a name match (chip args look like
    // "child=researcher prompt=…"). With concurrent siblings the first unbound
    // match is the right one — the earlier ones already took their own run.
    const named = candidates.filter((i) => chipChild(steps[i]) === run.child);
    if (named.length) target = named[0];
  }
  // No name on the run, or no chip carries it: only safe when exactly one chip
  // is still unbound, otherwise leave it for the settling event to resolve.
  if (target < 0 && candidates.length === 1) target = candidates[0];
  if (target < 0) return steps;
  return steps.map((s, i) => (i === target ? { ...s, subagent_run: run.id, subagent: run.child } : s));
}

/** The squad-member name from a chip's one-line args summary, if present.
 *  The delegate tool takes it as `label` and accepts `child` as an alias, so
 *  both spellings can reach a chip. */
function chipChild(step: Step): string | null {
  const m = /(?:^|\s)(?:child|label)=(\S+)/.exec(step.args ?? "");
  return m ? m[1] : null;
}
