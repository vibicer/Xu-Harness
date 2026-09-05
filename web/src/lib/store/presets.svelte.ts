import type { PresetInfo, StatePanel } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/**
 * Orchestration presets (Config → Orchestrate): the saved agent compositions
 * and which one the active session is bound to.
 *
 * Stateless by design — `refreshPresets` returns the list to its caller rather
 * than storing it, and the session's bound preset lives in `state.preset`.
 */
export function PresetsMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Presets extends Base {
    async refreshPresets(): Promise<PresetInfo[]> {
      const got = await this.client.call<{ presets: PresetInfo[] }>("preset.list");
      return got.presets;
    }

    async upsertPreset(payload: { id?: string; name: string; tree: Record<string, unknown> }): Promise<{ preset: Record<string, unknown> }> {
      const p: Record<string, unknown> = { name: payload.name, tree: payload.tree };
      if (payload.id) p.id = payload.id;
      return await this.client.call<{ preset: Record<string, unknown> }>("preset.upsert", p);
    }

    async deletePreset(id: string): Promise<void> {
      await this.client.call("preset.delete", { id });
    }

    async setSessionPreset(preset_id: string | null): Promise<void> {
      const sid = this.activeSessionId;
      if (!sid) return;
      await this.client.call("session.set_preset", { session_id: sid, preset_id });
      this.state = { ...this.state, preset: preset_id ? this.state.preset : null };
      try {
        const st = await this.client.call<{ preset: unknown }>("state.get", { session_id: sid });
        this.state = { ...this.state, preset: (st.preset as StatePanel["preset"]) ?? null };
      } catch { /* keep current */ }
    }
  };
}
