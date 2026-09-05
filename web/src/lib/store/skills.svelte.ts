import type { SkillInfo } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/** Skill catalog (Config → Skills): enable/disable + on-demand body load. */
export function SkillsMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Skills extends Base {
    skills = $state<SkillInfo[]>([]);

    async refreshSkills(): Promise<void> {
      const got = await this.client.call<{ skills: SkillInfo[] }>("skill.list");
      this.skills = got.skills;
    }

    async setSkill(id: string, enabled: boolean): Promise<void> {
      const got = await this.client.call<{ skills: SkillInfo[] }>("skill.set", { id, enabled });
      this.skills = got.skills;
    }

    async loadSkillBody(id: string): Promise<string> {
      const got = await this.client.call<{ id: string; body: string }>("skill.body", { id });
      return got.body;
    }
  };
}
