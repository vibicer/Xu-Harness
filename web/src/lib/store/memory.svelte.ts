import type { MemoryEntry } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/** MEMORY.md entries (Config → Memory). */
export function MemoryMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Memory extends Base {
    memories = $state<MemoryEntry[]>([]);

    async refreshMemory(): Promise<void> {
      const got = await this.client.call<{ entries: MemoryEntry[] }>("memory.list");
      this.memories = got.entries;
    }

    async updateMemory(id: string, text: string): Promise<void> {
      await this.client.call("memory.update", { id, text });
      await this.refreshMemory();
    }

    async addMemory(text: string): Promise<void> {
      const got = await this.client.call<{ entries: MemoryEntry[] }>("memory.add", { text });
      this.memories = got.entries;
    }

    async deleteMemory(id: string): Promise<void> {
      await this.client.call("memory.delete", { id });
      this.memories = this.memories.filter((m) => m.id !== id);
    }

    /** Whole-file editor: one paragraph per entry; the brain diffs positionally. */
    async replaceMemories(paragraphs: string[]): Promise<MemoryEntry[]> {
      const got = await this.client.call<{ entries: MemoryEntry[] }>("memory.replace_all", { entries: paragraphs });
      this.memories = got.entries;
      return got.entries;
    }
  };
}