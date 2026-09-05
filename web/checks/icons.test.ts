// Runnable check for the Lucide icon registry: `node checks/icons.test.ts`
//
// The shell's glyphs were three systems at once (hand-inlined `{@html}` SVG
// strings in the dock, unicode carets everywhere, a 📎 emoji in the composer).
// They are now one: `src/lib/icons.ts` maps a Lucide slug to a deep-imported
// component and `<Icon name="…" />` is the only way an icon reaches the UI.
//
// Regression each group exists for:
//   * slug/import parity — `check: X` silently renders an ✕ and nothing catches
//     it, because both sides typecheck fine
//   * package reality — a slug Lucide renamed (e.g. `history`) still resolves
//     through a deprecated alias file, so `tsc` passes and the icon is a
//     maintenance trap; only the canonical `<slug>.svelte` counts
//   * call-site names — `<Icon name="chevron_down" />` is a type error today but
//     a *runtime blank* the moment anyone widens the prop, and a blank caret
//     reads as "this row doesn't expand"
//   * VIEW_ICONS totality — the same destination must not be a chat bubble in
//     the dock and a house in the sidebar, which is what having two per-layout
//     glyph tables caused
//   * no glyph regressions — the emoji/box-drawing icons are the thing we
//     removed; a new one is a new visual language
//
// Source-text assertions: `icons.ts` deep-imports Svelte components, so a Node
// test cannot import it. It reads the files instead.
import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";

const root = new URL("..", import.meta.url).pathname;
const iconsSrc = readFileSync(join(root, "src/lib/icons.ts"), "utf8");
const lucideIcons = join(root, "node_modules/@lucide/svelte/dist/icons");

/** Every `.svelte` under src/, recursively. */
function svelteFiles(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
    e.isDirectory()
      ? svelteFiles(join(dir, e.name))
      : e.name.endsWith(".svelte")
        ? [join(dir, e.name)]
        : [],
  );
}

// ---- 1. slug/import parity: key "check" must hold the import of icons/check
{
  const imports = new Map(
    [...iconsSrc.matchAll(/^import (\w+) from "@lucide\/svelte\/icons\/([\w-]+)";$/gm)].map(
      (m) => [m[1], m[2]] as const,
    ),
  );
  assert.ok(imports.size > 20, `expected a real icon set, got ${imports.size}`);

  const body = iconsSrc.split("export const ICONS = {")[1]?.split("} satisfies")[0];
  assert.ok(body, "ICONS map not found — the registry shape changed");

  const entries = [...body.matchAll(/^\s*"?([\w-]+)"?:\s*(\w+),$/gm)].map(
    (m) => [m[1], m[2]] as const,
  );
  assert.equal(entries.length, imports.size, "every import must be mapped exactly once");

  for (const [slug, ident] of entries) {
    assert.equal(
      imports.get(ident),
      slug,
      `ICONS["${slug}"] holds ${ident}, which is imported from icons/${imports.get(ident)}`,
    );
  }

  const dups = entries.map(([s]) => s).filter((s, i, a) => a.indexOf(s) !== i);
  assert.deepEqual(dups, [], "duplicate ICONS keys silently shadow each other");

  // Barrel import would pull ~7700 components into the dev-server graph.
  assert.equal(
    /^import [^;]*from "@lucide\/svelte";$/m.test(iconsSrc.replace(/^import type .*$/gm, "")),
    false,
    "import icons from @lucide/svelte/icons/<name>, not the barrel",
  );
}

// ---- 2. package reality: each slug is a canonical Lucide icon, not an alias
{
  const slugs = [...iconsSrc.matchAll(/@lucide\/svelte\/icons\/([\w-]+)"/g)].map((m) => m[1]);
  assert.ok(existsSync(lucideIcons), "@lucide/svelte is not installed");
  for (const slug of slugs) {
    assert.ok(
      existsSync(join(lucideIcons, `${slug}.svelte`)),
      `icons/${slug} has no canonical ${slug}.svelte — renamed or misspelled`,
    );
  }
}

// ---- 3. every literal <Icon name="…"> is a registry key
{
  const keys = new Set(
    [...(iconsSrc.split("export const ICONS = {")[1] ?? "")
      .split("} satisfies")[0]
      .matchAll(/^\s*"?([\w-]+)"?:\s*\w+,$/gm)].map((m) => m[1]),
  );

  let seen = 0;
  for (const file of svelteFiles(join(root, "src"))) {
    const text = readFileSync(file, "utf8");
    for (const tag of text.match(/<Icon\b[^>]*>/g) ?? []) {
      // `name="x"` and `name={cond ? "a" : "b"}`; `name={m.icon}` has no literal.
      const attr = tag.match(/name=(?:"([\w-]+)"|\{([^}]*)\})/);
      if (!attr) continue;
      // Drop the right side of any comparison first: in
      // `name={kind === "ok" ? "check" : "circle-x"}` the "ok" is a state value,
      // not an icon slug.
      const expr = (attr[2] ?? "").replace(/[=!]==?\s*"[^"]*"/g, "");
      const names = attr[1] ? [attr[1]] : [...expr.matchAll(/"([\w-]+)"/g)].map((m) => m[1]);
      for (const name of names) {
        seen += 1;
        assert.ok(keys.has(name), `${file}: <Icon name="${name}"> is not in ICONS`);
      }
    }
  }
  assert.ok(seen > 30, `expected the shell to use icons, found ${seen} literal names`);
}

// ---- 4. VIEW_ICONS covers every view, with registry keys
{
  const views = readFileSync(join(root, "src/lib/store/shared.ts"), "utf8")
    .match(/export type ViewName = ([^;]+);/)?.[1]
    .match(/"(\w+)"/g)
    ?.map((s) => s.slice(1, -1));
  assert.ok(views?.length, "ViewName union not found");

  const map = iconsSrc.split("export const VIEW_ICONS")[1];
  assert.ok(map, "VIEW_ICONS not exported — layouts would each invent a glyph table");
  for (const view of views!) {
    assert.match(map, new RegExp(`\\b${view}:\\s*"[\\w-]+"`), `VIEW_ICONS has no ${view}`);
  }

  // Both shipped layouts must read the shared map rather than a local one.
  for (const layout of ["default", "simple"]) {
    const src = readFileSync(join(root, `src/lib/layouts/${layout}/Layout.svelte`), "utf8");
    const usesDock = layout === "default" && src.includes("<Dock");
    assert.ok(
      src.includes("VIEW_ICONS") || usesDock,
      `${layout} layout does not use VIEW_ICONS for its nav`,
    );
  }
}

// ---- 5. no text-glyph or emoji icons back in the shell
{
  // Box-drawing carets, tick/cross glyphs and the emoji that used to stand in
  // for icons. Excludes ⌘/⌥ key hints, arrows inside prose, and the braille
  // spinner (an animated text glyph, deliberately kept).
  const banned = /[▾▸▴▿↻⚠✓✗✕⨯☰⏹⌂▣⚙☷＋📎➤◼⬆]/;
  const offenders: string[] = [];
  for (const file of svelteFiles(join(root, "src"))) {
    readFileSync(file, "utf8")
      .split("\n")
      .forEach((line, i) => {
        if (banned.test(line)) offenders.push(`${file}:${i + 1}: ${line.trim()}`);
      });
  }
  assert.deepEqual(offenders, [], `use <Icon name="…"> instead of a text glyph`);
}

console.log("icons: ok");
