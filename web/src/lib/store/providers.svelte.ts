import type { ProviderInfo } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/** Model providers (Config → Providers, Onboarding) and the model dropdown. */
export function ProvidersMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Providers extends Base {
    providers = $state<ProviderInfo[]>([]);
    models = $state<string[]>([]);

    /** Flatten all provider models into a single id list for the MODEL dropdown. */
    protected collectModels(): string[] {
      const out: string[] = [];
      for (const p of this.providers) if (p.enabled) for (const m of p.models) out.push(m);
      // Auto-select the first detected model when none is active so the chat
      // can start without a manual dropdown pick.
      if (out.length > 0 && !this.state.model) {
        this.state = { ...this.state, model: out[0] };
      }
      return out;
    }

    async refreshProviders(): Promise<void> {
      const got = await this.client.call<{ providers: ProviderInfo[] }>("provider.list");
      this.providers = got.providers;
      this.models = this.collectModels();
      this.onboarded = this.providers.length > 0;
    }

    async addProvider(form: {
      id?: string;
      type?: string;
      name?: string;
      base_url: string;
      api_key?: string;
      models?: string[];
    }): Promise<string> {
      const r = await this.client.call<{ id: string }>("provider.upsert", form);
      await this.refreshProviders();
      return r.id;
    }

    async testProvider(id: string): Promise<{ ok: boolean; latency_ms?: number; models?: string[]; error?: string }> {
      const r = await this.client.call<{ ok: boolean; latency_ms: number; models: string[]; error?: string }>(
        "provider.test",
        { id },
      );
      if (r.ok && r.models?.length) await this.refreshProviders();
      return r;
    }

    async deleteProvider(id: string): Promise<void> {
      await this.client.call("provider.delete", { id });
      await this.refreshProviders();
    }

    async setProviderEnabled(id: string, enabled: boolean): Promise<void> {
      await this.client.call("provider.set_enabled", { id, enabled });
      await this.refreshProviders();
    }

    async setModel(model: string, provider?: string): Promise<void> {
      await this.client.call("state.set_model", {
        session_id: this.activeSessionId,
        model,
        ...(provider !== undefined ? { provider } : {}),
      });
      this.state = { ...this.state, model };
    }
  };
}
