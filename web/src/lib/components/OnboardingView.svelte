<script lang="ts">
  import { brain } from "../store.svelte";
  import Icon from "./Icon.svelte";

  let step = $state(1);
  let form = $state({ type: "openai-compatible", name: "", base_url: "", api_key: "", models: "" });
  let testing = $state(false);
  let result = $state<{ ok: boolean; count: number; latency?: number; error: string } | null>(null);
  let addedProvider = $state(false);

  function next(): void {
    step = 2;
  }

  async function testConnection(): Promise<void> {
    if (!form.base_url) return;
    testing = true;
    result = null;
    try {
      const id = await brain.addProvider({
        type: form.type,
        name: form.name || undefined,
        base_url: form.base_url,
        api_key: form.api_key || undefined,
        models: form.models
          ? form.models.split(",").map((m) => m.trim()).filter(Boolean)
          : [],
      });
      addedProvider = true;
      const r = await brain.testProvider(id);
      result = { ok: r.ok, count: r.models?.length ?? 0, latency: r.latency_ms, error: r.error ?? "" };
    } catch (e) {
      result = {
        ok: false,
        count: 0,
        error: e instanceof Error ? e.message : String(e),
      };
    } finally {
      testing = false;
    }
  }

  function skip(): void {
    step = 3;
  }

  function proceed(): void {
    step = 3;
  }

  async function launch(): Promise<void> {
    await brain.refreshProviders();
    brain.setView("workspace");
  }
</script>

<div class="view-inner" id="onboarding">
  <div class="page-title">Xu // Setup</div>
  <div class="page-sub">
    First-run configuration. Connect a model provider, or skip and wire it later in Config.
  </div>

  <div class="steps">
    <span class="dot" class:on={step >= 1}>1</span>
    <span class="bar" class:on={step >= 2}></span>
    <span class="dot" class:on={step >= 2}>2</span>
    <span class="bar" class:on={step >= 3}></span>
    <span class="dot" class:on={step >= 3}>3</span>
  </div>

  {#if step === 1}
    <div class="panel ob-panel">
      <h3>WELCOME <span class="hint">step 1 / 3</span></h3>
      <p class="ob-text">
        Xu is a pixel-tier agent brain — it thinks, calls tools, and asks for
        approval before dangerous moves. To talk to a real model, point it at a
        provider. Local stubs work without one, but answers will be hollow.
      </p>
      <p class="ob-text">
        This takes about a minute. You can skip the provider and set it up later
        in Config.
      </p>
      <button class="btn primary" onclick={next}>GET STARTED</button>
    </div>
  {:else if step === 2}
    <div class="panel ob-panel">
      <h3>PROVIDER <span class="hint">step 2 / 3</span></h3>
      <p class="ob-text">
        OpenAI-compatible (Ollama, vLLM, LM Studio, OpenAI) or Anthropic-compatible.
        Localhost needs no key.
      </p>
      <div class="form-grid" style="margin-top:12px">
        <label>
          TYPE
          <select bind:value={form.type}>
            <option value="openai-compatible">openai-compatible</option>
            <option value="anthropic-compatible">anthropic-compatible</option>
          </select>
        </label>
        <label>
          NAME
          <input type="text" bind:value={form.name} placeholder="e.g. DeepSeek, Ollama, Claude" />
        </label>
        <label>
          BASE URL
          <input type="url" bind:value={form.base_url} placeholder="http://localhost:11434/v1" />
        </label>
        <label>
          API KEY <span style="color:var(--faint);font-family:var(--crt);font-size:12px">(optional for localhost)</span>
          <input type="password" bind:value={form.api_key} placeholder="sk-…" />
        </label>
        <label>
          MODELS <span style="color:var(--faint);font-family:var(--crt);font-size:12px">(comma-separated; auto-fetched on TEST)</span>
          <input type="text" bind:value={form.models} placeholder="deepseek-v4-flash-0731, local: qwen2.5-coder:7b" />
        </label>
      </div>
      <div class="ob-actions">
        <button
          class="btn primary"
          disabled={testing || form.base_url === ""}
          onclick={() => void testConnection()}
        >
          {testing ? "TESTING…" : "TEST CONNECTION"}
        </button>
        {#if result}
          {#if result.ok}
            <span class="ob-result ok"><Icon name="check" size={13} /> OK · {result.count} model{result.count === 1 ? "" : "s"}{#if result.latency !== undefined} · {result.latency}ms{/if}</span>
          {:else}
            <span class="ob-result fail"><Icon name="circle-x" size={13} /> {result.error || "connection failed"}</span>
          {/if}
        {/if}
        <span class="spacer"></span>
        <button class="btn" onclick={proceed}>CONTINUE</button>
        <button class="btn" onclick={skip}>SKIP</button>
      </div>
    </div>
  {:else}
    <div class="panel ob-panel">
      <h3>DONE <span class="hint">step 3 / 3</span></h3>
      <p class="ob-text">
        {#if addedProvider}
          Provider configured. Xu will auto-detect it on every launch from here on.
        {:else}
          No provider set — Xu will run in stub mode. Add one any time from Config.
        {/if}
      </p>
      <button class="btn primary" onclick={() => void launch()}>LAUNCH XU</button>
    </div>
  {/if}
</div>

<style>
  #onboarding {
    max-width: 760px;
  }
  .ob-panel {
    margin-top: 22px;
  }
  .ob-text {
    color: var(--dim);
    font-size: 18px;
    line-height: 1.5;
    margin-bottom: 16px;
  }
  .steps {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 24px;
  }
  .dot {
    font-family: var(--pixel);
    font-size: 15px;
    color: var(--faint);
    width: 26px;
    height: 26px;
    display: flex;
    align-items: center;
    justify-content: center;
    border: 3px solid var(--border-strong);
    background: var(--surface-2);
  }
  .dot.on {
    color: var(--magenta);
    border-color: var(--magenta);
    background: var(--surface-3);
  }
  .bar {
    flex: 1;
    max-width: 60px;
    height: 3px;
    background: var(--border);
  }
  .bar.on {
    background: var(--magenta);
  }
  .ob-actions {
    display: flex;
    align-items: center;
    gap: 12px;
    margin-top: 18px;
    flex-wrap: wrap;
  }
  .ob-actions .spacer {
    flex: 1;
  }
  .ob-result {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-family: var(--pixel);
    font-size: 14px;
  }
  .ob-result.ok {
    color: var(--ok);
  }
  .ob-result.fail {
    color: var(--danger);
  }
</style>
