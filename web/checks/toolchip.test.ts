// Server-render component checks: `node checks/toolchip.test.ts` (no browser/server).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { render } from "svelte/server";
import type { Step } from "../src/lib/types.ts";
import { registerFrontendImports } from "./support/frontend-node.ts";

const hooks = registerFrontendImports();
const { default: ToolChip } =
  await import("../src/lib/components/ToolChip.svelte");
hooks.deregister();

function chip(step: Step, startOpen = false): string {
  return render(ToolChip, { props: { step, startOpen, onsub: () => {} } }).body;
}

function head(html: string): string {
  const match = /<button\b[^>]*class="tl-head[^]*?<\/button>/.exec(html);
  assert.ok(match, "the disclosure is a native button");
  return match[0];
}

function text(html: string): string {
  return html.replace(/<[^>]*>/g, "").trim();
}

const step: Step = {
  kind: "tool",
  tool: "bash",
  args: "command=npm test timeout=120",
  note: "Check the tool log regressions",
  cwd: "/work/web",
  status: "ok",
  elapsed: 1.25,
  output: "all passed\n[exit 0 · 1.25s]",
};

// Contract: tool/location/description then status + time/caret, not a command-as-title.
const closed = chip(step);
const row = head(closed);
assert.ok(row.indexOf("tl-tool") < row.indexOf("tl-location"));
assert.ok(row.indexOf("tl-location") < row.indexOf("tl-description"));
assert.ok(row.indexOf("tl-description") < row.indexOf("tl-icon"));
assert.ok(row.indexOf("tl-icon") < row.indexOf("tl-state"));
assert.ok(row.indexOf("tl-state") < row.indexOf("tl-caret"));
assert.ok(row.includes('title="/work/web"'));
assert.ok(row.includes('title="Check the tool log regressions"'));
assert.ok(
  !row.includes("npm test"),
  "the AI note replaces the command in the compact row",
);
assert.ok(!row.includes(" disabled"));
assert.ok(row.includes('aria-expanded="false"'));

const expanded = chip(step, true);
assert.ok(expanded.includes("all passed"));
assert.ok(expanded.includes("exit 0 · 1.25s"));
assert.ok(!expanded.includes("tl-metadata"));
assert.ok(!expanded.includes("Target"));
assert.ok(!expanded.includes("Execution cwd"));
assert.ok(!expanded.includes("Arguments"));
assert.ok(!expanded.includes("command=npm test timeout=120"));
assert.ok(head(expanded).includes('aria-expanded="true"'));

// Without output there is nothing to expand: metadata belongs in the compact
// row only and must not create a second details panel.
for (const [tool, args] of [
  ["bash", "command=printf hello timeout=10"],
  ["eval", "code=1 + 1"],
  ["write", "path=/work/a.txt content=available input"],
  ["edit", "path=/work/a.txt"],
  ["grep", "pattern=TODO path=src"],
  ["odd", "foo=bar"],
]) {
  const noOutput: Step = { kind: "tool", tool, args, status: "running" };
  assert.ok(
    head(chip(noOutput)).includes(" disabled"),
    `${tool}: no output disables disclosure`,
  );
}
const metadataOnly = chip({
  kind: "tool",
  tool: "bash",
  note: "List project files",
  cwd: "/work",
  status: "running",
});
assert.ok(text(head(metadataOnly)).includes("/work"));
assert.ok(text(head(metadataOnly)).includes("List project files"));
assert.ok(head(metadataOnly).includes(" disabled"));
const empty = chip({ kind: "tool", tool: "odd", status: "ok" });
assert.ok(head(empty).includes(" disabled"));
assert.ok(!head(empty).includes("tl-caret"));

// Old history remains literal: command/query fallback but no invented intent/cwd.
const legacy = chip({
  kind: "tool",
  tool: "bash",
  args: "command=pwd",
  status: "ok",
});
assert.ok(text(head(legacy)).includes("pwd"));
assert.ok(!head(legacy).includes("tl-location"));
const legacyFile = chip({
  kind: "tool",
  tool: "read",
  args: "path=/work/src/lib/main.ts",
  status: "ok",
});
assert.ok(head(legacyFile).includes('title="/work/src/lib/main.ts"'));
assert.ok(
  !head(legacyFile).includes("tl-description"),
  "a path is not invented intent",
);
const remote = chip({
  kind: "tool",
  tool: "web_search",
  args: "query=Svelte runes",
  cwd: "/work",
  status: "ok",
});
assert.ok(!head(remote).includes("/work"));
assert.ok(text(head(remote)).includes("Svelte runes"));

// Delegates keep resolved names, counts and the separate activity button.
const delegate: Step = {
  kind: "tool",
  tool: "delegate",
  args: "label=requested prompt=go",
  subagent: "resolved",
  subagent_run: "run-1",
  subagent_count: 1,
  note: "Review the implementation",
  status: "ok",
};
const single = chip(delegate);
assert.ok(text(head(single)).includes("resolved"));
assert.ok(!text(head(single)).includes("requested"));
assert.ok(text(head(single)).includes("1 agent spawned"));
assert.ok(
  single.indexOf("tl-sub") > single.indexOf("</button>"),
  "activity is not nested in the disclosure",
);
assert.ok(single.includes("↳ activity"));
const multiple = chip({ ...delegate, subagent_count: 3 });
assert.ok(text(head(multiple)).includes("3 agents spawned"));
assert.ok(
  !text(head(multiple)).includes("resolved"),
  "a single name must not label a multi-agent round",
);
assert.ok(text(head(multiple)).includes("Review the implementation"));
assert.ok(
  !chip({ ...delegate, subagent_count: 3 }, true).includes("tl-metadata"),
  "multi-agent rows do not restore the removed metadata form",
);

// Images remain visible without opening and retain their zoom control/caption.
const image = chip({
  kind: "tool",
  tool: "show_image",
  args: "path=chart.png",
  image: "data:image/png;base64,AA==",
  image_alt: "A chart",
  status: "ok",
});
assert.ok(image.includes('src="data:image/png;base64,AA=="'));
assert.ok(image.includes('alt="A chart"'));
assert.ok(image.includes('title="zoom image"'));
assert.ok(image.includes("<figcaption"));

// Metadata, arguments, and output are text, never injected HTML.
const hostile = chip(
  {
    ...step,
    note: '<img src=x onerror="evil()">',
    cwd: "<script>evil()</script>",
    args: "command=<unsafe>",
    output: "<unsafe-output>",
  },
  true,
);
assert.ok(!hostile.includes("<img src=x"));
assert.ok(!hostile.includes("<script>evil()"));
assert.ok(!hostile.includes("<unsafe>"));
assert.ok(hostile.includes("&lt;unsafe"));

// Available output should not silently disappear after line 400.
const manyLines = Array.from({ length: 405 }, (_, i) => `line-${i}`).join("\n");
assert.ok(chip({ ...step, output: manyLines }, true).includes("line-404"));

// The two compact fields ellipsize independently.
const source = readFileSync(
  new URL("../src/lib/components/ToolChip.svelte", import.meta.url),
  "utf8",
);
const css = source.slice(source.indexOf("<style>"));
for (const selector of ["tl-location", "tl-description"]) {
  assert.match(
    css,
    new RegExp(`\\.${selector}[^{}]*\\{[^}]*text-overflow:\\s*ellipsis`, "s"),
  );
}
assert.doesNotMatch(
  source,
  /tl-metadata|Execution cwd|<dt>Target|<dt>Arguments/,
);
