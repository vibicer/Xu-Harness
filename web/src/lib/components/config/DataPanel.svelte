<script lang="ts">
  import { brain } from "../../store.svelte";
  import Icon from "../Icon.svelte";
  let { activeModule = "data" }: { activeModule?: string } = $props();
  let dataHome = $state("");
  let copiedPath = $state(false);
  let dataErr = $state<string | null>(null);
  let personaEdit = $state<string | null>(null);
  let personaLoading = $state(false);
  let personaText = $state("");
  /** What the brain stores for the open persona — the baseline `dirty` compares against. */
  let personaOrig = $state("");
  let personaNewId = $state("");
  let personaSaving = $state(false);
  let personaSaved = $state(false);
  let personaDeleting = $state<Record<string, boolean>>({});
  let personaConfirm = $state<string | null>(null);
  let personaErr = $state<string | null>(null);
  let confirmTimer: ReturnType<typeof setTimeout> | undefined;
  const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e));
  async function openPersona(id: string): Promise<void> {
    if (personaEdit === id) { personaEdit = null; return; }
    personaEdit = id; personaLoading = true; personaText = ""; personaOrig = ""; personaErr = null;
    try { personaText = await brain.getPersona(id); personaOrig = personaText; } catch (e) { personaErr = `load failed: ${errText(e)}`; } finally { personaLoading = false; }
  }
  function newPersona(): void { personaEdit = "__new__"; personaNewId = ""; personaText = "# New persona\n\nYou are…\n"; personaOrig = personaText; personaErr = null; }
  const canSavePersona = $derived((personaEdit === "__new__" ? personaNewId.trim() !== "" : personaEdit !== null) && personaText.trim() !== "");
  const personaDirty = $derived(personaEdit !== null && (personaText !== personaOrig || (personaEdit === "__new__" && personaNewId.trim() !== "")));
  async function savePersona(): Promise<void> {
    const id = personaEdit === "__new__" ? personaNewId.trim() : personaEdit;
    if (!id || !personaText.trim()) return;
    personaSaving = true; personaErr = null;
    try {
      await brain.savePersona(id, personaText);
      personaOrig = personaText; personaSaved = true; setTimeout(() => personaSaved = false, 1200);
    } catch (e) { personaErr = `save failed: ${errText(e)}`; } finally { personaSaving = false; }
  }
  function armConfirm(id: string): void { const next = personaConfirm === id ? null : id; personaConfirm = next; clearTimeout(confirmTimer); if (next !== null) confirmTimer = setTimeout(() => (personaConfirm = null), 3000); }
  async function deletePersona(id: string): Promise<void> {
    personaDeleting = { ...personaDeleting, [id]: true }; personaErr = null;
    try { await brain.deletePersona(id); if (personaEdit === id) personaEdit = null; }
    catch (e) { personaErr = `delete failed: ${errText(e)}`; } finally { personaDeleting = { ...personaDeleting, [id]: false }; }
  }
  /** Wrapped so a rejected `persona.set_active` is visible instead of an unhandled promise. */
  async function setDefaultPersona(id: string | null): Promise<void> {
    personaErr = null;
    try { await brain.setActivePersona(id); } catch (e) { personaErr = `default failed: ${errText(e)}`; }
  }
  async function copyDataHome(): Promise<void> {
    if (!dataHome) return;
    dataErr = null;
    try { await navigator.clipboard.writeText(dataHome); copiedPath = true; setTimeout(() => copiedPath = false, 1200); }
    catch { dataErr = "clipboard unavailable — select the path and copy manually"; }
  }
  $effect(() => { const info = brain.client.call<{ data_home: string }>("app.info"); void info.then((x) => dataHome = x.data_home).catch((e) => dataErr = `data home unavailable: ${errText(e)}`); });
</script>

<div class="cfg-pane" class:active={activeModule === "data"}>
  {#snippet personaEditor()}
    {#if personaEdit === "__new__"}
      <div class="cfg-row" class:dirty={personaNewId.trim() !== ""}>
        <div class="label">id<small>preset name, used to pick it per session</small></div>
        <div class="ctrl"><input type="text" bind:value={personaNewId} placeholder="preset id (e.g. code-reviewer)" /></div>
      </div>
    {/if}
    <div class="cfg-row block" class:dirty={personaText !== personaOrig}>
      <div class="label">
        {personaEdit === "__new__" ? "persona text" : `${personaEdit} — persona text`}
        <small>markdown system prompt, replaces SOUL.md for sessions on this preset</small>
        <span class="diff">unsaved — SAVE writes it to the persona library</span>
      </div>
      <div class="ctrl">
        <textarea
          bind:value={personaText}
          rows="10"
          disabled={personaLoading}
          placeholder={personaLoading ? "loading…" : "# Persona — system prompt text"}
        ></textarea>
      </div>
    </div>
  {/snippet}

  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">DATA HOME</span>
      <span class="d">sessions, config, memory — all state lives under this path</span>
      <span class="sp"></span>
    </div>
    <div class="cfg-row">
      <div class="label">path<small>read-only — the brain resolves it at launch</small></div>
      <div class="ctrl data-home-ctrl">
        <input type="text" class="path-input" value={dataHome} readonly />
        <button type="button" class="k-btn sm" disabled={!dataHome} onclick={() => void copyDataHome()}>
          {#if copiedPath}<Icon name="check" size={12} /> COPIED{:else}COPY{/if}
        </button>
      </div>
    </div>
    {#if dataErr}
      <div class="cfg-bar err">
        <span class="st">{dataErr}</span>
        <span class="sp"></span>
        <button type="button" class="k-btn sm" onclick={() => (dataErr = null)}>DISMISS</button>
      </div>
    {/if}
  </div>

  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">PERSONAS</span>
      <span class="d">{brain.personas.length} personas · default for new sessions: {brain.activePersona ?? "SOUL.md"}</span>
      <span class="sp"></span>
      <button type="button" class="k-btn ghost" disabled={personaEdit !== null} onclick={newPersona}>+ NEW PERSONA</button>
    </div>
    {#if personaEdit !== null || personaErr !== null}
      <div class="cfg-bar" class:on={personaDirty} class:err={personaErr !== null}>
        <span class="st">
          {#if personaErr !== null}{personaErr}
          {:else}{personaEdit === "__new__" ? personaNewId.trim() || "new persona" : personaEdit} · {personaDirty ? "unsaved changes" : "no changes"}{/if}
        </span>
        <span class="sp"></span>
        <span class="k-saved" class:on={personaSaved}>saved</span>
        {#if personaEdit !== null}
          <button type="button" class="k-btn sm" onclick={() => (personaEdit = null)}>CANCEL</button>
          <button type="button" class="k-btn pri sm" disabled={personaSaving || personaLoading || !canSavePersona} onclick={() => void savePersona()}>
            {personaSaving ? "SAVING…" : "SAVE"}
          </button>
        {:else}
          <button type="button" class="k-btn sm" onclick={() => (personaErr = null)}>DISMISS</button>
        {/if}
      </div>
    {/if}
    {#if brain.personas.length === 0 && personaEdit === null}
      <div class="k-empty">No personas yet — the SOUL.md fallback is used for every session.</div>
    {:else}
      {#each brain.personas as p (p.id)}
        <div class="cfg-row">
          <div class="label">
            {p.name}
            <small>{p.id}</small>
          </div>
          <div class="ctrl">
            {#if brain.activePersona === p.id}
              <span class="k-chip auto">DEFAULT</span>
              <button type="button" class="k-btn sm" title="fall back to SOUL.md" onclick={() => void setDefaultPersona(null)}>UNSET</button>
            {:else}
              <button type="button" class="k-btn sm" onclick={() => void setDefaultPersona(p.id)}>MAKE DEFAULT</button>
            {/if}
            <button type="button" class="k-btn sm" class:pri={personaEdit === p.id} onclick={() => void openPersona(p.id)}>
              {personaEdit === p.id ? "CLOSE" : "EDIT"}
            </button>
            <button
              type="button"
              class="k-btn dgr sm"
              class:armed={personaConfirm === p.id}
              disabled={personaDeleting[p.id]}
              onclick={() => (personaConfirm === p.id ? void deletePersona(p.id) : armConfirm(p.id))}
            >
              {personaConfirm === p.id ? (personaDeleting[p.id] ? "…" : "SURE?") : "DEL"}
            </button>
          </div>
        </div>
        {#if personaEdit === p.id}
          {@render personaEditor()}
        {/if}
      {/each}
    {/if}
    {#if personaEdit === "__new__"}
      {@render personaEditor()}
    {/if}
  </div>
</div>
