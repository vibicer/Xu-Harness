// Runnable check for plugin-contributed layouts: `node checks/plugin-layout.test.ts`
//
// A plugin whose manifest says `ui.mount: "layout"` replaces the whole shell.
// Selecting it is a three-way handshake — the store holds the id, the brain
// lists the plugin, the shell resolves one against the other — and each side
// can pass its own unit test while the handshake stays broken. These assertions
// pin the contract itself.
//
// Regression this exists for: `plugin.list` was only called when Config opened,
// so on a cold start `brain.plugins` was empty, `PluginLayout` found no match,
// and a working layout rendered "not enabled".
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

// The store is split across the composition root and concern mixins (phase D2):
// bootstrap and the plugin list stay in the root, the layout/scheme ids and the
// <html> repaint moved to the appearance concern. Each assertion reads the file
// that owns the name it pins.
const store = readFileSync(new URL("../src/lib/store.svelte.ts", import.meta.url), "utf8");
const appearance = readFileSync(new URL("../src/lib/store/appearance.svelte.ts", import.meta.url), "utf8");
const stores = [store, appearance].join("\n");
const app = readFileSync(new URL("../src/App.svelte", import.meta.url), "utf8");
const layout = readFileSync(new URL("../src/lib/components/PluginLayout.svelte", import.meta.url), "utf8");
const plugins = readFileSync(new URL("../src/lib/store/plugins.svelte.ts", import.meta.url), "utf8");
// The layout/scheme picker moved out of ConfigView into its own pane (D3).
const themesPane = readFileSync(new URL("../src/lib/components/config/ThemesPanel.svelte", import.meta.url), "utf8");

// 1. bootstrap() must fetch the plugin list. Without it every `plugin:*` layout
//    cold-starts as "not enabled" — the bug this file was written for.
{
  const start = store.indexOf("private async bootstrap()");
  const end = store.indexOf("private closeSessionTab", start);
  assert.ok(start > 0 && end > start, "bootstrap() not found");
  // The call is transitive since D3: bootstrap delegates to the plugins concern
  // instead of inlining the RPC. Assert the whole chain so neither half can rot.
  assert.match(
    store.slice(start, end),
    /this\.refreshPlugins\(\)/,
    "bootstrap() must call refreshPlugins() — otherwise brain.plugins is empty on a cold load",
  );
  assert.match(
    plugins,
    /async refreshPlugins\(\)[\s\S]{0,200}?"plugin\.list"/,
    "refreshPlugins() must fetch plugin.list — bootstrap relies on it",
  );
}

// 2. A `plugin:*` id must survive a page reload. The appearance concern
//    (store/appearance.svelte.ts) owns the persisted layout id since D2.
{
  assert.match(
    appearance,
    /savedLayout\?\.startsWith\("plugin:"\)/,
    "a persisted plugin layout must be restored from localStorage",
  );
}

// 3. setLayout must not require a matching color scheme for a plugin layout:
//    the plugin ships its own stylesheet and has no entry in `themes`. The
//    bypass has to sit ahead of the compatible-scheme guard inside the same
//    function — scoped to setLayout's own body so an unrelated `.some` guard
//    elsewhere in the file cannot stand in for it.
{
  const start = appearance.indexOf("setLayout(id: LayoutId)");
  assert.ok(start > 0, "setLayout not found");
  const body = appearance.slice(start, appearance.indexOf("\n    setCustomLayout(", start));
  assert.ok(body.length > 0, "setLayout body not delimited");
  const guard = body.search(/if \(!this\.(?:all)?[Tt]hemes\.some/);
  const bypass = body.indexOf('id.startsWith("plugin:")');
  assert.ok(guard > 0, "setLayout must still guard on a compatible scheme");
  assert.ok(bypass > 0 && bypass < guard, "the plugin bypass must precede the theme guard");
}

// 4. The class the store puts on <html> must be selector-safe. `plugin:name`
//    contains a colon, which needs escaping in every rule that targets it.
{
  assert.match(
    appearance,
    /layout-\$\{this\.layout\.replace\(":", "-"\)\}/,
    "the layout class must normalise the colon",
  );
  assert.doesNotMatch(stores, /layout-\$\{this\.layout\}`/, "raw colon in a class name");
}

// 5. App.svelte routes `plugin:<name>` away from the built-in layout map, and
//    must not try to import a module for it.
{
  assert.match(app, /brain\.layout\.startsWith\("plugin:"\)/);
  assert.match(app, /<PluginLayout name=\{pluginName\}/);
  const effect = app.slice(app.indexOf("$effect(() =>"), app.indexOf("</script>"));
  assert.match(effect, /if \(pluginName\) return;/, "the built-in import must be skipped");
}

// 6. PluginLayout must not claim "not enabled" before the list has arrived —
//    an empty list means unknown, not absent.
{
  assert.match(
    layout,
    /!plugin && brain\.plugins\.length > 0/,
    "the not-enabled branch must wait for a non-empty plugin list",
  );
}

// 7. The store is handed over, and the element is told to repaint. A custom
//    element cannot subscribe to runes, so both halves are required.
{
  assert.match(layout, /\.brain = brain/, "the element must receive the store");
  assert.match(layout, /el\.sync\(\)/, "the element must be told to repaint");
  // The bridge subscribes by *reading* fields. A layout may render the agent
  // state itself — model/persona pickers, a live toolset list — so provider and
  // tool changes have to reach it too, or those controls silently go stale.
  for (const field of [
    "brain.view", "brain.messages.length", "brain.draft", "brain.approvals.length",
    "brain.providers", "brain.personas.length", "brain.toolsets", "brain.dropins",
  ]) {
    assert.ok(layout.includes(field), `the reactivity bridge must read ${field}`);
  }
}

// 8. Config must offer plugin layouts, or an installed one is unreachable.
{
  assert.match(themesPane, /PLUGIN_LAYOUTS/, "the themes pane must list plugin layouts");
  assert.match(themesPane, /p\.ui\.mount === "layout"/, "only layout-mount plugins qualify");
  assert.match(themesPane, /ALL_LAYOUTS = \$derived\(\[\.\.\.LAYOUTS, \.\.\.CUSTOM_LAYOUTS, \.\.\.PLUGIN_LAYOUTS\]\)/);
}

console.log("plugin-layout: 8 groups passed");
