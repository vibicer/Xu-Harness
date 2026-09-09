import type { DropinInfo, ToolsetInfo } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/**
 * Toolsets and drop-in tools: Config → Tools manages the GLOBAL defaults
 * (`toolsets`/`dropins`), Agent State → TOOLS manages the ACTIVE SESSION's
 * effective state (`sessionToolsets`/`sessionDropins`). A session without
 * overrides mirrors the global set.
 *
 * `refreshToolsets` also refreshes plugins: `tool.list` and `plugin.list` are
 * the two halves of what the tools panel shows, and a plugin can contribute
 * tools. That chain is why this mixin sits above the plugins one.
 */
export function ToolsMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Tools extends Base {
    toolsets = $state<ToolsetInfo[]>([]);
    dropins = $state<DropinInfo[]>([]);
    /** Active session's effective toolsets / drop-in tools (overrides applied). */
    sessionToolsets = $state<ToolsetInfo[]>([]);
    sessionDropins = $state<DropinInfo[]>([]);

    async refreshToolsets(): Promise<void> {
      const got = await this.client.call<{ toolsets: ToolsetInfo[]; dropins: DropinInfo[] }>("tool.list");
      this.toolsets = got.toolsets;
      this.dropins = got.dropins;
      await this.refreshPlugins();
    }

    async refreshSessionToolsets(): Promise<void> {
      const sid = this.activeSessionId;
      if (!sid) {
        this.sessionToolsets = [];
        this.sessionDropins = [];
        return;
      }
      const got = await this.client.call<{ toolsets: ToolsetInfo[]; dropins: DropinInfo[] }>("tool.list", { session_id: sid });
      this.sessionToolsets = got.toolsets;
      this.sessionDropins = got.dropins;
    }

    async setToolEnabled(toolset: string, enabled: boolean): Promise<void> {
      await this.client.call("tool.set_enabled", { toolset, enabled });
      this.toolsets = this.toolsets.map((t) =>
        t.toolset === toolset ? { ...t, enabled } : t,
      );
    }

    /** Toggle a toolset for the active session only — the global default is untouched. */
    async setSessionToolEnabled(toolset: string, enabled: boolean): Promise<void> {
      const sid = this.activeSessionId;
      if (!sid) return;
      await this.client.call("tool.set_enabled", { toolset, enabled, session_id: sid });
      this.sessionToolsets = this.sessionToolsets.map((t) =>
        t.toolset === toolset ? { ...t, enabled } : t,
      );
    }

    async setDropinEnabled(name: string, enabled: boolean): Promise<void> {
      await this.client.call("tool.set_dropin_enabled", { name, enabled });
      this.dropins = this.dropins.map((d) =>
        d.name === name ? { ...d, enabled } : d,
      );
    }

    /** Toggle a drop-in tool for the active session only — the global default is untouched. */
    async setSessionDropinEnabled(name: string, enabled: boolean): Promise<void> {
      const sid = this.activeSessionId;
      if (!sid) return;
      await this.client.call("tool.set_dropin_enabled", { name, enabled, session_id: sid });
      this.sessionDropins = this.sessionDropins.map((d) =>
        d.name === name ? { ...d, enabled } : d,
      );
    }
  };
}