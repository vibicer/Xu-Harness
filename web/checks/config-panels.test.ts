// Runnable check for the Config shell / panel split:
// `node checks/config-panels.test.ts`
//
// ConfigView.svelte was reduced from 1235 to ~132 lines: it is now a pure
// shell (activeModule + module tile grid + msg banner + refresh/presets) that
// mounts nine panel components from src/lib/components/config/, each rendering
// its own `<div class="cfg-pane" class:active={activeModule === "<id>"}>`.
// The defect this extraction removed: a duplicate `let providers = $state`
// local copy plus a second `provider.list` RPC living in the view. These
// assertions pin that contract on source text (runes modules cannot be
// imported here, so they read the files instead).
//
// Regression each group exists for:
//   * panes-per-id — a union id with zero panes is an unreachable tile; two
//     panes for one id means one quietly shadows the other
//   * tiles<->union — a MODULES entry whose id is not a CfgModule can never be
//     activated, and a union id with no tile is a pane nobody can open
//   * mounted panels — the extraction's failure mode is a panel file that got
//     written but neither imported nor rendered; the dir walk (not a
//     hardcoded list) covers panels added later
//   * shell-stays-shell — the pane toggle drifting back into ConfigView is
//     the first symptom of the file re-growing into the 1235-line monolith
//   * single provider source — `brain.providers` is the only copy; a local
//     mirror + second `provider.list` fetch was the original defect
//   * one allModels — it lives in config/shared.svelte.ts; panels import it;
//     a private duplicate re-creates the copy-paste this shared file ended
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = new URL("../", import.meta.url);
const CONFIG_DIR = new URL("src/lib/components/config/", ROOT);
const configView = readFileSync(new URL("src/lib/components/ConfigView.svelte", ROOT), "utf8");

let groups = 0;
const group = (name: string, fn: () => void) => { fn(); groups++; void name; };

/** Walk a directory (recursive, files only) and return full paths. */
function walk(dir: URL): string[] {
  const out: string[] = [];
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory()) out.push(...walk(new URL(`${e.name}/`, dir)));
    else out.push(join(fileURLToPath(dir), e.name));
  }
  return out;
}

/** The CfgModule union, parsed out — ids with quotes, e.g. `"providers"`. */
const unionLine = /type CfgModule = ([^;]+);/.exec(configView)?.[1];
assert.ok(unionLine, "ConfigView.svelte must declare `type CfgModule = ...;`");
const unionIds = [...unionLine.matchAll(/"([^"]+)"/g)].map((m) => m[1]);
assert.ok(unionIds.length >= 1, "the CfgModule union must list at least one id");

/** Every *.svelte under config/ (excludes .svelte.ts helpers like shared). */
const panelFiles = readdirSync(CONFIG_DIR).filter((f) => f.endsWith(".svelte")).sort();
const panelSources = new Map(panelFiles.map((f) => [f, readFileSync(new URL(f, CONFIG_DIR), "utf8")]));

/** The pane marker a panel owns for one module id. The closing quote+brace
 *  keep `=== "data"` from matching a hypothetical `=== "database"`. */
const paneMarker = (id: string) => `class:active={activeModule === "${id}"}`;

// 1. Every module id resolves to exactly one pane.
group("panes-per-id", () => {
  for (const id of unionIds) {
    const owners = [...panelSources].filter(([, src]) => src.includes(paneMarker(id))).map(([f]) => f);
    assert.ok(owners.length >= 1, `module "${id}" has no pane (${paneMarker(id)} not found in any config/*.svelte) — its tile is unreachable`);
    assert.ok(owners.length === 1, `module "${id}" has ${owners.length} panes (${owners.join(", ")}) — exactly one panel may own it`);
  }
});

// 2. MODULES tile ids and the CfgModule union are the same set.
group("tiles-match-union", () => {
  const modulesBlock = /const MODULES[\s\S]*?\]\);/.exec(configView)?.[0];
  assert.ok(modulesBlock, "ConfigView.svelte must declare the MODULES tile grid");
  const tileIds = [...modulesBlock.matchAll(/\{\s*id:\s*"([^"]+)"/g)].map((m) => m[1]);
  assert.deepEqual([...tileIds].sort(), [...unionIds].sort(),
    `MODULES ids (${tileIds.join(", ")}) must be exactly the CfgModule union (${unionIds.join(", ")})`);
  assert.equal(new Set(tileIds).size, tileIds.length, `MODULES ids must be unique, saw: ${tileIds.join(", ")}`);
});

// 3. Every panel file is imported AND mounted by the shell.
group("panels-mounted", () => {
  assert.ok(panelFiles.length >= 9, `expected at least 9 config panels, saw ${panelFiles.length}: ${panelFiles.join(", ")}`);
  for (const f of panelFiles) {
    const name = f.replace(/\.svelte$/, "");
    assert.ok(
      new RegExp(`import\\s+${name}\\s+from\\s+"\\./config/${f}"`).test(configView),
      `ConfigView.svelte must import ${name} from ./config/${f}`,
    );
    assert.ok(
      new RegExp(`<${name}[\\s/>]`).test(configView),
      `ConfigView.svelte must mount <${name} …> — an imported-but-unmounted panel is dead code`,
    );
  }
});

// 4. The shell stays a shell. Caveat: the tile grid legitimately carries
//    `class:active={activeModule === m.id}` (unquoted loop var); the pane
//    form targeted here is the quoted-literal one.
group("shell-stays-shell", () => {
  const lines = configView.split("\n").length;
  assert.ok(lines < 200, `ConfigView.svelte is ${lines} lines; it must stay under 200 (it was 1235 pre-extraction)`);
  assert.ok(!configView.includes(`class:active={activeModule === "`),
    "ConfigView.svelte must not host any pane toggle (`class:active={activeModule === \"<id>\"}` lives in the panel files)");
});

// 5. One provider list. brain.providers is the single source fetched by the
//    store; the view must never grow a local copy or its own RPC.
group("single-provider-source", () => {
  const offenders = [["ConfigView.svelte", configView], ...panelSources]
    .filter(([, src]) => src.includes("let providers = $state"))
    .map(([f]) => f);
  assert.deepEqual(offenders, [], `these files re-declare the provider list locally: ${offenders.join(", ")} — use brain.providers`);
  const rpcHits = walk(new URL("src/lib/components/", ROOT))
    .filter((f) => readFileSync(f, "utf8").includes(`"provider.list"`));
  assert.deepEqual(rpcHits, [], `only the store may call "provider.list"; found in: ${rpcHits.join(", ")}`);
});

// 6. allModels() is defined once, in config/shared.svelte.ts, and imported
//    by the two panels that need it.
group("one-allModels", () => {
  const defs = walk(new URL("src/lib/components/", ROOT))
    .filter((f) => /(?:export\s+)?(?:function|const)\s+allModels\s*[=(]/.test(readFileSync(f, "utf8")));
  assert.deepEqual(defs, [join(fileURLToPath(CONFIG_DIR), "shared.svelte.ts")],
    `allModels must be defined exactly once, in config/shared.svelte.ts; saw: ${defs.join(", ") || "(none)"}`);
  for (const f of ["AgentPanel.svelte", "OrchestratePanel.svelte"]) {
    assert.match(panelSources.get(f) ?? "", /import\s*\{[^}]*\ballModels\b[^}]*\}\s*from\s*"\.\/shared\.svelte"/,
      `${f} must import allModels from ./shared.svelte, not redefine it`);
  }
});

console.log(`config-panels: ${groups} groups passed (${unionIds.length} modules, ${panelFiles.length} panels)`);
