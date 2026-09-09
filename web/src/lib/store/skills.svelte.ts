import type { SkillInfo } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/**
 * Skill catalog: Config → Skills manages the GLOBAL defaults (`skills`),
 * Agent State → SKILLS manages the ACTIVE SESSION's effective state
 * (`sessionSkills`). A session without overrides mirrors the global set.
 */
export function SkillsMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Skills extends Base {
    skills = $state<SkillInfo[]>([]);
    /** Active session's effective skill catalog (session overrides applied). */
    sessionSkills = $state<SkillInfo[]>([]);

    async refreshSkills(): Promise<void> {
      const got = await this.client.call<{ skills: SkillInfo[] }>("skill.list");
      this.skills = got.skills;
    }

    async refreshSessionSkills(): Promise<void> {
      const sid = this.activeSessionId;
      if (!sid) {
        this.sessionSkills = [];
        return;
      }
      const got = await this.client.call<{ skills: SkillInfo[] }>("skill.list", { session_id: sid });
      this.sessionSkills = got.skills;
    }

    async setSkill(id: string, enabled: boolean): Promise<void> {
      const got = await this.client.call<{ skills: SkillInfo[] }>("skill.set", { id, enabled });
      this.skills = got.skills;
    }

    /** Toggle a skill for the active session only — the global default is untouched. */
    async setSessionSkill(id: string, enabled: boolean): Promise<void> {
      const sid = this.activeSessionId;
      if (!sid) return;
      const got = await this.client.call<{ skills: SkillInfo[] }>("skill.set", { id, enabled, session_id: sid });
      this.sessionSkills = got.skills;
    }

    async loadSkillBody(id: string): Promise<string> {
      const got = await this.client.call<{ id: string; body: string }>("skill.body", { id });
      return got.body;
    }
  };
}