import type { PluginInfo } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/**
 * Plugins (Config → Plugins): the live `plugin.list` snapshot and every path
 * that replaces it.
 *
 * Every replacement goes through `adoptPlugins`, because a plugin owns any
 * colour scheme it contributes: dropping the list without reconciling would
 * leave the shell on a scheme id nothing matches. The reconcile + repaint
 * themselves belong to the appearance concern (it owns `theme`), reached
 * through the `reconcilePluginTheme` / `applyAppearance` seams.
 */
export function PluginsMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Plugins extends Base {
    plugins = $state<PluginInfo[]>([]);

    /** Adopt a fresh `plugin.list` result and repaint appearance. Protected,
     *  not private: the composition root's bootstrap fetches the first list
     *  itself (a cold start with an empty list cold-starts a `plugin:*` choice
     *  as "not enabled") and adopts it through here. */
    protected adoptPlugins(list: PluginInfo[]): void {
      this.plugins = list;
      this.reconcilePluginTheme();
      this.applyAppearance();
    }

    async refreshPlugins(): Promise<void> {
      const got = await this.client.call<{ plugins: PluginInfo[] }>("plugin.list");
      this.adoptPlugins(got.plugins);
    }

    async setPluginEnabled(name: string, enabled: boolean): Promise<void> {
      await this.client.call(enabled ? "plugin.enable" : "plugin.disable", { name });
      // Re-fetch rather than patch the flag locally: disabling a plugin retires any
      // scheme it contributed, and `adoptPlugins` is what falls back off a scheme
      // that just vanished. A local patch would leave the shell on a dead theme id.
      await this.refreshPlugins();
    }

    /** Write one setting a plugin's manifest declared. The brain validates the key
     *  and coerces the value, and returns the refreshed record — so the stored
     *  result is what lands in state, not the optimistic input. */
    async setPluginSetting(name: string, key: string, value: unknown): Promise<void> {
      const got = await this.client.call<{ plugin: PluginInfo | null }>(
        "plugin.set_setting",
        { name, key, value },
      );
      if (!got.plugin) return;
      this.plugins = this.plugins.map((p) => (p.name === name ? got.plugin! : p));
    }

    async reloadPlugins(): Promise<void> {
      const got = await this.client.call<{ plugins: PluginInfo[] }>("plugin.reload");
      this.adoptPlugins(got.plugins);
    }
  };
}
