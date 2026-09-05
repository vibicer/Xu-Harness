// Runnable check for delegate-round collapsing: `node checks/delegate-round.test.ts`
import assert from "node:assert/strict";
import { collapseDelegates } from "../src/lib/delegate-round.ts";
import type { Step } from "../src/lib/types.ts";

const del = (over: Partial<Step> = {}): Step => ({
  kind: "tool",
  tool: "delegate",
  args: "label=reviewer prompt=go",
  status: "ok",
  elapsed: 1,
  output: "done",
  subagent_count: 5,
  ...over,
});
const bash = (): Step => ({ kind: "tool", tool: "bash", args: "command=ls", status: "ok" });
const text = (t: string): Step => ({ kind: "text", text: t });

// 1. Five concurrent delegate chips with the same result → one chip.
{
  const steps = Array.from({ length: 5 }, (_, i) =>
    del({ subagent: `reviewer-${i + 1}`, subagent_run: `d${i}` }),
  );
  const out = collapseDelegates(steps);
  assert.equal(out.length, 1, "a round collapses to one chip");
  assert.equal(out[0].subagent_count, 5, "the count still carries the round");
  assert.equal(out[0].output, "done", "identical outputs collapse to one copy");
  assert.equal(out[0].subagent_run, "d0", "keeps a run id so activity still opens");
  assert.equal(out[0].subagent, null, "one name would misrepresent five agents");
}

// 2. A single delegate chip is untouched (and the array identity is kept).
{
  const steps = [text("hi"), del(), bash()];
  assert.equal(collapseDelegates(steps), steps, "same reference when nothing merges");
}

// 3. Non-delegate steps in between keep the rounds separate.
{
  const steps = [del(), del(), bash(), del(), del(), del()];
  const out = collapseDelegates(steps);
  assert.deepEqual(out.map((s) => s.tool), ["delegate", "bash", "delegate"]);
  assert.equal(out[0].subagent_count, 5);
  assert.equal(out[2].subagent_count, 5);
}

// 4. Status is the worst of the round: still running beats ok, error beats ok.
{
  assert.equal(collapseDelegates([del(), del({ status: "running" })])[0].status, "running");
  assert.equal(collapseDelegates([del(), del({ status: "error" })])[0].status, "error");
  assert.equal(collapseDelegates([del(), del()])[0].status, "ok");
}

// 5. Elapsed is the round's wall time (the slowest sibling), not the first.
{
  const out = collapseDelegates([del({ elapsed: 2 }), del({ elapsed: 9 }), del({ elapsed: null })]);
  assert.equal(out[0].elapsed, 9);
}

// 6. Differing outputs are all kept, labelled — nothing is silently dropped.
{
  const out = collapseDelegates([
    del({ subagent: "a", output: "found X" }),
    del({ subagent: "b", output: "found Y" }),
  ]);
  assert.match(out[0].output ?? "", /found X/);
  assert.match(out[0].output ?? "", /found Y/);
  assert.match(out[0].output ?? "", /── a ──/);
}

// 7. A round of chips that never produced output leaves output null (so the
//    chip stays non-expandable rather than opening an empty pane).
{
  const out = collapseDelegates([del({ output: null }), del({ output: null })]);
  assert.equal(out[0].output, null);
}

// 8. Count falls back to the group size when the brain sent no count.
{
  const out = collapseDelegates([
    del({ subagent_count: null }),
    del({ subagent_count: null }),
    del({ subagent_count: null }),
  ]);
  assert.equal(out[0].subagent_count, 3);
}

console.log("delegate-round: 8/8 checks passed");
