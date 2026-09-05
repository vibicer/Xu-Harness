export type ToolsetLike = {
  tools: { is_dropin?: boolean }[];
  enabled: boolean;
  toolset: string;
};

export function builtinToolsetsFrom<T extends ToolsetLike>(toolsets: T[]): T[] {
  return toolsets
    .map((ts) => ({ ...ts, tools: ts.tools.filter((t) => !t.is_dropin) }))
    .filter((ts) => ts.tools.length > 0);
}

export function enabledToolsetsCount(toolsets: ToolsetLike[]): number {
  return toolsets.filter((ts) => ts.enabled).length;
}
import { brain } from "../../store.svelte";

/** Every model id across ALL providers, enabled or not, deduped and sorted.
 *  Deliberately unfiltered by `enabled`: the vision-model and preset-model
 *  pickers must still show a model whose provider is temporarily off, or
 *  toggling a provider would silently blank a saved pick. `brain.models` is
 *  the enabled-only list used by the chat dropdown — not interchangeable. */
export function allModels(): string[] {
  const seen = new Set<string>();
  for (const p of brain.providers) for (const m of (p.models ?? [])) seen.add(m);
  return [...seen].sort();
}
