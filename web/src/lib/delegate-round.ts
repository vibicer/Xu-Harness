/** Collapse one tool round's delegate chips into a single chip.
 *
 *  A fan-out reaches the transcript as N chips — one per tool call — and the
 *  brain stamps each with the round's *total* count, so five concurrent
 *  sub-agents rendered five identical "5 agents spawned" rows whose activity
 *  buttons all open the same sibling-tabbed view. One chip carries the round.
 *
 *  Consecutiveness is the round: the loop captures every parsed call of a round
 *  before executing any of them, so a round's delegate chips are always
 *  adjacent in `steps`.
 *
 *  Runnable check: `node checks/delegate-round.test.ts`
 */
import type { Step } from "./types";

const isDelegate = (s: Step): boolean => s.kind === "tool" && s.tool === "delegate";

/** Same array reference when there is nothing to collapse, so callers that
 *  memoize on identity keep their DOM. */
export function collapseDelegates(steps: Step[]): Step[] {
  if (steps.filter(isDelegate).length < 2) return steps;
  const out: Step[] = [];
  for (let i = 0; i < steps.length; i++) {
    if (!isDelegate(steps[i])) {
      out.push(steps[i]);
      continue;
    }
    let j = i;
    while (j + 1 < steps.length && isDelegate(steps[j + 1])) j++;
    out.push(j > i ? mergeRound(steps.slice(i, j + 1)) : steps[i]);
    i = j;
  }
  return out.length === steps.length ? steps : out;
}

/** One chip standing for the whole round. The round is still running while any
 *  call is, and failed if any call failed — a green chip over a failed sibling
 *  would hide the failure. */
function mergeRound(group: Step[]): Step {
  const status = group.some((s) => (s.status ?? "running") === "running")
    ? "running"
    : group.some((s) => s.status === "error")
      ? "error"
      : "ok";
  const elapsed = group.reduce<number | null>(
    (max, s) => (s.elapsed == null ? max : Math.max(max ?? 0, s.elapsed)),
    null,
  );
  return {
    ...group[0],
    status,
    elapsed,
    output: mergeOutput(group),
    // The activity view tabs over every sibling of the parent turn, so binding
    // the first run id still reaches all of them.
    subagent_run: group.find((s) => s.subagent_run)?.subagent_run ?? null,
    // A single name is misleading for a round; the count carries it instead.
    subagent: null,
    subagent_count: Math.max(
      group.length,
      ...group.map((s) => s.subagent_count ?? 0),
    ),
  };
}

/** Keep every sibling's output — identical text collapses to one copy, differing
 *  text is labelled per agent so nothing is silently dropped. */
function mergeOutput(group: Step[]): string | null {
  const withOutput = group.filter((s) => s.output);
  if (!withOutput.length) return null;
  const unique = [...new Set(withOutput.map((s) => s.output as string))];
  if (unique.length === 1) return unique[0];
  return withOutput
    .map((s, i) => `── ${s.subagent ?? `agent ${i + 1}`} ──\n${s.output}`)
    .join("\n\n");
}
