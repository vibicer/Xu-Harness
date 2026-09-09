<script lang="ts">
  import { brain } from "../../store.svelte";
  let { activeModule = "memory" }: { activeModule?: string } = $props();

  /* ===== mnemosyne + auto-capture settings (straight-through writes) ===== */
type BoolKey = "memory_mnemosyne_inject" | "memory_mnemosyne_embeddings" | "memory_autocapture";
  type NumKey = "memory_mnemosyne_top_k" | "memory_mnemosyne_max_chars" | "memory_capture_min_interval";
  const errText = (e: unknown): string => (e instanceof Error ? e.message : String(e));

  let settingsErr = $state<string | null>(null);
let mnemo = $state<{ available: boolean; data_dir: string; embeddings_enabled: boolean; embeddings_installed: boolean } | null>(null);
  let mnemoErr = $state<string | null>(null);

  const cfg = $derived(brain.config);
  const mnemoInject = $derived(cfg.memory_mnemosyne_inject ?? true);
  const mnemoTopK = $derived(cfg.memory_mnemosyne_top_k ?? 5);
  const mnemoMaxChars = $derived(cfg.memory_mnemosyne_max_chars ?? 2000);
  const autoCapture = $derived(cfg.memory_autocapture ?? true);
  const captureInterval = $derived(cfg.memory_capture_min_interval ?? 300);
const mnemoEmbed = $derived(cfg.memory_mnemosyne_embeddings ?? true);

  $effect(() => {
const p = brain.client.call<{ available: boolean; data_dir: string; embeddings_enabled: boolean; embeddings_installed: boolean }>("memory.mnemo");
    void p.then((x) => { mnemo = x; mnemoErr = null; }).catch((e) => { mnemoErr = `status unavailable: ${errText(e)}`; });
  });

  async function setToggle(key: BoolKey, v: boolean): Promise<void> {
    settingsErr = null;
    try { await brain.saveConfig(key, v); } catch (e) { settingsErr = errText(e); }
  }
  async function setNum(key: NumKey, v: number, min: number): Promise<void> {
    const n = Math.round(v);
    if (Number.isNaN(n)) return;
    settingsErr = null;
    try { await brain.saveConfig(key, Math.max(min, n)); } catch (e) { settingsErr = errText(e); }
  }

  /* ===== MEMORY.md — one box, entries separated by blank lines ===== */
  let memSaving = $state(false);
  let memErr = $state<string | null>(null);
  let memBase = ""; // last server value adopted
  let memBlob = $state("");

  $effect(() => {
    const server = brain.memories.map((m) => m.text).join("\n\n");
    if (memBase === server) return;
    if (memBlob === memBase) memBlob = server; // adopt only where nothing is staged
    memBase = server;
  });
  const memDirty = $derived(memBlob !== memBase);
  function memRows(): number { return Math.min(28, Math.max(8, memBlob.split("\n").length + 2)); }
  async function saveMemoryBlob(): Promise<void> {
    memErr = null; memSaving = true;
    try {
      const paras = memBlob.split(/\n\s*\n/).map((s) => s.trim());
      const entries = await brain.replaceMemories(paras);
      memBase = entries.map((m) => m.text).join("\n\n");
      memBlob = memBase;
    } catch (e) { memErr = `save failed: ${errText(e)}`; } finally { memSaving = false; }
  }
</script>

<div class="cfg-pane" class:active={activeModule === "memory"}>
  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">MNEMOSYNE</span>
      <span class="d">long-term memory · ranked recall digest injected before each turn</span>
      <span class="sp"></span>
      <span class="k-chip" class:auto={mnemo?.available === true}>
        {mnemoErr !== null ? "N/A" : mnemo === null ? "…" : mnemo.available ? "INSTALLED" : "NOT INSTALLED"}
      </span>
    </div>
    {#if mnemoErr !== null}
      <div class="cfg-bar err"><span class="st">{mnemoErr}</span></div>
    {:else if mnemo !== null && !mnemo.available}
      <div class="k-empty">
        mnemosyne-memory is not installed in the brain env — tools and the digest stay
        inactive. `pip install mnemosyne-memory` to enable.
      </div>
    {:else}
      <div class="cfg-row">
        <div class="label">inject digest<small>top matches for the latest user message go into the system prompt</small></div>
        <div class="ctrl">
          <span class="state-badge">{mnemoInject ? "ON" : "OFF"}</span>
          <label class="toggle">
            <input type="checkbox" aria-label="inject mnemosyne digest" checked={mnemoInject}
                   onchange={(e) => void setToggle("memory_mnemosyne_inject", e.currentTarget.checked)} />
            <span class="track"></span>
            <span class="thumb"></span>
          </label>
        </div>
      </div>
      <div class="cfg-row">
          <div class="label">embeddings<small>semantic (vector) recall — falls back to keyword/FTS when off or unavailable</small></div>
          <div class="ctrl">
            <span class="state-badge">{mnemoEmbed ? "ON" : "OFF"}</span>
            <label class="toggle">
              <input type="checkbox" aria-label="use mnemosyne embeddings" checked={mnemoEmbed}
                     onchange={(e) => void setToggle("memory_mnemosyne_embeddings", e.currentTarget.checked)} />
              <span class="track"></span>
              <span class="thumb"></span>
            </label>
          </div>
      </div>
      {#if mnemo !== null && mnemoEmbed && mnemo.embeddings_enabled && !mnemo.embeddings_installed}
        <div class="k-empty">
          embedding extra not installed — recall runs keyword-only. Install it in the brain env:
          `pip install "mnemosyne-memory[embeddings]"` (or set MNEMOSYNE_EMBEDDINGS_VIA_API + a key for a remote model).
        </div>
      {/if}
      <div class="cfg-row">
        <div class="label">digest size<small>memories per digest / hard cap in characters</small></div>
        <div class="ctrl k-step-pair">
          <span class="k-step">
            <input type="number" aria-label="memories per digest" min={1} step={1} value={mnemoTopK}
                   onchange={(e) => void setNum("memory_mnemosyne_top_k", Number(e.currentTarget.value), 1)} />
            <button type="button" aria-label="memories per digest down" onclick={() => void setNum("memory_mnemosyne_top_k", mnemoTopK - 1, 1)}>&minus;</button>
            <button type="button" aria-label="memories per digest up" onclick={() => void setNum("memory_mnemosyne_top_k", mnemoTopK + 1, 1)}>+</button>
          </span>
          <span class="k-step">
            <input type="number" aria-label="digest character cap" min={200} step={200} value={mnemoMaxChars}
                   onchange={(e) => void setNum("memory_mnemosyne_max_chars", Number(e.currentTarget.value), 200)} />
            <span class="u">ch</span>
            <button type="button" aria-label="digest cap down" onclick={() => void setNum("memory_mnemosyne_max_chars", mnemoMaxChars - 200, 200)}>&minus;</button>
            <button type="button" aria-label="digest cap up" onclick={() => void setNum("memory_mnemosyne_max_chars", mnemoMaxChars + 200, 200)}>+</button>
          </span>
        </div>
      </div>
      {#if mnemo?.data_dir}
        <div class="cfg-row">
          <div class="label">store<small>one SQLite file under this directory — zero cloud</small></div>
          <div class="ctrl"><input type="text" class="path-input" value={mnemo.data_dir} readonly /></div>
        </div>
      {/if}
    {/if}
  </div>

  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">AUTO-CAPTURE</span>
      <span class="d">turn-tail extractor lifts durable facts into Mnemosyne</span>
      <span class="sp"></span>
    </div>
    <div class="cfg-row">
      <div class="label">capture facts<small>runs only on completed root turns; deduped against recall</small></div>
      <div class="ctrl">
        <span class="state-badge">{autoCapture ? "ON" : "OFF"}</span>
        <label class="toggle">
          <input type="checkbox" aria-label="auto-capture memories" checked={autoCapture}
                 onchange={(e) => void setToggle("memory_autocapture", e.currentTarget.checked)} />
          <span class="track"></span>
          <span class="thumb"></span>
        </label>
      </div>
    </div>
    {#if autoCapture}
      <div class="cfg-row">
        <div class="label">min interval<small>per-session cooldown between extractor calls</small></div>
        <div class="ctrl">
          <span class="k-step">
            <input type="number" aria-label="minimum capture interval" min={0} step={30} value={captureInterval}
                   onchange={(e) => void setNum("memory_capture_min_interval", Number(e.currentTarget.value), 0)} />
            <span class="u">s</span>
            <button type="button" aria-label="interval down" onclick={() => void setNum("memory_capture_min_interval", captureInterval - 30, 0)}>&minus;</button>
            <button type="button" aria-label="interval up" onclick={() => void setNum("memory_capture_min_interval", captureInterval + 30, 0)}>+</button>
          </span>
        </div>
      </div>
    {/if}
  </div>

  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">MEMORY</span>
      <span class="d">MEMORY.md · {brain.memories.length} entries</span>
      <span class="sp"></span>
    </div>
    {#if memDirty || memErr !== null}
      <div class="cfg-bar" class:on={memDirty} class:err={memErr !== null}>
        <span class="st">{memErr ?? `${memBlob.split(/\n\s*\n/).filter((s) => s.trim()).length} entries staged — one per paragraph`}</span>
        <span class="sp"></span>
        <button type="button" class="k-btn sm" disabled={memSaving} onclick={() => { memBlob = memBase; memErr = null; }}>DISCARD</button>
        <button type="button" class="k-btn pri sm" disabled={memSaving || !memDirty} onclick={() => void saveMemoryBlob()}>
          {memSaving ? "SAVING…" : "SAVE"}
        </button>
      </div>
    {/if}
    <div class="cfg-row block">
      <div class="label">entries<small>one memory per paragraph — blank line separates; delete a paragraph to delete the memory</small></div>
      <div class="ctrl">
        <textarea
          class="mem-blob"
          rows={memRows()}
          value={memBlob}
          disabled={memSaving}
          oninput={(e) => (memBlob = e.currentTarget.value)}
        ></textarea>
      </div>
    </div>
  </div>

  {#if settingsErr !== null}
    <div class="cfg-bar err"><span class="st">{settingsErr}</span></div>
  {/if}
</div>
