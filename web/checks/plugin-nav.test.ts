// Runnable check for plugin-contributed nav entries + full views:
// `node checks/plugin-nav.test.ts`
//
// A plugin can contribute a whole view (`ui.mount: "view"`) and reach it from
// the nav under the reserved `plugin:<name>` view id. Three sides must agree —
// the store's ViewName union, the nav + view slot in BOTH shipped layouts, and
// the icon resolver's fallback — and each can pass its own review while the
// wiring is broken, so these assertions pin the contract itself.
//
// Regression this exists for: the nav and both layouts were hardcoded to the
// built-in views, so a `view`-mount plugin was unreachable — installed,
// enabled, and invisible.
//
// The guards are behavioral (plugin-views.svelte.ts is pure — type-only
// imports, no runes — so Node can import it, unlike icons.ts which pulls in
// Svelte components); the wiring pins are source text, like plugin-layout.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import { pluginViews, pluginViewName, pluginViewGuard } from "../src/lib/plugin-views.svelte.ts";
import type { PluginInfo } from "../src/lib/types.ts";

const read = (p: string) => readFileSync(new URL(`../${p}`, import.meta.url), "utf8");
const shared = read("src/lib/store/shared.ts");
const dock = read("src/lib/components/Dock.svelte");
const defaultLayout = read("src/lib/layouts/default/Layout.svelte");
const simpleLayout = read("src/lib/layouts/simple/Layout.svelte");

/** Minimal PluginInfo, patched per case. */
const plugin = (over: Partial<PluginInfo> & { name: string }): PluginInfo => ({
  version: "1.0.0",
  description: "",
  provides: [],
  requires: [],
  enabled: true,
  ...over,
});
const ui = (mount: string) => ({ module: "ui.js", element: "x-p", mount });
/** The least pluginViewGuard needs — the real store satisfies it structurally. */
const fakeBrain = (view: string, plugins: PluginInfo[]) => {
  let set: string | null = null;
  return {
    view,
    plugins,
    setView: (v: string) => { set = v; },
    get resetTo() { return set; },
  };
};

// ---- 1. the view id spelling: `plugin:<name>` is a ViewName, not a builtin
{
  assert.match(
    shared,
    /export type ViewName = [^;]*`plugin:\$\{string\}`/,
    "ViewName must carry the `plugin:*` member",
  );
  assert.equal(pluginViewName("workspace"), null, "a built-in view has no plugin name");
  assert.equal(pluginViewName("plugin:foo"), "foo", "the name is what follows `plugin:`");
}

// ---- 2. pluginViews: eligibility is enabled + ui.element + mounted here
{
  const list = [
    plugin({ name: "view", ui: ui("view") }),
    plugin({ name: "off", enabled: false, ui: ui("view") }),
    plugin({ name: "status", ui: ui("statusbar") }),
    plugin({ name: "no-ui" }),
    plugin({ name: "no-element", ui: { ...ui("view"), element: "" } }),
  ];
  assert.deepEqual(
    pluginViews(list).map((p) => p.name),
    ["view"],
    "only enabled view-mount plugins with an element qualify",
  );
}

// ---- 3. pluginViewGuard: a vanished view falls back to the chat, a live one
//        and a built-in stay put. View is not persisted, so the stale case is
//        mid-session (plugin disabled/removed while its view is open) — the
//        point is never leaving the shell on a blank screen.
{
  const live = plugin({ name: "live", ui: ui("view") });

  const stale = fakeBrain("plugin:gone", []);
  pluginViewGuard(stale);
  assert.equal(stale.resetTo, "workspace", "a stale plugin view must reset to workspace");

  const open = fakeBrain("plugin:live", [live]);
  pluginViewGuard(open);
  assert.equal(open.resetTo, null, "a live plugin view must not be reset");

  const builtin = fakeBrain("sessions", [live]);
  pluginViewGuard(builtin);
  assert.equal(builtin.resetTo, null, "a built-in view must not be reset");
}

// ---- 4. the dock (default layout's nav) lists plugin views as peers:
//        built-in ORDER untouched, plugin buttons appended after it
{
  assert.match(dock, /pluginViews\(brain\.plugins\)/, "dock entries must come from pluginViews");
  assert.match(
    dock,
    /`plugin:\$\{p\.name\}`/,
    "a plugin entry must setView its `plugin:<name>` id",
  );
  assert.match(dock, /viewIcon\(/, "a plugin entry must resolve its icon safely");
  // Same keyboard path as the built-ins (a plugin view is a peer, not a guest).
  assert.match(dock, /onkeydown=\{\(e\) => handleKey\(view, e\)\}/);
  const order = dock.indexOf("{#each ORDER");
  const plugins = dock.indexOf("{#each pluginViews");
  assert.ok(order > -1 && plugins > order, "plugin entries must come after the built-in ORDER");
}

// ---- 5. both layouts render the active plugin view and stand the guard
{
  for (const [name, src] of [["default", defaultLayout], ["simple", simpleLayout]] as const) {
    assert.ok(
      src.includes('<PluginSlot mount="view"'),
      `${name} layout must render the plugin view slot`,
    );
    assert.ok(
      src.includes("pluginViewGuard(brain)"),
      `${name} layout must run the stale-view guard`,
    );
  }
}

console.log("plugin-nav: 5 groups passed");
