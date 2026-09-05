import type { DropinInfo, ToolsetInfo } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/**
 * Toolsets and drop-in tools (Config → Tools, Agent State → TOOLS).
 *
 * `refreshToolsets` also refreshes plugins: `tool.list` and `plugin.list` are
 * the two halves of what the tools panel shows, and a plugin can contribute
 * tools. That chain is why this mixin sits above the plugins one.
 */
export function ToolsMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Tools extends Base {
    toolsets = $state<ToolsetInfo[]>([]);
    dropins = $state<DropinInfo[]>([]);

    async refreshToolsets(): Promise<void> {
      const got = await this.client.call<{ toolsets: ToolsetInfo[]; dropins: DropinInfo[] }>("tool.list");
      this.toolsets = got.toolsets;
      this.dropins = got.dropins;
      await this.refreshPlugins();
    }

    async setToolEnabled(toolset: string, enabled: boolean): Promise<void> {
      await this.client.call("tool.set_enabled", { toolset, enabled });
      this.toolsets = this.toolsets.map((t) =>
        t.toolset === toolset ? { ...t, enabled } : t,
      );
    }

    async setDropinEnabled(name: string, enabled: boolean): Promise<void> {
      await this.client.call("tool.set_dropin_enabled", { name, enabled });
      this.dropins = this.dropins.map((d) =>
        d.name === name ? { ...d, enabled } : d,
      );
    }
  };
}
