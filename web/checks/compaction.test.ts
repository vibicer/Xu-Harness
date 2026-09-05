// Runnable check for the transcript's compaction dividers: `node checks/compaction.test.ts`
import assert from "node:assert/strict";
import { compactionDivider } from "../src/lib/compaction.ts";
import type { ChatMessage, Step } from "../src/lib/types.ts";

const row = (content: string, steps: Step[] = []): ChatMessage =>
  ({ role: "system", content, steps }) as ChatMessage;

// 1. A landed checkpoint with stats → sizes in the label, summary as the body
{
  const d = compactionDivider(
    row("[conversation summary]\n<compacted-summary>the gist</compacted-summary>",
        [{ kind: "compaction", before: 12000, after: 3400, dropped: 7 } as Step]),
  );
  assert.ok(d);
  assert.equal(d.failed, false);
  assert.equal(d.label, "context compacted · 12k → 3.4k chars · 7 merged");
  assert.equal(d.detail, "the gist");
}

// 2. No stats step (rows written before stats existed) → label-only, body kept
{
  const d = compactionDivider(row("[conversation summary]\nolder checkpoint"));
  assert.ok(d);
  assert.equal(d.label, "context compacted");
  assert.equal(d.detail, "older checkpoint");
}

// 3. A failure carries the error, and a repeat count when the brain retried
{
  const d = compactionDivider(
    row("[compaction failed] provider refused",
        [{ kind: "compaction", error: "provider refused", count: 3 } as Step]),
  );
  assert.ok(d);
  assert.equal(d.failed, true);
  assert.equal(d.label, "compaction failed · provider refused · ×3");
  assert.equal(d.detail, "provider refused");
}

// 4. A failure with no recorded error still reads as one
{
  const d = compactionDivider(row("[compaction failed]"));
  assert.ok(d);
  assert.equal(d.label, "compaction failed · unknown error");
}

// 5. Every other row is not a divider — persona/skills system rows are rebuilt
//    per turn and must never render as one.
assert.equal(compactionDivider(row("You are Xu, a concise personal AI.")), null);
assert.equal(compactionDivider({ role: "user", content: "[conversation summary] nope" } as ChatMessage), null);
assert.equal(compactionDivider({ role: "assistant", content: "hi" } as ChatMessage), null);

// 6. Content-parts bodies are not strings — must not throw, must not match
assert.equal(compactionDivider({ role: "system", content: [{ text: "x" }] } as unknown as ChatMessage), null);

// 7. Only the LAST close tag ends the summary, so nested framing survives
{
  const d = compactionDivider(
    row("[conversation summary]<compacted-summary>a</compacted-summary>b</compacted-summary>"),
  );
  assert.ok(d);
  assert.equal(d.detail, "a</compacted-summary>b");
}

// 8. Sub-1000 sizes are not abbreviated
{
  const d = compactionDivider(
    row("[conversation summary]x", [{ kind: "compaction", before: 900, after: 120 } as Step]),
  );
  assert.ok(d);
  assert.equal(d.label, "context compacted · 900 → 120 chars");
}

console.log("compaction: 8/8 checks passed");
