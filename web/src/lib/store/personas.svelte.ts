import type { PersonaInfo } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/**
 * Persona library (Config → Agent → personas): the catalog and which one is
 * the default for new sessions.
 *
 * `setPersona` is NOT here — it writes the ACTIVE SESSION's persona via
 * `state.set_persona` and belongs to the session-state concern.
 */
export function PersonasMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Personas extends Base {
    personas = $state<PersonaInfo[]>([]);
    activePersona = $state<string | null>(null);

    async refreshPersonas(): Promise<void> {
      const got = await this.client.call<{ personas: PersonaInfo[]; active: string | null }>("persona.list");
      this.personas = got.personas;
      this.activePersona = got.active;
    }

    async getPersona(id: string): Promise<string> {
      const got = await this.client.call<{ id: string; text: string }>("persona.get", { id });
      return got.text;
    }

    async savePersona(id: string, text: string): Promise<void> {
      await this.client.call("persona.upsert", { id, text });
      await this.refreshPersonas();
    }

    async deletePersona(id: string): Promise<void> {
      await this.client.call("persona.delete", { id });
      await this.refreshPersonas();
    }

    async setActivePersona(id: string | null): Promise<void> {
      await this.client.call("persona.set_active", { id });
      this.activePersona = id;
    }
  };
}
