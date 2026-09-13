// Runnable check for the zoom-aware overlay caps: `node checks/overlay-caps.test.ts`
//
// Why a source assertion: the bug it pins is measurement-only. The app puts
// `zoom: var(--scale)` on <body>; standardized zoom (Baseline 2024) makes that
// zoomed body the containing block for every fixed-position overlay it holds.
// `inset: 0` still spans the viewport exactly — but raw px and vh/vw caps
// *inside* such an overlay resolve zoom-multiplied: 760px painted 869, 80vh
// painted 91.5vh, and 94vw painted 107vw — a sub-agent activity panel that
// swallowed the window. The fix is caps as a % of the overlay (which telescopes
// to % of the visible viewport at any zoom) plus px wishes divided by --scale.
// Headless Chromium passed every geometry render while doing this silently, so
// the guard has to be the CSS text itself.
//
// The lesson is not shrug: before writing files, confirm the exact rule the
// check will read.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

/** CSS comments are stripped before scanning: the .sub-panel rule documents the
 *  zoom trap in prose (the prose names 80vh and 94vw), and matching that prose
 *  would fail the very check whose job is to keep the prose honest. */
const strip = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");

const theme = strip(read("src/lib/theme.css"));

/** Pull one rule body by its opening selector + `{`, through the first `}`. */
const rule = (selector) => {
  const sel = selector.trim();
  const at = theme.indexOf("\n" + sel + " {");
  if (at < 0) throw new Error(`rule not found: ${selector}`);
  return theme.slice(at, theme.indexOf("}", at));
};

// The four fixed-position overlay panels inside theme.css. Each has an
// absolute-wish width cap and a viewport-fraction height cap; both used to be
// written with raw vh/vw, which the zoomed coordinate space multiplies.
const cases = [
  ["sub-panel", { width: "min(calc(760px / var(--scale, 1)), 94%)", maxHeight: "max-height: 80%" }],
  ["dir-modal-panel", { width: "min(calc(460px / var(--scale, 1)), 92%)", maxHeight: "max-height: 72%" }],
  ["logs-modal-panel", { width: "min(calc(820px / var(--scale, 1)), 96%)", maxHeight: "max-height: 86%" }],
  ["about-panel", { width: "min(calc(460px / var(--scale, 1)), 92%)", maxHeight: "max-height: 88%" }],
];

for (const [selector, { width, maxHeight }] of cases) {
  const body = rule("." + selector);
  assert.ok(body.includes(width), `.${selector}: width cap must be ${width}, got: ${body.split(";")[0]}`);
  assert.ok(body.includes(maxHeight), `.${selector}: height cap must be ${maxHeight || "(none)"}`);
}
