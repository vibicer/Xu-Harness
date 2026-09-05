// Runnable check for the self-registering layout registry:
// `node checks/layout-registry.test.ts`
//
// web/src/lib/layouts/ was restructured so each built-in layout owns a folder
// (a `layout.ts` default-exporting a descriptor + its `Layout.svelte`) and the
// registry discovers them with an `import.meta.glob`. The point of the phase:
// a new layout is a dropped-in folder, never an edit in App.svelte, the store,
// or Config. These assertions pin that contract on source text (runes modules
// cannot be imported here, so they read the files instead).
//
// Regression each group exists for:
//   * glob discovery — a hand-written import list is the thing we just removed;
//     re-introducing it means adding a layout edits five files again
//   * folder contract — a folder with only one of the pair loads undefined at
//     runtime; walking the real dir (not a hardcoded list) covers new folders
//   * descriptor shape — Config/resolveLayout read id/name/desc/title/load/themes
//     off every entry, so a missing field is a runtime break, not a type one
//   * lazy own-folder load — a static component import would ship every layout's
//     CSS to every user; the lazy chunk is the whole reason `load()` exists
//   * `default` fallback — resolveLayout() dereferences `layoutById("default")!`
//     for unknown/stale/`custom:`/`plugin:` ids, so that folder vanishing is a
//     blank page, not a graceful no-op
//   * App resolves via the registry — the prior App had a layout id→component map
//     AND a title map; both are gone and must stay gone
//   * no registry→store import — store imports the registry, so the reverse is a
//     cycle that breaks tree-shaking and surprises the bundler
//   * PALETTE surface — store.svelte.ts re-exports PALETTE from the registry for
//     components that still import the back-compat surface; breaking the hop
//     silently breaks every custom-scheme color picker

import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";

const ROOT = new URL("../", import.meta.url);
const LAYOUTS_DIR = new URL("src/lib/layouts/", ROOT);
const registry = readFileSync(new URL("src/lib/layouts/registry.ts", ROOT), "utf8");
const app = readFileSync(new URL("src/App.svelte", ROOT), "utf8");
const store = readFileSync(new URL("src/lib/store.svelte.ts", ROOT), "utf8");

/** Every real subfolder of layouts/ — the check must not hardcode this list,
 *  or adding a folder would slip past it. */
const folders = readdirSync(LAYOUTS_DIR, { withFileTypes: true })
  .filter((d) => d.isDirectory())
  .map((d) => d.name);

let groups = 0;
const group = (name: string, fn: () => void) => { fn(); groups++; void name; };

// 1. Discovery is a glob, not a hand-written import list. `eager: true` is
//    load-bearing: LAYOUTS is built at module top-level from `Object.values`,
//    so a non-eager glob would hand it loader functions, not descriptors.
group("discovery", () => {
  assert.ok(
    /import\.meta\.glob/.test(registry),
    "registry must discover layouts via import.meta.glob, not a static import list",
  );
  assert.ok(
    registry.includes(`"./*/layout.ts"`),
    'the glob must target "./*/layout.ts" so a new folder is auto-discovered',
  );
  assert.match(
    registry,
    /eager:\s*true/,
    "the glob must be eager — LAYOUTS is built synchronously from Object.values()",
  );
  // A hand-written `import ... from "./x/layout.ts"` re-introduces the edit-
  // five-files regression. The glob uses import.meta.glob, a separate form.
  assert.doesNotMatch(
    registry,
    /^[ \t]*import\b[^;]*\bfrom\s+"\.\/[^"]+\/layout\.ts"/m,
    "registry must not statically import any folder's layout.ts — that is the hand-written list the glob replaced",
  );
});

// 2. Folder contract: every real subfolder carries BOTH files. Walking the live
//    directory means a future folder is checked without editing this file.
group("folder-contract", () => {
  assert.ok(folders.length > 0, "layouts/ has no layout subfolders");
  for (const name of folders) {
    const dir = new URL(`src/lib/layouts/${name}/`, ROOT);
    // both ship — a folder with one ships undefined at runtime
    assert.ok(
      existsSync(new URL("layout.ts", dir)),
      `layouts/${name}/ is missing layout.ts`,
    );
    assert.ok(
      existsSync(new URL("Layout.svelte", dir)),
      `layouts/${name}/ is missing Layout.svelte`,
    );
  }
  // `default` must ship: resolveLayout() falls back to layoutById("default")!
  // for unknown/stale/`custom:`/`plugin:` ids, so its absence is a blank page.
  assert.ok(
    folders.includes("default"),
    "the `default` folder must exist — resolveLayout() dereferences it on every unknown id",
  );
  assert.ok(
    folders.includes("simple"),
    "the `simple` folder is a documented built-in today; if it is renamed, update this baseline",
  );
});

// 3. Descriptor shape: each layout.ts default-exports the fields Config and
//    resolveLayout read off every entry. A missing field is a runtime break.
group("descriptor-shape", () => {
  for (const name of folders) {
    const src = readFileSync(new URL(`src/lib/layouts/${name}/layout.ts`, ROOT), "utf8");
    assert.match(src, /export default/, `layouts/${name}/layout.ts must default-export its descriptor`);
    for (const key of ["id", "name", "desc", "title", "themes"] as const) {
      assert.match(
        src,
        new RegExp(`\\b${key}:`),
        `layouts/${name}/layout.ts descriptor is missing \`${key}\``,
      );
    }
    assert.match(
      src,
      /\bload:\s*\(/,
      `layouts/${name}/layout.ts descriptor is missing a \`load()\` loader`,
    );
  }
});

// 4. Laziness: `load()` is the single chunk boundary. A static component import
//    would pull every layout's CSS into the initial bundle for every visitor.
//    The loader must import its OWN folder's Layout.svelte (relative "./"),
//    not a sibling's — cross-folder imports defeat the per-layout chunk.
group("lazy-own-folder-load", () => {
  for (const name of folders) {
    const src = readFileSync(new URL(`src/lib/layouts/${name}/layout.ts`, ROOT), "utf8");
    // the dynamic import of its own component, inside load()
    assert.match(
      src,
      /load:\s*\(\)\s*=>\s*import\("\.\/Layout\.svelte"\)/,
      `layouts/${name}/layout.ts load() must lazily import "./Layout.svelte" — a static import ships every layout's CSS to every user`,
    );
    // no top-level static component import of Layout.svelte
    assert.doesNotMatch(
      src,
      /^[ \t]*import\b[^;]*\bfrom\s+"\.\/Layout\.svelte"/m,
      `layouts/${name}/layout.ts must not statically import "./Layout.svelte" — that ships the component eagerly`,
    );
  }
});

// 5. resolveLayout() falls back to `default`, which always ships. The
//    non-null assertion means a missing `default` dereferences undefined.
group("default-fallback", () => {
  assert.match(
    registry,
    /layoutById\("default"\)!/,
    "resolveLayout() must fall back to layoutById(\"default\")! — the non-null assertion breaks the page if `default` is removed",
  );
});

// 6. App.svelte resolves both component and title through the registry. The
//    prior App hardcoded a layout id→Component map AND a title map; both are
//    gone. A static component import or a Record map is the regression.
group("app-via-registry", () => {
  assert.match(
    app,
    /import\s*\{\s*resolveLayout\s*\}\s*from\s+"\.\/lib\/layouts\/registry"/,
    "App.svelte must import resolveLayout from the registry",
  );
  assert.match(
    app,
    /resolveLayout\(brain\.layout\)/,
    "App.svelte must resolve the active layout through the registry, not a local map",
  );
  assert.match(
    app,
    /descriptor\.title/,
    "App.svelte must read its <title> from the resolved descriptor, not a hardcoded title map",
  );
  // the layout→component map regression: re-importing a built-in Layout.svelte
  // into App is exactly the static map the registry replaced. (PluginLayout
  // lives under components/, so this pattern does not match it.)
  assert.doesNotMatch(
    app,
    /import\s+[^\n]+\s+from\s+"\.\/lib\/layouts\/[^"]+\/Layout\.svelte"/,
    "App.svelte must not statically import any built-in Layout.svelte — that re-introduces the id→component map",
  );
  // the title-map regression shape, narrow enough not to fire on an edit
  assert.doesNotMatch(
    app,
    /const\s+\w*[Tt]itles?\s*:\s*Record</,
    "App.svelte must not carry a hardcoded title map",
  );
  assert.doesNotMatch(
    app,
    /const\s+\w*[Ll]ayouts?\s*:\s*Record<[^>]*,\s*Component>/,
    "App.svelte must not carry a hardcoded layout id→Component map",
  );
  // plugin handoff still intact: App slices "plugin:" and renders PluginLayout,
  // never calling desc.load() for a plugin-owned shell
  assert.match(
    app,
    /startsWith\("plugin:"\)/,
    "App.svelte must keep the `plugin:` prefix check so a plugin-owned shell short-circuits the registry loader",
  );
  assert.match(app, /PluginLayout/, "App.svelte must render PluginLayout for a plugin-owned shell");
});

// 7. Import direction is one-way: store -> registry, never registry -> store.
//    Reversing it makes a cycle the bundler has to untangle and breaks
//    tree-shaking of layout chunks.
group("no-registry-to-store-cycle", () => {
  assert.doesNotMatch(
    registry,
    /from\s+"\.\.\/store(\.svelte)?"/,
    "registry.ts must not import store.svelte.ts — store imports the registry, so the reverse is a cycle",
  );
  assert.doesNotMatch(
    registry,
    /from\s+"\.\/store(\.svelte)?"/,
    "registry.ts must not import store.svelte.ts (alternate relative) — that is a cycle",
  );
});

// 8. PALETTE back-compat surface. palette.ts owns it, registry re-exports it,
//    and store re-exports it onward for components importing the old surface.
//    Breaking either hop silently breaks the custom-scheme color picker.
group("palette-surface", () => {
  assert.match(
    registry,
    /import\s*\{[^}]*\bPALETTE\b[^}]*\}\s*from\s+"\.\/palette"/,
    "registry.ts must import PALETTE from ./palette",
  );
  assert.match(
    registry,
    /export\s*\{[^}]*\bPALETTE\b[^}]*\}\s*from\s+"\.\/palette"/,
    "registry.ts must re-export PALETTE for store.svelte.ts's back-compat surface",
  );
  assert.match(
    store,
    /export\s*\{[^}]*\bPALETTE\b[^}]*\}\s*from\s+"\.\/layouts\/registry"/,
    "store.svelte.ts must re-export PALETTE from the registry — components still import this surface",
  );
});

console.log(`layout-registry: ${groups} groups passed (over ${folders.length} layout folder${folders.length === 1 ? "" : "s"}: ${folders.join(", ")})`);
