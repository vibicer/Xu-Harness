// Runnable check for plugin-contributed colour schemes: `node checks/plugin-theme.test.ts`
//
// A plugin whose manifest carries `themes[]` adds selectable colour schemes.
// The wiring spans three files that can each pass their own test while the
// feature stays broken:
//
//   store    — flattens plugin.list into `pluginThemes`, merges into `allThemes`,
//              paints vars + swaps the one <link> for a scheme's extra CSS
//   Config   — offers them beside the built-ins, refuses to edit/delete them
//   types    — the shape the brain actually sends
//
// The load-bearing invariants, and what breaks without them:
//
//   * plugin schemes stay OUT of `themes` — otherwise persistThemes() copies
//     them into localStorage and a stale fork of the plugin's scheme survives
//     the plugin being updated or removed
//   * every lookup uses `allThemes` — a lookup against `themes` silently can't
//     find a plugin scheme, so selecting one appears to do nothing
//   * `appliedVars` is cleared on switch — a plugin scheme may set vars outside
//     PALETTE, and PALETTE-only cleanup leaks them into the next scheme
//   * a vanished plugin scheme falls back — otherwise the shell sits on an id
//     nothing matches, painting defaults with nothing selected in Config
//
// The "store" side is now three files (phase D2): the appearance concern
// (store/appearance.svelte.ts) owns the schemes, the painting and the persisted
// ids; the plugins concern (store/plugins.svelte.ts) owns the list and every
// path that replaces it; the composition root still fetches the first list on
// boot. Each assertion reads the file that owns the name it is about; `stores`
// is the union, used only where the invariant is that NO file does a thing.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const root = readFileSync(new URL("../src/lib/store.svelte.ts", import.meta.url), "utf8");
const appearance = readFileSync(new URL("../src/lib/store/appearance.svelte.ts", import.meta.url), "utf8");
const plugins = readFileSync(new URL("../src/lib/store/plugins.svelte.ts", import.meta.url), "utf8");
const stores = [root, appearance, plugins].join("\n");
// The scheme picker + preset editor moved out of ConfigView into their own pane (D3).
const config = readFileSync(new URL("../src/lib/components/config/ThemesPanel.svelte", import.meta.url), "utf8");
const types = readFileSync(new URL("../src/lib/types.ts", import.meta.url), "utf8");

let groups = 0;
const group = (name: string, fn: () => void) => { fn(); groups++; void name; };

// 1. The brain's shape is declared, or `p.themes` is a type error at every use.
group("types", () => {
  assert.match(types, /export interface PluginTheme/, "PluginTheme must be declared");
  assert.match(types, /themes\?: PluginTheme\[\]/, "PluginInfo must carry themes");
  for (const field of ["id", "name", "colors", "css"]) {
    assert.ok(new RegExp(`\\b${field}\\??:`).test(types), `PluginTheme needs ${field}`);
  }
});

// 2. Plugin schemes are derived from the live plugin list — not copied into
//    `themes`, and only from ENABLED plugins.
group("derivation", () => {
  const start = appearance.indexOf("pluginThemes = $derived(");
  assert.ok(start > 0, "pluginThemes must be a $derived off brain.plugins");
  const body = appearance.slice(start, appearance.indexOf("allThemes", start));
  assert.match(body, /p\.enabled/, "a disabled plugin must contribute no scheme");
  assert.match(
    body,
    /plugin:\$\{p\.name\}:\$\{t\.id\}/,
    "ids must be namespaced plugin:<plugin>:<id> so two plugins can't collide",
  );
  assert.match(body, /plugin: p\.name/, "each scheme must record its owning plugin");
  assert.match(appearance, /allThemes = \$derived/, "allThemes must merge both sources");
});

// 3. Plugin schemes must never be persisted: persistThemes writes `themes`, so
//    the only protection is that they were never merged into it.
group("no persistence", () => {
  const decl = appearance.slice(appearance.indexOf("themes = $state<ThemePreset[]>"), appearance.indexOf("private readonly layoutKey"));
  assert.match(decl, /BUILTIN_THEMES/, "themes still seeds from BUILTIN_THEMES only");
  assert.ok(
    !/themes = \$state<ThemePreset\[\]>\(\[\.\.\.BUILTIN_THEMES, \.\.\.this\.pluginThemes/.test(stores),
    "plugin schemes must NOT be merged into the persisted `themes` array",
  );
  const persist = appearance.slice(appearance.indexOf("private persistThemes()"), appearance.indexOf("private appliedVars"));
  assert.ok(
    !persist.includes("pluginThemes") && !persist.includes("allThemes"),
    "persistThemes must only ever write user presets",
  );
});

// 4. Every selection path resolves against allThemes, or a plugin scheme is
//    unselectable while still being listed.
group("lookups", () => {
  for (const fn of ["setTheme(id: string)", "setLayout(id: LayoutId)", "createTheme()"]) {
    const start = appearance.indexOf(fn);
    assert.ok(start > 0, `${fn} not found`);
    const body = appearance.slice(start, start + 900);
    assert.match(body, /allThemes/, `${fn} must resolve against allThemes`);
  }
  assert.match(
    config,
    /brain\.allThemes\.filter/,
    "Config's compatibleThemes must read allThemes",
  );
});

// 5. Painting: plugin vars are applied AND tracked for removal, and the extra
//    stylesheet is a single swapped <link>.
group("painting", () => {
  const start = appearance.indexOf("protected applyAppearance()");
  assert.ok(start > 0, "applyAppearance must exist");
  const body = appearance.slice(start, appearance.indexOf("private applyThemeCss"));
  assert.match(body, /for \(const v of this\.appliedVars\)/, "tracked vars must be cleared first");
  assert.match(body, /this\.appliedVars = \[\]/, "the tracking list must be reset");
  assert.match(body, /t\.plugin/, "a plugin scheme needs its own painting branch");
  assert.match(body, /Object\.entries\(t\.colors\)/, "a plugin scheme is not limited to PALETTE");
  assert.match(body, /this\.appliedVars\.push\(name\)/, "each var set must be tracked");
  assert.match(body, /this\.applyThemeCss\(/, "the stylesheet swap must run on every apply");

  const css = appearance.slice(appearance.indexOf("private applyThemeCss"), appearance.indexOf("setLayout("));
  assert.match(css, /existing\?\.remove\(\)/, "a scheme without css must remove a stale link");
  assert.match(css, /encodeURIComponent/, "the href must be encoded");
  assert.match(css, /\/plugins\//, "the href must point at the brain's plugin route");
});

// 6. A scheme that disappeared (plugin disabled/uninstalled) must not strand the
//    shell on a dead id — and the reconcile must run on every list refresh.
//    The fallback itself is appearance's (`reconcilePluginTheme`, it owns
//    `theme`); adoptPlugins is the one caller that must invoke it and repaint.
group("reconcile", () => {
  const start = appearance.indexOf("protected reconcilePluginTheme(");
  assert.ok(start > 0, "reconcilePluginTheme must exist");
  const fallback = appearance.slice(start, start + 900);
  assert.match(fallback, /theme\.startsWith\("plugin:"\)/, "only a plugin id needs reconciling");
  assert.match(fallback, /!this\.pluginThemes\.some/, "a missing scheme must be detected");

  const adopt = plugins.indexOf("protected adoptPlugins(");
  assert.ok(adopt > 0, "adoptPlugins must exist");
  const body = plugins.slice(adopt, adopt + 900);
  assert.match(body, /this\.reconcilePluginTheme\(\)/, "adopting must reconcile a vanished scheme");
  assert.match(body, /this\.applyAppearance\(\)/, "adopting must repaint");
  // Every path that replaces the plugin list has to go through it. Since D3 the
  // composition root no longer fetches the list itself (it delegates to
  // refreshPlugins), so assert the invariant structurally instead of by count:
  // every plugin-list wire response must be adopted, and `plugin.list` is
  // fetched from exactly one place so there is no second path to forget.
  const wire = [...stores.matchAll(/\{ plugins: PluginInfo\[\] \}>\(/g)];
  assert.ok(wire.length >= 2, `expected the plugin-list wire reads, saw ${wire.length}`);
  for (const m of wire) {
    assert.match(
      stores.slice(m.index!, m.index! + 300),
      /this\.adoptPlugins\(/,
      "a plugin-list response that skips adoptPlugins leaves the scheme list stale",
    );
  }
  assert.equal(
    (stores.match(/"plugin\.list"/g) ?? []).length,
    1,
    "plugin.list must be fetched from exactly one place (refreshPlugins)",
  );
  for (const fn of ["async refreshPlugins()", "async reloadPlugins()"]) {
    const b = plugins.slice(plugins.indexOf(fn), plugins.indexOf(fn) + 260);
    assert.match(b, /adoptPlugins/, `${fn} must adopt, not assign`);
  }
  const toggle = plugins.slice(plugins.indexOf("async setPluginEnabled("), plugins.indexOf("async setPluginEnabled(") + 500);
  assert.match(toggle, /refreshPlugins\(\)/, "toggling must re-fetch so a retired scheme is noticed");
});

// 7. A persisted plugin scheme survives a cold start: the plugin list arrives
//    after the restore, so the id must be kept on faith.
group("cold start", () => {
  const start = appearance.indexOf("protected restoreAppearance()");
  assert.ok(start > 0, "restoreAppearance must exist");
  const restore = appearance.slice(start, appearance.indexOf("protected async refreshLayouts", start));
  assert.match(restore, /savedTheme\?\.startsWith\("plugin:"\)/, "a plugin scheme id must be restored");
  assert.match(
    restore,
    /!this\.theme\.startsWith\("plugin:"\) &&/,
    "the built-in compatibility guard must not clobber a plugin id",
  );
  // ...and the root must actually run it, or nothing is restored at all.
  assert.match(root, /this\.restoreAppearance\(\)/, "the constructor must call restoreAppearance");
});

// 8. A plugin owns its scheme: Config must not offer to delete or edit it.
group("ownership", () => {
  assert.match(
    config,
    /!t\.builtin && !t\.plugin/,
    "the delete button must be hidden for a plugin scheme",
  );
  const activeCustom = config.slice(config.indexOf("const activeCustom = $derived"), config.indexOf("const activeCustom = $derived") + 200);
  assert.match(
    activeCustom,
    /brain\.themes\./,
    "the preset editor must read `themes` (user presets only), never allThemes",
  );
});

console.log(`plugin-theme: ${groups} groups passed`);
