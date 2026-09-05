// Runnable check for the activity view's sibling tabs: `node checks/subrun-tabs.test.ts`
import assert from "node:assert/strict";
import { mergeRuns, runLabel, siblingRuns } from "../src/lib/subrun-tabs.ts";
import type { SubagentRun } from "../src/lib/types.ts";

const run = (
  id: string,
  child: string,
  group: string | undefined,
  started = 1,
  status: SubagentRun["status"] = "running",
): SubagentRun => ({
  id, child, prompt: "p", parent_session: "s1", group, status,
  started, finished: null, result: "", steps: [], reasoning: "",
});

// 1. One fan-out → every sibling, in launch order, whichever one was opened
{
  const pool = [
    run("d3", "reviewer-3", "t1", 3),
    run("d1", "reviewer-1", "t1", 1),
    run("d4", "reviewer-4", "t1", 4),
    run("d2", "reviewer-2", "t1", 2),
  ];
  const tabs = siblingRuns(pool, "d3");
  assert.deepEqual(tabs.map((r) => r.id), ["d1", "d2", "d3", "d4"]);
  assert.deepEqual(siblingRuns(pool, "d1").map((r) => r.id), ["d1", "d2", "d3", "d4"]);
}

// 2. Same-millisecond births still sort stably (id tie-break)
{
  const pool = [run("db", "b", "t1", 5), run("da", "a", "t1", 5)];
  assert.deepEqual(siblingRuns(pool, "da").map((r) => r.id), ["da", "db"]);
}

// 3. Other groups and ungrouped runs stay out
{
  const pool = [
    run("d1", "reviewer-1", "t1", 1),
    run("d2", "reviewer-2", "t2", 2),
    run("d3", "loner", undefined, 3),
  ];
  assert.deepEqual(siblingRuns(pool, "d1").map((r) => r.id), ["d1"]);
  assert.deepEqual(siblingRuns(pool, "d3").map((r) => r.id), ["d3"], "no group → itself only");
}

// 4. Unknown id → nothing (caller shows its own error)
assert.deepEqual(siblingRuns([run("d1", "a", "t1")], "nope"), []);

// 5. Live pushes win over server snapshots, but a finished record is not
//    overwritten by a stale running push
{
  const loaded = [run("d1", "reviewer-1", "t1", 1, "ok"), run("d2", "reviewer-2", "t1", 2)];
  const streamed = { ...run("d2", "reviewer-2", "t1", 2), steps: [{ kind: "text", text: "hi" }] } as SubagentRun;
  const merged = mergeRuns(loaded, { d1: run("d1", "reviewer-1", "t1", 1), d2: streamed });
  assert.deepEqual(merged.map((r) => r.id), ["d1", "d2"]);
  assert.equal(merged[0].status, "ok", "finished server record kept");
  assert.equal(merged[1].steps.length, 1, "live steps kept");
}

// 6. A live-only run (born after the fetch) still reaches the tab strip
{
  const merged = mergeRuns([], { d9: run("d9", "reviewer-9", "t1", 9) });
  assert.deepEqual(merged.map((r) => r.id), ["d9"]);
}

// 7. Labels fall back to a short id when the run has no name
assert.equal(runLabel(run("d1", "reviewer-1", "t1")), "reviewer-1");
assert.equal(runLabel(run("dabcdef12", "", "t1")), "dabcd");

console.log("subrun-tabs: 7/7 checks passed");
