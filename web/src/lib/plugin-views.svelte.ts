/** Plugin-contributed full views (`ui.mount: "view"`).
 *
 * A view-mount plugin is reachable from the nav like a built-in view, under
 * the reserved `plugin:<name>` ViewName. This module is the one place that
 * knows how those two spellings map onto each other, so the dock and both
 * layouts cannot disagree about what qualifies or what is stale. Pure
 * functions only — no runes — so checks can import it (see
 * checks/plugin-nav.test.ts). */
import type { PluginInfo } from "./types";
import type { ViewName } from "./store/shared";

/** Enabled plugins contributing a full view. Same eligibility rule as the
 *  config panes: enabled, an `ui.element` to mount, mounted here. */
export function pluginViews(plugins: PluginInfo[]): PluginInfo[] {
  return plugins.filter((p) => p.enabled && !!p.ui?.element && p.ui.mount === "view");
}

/** The plugin name inside a `plugin:<name>` view id, or null for a built-in. */
export function pluginViewName(view: ViewName): string | null {
  return view.startsWith("plugin:") ? view.slice("plugin:".length) : null;
}

/** The least the guard needs from the store, so it runs against a fake in
 *  checks without importing the brain. The real store satisfies it. */
interface GuardBrain {
  view: ViewName;
  plugins: PluginInfo[];
  setView(view: ViewName): void;
}

/** A plugin view can vanish while it is open — disabled, uninstalled, or
 *  reloaded away. The view is not persisted, so this only happens mid-session,
 *  but when it does the layout must not sit on a blank screen. Call inside a
 *  layout $effect so both `view` and the plugin list are dependencies. */
export function pluginViewGuard(brain: GuardBrain): void {
  const name = pluginViewName(brain.view);
  if (name === null) return;
  if (!pluginViews(brain.plugins).some((p) => p.name === name)) {
    brain.setView("workspace");
  }
}
