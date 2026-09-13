// Runnable check for the mobile CSS contracts: `node checks/mobile-css.test.ts`
//
// Why these are source assertions and not browser tests: both bugs they pin
// were invisible to headless Chromium. It has no browser chrome, so
// `visualViewport.height === innerHeight`, `--kb` stayed 0, and every render
// check passed — while a real phone showed nothing but the input. Only a device
// with a URL bar can fail them, so the guard has to be the text itself.
//
// Regression each group exists for:
//   * dvh + --kb — `100dvh` already *is* the visible box, and `--kb` is
//     `innerHeight - visualViewport.height`, so subtracting both removes the
//     browser chrome twice. On a keyboard-open iPhone that is a 16px chat.
//   * `#astate` scoping — theme.css is shared by every layout, and `#astate` is
//     the AgentState *component's* root: the default layout uses it as the rail,
//     simple nests it *inside* `.simple-rail`. Unscoped, the drawer rule
//     translated simple's content out of its own panel — an empty drawer.
//   * clipped drawer parents — a translated element still counts toward
//     `scrollWidth`, so an off-canvas rail gave the page a sideways pan.
//   * keyboard close — a drawer you can only dismiss by tapping is not dismissible.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../", import.meta.url);
const read = (p) => readFileSync(new URL(p, ROOT), "utf8");

/** CSS comments are stripped before scanning: the mobile section documents the
 *  `100dvh` + `--kb` trap in prose, and matching that prose would fail the very
 *  check whose job is to keep the prose honest. */
const strip = (css) => css.replace(/\/\*[\s\S]*?\*\//g, "");

const rawTheme = read("src/lib/theme.css");
const theme = strip(rawTheme);
const simpleCss = strip(read("src/lib/layouts/simple/simple.css"));

// ---------- 1. never subtract the browser chrome twice ----------
/* Slice from the banner's own `/*`, so strip() sees a complete comment: the
   prose inside it names the 100dvh + --kb trap, and a half-stripped comment
   would leave that prose looking like CSS. */
const banner = rawTheme.indexOf("MOBILE — the default layout");
assert.ok(banner > -1, "theme.css must carry the mobile section banner");
const mobileCss = strip(rawTheme.slice(rawTheme.lastIndexOf("/*", banner)));

for (const [name, css] of [["theme.css", mobileCss], ["simple.css", simpleCss]]) {
  for (const line of css.split("\n")) {
    if (line.includes("100dvh") && line.includes("--kb")) {
      assert.fail(`${name}: "${line.trim()}" mixes 100dvh with --kb — dvh already excludes the browser chrome, so --kb would be counted twice and the chat collapses`);
    }
  }
}
assert.match(
  mobileCss,
  /#app,\s*\.app-root\s*\{\s*height:\s*calc\(\(100vh\s*-\s*var\(--kb/,
  "mobile shell height must be 100vh minus --kb — one subtraction, against the layout viewport",
);

// ---------- 2. #astate rules must name the layout that owns them ----------
const unscoped = mobileCss
  .split("\n")
  .filter((l) => /#astate\b(?!-toggle)/.test(l) && !/\.app-root/.test(l))
  .map((l) => l.trim());
assert.deepEqual(
  unscoped,
  [],
  "theme.css is shared by all layouts — every #astate rule in the mobile section needs an .app-root ancestor",
);

// ---------- 3. every off-canvas drawer has a clipping parent ----------
assert.match(mobileCss, /#ws-row\s*\{[^}]*overflow:\s*hidden/, "default: #ws-row must clip its off-canvas rail");
assert.match(simpleCss, /\.simple-work\s*\{[^}]*overflow:\s*hidden/, "simple: .simple-work must clip its off-canvas rail");

// ---------- 4. both layouts can actually close their drawer ----------
for (const name of ["default", "simple"]) {
  const src = read(`src/lib/layouts/${name}/Layout.svelte`);
  assert.match(src, /Escape/, `${name}: Escape must dismiss the drawer`);
  assert.match(src, /aria-label="Close agent state"/, `${name}: the backdrop needs an accessible label`);
}

console.log("mobile css: ok");
