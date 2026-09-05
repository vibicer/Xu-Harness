import type { SubagentInfo, SubagentRun, TodoInfo } from "../types";
import type { Ctor, StoreCoreBase } from "./core.svelte";

/**
 * Delegated sub-agents: the roster, live run activity, and the active
 * session's todo list (written by the `todo.updated` event, which is why it
 * lives with the sub-agent state that the same activity view renders).
 *
 * The event router in store.svelte.ts writes `subRuns` /
 * `subagentActivityUpdate` while streaming, and `loadSession` clears them on a
 * tab switch. Those writes go through the public accessors this mixin
 * generates, so they stay reactive.
 */
export function SubagentsMixin<T extends Ctor<StoreCoreBase>>(Base: T) {
  return class Subagents extends Base {
    subagents = $state<SubagentInfo[]>([]);
    subagentActivityUpdate = $state<SubagentRun | null>(null);
    /** Every live run of the active session, keyed by run id. Concurrent
     *  siblings each stream their own activity; the single slot above only ever
     *  held the newest, so the other sub-agents' logs were dropped on arrival.
     *  The activity view tabs over this. */
    subRuns = $state<Record<string, SubagentRun>>({});
    /** Layout → Chat handshake: set this to ask Chat to open a sub-agent run's
     *  activity modal (Chat owns the modal + live-streaming path). */
    requestSubRunId = $state<string | null>(null);
    todos = $state<TodoInfo>({ phases: [] });

    private _subagentsRefreshing = false;
    async refreshSubagents(): Promise<SubagentInfo[]> {
      if (this._subagentsRefreshing) return this.subagents;
      this._subagentsRefreshing = true;
      try {
        const got = await this.client.call<{ subagents: SubagentInfo[] }>("subagent.list");
        this.subagents = got.subagents;
        return got.subagents;
      } finally {
        this._subagentsRefreshing = false;
      }
    }

    async spawnSubagent(prompt: string): Promise<string> {
      const got = await this.client.call<{ id: string }>("subagent.send", { prompt });
      return got.id;
    }

    async interruptSubagent(id: string): Promise<void> {
      await this.client.call("subagent.interrupt", { id });
    }

    /** What a delegated sub-agent did — one run by id, one fan-out's siblings by
     *  `group`, or all runs of a session. */
    async subagentActivity(
      opts: { run_id?: string; session_id?: string; group?: string },
    ): Promise<SubagentRun[]> {
      const got = await this.client.call<{ runs: SubagentRun[] }>("subagent.activity", {
        ...opts,
        session_id: opts.session_id ?? this.activeSessionId,
      });
      return got.runs ?? [];
    }

    async refreshTodo(): Promise<void> {
      const sid = this.activeSessionId;
      if (!sid) return;
      try {
        const got = await this.client.call<TodoInfo>("todo.get", { session_id: sid });
        this.todos = got;
      } catch { /* session may not be ready */ }
    }
  };
}
