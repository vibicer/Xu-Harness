// Runnable check for live tool event merging: `node checks/tool-events.test.ts`
import assert from "node:assert/strict";
import type { TurnDraft } from "../src/lib/store/shared.ts";
import { registerFrontendImports } from "./support/frontend-node.ts";

const hooks = registerFrontendImports();
const { EventsMixin } = await import("../src/lib/store/events.svelte.ts");
const { StoreCoreBase } = await import("../src/lib/store/core.svelte.ts");
hooks.deregister();

// Exercise the actual mixin's event handler. No store boot, network, or runes.
class DraftBase extends StoreCoreBase {
  draft: TurnDraft = { steps: [], reasoning: "" };

  protected get activeSessionId(): string {
    return "s1";
  }

  protected isOpenTab(id: string): boolean {
    return id === "s1";
  }

  protected applyDraftFor(
    _sid: string,
    updater: (draft: TurnDraft) => TurnDraft,
  ): void {
    this.draft = updater(this.draft);
  }
}

class TestEvents extends EventsMixin(DraftBase) {
  emitTool(params: Record<string, unknown>): void {
    this.onEvent("turn.tool", { session_id: "s1", ...params });
  }
}

const events = new TestEvents();
events.emitTool({
  tool: "bash",
  args: "command=npm test",
  status: "running",
  call_id: "call-1",
  note: "Run the focused frontend checks",
  cwd: "/work/web",
});

assert.deepEqual(
  { note: events.draft.steps[0].note, cwd: events.draft.steps[0].cwd },
  { note: "Run the focused frontend checks", cwd: "/work/web" },
  "the running chip receives display metadata",
);

events.emitTool({
  tool: "bash",
  status: "ok",
  call_id: "call-1",
  elapsed: 1.2,
  output: "passed",
});

assert.equal(
  events.draft.steps.length,
  1,
  "the settling event updates the running chip",
);
assert.equal(
  events.draft.steps[0].args,
  "command=npm test",
  "partial settlement preserves arguments",
);
assert.equal(
  events.draft.steps[0].note,
  "Run the focused frontend checks",
  "partial settlement preserves note",
);
assert.equal(
  events.draft.steps[0].cwd,
  "/work/web",
  "partial settlement preserves cwd",
);
assert.equal(events.draft.steps[0].status, "ok");
assert.equal(events.draft.steps[0].output, "passed");

// Some history/live snapshots arrive without a preceding running event.
events.emitTool({
  tool: "read",
  args: "path=src/main.ts",
  status: "ok",
  call_id: "call-2",
  note: "Inspect the event handler",
  cwd: "/work",
  output: null,
});
assert.equal(events.draft.steps[1].note, "Inspect the event handler");
assert.equal(events.draft.steps[1].cwd, "/work");

// Missing ids still use the legacy matching rule, without synthesizing metadata.
events.emitTool({ tool: "glob", args: "pattern=*.ts", status: "running" });
events.emitTool({ tool: "glob", status: "ok", output: "main.ts" });
assert.equal(events.draft.steps.length, 3);
assert.equal(events.draft.steps[2].args, "pattern=*.ts");
assert.equal(events.draft.steps[2].note, undefined);
assert.equal(events.draft.steps[2].cwd, undefined);

// Concurrent calls settle by id, never by their common tool name.
events.emitTool({
  tool: "bash",
  call_id: "a",
  status: "running",
  note: "List files",
  cwd: "/a",
});
events.emitTool({
  tool: "bash",
  call_id: "b",
  status: "running",
  note: "Read status",
  cwd: "/b",
});
events.emitTool({
  tool: "bash",
  call_id: "b",
  status: "error",
  output: "failed",
});
assert.equal(events.draft.steps[3].status, "running");
assert.equal(events.draft.steps[4].note, "Read status");
assert.equal(events.draft.steps[4].cwd, "/b");
assert.equal(events.draft.steps[4].status, "error");

// Invalid/absent metadata cannot erase the earlier strings or become "[object Object]".
events.emitTool({
  tool: "bash",
  call_id: "a",
  status: "ok",
  note: null,
  cwd: { path: "/wrong" },
});
assert.equal(events.draft.steps[3].note, "List files");
assert.equal(events.draft.steps[3].cwd, "/a");
events.emitTool({
  tool: "bash",
  call_id: "a",
  status: "ok",
  note: "",
  cwd: "",
});
assert.equal(
  events.draft.steps[3].note,
  "",
  "an explicit empty string is a real update",
);
assert.equal(events.draft.steps[3].cwd, "");

// Delegate activity metadata must survive a sparse settling event too.
events.emitTool({
  tool: "delegate",
  call_id: "delegate-1",
  status: "running",
  args: "label=reviewer prompt=review it",
  note: "Review the new tool rows",
  cwd: "/work",
  subagent_run: "run-1",
  subagent: "reviewer",
  subagent_count: 3,
});
events.emitTool({
  tool: "delegate",
  call_id: "delegate-1",
  status: "ok",
  output: "done",
});
const delegate = events.draft.steps.at(-1)!;
assert.equal(delegate.subagent_run, "run-1");
assert.equal(delegate.subagent, "reviewer");
assert.equal(delegate.subagent_count, 3);
assert.equal(delegate.note, "Review the new tool rows");

// Image payloads remain available after later updates.
events.emitTool({
  tool: "show_image",
  call_id: "image-1",
  status: "ok",
  args: "path=chart.png",
  image: "data:image/png;base64,AA==",
  image_alt: "A chart",
  note: "Show the comparison",
  cwd: "/work",
});
events.emitTool({ tool: "show_image", call_id: "image-1", status: "ok" });
assert.equal(events.draft.steps.at(-1)!.image, "data:image/png;base64,AA==");
assert.equal(events.draft.steps.at(-1)!.image_alt, "A chart");
assert.equal(events.draft.steps.at(-1)!.note, "Show the comparison");

const count = events.draft.steps.length;
events.emitTool({
  session_id: "closed",
  tool: "read",
  status: "ok",
  note: "Do not attach elsewhere",
});
assert.equal(events.draft.steps.length, count);
