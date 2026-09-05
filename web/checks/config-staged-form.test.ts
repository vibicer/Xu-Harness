// Runnable check for the Config staged-form design system:
//   `node checks/config-staged-form.test.ts`
//
// Config content was rebuilt around one vocabulary: a pane is `.cfg-sec`
// sections, a section is `.cfg-row` rows, a row is `.label` (name + `<small>`
// description + staged `.diff`) on the left and `.ctrl` on the right, and every
// control comes from the kit (`.k-btn .k-in .k-step .k-seg .k-dd .k-chip
// .k-slide`) instead of the five overlapping dialects that were there before
// (`.btn`, `.am-btn`, `.am-save`, `.mini-select-*`, `.pfield`).
//
// Regression each group exists for:
//   * row-shape — `.cfg-row` is a two-column CSS grid, so a third element child
//     silently falls onto a second grid line; the layout looks broken but no
//     tool reports anything
//   * retired-dialects — a panel written from an old example reintroduces `.btn`
//     or `.am-*`, whose CSS no longer exists, so it renders unstyled
//   * classes-exist — a typo (`k-bnt`) or an invented class name is invisible
//     at build time: Svelte does not check global class names
//   * staged-fields — the whole point of the redesign is that typed values are
//     not written on every keystroke/blur; an `onchange` that saves a number
//     field is the anti-pattern coming back
//   * dropdown-clipping — `.panel` carries a clip-path, which clips every
//     descendant regardless of z-index, so an absolutely-positioned `.k-dd-pop`
//     inside a `.panel` is invisible
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";

const ROOT = new URL("../", import.meta.url);
const CONFIG_DIR = new URL("src/lib/components/config/", ROOT);
const css = readFileSync(new URL("src/lib/theme.css", ROOT), "utf8")
  + readFileSync(new URL("src/lib/layouts/simple/simple.css", ROOT), "utf8");

let groups = 0;
const group = (name: string, fn: () => void) => { fn(); groups++; void name; };

const panels = readdirSync(CONFIG_DIR).filter((f) => f.endsWith(".svelte")).sort();
const src = new Map(panels.map((f) => [f, readFileSync(new URL(f, CONFIG_DIR), "utf8")]));

interface Tag { name: string; close: boolean; selfClose: boolean; attrs: string; start: number; end: number }

/** Scan real tags. A regex cannot do this: Svelte attribute values hold arrow
 *  functions (`onclick={() => a > b}`), so the first `>` after `<div` is often
 *  not the end of the tag. Track quotes and brace depth instead. */
function tags(text: string): Tag[] {
  const out: Tag[] = [];
  for (let i = 0; i < text.length; i++) {
    if (text[i] !== "<") continue;
    const close = text[i + 1] === "/";
    let j = i + (close ? 2 : 1);
    if (!/[a-zA-Z]/.test(text[j] ?? "")) continue;
    while (j < text.length && /[\w:.-]/.test(text[j])) j++;
    const name = text.slice(i + (close ? 2 : 1), j);
    let depth = 0, quote = "";
    for (; j < text.length; j++) {
      const c = text[j];
      if (quote) { if (c === quote) quote = ""; continue; }
      if (c === '"' || c === "'") { quote = c; continue; }
      if (c === "{") depth++;
      else if (c === "}") depth--;
      else if (c === ">" && depth === 0) break;
    }
    const attrs = text.slice(i + name.length + (close ? 2 : 1), j);
    out.push({ name, close, selfClose: attrs.trimEnd().endsWith("/"), attrs, start: i, end: j });
    i = j;
  }
  return out;
}

const VOID = ["input", "br", "hr", "img", "source", "track"];
const classOf = (attrs: string) => /class="([^"]*)"/.exec(attrs)?.[1] ?? "";

/** Element children of one markup slice, one level deep. Svelte control-flow
 *  blocks are transparent: `{#if}` wrapping a `.ctrl` still yields one child. */
function childClasses(body: string): string[] {
  const out: string[] = [];
  let depth = 0;
  for (const t of tags(body)) {
    if (t.close) { depth--; continue; }
    if (depth === 0) out.push(classOf(t.attrs));
    if (!t.selfClose && !VOID.includes(t.name)) depth++;
  }
  return out;
}

/** Every `.cfg-row` block in a file, as { classes, body }. */
function rows(text: string): { cls: string; body: string }[] {
  const all = tags(text);
  const out: { cls: string; body: string }[] = [];
  for (let i = 0; i < all.length; i++) {
    const open = all[i];
    if (open.close || !/\bcfg-row\b/.test(classOf(open.attrs))) continue;
    let depth = 1;
    for (let j = i + 1; j < all.length && depth > 0; j++) {
      const t = all[j];
      if (t.name !== open.name) continue;
      depth += t.close ? -1 : t.selfClose ? 0 : 1;
      if (depth === 0) out.push({ cls: classOf(open.attrs), body: text.slice(open.end + 1, t.start) });
    }
  }
  return out;
}

// 1. A row is exactly `.label` then `.ctrl` — nothing else at the top level.
group("row-shape", () => {
  let seen = 0;
  for (const [file, text] of src) {
    for (const { cls, body } of rows(text)) {
      const kids = childClasses(body).map((c) => c.split(/\s+/));
      const labels = kids.filter((k) => k.includes("label"));
      const ctrls = kids.filter((k) => k.includes("ctrl"));
      const other = kids.filter((k) => !k.includes("label") && !k.includes("ctrl"));
      const where = `${file} row "${cls}"`;
      assert.equal(labels.length, 1, `${where}: expected 1 .label child, saw ${labels.length}`);
      assert.ok(ctrls.length >= 1, `${where}: no .ctrl child — the control column would be empty`);
      assert.equal(other.length, 0,
        `${where}: extra top-level child (${other.map((o) => o.join(".")).join(", ")}) — the grid gives a row two cells only`);
      seen++;
    }
  }
  assert.ok(seen > 20, `expected the panels to define rows; parsed only ${seen}`);
});

// 2. The dialects the kit replaced must not come back.
group("retired-dialects", () => {
  const bannedToken = new Map<string, string>([
    ["btn", "use k-btn (pri/ghost/dgr/ok/warn/icon/sm)"],
    ["unit", "the unit lives in .k-step .u"],
    ["cfg-group", "the <details> accordion is gone — use .cfg-sec + .cfg-sec-hd"],
    ["model-tag", "use .k-chip"],
    ["model-add", "use .cfg-row + .k-in"],
    ["pfield", "use .cfg-row"],
    ["pfield-l", "the row's .label is the field label"],
  ]);
  const bannedPrefix: [string, string][] = [
    ["am-", "am-* approval styles are gone — use k-* + .cfg-mode"],
    ["mini-select", "use .k-dd + .k-dd-pop"],
  ];
  for (const [file, text] of src) {
    assert.ok(!/<summary>/.test(text), `${file}: the <details> accordion is gone — use .cfg-sec + .cfg-sec-hd`);
    const tokens = new Set([...text.matchAll(/class="([^"{]*)"/g)].flatMap((m) => m[1].split(/\s+/)));
    for (const t of tokens) {
      const why = bannedToken.get(t) ?? bannedPrefix.find(([p]) => t.startsWith(p))?.[1];
      assert.ok(!why, `${file}: class "${t}" is retired — ${why}`);
    }
  }
});

// 3. Every class the panels name exists in the stylesheets.
group("classes-exist", () => {
  const dynamic = /^\{|\}$/; // `class="{x}"` and friends are checked at runtime
  const missing = new Set<string>();
  for (const text of src.values()) {
    const names = [
      ...[...text.matchAll(/class="([^"{]*)"/g)].flatMap((m) => m[1].split(/\s+/)),
      ...[...text.matchAll(/class:([\w-]+)/g)].map((m) => m[1]),
    ].filter((c) => c && !dynamic.test(c));
    for (const c of new Set(names)) if (!css.includes(`.${c}`)) missing.add(c);
  }
  assert.equal(missing.size, 0,
    `classes used by config panels with no rule in theme.css/simple.css: ${[...missing].join(", ")}`);
});

// 4. Typed values stage; only the bar writes them.
group("staged-fields", () => {
  const agent = src.get("AgentPanel.svelte") ?? "";
  assert.match(agent, /class="cfg-bar"/, "AgentPanel must render the commit bar");
  assert.match(agent, /discard/, "the bar must offer discard as well as save");
  // a number/text input that saves on change is the pattern this replaced
  for (const [file, text] of src) {
    for (const m of text.matchAll(/<input\b[^>]*>/g)) {
      const el = m[0];
      const typed = /type=(?:"|\{)?(number|text|url|password)/.test(el) || /bind:value/.test(el);
      const saves = /on(?:change|input)=\{[^}]*\b(?:save|update)/.test(el);
      assert.ok(!(typed && saves),
        `${file}: ${el.slice(0, 90)}… writes on change — typed values stage into a draft and commit from .cfg-bar`);
    }
  }
});

// 5. An absolute dropdown popup inside a clip-path panel is invisible.
group("dropdown-clipping", () => {
  assert.match(css, /\.k-dd-pop \{[^}]*position: absolute/, ".k-dd-pop is expected to be absolutely positioned");
  for (const [file, text] of src) {
    if (!text.includes("k-dd")) continue;
    assert.ok(!/class="[^"]*\bpanel\b/.test(text),
      `${file}: .k-dd inside a .panel — .panel's clip-path cuts the popup off; use .cfg-sec sections`);
  }
});

console.log(`config-staged-form: ${groups} groups passed (${panels.length} panels)`);
