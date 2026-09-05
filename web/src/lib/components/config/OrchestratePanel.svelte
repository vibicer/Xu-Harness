<script lang="ts">
  import { tick } from "svelte";
  import { brain } from "../../store.svelte";
  import Icon from "../Icon.svelte";
  import { allModels } from "./shared.svelte";

import type { PresetInfo } from "../../types";

  let { activeModule = "orchestrate", presets, loadPresets }: { activeModule?: string; presets: PresetInfo[]; loadPresets: () => Promise<void> } = $props();

  // ---- orchestration presets: designer state ----
  type PresetNode = {
    id: string;
    name: string;
    role: "orchestrator" | "agent";
    persona: string;
    job: string;
    model: string;
    /** ordered backup models tried when `model` fails ([] = only the global chain) */
    fallbacks: string[];
    children: PresetNode[];
_skills: string[] | null; _tools: string[] | null; _memoryT: string;
};
function blankNode(role: "orchestrator" | "agent" = "agent"): PresetNode {
return { id: crypto.randomUUID(), name: "agent", role, persona: "", job: "",
model: "", fallbacks: [], children: [],
_skills: null, _tools: null, _memoryT: "" };
}
let presetId = $state<string | null>(null);
let presetName = $state("");
let presetRoot = $state<PresetNode>(blankNode("orchestrator"));
let presetSaving = $state(false);
let presetMsg = $state("");
let presetErr = $state(false);
let presetOpen = $state(false);
/** the open preset as the server has it — the yardstick for "unsaved changes" */
let presetBase = $state("");
let modelPickerOpen = $state<string | null>(null);

  // ---- two-click guard for writes that hit the brain with no undo ----
  // First click arms, second commits, 3s of silence disarms. Same shape as
  // DataPanel's armConfirm, keyed so only one button is ever armed.
  let armedKey = $state<string | null>(null);
  let armTimer: ReturnType<typeof setTimeout> | undefined;
  function arm(key: string, commit: () => void): void {
    clearTimeout(armTimer);
    if (armedKey === key) { armedKey = null; commit(); return; }
    armedKey = key;
    armTimer = setTimeout(() => (armedKey = null), 3000);
  }

  // ---- model fallback chains (global config + per preset agent) ----
  // Ordered backup models: the brain walks them when the active model can't be
  // resolved or its provider keeps failing mid-turn.
  let modelFallbacks = $state<string[]>([...(brain.config.model_fallbacks ?? [])]);
  // Reseed when the config refreshes — the shared mirror $effect that used to
  // do this lived in ConfigView and went to AgentPanel with the other mirrors.
  $effect(() => {
    modelFallbacks = [...(brain.config.model_fallbacks ?? [])];
  });
  // One open dropdown at a time, dismissed by a click anywhere outside a `.k-dd`.
  $effect(() => {
    if (modelPickerOpen === null) return;
    const onDoc = (e: MouseEvent) => {
      if (!(e.target as Element | null)?.closest(".k-dd")) modelPickerOpen = null;
    };
    document.addEventListener("mousedown", onDoc, true);
    return () => document.removeEventListener("mousedown", onDoc, true);
  });
  function chainAdd(chain: string[], m: string): string[] {
    return m && !chain.includes(m) ? [...chain, m] : chain;
  }
  function chainMove(chain: string[], i: number, dir: -1 | 1): string[] {
    const j = i + dir;
    if (j < 0 || j >= chain.length) return chain;
    const next = chain.slice();
    [next[i], next[j]] = [next[j], next[i]];
    return next;
  }
  async function saveModelFallbacks(next: string[]): Promise<void> {
    modelFallbacks = next;
    modelPickerOpen = null;
    await brain.saveConfig("model_fallbacks", next);
  }

  function asNode(d: Record<string, unknown>): PresetNode {
    const opt = (v: unknown): string[] | null =>
      v == null ? null : (v as unknown[]).map(String);
    return {
      id: String(d.id ?? crypto.randomUUID()),
      name: String(d.name ?? "agent"),
      role: d.role === "orchestrator" ? "orchestrator" : "agent",
      persona: String(d.persona ?? ""),
      job: String(d.job ?? ""),
      model: (d.model as string) ?? "",
      fallbacks: Array.isArray(d.fallbacks) ? d.fallbacks.map(String) : [],
      children: Array.isArray(d.children)
        ? d.children.map((c) => asNode(c as Record<string, unknown>)) : [],
_skills: opt(d.skills),
_tools: opt(d.tools),
_memoryT: (opt(d.memory) ?? []).join(", "),
};
}
  function listOrNull(v: string): string[] | null {
    const out = v.split(",").map((x) => x.trim()).filter(Boolean);
    return out.length ? out : null;
  }
  function toRaw(n: PresetNode): Record<string, unknown> {
    return {
id: n.id, name: n.name, role: n.role, persona: n.persona, job: n.job,
model: n.model || null,
fallbacks: n.fallbacks,
skills: n._skills,
tools: n._tools,
memory: listOrNull(n._memoryT),
children: n.children.map(toRaw),
    };
  }
  /** The draft exactly as `savePreset` would send it — the basis for dirty state. */
  function snapshot(): string {
    return JSON.stringify({ name: presetName.trim(), tree: toRaw(presetRoot) });
  }
  // Real comparison, not a keystroke flag: type a character and undo it and the
  // commit bar goes away again.
  const presetDirty = $derived(presetOpen && snapshot() !== presetBase);
  function addChild(parent: PresetNode): void {
    parent.children = [...parent.children, blankNode("agent")];
  }
  function removeChild(parent: PresetNode, i: number): void {
    parent.children = parent.children.filter((_, idx) => idx !== i);
  }
  function duplicateChild(parent: PresetNode, i: number): void {
    const src = parent.children[i];
    if (!src) return;
    const copy = JSON.parse(JSON.stringify(src)) as PresetNode;
    copy.id = crypto.randomUUID();
    // number duplicates: src → src 1, then 2, 3… skipping taken names
    const base = src.name;
    const taken = new Set(parent.children.map((c) => c.name));
    let n = 1;
    while (taken.has(`${base} ${n}`)) n++;
    copy.name = `${base} ${n}`;
    const next = parent.children.slice();
    next.splice(i + 1, 0, copy);
    parent.children = next;
  }
function hasItem(list: string[] | null, item: string): boolean {
return (list ?? []).includes(item);
}
function toggleItem(list: string[] | null, item: string): string[] | null {
const cur = list ?? [];
return cur.includes(item) ? (cur.length === 1 ? null : cur.filter((x) => x !== item)) : [...cur, item];
}
function startNewPreset(): void {
presetId = null; presetName = ""; presetRoot = blankNode("orchestrator");
presetMsg = ""; presetErr = false; presetOpen = true;
presetBase = snapshot();
}
  /** Pull the server copy into the draft and re-baseline it (load and discard). */
  async function loadPresetInto(id: string): Promise<void> {
    const got = await brain.client.call<{ preset: Record<string, unknown> }>("preset.get", { id });
    presetId = id;
    presetName = String(got.preset.name ?? "");
    presetRoot = asNode(got.preset.root as Record<string, unknown>);
    presetMsg = ""; presetErr = false;
    presetBase = snapshot();
  }
  // The config pane scrolls inside `.view-inner`, so scroll that box explicitly.
  // Element-level "scroll into view" helpers are banned in this UI: inside an
  // embedded frame they drag the outer document along with them.
  async function scrollToEditor(): Promise<void> {
    await tick();
    const el = document.getElementById("preset-editor");
    const box = el?.closest(".view-inner") as HTMLElement | null;
    if (!el || !box) return;
    box.scrollTop += el.getBoundingClientRect().top - box.getBoundingClientRect().top - 12;
  }
  async function editPreset(id: string): Promise<void> {
    try {
      await loadPresetInto(id);
presetOpen = true;
await scrollToEditor();
    } catch { presetMsg = "failed to load"; presetErr = true; }
  }
  /** Throw the staged edits away: back to the server copy, or to a blank tree. */
  async function discardPreset(): Promise<void> {
    if (presetId) {
      try { await loadPresetInto(presetId); }
      catch { presetMsg = "failed to reload"; presetErr = true; }
      return;
    }
    presetName = ""; presetRoot = blankNode("orchestrator");
    presetMsg = ""; presetErr = false;
    presetBase = snapshot();
  }
  let flashTimer: ReturnType<typeof setTimeout> | undefined;
  /** Green confirmation in the section head. It fades, so a stale "saved" can
   *  never sit next to a commit bar reporting unsaved changes. */
  function flashSaved(text: string): void {
    presetMsg = text; presetErr = false;
    clearTimeout(flashTimer);
    flashTimer = setTimeout(() => { if (!presetErr) presetMsg = ""; }, 1800);
  }
  async function savePreset(): Promise<void> {
    if (!presetName.trim()) { presetMsg = "name required"; presetErr = true; return; }
    presetSaving = true; presetMsg = ""; presetErr = false;
    try {
      const r = await brain.upsertPreset({ id: presetId ?? undefined, name: presetName, tree: toRaw(presetRoot) });
      presetId = String(r.preset.id);
      presetBase = snapshot();
      flashSaved("preset saved");
      await loadPresets();
    } catch (e) {
      presetMsg = `save failed: ${e instanceof Error ? e.message : String(e)}`; presetErr = true;
    } finally { presetSaving = false; }
  }
  async function deletePresetNow(id: string): Promise<void> {
    try {
      await brain.deletePreset(id);
if (presetId === id) { startNewPreset(); presetOpen = false; }
      await loadPresets();
    } catch { /* ignored */ }
  }
</script>

  <!-- ========== ORCHESTRATE ========== -->
  <div class="cfg-pane" class:active={activeModule === "orchestrate"}>
    {#if presetOpen && (presetDirty || presetErr)}
      <div class="cfg-bar" class:on={presetDirty} class:err={presetErr}>
        <span class="st">{presetErr ? presetMsg : "unsaved preset changes"}</span>
        <span class="sp"></span>
        <button type="button" class="k-btn ghost" disabled={presetSaving || !presetDirty}
          onclick={() => void discardPreset()}>discard</button>
        <button type="button" class="k-btn pri" disabled={presetSaving || !presetDirty}
          onclick={() => void savePreset()}>{presetSaving ? "…" : "SAVE PRESET"}</button>
      </div>
    {/if}

    <div class="cfg-sec">
      <div class="cfg-sec-hd">
        <span class="t">MODEL FALLBACKS</span>
        <span class="d">global chain · {modelFallbacks.length} deep · applied immediately</span>
        <span class="sp"></span>
      </div>
      {#if allModels().length === 0}
        <div class="cfg-row">
          <div class="label">no models<small>add a provider and fetch its models first</small></div>
          <div class="ctrl"><span class="k-empty">nothing to chain</span></div>
        </div>
      {:else}
        {#each modelFallbacks as m, i (m)}
          <div class="cfg-row">
            <div class="label">
              <div class="fb-item"><span class="fb-ord">{i + 1}</span><span class="fb-name">{m}</span></div>
            </div>
            <div class="ctrl fb-actions">
              <button type="button" class="k-btn icon" aria-label="move {m} earlier" disabled={i === 0}
                onclick={() => void saveModelFallbacks(chainMove(modelFallbacks, i, -1))}>↑</button>
              <button type="button" class="k-btn icon" aria-label="move {m} later" disabled={i === modelFallbacks.length - 1}
                onclick={() => void saveModelFallbacks(chainMove(modelFallbacks, i, 1))}>↓</button>
              <button type="button" class="k-btn dgr" class:armed={armedKey === `fb:${m}`}
                onclick={() => arm(`fb:${m}`, () => void saveModelFallbacks(modelFallbacks.filter((x) => x !== m)))}
                >{armedKey === `fb:${m}` ? "confirm?" : "remove"}</button>
            </div>
          </div>
        {/each}
        <div class="cfg-row">
          <div class="label">add a backup model<small>Tried when a turn's model is gone or its provider fails past retries. Applies to every agent — main and sub-agents, with or without a preset — after each agent's own fallbacks.</small></div>
          <div class="ctrl">
            <div class="k-dd">
              <button type="button" class="k-btn" aria-haspopup="listbox"
                aria-expanded={modelPickerOpen === "__fallback__"}
                onclick={() => (modelPickerOpen = modelPickerOpen === "__fallback__" ? null : "__fallback__")}>
                <span class="v" class:ph={modelFallbacks.length === 0}>{modelFallbacks.length === 0 ? "no fallback — a failed model ends the turn" : "+ add another"}</span>
                <Icon name={modelPickerOpen === "__fallback__" ? "chevron-up" : "chevron-down"} size={12} />
              </button>
              {#if modelPickerOpen === "__fallback__"}
                <div class="k-dd-pop" role="listbox" aria-label="add fallback model">
                  {#each allModels().filter((m) => !modelFallbacks.includes(m)) as m (m)}
                    <button type="button" role="option" aria-selected="false"
                      onclick={() => void saveModelFallbacks(chainAdd(modelFallbacks, m))}>{m}</button>
                  {:else}
                    <div class="grp">every known model is already in the chain</div>
                  {/each}
                </div>
              {/if}
            </div>
          </div>
        </div>
      {/if}
    </div>

    <div class="cfg-sec">
      <div class="cfg-sec-hd">
        <span class="t">ORCHESTRATION PRESETS</span>
        <span class="d">{presets.length} total</span>
        {#if presetErr && !presetOpen}<span class="cfg-err">{presetMsg}</span>{/if}
        <span class="sp"></span>
        <button type="button" class="k-btn ghost" onclick={() => void loadPresets()}>refresh</button>
        <button type="button" class="k-btn ghost" onclick={startNewPreset}>+ new preset</button>
      </div>
      {#if presets.length === 0}
        <div class="cfg-row">
          <div class="label">no presets yet<small>build one with + new preset, then pick it in the session's Agent State panel</small></div>
          <div class="ctrl"><span class="k-empty">—</span></div>
        </div>
      {:else}
        {#each presets as p (p.id)}
          <div class="cfg-row">
            <div class="label">
              {p.name}
              <small>{p.node_count} agents · {new Date(p.updated * 1000).toLocaleString()}</small>
            </div>
            <div class="ctrl">
              <button type="button" class="k-btn" onclick={() => void editPreset(p.id)}>edit</button>
              <button type="button" class="k-btn dgr" class:armed={armedKey === `preset:${p.id}`}
                onclick={() => arm(`preset:${p.id}`, () => void deletePresetNow(p.id))}
                >{armedKey === `preset:${p.id}` ? "confirm?" : "delete"}</button>
            </div>
          </div>
        {/each}
      {/if}
    </div>

    {#if presetOpen}
      <div class="cfg-sec" id="preset-editor">
        <div class="cfg-sec-hd">
          <span class="t">{presetId ? "EDIT PRESET" : "NEW PRESET"}</span>
          <span class="d">orchestrator + sub-agents · staged until you save</span>
          <span class="sp"></span>
          <span class="k-saved" class:on={!!presetMsg && !presetErr}>{presetErr ? "" : presetMsg}</span>
        </div>

        <div class="cfg-row">
          <div class="label">preset name<small>how the preset is listed in the session's Agent State panel</small></div>
          <div class="ctrl"><input type="text" placeholder="preset name" aria-label="preset name" bind:value={presetName} /></div>
        </div>

        <div class="preset-section">
          <div class="preset-sec-head">
            <h4>ORCHESTRATOR · MAIN AGENT</h4>
          </div>
          {@render agentFields(presetRoot)}
        </div>

        <div class="preset-section">
          <div class="preset-sec-head">
            <h4>SUBAGENTS · {presetRoot.children.length}</h4>
            <button type="button" class="k-btn ghost" onclick={() => addChild(presetRoot)}>+ add sub-agent</button>
          </div>
          {#if presetRoot.children.length === 0}
            <div class="k-empty">No sub-agents yet.</div>
          {:else}
            {#each presetRoot.children as c, i (c.id)}
              <div class="preset-node">
                <div class="cfg-row">
                  <div class="label">sub-agent {i + 1}<small>the label you pass to delegate</small></div>
                  <div class="ctrl">
                    <input type="text" aria-label="sub-agent name" placeholder="name" bind:value={c.name} />
                    <button type="button" class="k-btn icon" aria-label="duplicate sub-agent {c.name}"
                      onclick={() => duplicateChild(presetRoot, i)}>⧉</button>
                    <button type="button" class="k-btn dgr" onclick={() => removeChild(presetRoot, i)}>remove</button>
                  </div>
                </div>
                {@render agentFields(c)}
              </div>
            {/each}
          {/if}
        </div>
      </div>
    {/if}


{#snippet agentFields(node: PresetNode)}
  <div class="cfg-row block">
    <div class="label">persona<small>this agent's system prompt — inline and independent</small></div>
    <div class="ctrl"><textarea rows="4" placeholder="this agent's system prompt" bind:value={node.persona}></textarea></div>
  </div>
  <div class="cfg-row">
    <div class="label">model<small>blank = inherit the session model</small></div>
    <div class="ctrl">
      <div class="k-dd">
        <button type="button" class="k-btn" aria-haspopup="listbox" aria-expanded={modelPickerOpen === node.id}
          onclick={() => (modelPickerOpen = modelPickerOpen === node.id ? null : node.id)}>
          <span class="v" class:ph={!node.model}>{node.model || "inherit session model"}</span>
          <Icon name={modelPickerOpen === node.id ? "chevron-up" : "chevron-down"} size={12} />
        </button>
        {#if modelPickerOpen === node.id}
          <div class="k-dd-pop" role="listbox" aria-label="model">
            <button type="button" role="option" aria-selected={!node.model} class:sel={!node.model}
              onclick={() => { node.model = ""; modelPickerOpen = null; }}>inherit session model</button>
            {#each allModels() as m (m)}
              <button type="button" role="option" aria-selected={node.model === m} class:sel={node.model === m}
                onclick={() => { node.model = m; modelPickerOpen = null; }}>{m}</button>
            {/each}
          </div>
        {/if}
      </div>
    </div>
  </div>
  <div class="cfg-row">
    <div class="label">fallback models<small>tried in order before the global chain</small></div>
    <div class="ctrl">
      <div class="k-dd">
        <button type="button" class="k-btn" aria-haspopup="listbox"
          aria-expanded={modelPickerOpen === `${node.id}:fb`}
          onclick={() => (modelPickerOpen = modelPickerOpen === `${node.id}:fb` ? null : `${node.id}:fb`)}>
          <span class="v" class:ph={node.fallbacks.length === 0}>{node.fallbacks.length === 0 ? "none — use the global chain only" : "+ add another"}</span>
          <Icon name={modelPickerOpen === `${node.id}:fb` ? "chevron-up" : "chevron-down"} size={12} />
        </button>
        {#if modelPickerOpen === `${node.id}:fb`}
          <div class="k-dd-pop" role="listbox" aria-label="add fallback model">
            {#each allModels().filter((m) => m !== node.model && !node.fallbacks.includes(m)) as m (m)}
              <button type="button" role="option" aria-selected="false"
                onclick={() => { node.fallbacks = chainAdd(node.fallbacks, m); modelPickerOpen = null; }}>{m}</button>
            {:else}
              <div class="grp">no other model left to add</div>
            {/each}
          </div>
        {/if}
      </div>
    </div>
  </div>
  {#each node.fallbacks as m, i (m)}
    <div class="cfg-row sub">
      <div class="label">
        <div class="fb-item"><span class="fb-ord">{i + 1}</span><span class="fb-name">{m}</span></div>
      </div>
      <div class="ctrl fb-actions">
        <button type="button" class="k-btn icon" aria-label="move {m} earlier" disabled={i === 0}
          onclick={() => (node.fallbacks = chainMove(node.fallbacks, i, -1))}>↑</button>
        <button type="button" class="k-btn icon" aria-label="move {m} later" disabled={i === node.fallbacks.length - 1}
          onclick={() => (node.fallbacks = chainMove(node.fallbacks, i, 1))}>↓</button>
        <button type="button" class="k-btn dgr"
          onclick={() => (node.fallbacks = node.fallbacks.filter((_, idx) => idx !== i))}>remove</button>
      </div>
    </div>
  {/each}
  <div class="cfg-row block">
    <div class="label">skills<small>none checked = inherit the session's skills</small></div>
    <div class="ctrl k-chips">
      {#each brain.skills as s (s.id)}
        <label class="chk"><input type="checkbox" checked={hasItem(node._skills, s.id)} onchange={() => (node._skills = toggleItem(node._skills, s.id))} /><span class="box"></span> {s.name}</label>
      {/each}
    </div>
  </div>
  <div class="cfg-row block">
    <div class="label">tools<small>none checked = inherit the session's toolsets</small></div>
    <div class="ctrl k-chips">
      {#each brain.toolsets as t (t.toolset)}
        <label class="chk"><input type="checkbox" checked={hasItem(node._tools, t.toolset)} onchange={() => (node._tools = toggleItem(node._tools, t.toolset))} /><span class="box"></span> {t.toolset}</label>
      {/each}
    </div>
  </div>
  <div class="cfg-row">
    <div class="label">memory<small>entry-id prefixes, comma separated; blank = inherit</small></div>
    <div class="ctrl"><input type="text" placeholder="e.g. m1ab, m2cd" bind:value={node._memoryT} /></div>
  </div>
{/snippet}
  </div>
