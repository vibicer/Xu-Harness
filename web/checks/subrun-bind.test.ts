// Runnable check for the delegate-chip live binding: `node checks/subrun-bind.test.ts`
import assert from "node:assert/strict";
import { bindSubRun } from "../src/lib/subrun-bind.ts";
import type { Step, SubagentRun } from "../src/lib/types.ts";

const run = (id: string, child: string): SubagentRun => ({
  id, child, prompt: "p", parent_session: "s1", status: "running",
  started: 1, finished: null, result: "", steps: [], reasoning: "",
});
const chip = (tool: string, args: string, status: Step["status"] = "running", subagent_run?: string): Step =>
  ({ kind: "tool", tool, args, status, subagent_run });

// 1. Single running delegate chip → bound, other fields preserved
{
  const steps = [chip("delegate", "child=researcher prompt=go")];
  const out = bindSubRun(steps, run("d1", "researcher"));
  assert.notEqual(out, steps, "bound → new array");
  assert.equal(out[0].subagent_run, "d1");
  assert.equal(out[0].subagent, "researcher");
  assert.equal(out[0].args, "child=researcher prompt=go");
  assert.equal(out[0].status, "running");
}

// 2. Two running chips, different children → only the name match is bound
{
  const steps = [
    chip("delegate", "child=researcher prompt=a"),
    chip("delegate", "child=writer prompt=b"),
  ];
  const out = bindSubRun(steps, run("d2", "writer"));
  assert.equal(out[0].subagent_run, undefined, "non-matching chip untouched");
  assert.equal(out[1].subagent_run, "d2");
  assert.equal(out[1].subagent, "writer");
}

// 2b. Same, spelled `label=` — the schema's own key, not just the alias
{
  const steps = [
    chip("delegate", "label=researcher prompt=a"),
    chip("delegate", "label=writer prompt=b"),
  ];
  const out = bindSubRun(steps, run("d2b", "writer"));
  assert.equal(out[0].subagent_run, undefined);
  assert.equal(out[1].subagent_run, "d2b");
}

// 3. Two running chips, same child → the first unbound one takes this run, and
//    the next push takes the other. Concurrent siblings share a label, so
//    refusing to bind here left every sub-agent after the first with no chip.
{
  const steps = [
    chip("delegate", "child=researcher prompt=a"),
    chip("delegate", "child=researcher prompt=b"),
  ];
  const first = bindSubRun(steps, run("d3a", "researcher"));
  assert.equal(first[0].subagent_run, "d3a");
  assert.equal(first[1].subagent_run, undefined, "second chip still waiting");
  const second = bindSubRun(first, run("d3b", "researcher"));
  assert.equal(second[0].subagent_run, "d3a", "first binding preserved");
  assert.equal(second[1].subagent_run, "d3b");
}

// 3b. Four concurrent siblings → four distinct runs, one chip each
{
  let steps = [1, 2, 3, 4].map((n) => chip("delegate", `label=reviewer-${n} prompt=part${n}`));
  for (const n of [4, 1, 3, 2]) steps = bindSubRun(steps, run(`d${n}`, `reviewer-${n}`));
  assert.deepEqual(steps.map((s) => s.subagent_run), ["d1", "d2", "d3", "d4"]);
  assert.equal(new Set(steps.map((s) => s.subagent_run)).size, 4, "no run bound twice");
}

// 3c. Re-push of an already-bound run → same reference (no duplicate binding)
{
  const steps = bindSubRun([chip("delegate", "label=solo prompt=a")], run("dsolo", "solo"));
  assert.equal(bindSubRun(steps, run("dsolo", "solo")), steps, "repeat push → same reference");
}

// 4. Chip already bound → untouched
{
  const steps = [chip("delegate", "child=researcher prompt=a", "running", "d0")];
  assert.equal(bindSubRun(steps, run("d4", "researcher")), steps, "already bound → same reference");
}

// 5. No live delegate chip (other tools / finished chip) → untouched
{
  const steps = [
    chip("bash", "ls"),
    chip("delegate", "child=researcher prompt=a", "ok"),
  ];
  assert.equal(bindSubRun(steps, run("d5", "researcher")), steps, "no running delegate → same reference");
}

// 6. Empty draft → untouched (same reference)
{
  const empty: Step[] = [];
  assert.equal(bindSubRun(empty, run("d6", "researcher")), empty, "empty → same reference");
}

console.log("subrun-bind: 9/9 checks passed");
