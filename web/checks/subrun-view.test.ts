// Runnable check for the shared activity-view rules:
// `node checks/subrun-view.test.ts`
//
// `createSubRunView` itself needs a component to host its runes, so the check
// covers the pure decision rules it wires together — the parts that were
// duplicated (and diverged) across the layouts.
import assert from "node:assert/strict";
import { mergeRuns, needsPoll, runDuration, siblingRuns } from "../src/lib/subrun-tabs.ts";
import type { SubagentRun } from "../src/lib/types.ts";

const run = (id: string, over: Partial<SubagentRun> = {}): SubagentRun => ({
  id, child: id, prompt: "p", parent_session: "s1", group: "t1",
  status: "running", started: 100, finished: null, result: "", steps: [], reasoning: "",
  ...over,
});

// 1. Poll while any sibling runs; stop once the whole group has settled.
{
  const mixed = [run("a", { status: "ok" }), run("b")];
  const settled = [run("a", { status: "ok" }), run("b", { status: "error" })];
  assert.equal(needsPoll("a", mixed), true, "a running sibling keeps the poller alive");
  assert.equal(needsPoll("a", settled), false, "a settled group needs no polling");
  assert.equal(needsPoll(null, mixed), false, "a closed panel never polls");
  assert.equal(needsPoll("a", []), false, "no tabs, nothing to fetch");
}

// 2. Duration counts up while running and freezes when finished.
{
  assert.equal(runDuration(run("a"), 104.22), "4.2s", "running counts up to now");
  assert.equal(runDuration(run("a", { finished: 102.5 }), 999), "2.5s", "finished is fixed");
  // Clock skew must not render "-0.3s".
  assert.equal(runDuration(run("a"), 99.7), "0.0s", "never negative");
}

// 3. Opening any sibling shows the whole group, in launch order — the reason one
//    delegate chip can reach five sub-agents.
{
  const pool = [run("d3", { started: 3 }), run("d1", { started: 1 }), run("d2", { started: 2 })];
  assert.deepEqual(siblingRuns(pool, "d3").map((r) => r.id), ["d1", "d2", "d3"]);
}

// 4. A live push outranks a stale snapshot, but a finished snapshot outranks a
//    stale "running" push — otherwise a settled run flickers back to RUNNING.
{
  const snapshot = [run("d1", { status: "ok", result: "done", finished: 200 })];
  const stalePush = { d1: run("d1", { status: "running" }) };
  assert.equal(mergeRuns(snapshot, stalePush)[0].status, "ok");

  const older = [run("d1", { steps: [] })];
  const fresher = { d1: run("d1", { steps: [{ kind: "text", text: "hi" }] }) };
  assert.equal(mergeRuns(older, fresher)[0].steps.length, 1, "live steps win while running");
}

console.log("subrun-view: 4/4 checks passed");
