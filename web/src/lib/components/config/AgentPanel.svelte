<script lang="ts">
  import { brain } from "../../store.svelte";
  import Icon from "../Icon.svelte";
  import { allModels } from "./shared.svelte";

  let { activeModule = "agent" }: { activeModule?: string } = $props();

  /* ===== staged values =====
     Typed and dragged fields edit a draft and commit from the bar at the foot of
     the pane: a half-typed `2000` is never written as a context length, and the
     row shows what the value will become before it becomes it. Switches, the
     mode segment and the model picker below write straight through — one
     deliberate click, instantly reversible, nothing worth batching. */
  type NumKey =
    | "context_length" | "compress_threshold" | "retain_ratio" | "compaction_retries"
    | "context_skill_budget" | "max_parallel_subagents" | "job_timeout" | "retry_max"
    | "retry_interval";
  type StagedKey = NumKey | "firecrawl_key";
  type Staged = Record<NumKey, number | null> & { firecrawl_key: string };

  const SPEC: Record<NumKey, { min?: number; max?: number; step: number; unit?: string }> = {
    context_length: { min: 1024, step: 1024, unit: "tok" },
    compress_threshold: { min: 0, max: 100, step: 1, unit: "%" },
    retain_ratio: { min: 0.01, max: 0.99, step: 0.01 },
    compaction_retries: { min: 0, max: 10, step: 1 },
    context_skill_budget: { min: 0, step: 500, unit: "ch" },
    max_parallel_subagents: { min: 1, step: 1 },
    job_timeout: { min: 1, step: 1, unit: "s" },
    retry_max: { min: 0, step: 1 },
    retry_interval: { min: 1, step: 1, unit: "s" },
  };
  const KEYS = Object.keys(SPEC).concat("firecrawl_key") as StagedKey[];

  function fromConfig(): Staged {
    const c = brain.config;
    return {
      context_length: c.context_length,
      compress_threshold: c.compress_threshold,
      retain_ratio: c.retain_ratio,
      compaction_retries: c.compaction_retries,
      context_skill_budget: c.context_skill_budget,
      max_parallel_subagents: c.max_parallel_subagents,
      job_timeout: c.job_timeout,
      retry_max: c.retry_max,
      retry_interval: c.retry_interval,
      firecrawl_key: c.firecrawl_key ?? "",
    };
  }

  let draft = $state<Staged>(fromConfig());
  const live = $derived(fromConfig()); // the brain's copy, always current
  let base = fromConfig(); // plain, not reactive: the last server value we adopted
  let saving = $state(false);
  let err = $state("");

  /** A refresh from elsewhere (another window, a plugin, a restart) must not
   *  silently overwrite what the operator is typing, and must not read as an
   *  edit they never made: adopt the new value only where nothing is staged. */
  $effect(() => {
    for (const k of KEYS) {
      if (base[k] === live[k]) continue;
      if (draft[k] === base[k]) draft[k] = live[k] as never;
      base[k] = live[k] as never;
    }
  });

  function changed(k: StagedKey): boolean {
    return k === "firecrawl_key"
      ? draft.firecrawl_key.trim() !== live.firecrawl_key
      : draft[k] !== live[k];
  }
  const dirty = $derived(KEYS.filter(changed));

  function clamp(k: NumKey, v: number): number {
    const s = SPEC[k];
    let x = v;
    if (s.min !== undefined) x = Math.max(s.min, x);
    if (s.max !== undefined) x = Math.min(s.max, x);
    return s.step < 1 ? Math.round(x * 100) / 100 : Math.round(x);
  }
  function nudge(k: NumKey, dir: 1 | -1): void {
    const s = SPEC[k];
    const cur = draft[k] ?? s.min ?? 0;
    draft[k] = clamp(k, cur + dir * s.step);
  }
  function fmt(k: NumKey, v: number | null): string {
    if (v === null || Number.isNaN(v)) return "never";
    return SPEC[k].unit ? `${v} ${SPEC[k].unit}` : String(v);
  }

  async function saveStaged(): Promise<void> {
    err = "";
    saving = true;
    try {
      for (const k of [...dirty]) {
        if (k === "firecrawl_key") {
          await brain.saveConfig(k, draft.firecrawl_key.trim() || null);
          continue;
        }
        const raw = draft[k];
        // job timeout is the one field where blank is a value: "never".
        const v = raw === null || Number.isNaN(raw) ? (k === "job_timeout" ? null : SPEC[k].min ?? 0) : clamp(k, raw);
        draft[k] = v;
        await brain.saveConfig(k, v);
      }
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    } finally {
      saving = false;
    }
  }
  function discard(): void {
    draft = fromConfig();
    err = "";
  }

  /* ===== immediate: approval mode, custom modes, vision model, firecrawl switch ===== */
  const customModes = $derived(brain.config.approval_modes ?? {});
  const allTools = $derived(
    [...new Set(brain.toolsets.flatMap((ts) => ts.tools.map((t) => t.name)))].sort(),
  );

  async function setApproval(mode: string): Promise<void> {
    err = "";
    try {
      await brain.saveConfig("approval_mode", mode);
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    }
  }

  // custom approval modes — chip builder over real tool names (no free typing)
  let draftName = $state("");
  let draftAuto = $state<string[]>([]);
  let draftPrompt = $state<string[]>([]);
  let toolPick = $state("");
  let editingOriginal = $state<string | null>(null); // name being edited (rename-safe)
  let modeError = $state("");
  let openDd = $state<"tool" | "vision" | null>(null);
  const draftDirty = $derived(!!draftName.trim() || draftAuto.length > 0 || draftPrompt.length > 0);

  function resetDraft(): void {
    draftName = ""; draftAuto = []; draftPrompt = []; toolPick = "";
    editingOriginal = null; modeError = "";
  }
  function assignTool(bucket: "auto" | "prompt"): void {
    const t = toolPick.trim();
    if (!t) return;
    // a tool lives in exactly one bucket
    draftAuto = draftAuto.filter((x) => x !== t);
    draftPrompt = draftPrompt.filter((x) => x !== t);
    if (bucket === "auto") draftAuto = [...draftAuto, t];
    else draftPrompt = [...draftPrompt, t];
    toolPick = "";
  }
  function dropTool(bucket: "auto" | "prompt", t: string): void {
    if (bucket === "auto") draftAuto = draftAuto.filter((x) => x !== t);
    else draftPrompt = draftPrompt.filter((x) => x !== t);
  }
  function editMode(name: string): void {
    const spec = customModes[name];
    if (!spec) return;
    draftName = name;
    draftAuto = [...spec.auto];
    draftPrompt = [...spec.prompt];
    editingOriginal = name;
    modeError = "";
  }
  async function saveDraft(): Promise<void> {
    modeError = "";
    const name = draftName.trim();
    if (!name) { modeError = "name required"; return; }
    if (name === "manual" || name === "yolo") { modeError = "reserved name"; return; }
    if (name !== editingOriginal && customModes[name]) { modeError = "name already exists"; return; }
    if (!draftAuto.length && !draftPrompt.length) { modeError = "assign at least one tool"; return; }
    const next = { ...customModes };
    if (editingOriginal && editingOriginal !== name) delete next[editingOriginal];
    next[name] = { auto: draftAuto, prompt: draftPrompt };
    try {
      await brain.saveConfig("approval_modes", next);
      resetDraft();
    } catch (e) {
      modeError = e instanceof Error ? e.message : String(e);
    }
  }
  let modeConfirm = $state<string | null>(null); // mode name armed for 2nd-click delete
  let modeConfirmTimer: ReturnType<typeof setTimeout> | undefined;
  function askRemoveMode(name: string): void {
    if (modeConfirm === name) { clearTimeout(modeConfirmTimer); modeConfirm = null; void removeCustomMode(name); return; }
    modeConfirm = name;
    clearTimeout(modeConfirmTimer);
    modeConfirmTimer = setTimeout(() => { modeConfirm = null; }, 3000);
  }
  async function removeCustomMode(name: string): Promise<void> {
    const next = { ...customModes };
    delete next[name];
    try {
      await brain.saveConfig("approval_modes", next);
      if (editingOriginal === name) resetDraft();
    } catch (e) {
      modeError = e instanceof Error ? e.message : String(e);
    }
  }

  const visionModel = $derived(brain.config.vision_model ?? "");
  function visionOptions(): string[] {
    const list = allModels();
    return visionModel && !list.includes(visionModel) ? [visionModel, ...list] : list;
  }
  async function pickVision(v: string): Promise<void> {
    openDd = null;
    err = "";
    try {
      await brain.saveConfig("vision_model", v || null);
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    }
  }

  const firecrawlEnabled = $derived(brain.config.firecrawl_enabled ?? false);
  let showKey = $state(false);
  async function toggleFirecrawl(on: boolean): Promise<void> {
    err = "";
    try {
      await brain.saveConfig("firecrawl_enabled", on);
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    }
  }

  // one open dropdown at a time; outside click or Escape closes it
  $effect(() => {
    if (!openDd) return;
    const onDoc = (e: MouseEvent) => {
      if (!(e.target as HTMLElement | null)?.closest?.(".k-dd")) openDd = null;
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") openDd = null; };
    document.addEventListener("mousedown", onDoc, true);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDoc, true);
      document.removeEventListener("keydown", onKey, true);
    };
  });
</script>

{#snippet numRow(k: NumKey, name: string, desc: string, ph = "")}
  <div class="cfg-row" class:dirty={changed(k)}>
    <div class="label">
      {name}<small>{desc}</small>
      <span class="diff">{fmt(k, live[k])} &rarr; {fmt(k, draft[k])}</span>
    </div>
    <div class="ctrl">
      <span class="k-step">
        <input
          type="number" aria-label={name} placeholder={ph}
          min={SPEC[k].min} max={SPEC[k].max} step={SPEC[k].step}
          bind:value={draft[k]}
        />
        {#if SPEC[k].unit}<span class="u">{SPEC[k].unit}</span>{/if}
        <button type="button" aria-label="{name} down" onclick={() => nudge(k, -1)}>&minus;</button>
        <button type="button" aria-label="{name} up" onclick={() => nudge(k, 1)}>+</button>
      </span>
    </div>
  </div>
{/snippet}

{#snippet slideRow(k: NumKey, name: string, desc: string, out: string)}
  <div class="cfg-row" class:dirty={changed(k)}>
    <div class="label">
      {name}<small>{desc}</small>
      <span class="diff">{fmt(k, live[k])} &rarr; {fmt(k, draft[k])}</span>
    </div>
    <div class="ctrl">
      <span class="k-slide">
        <input
          type="range" aria-label={name}
          min={SPEC[k].min} max={SPEC[k].max} step={SPEC[k].step}
          value={draft[k] ?? SPEC[k].min ?? 0}
          oninput={(e) => (draft[k] = clamp(k, e.currentTarget.valueAsNumber))}
        />
        <output>{out}</output>
      </span>
    </div>
  </div>
{/snippet}

{#snippet ruleSet(kind: "auto" | "prompt", tools: string[], drop?: (t: string) => void)}
  <div class="set">
    <span class="rk {kind === 'auto' ? 'a' : 'p'}">{kind === "auto" ? "never ask" : "always ask"}</span>
    {#if tools.length}
      <span class="k-chips">
        {#each tools as t (t)}
          <span class="k-chip {kind}">{t}
            {#if drop}<button type="button" aria-label="remove {t}" onclick={() => drop(t)}>&times;</button>{/if}
          </span>
        {/each}
      </span>
    {:else}
      <span class="k-empty">&mdash;</span>
    {/if}
  </div>
{/snippet}

<!-- ========== AGENT ========== -->
<div class="cfg-pane" class:active={activeModule === "agent"}>
  {#if dirty.length || err}
    <div class="cfg-bar" class:on={dirty.length > 0} class:err={!!err}>
      <span class="st">
        {err || `${dirty.length} unsaved change${dirty.length === 1 ? "" : "s"}`}
      </span>
      <span class="sp"></span>
      <button type="button" class="k-btn ghost" onclick={discard} disabled={saving}>discard</button>
      <button type="button" class="k-btn pri" onclick={() => void saveStaged()} disabled={saving || dirty.length === 0}>
        {saving ? "saving…" : "save"}
      </button>
    </div>
  {/if}
  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">Context &amp; compaction</span>
      <span class="d">how much history a turn may carry, and what survives a compaction</span>
    </div>
    {@render numRow("context_length", "context length", "hard cap for prompt build")}
    {@render slideRow(
      "compress_threshold",
      "compression threshold",
      "compress once the prompt reaches this share of the context",
      `${draft.compress_threshold ?? 0}%`,
    )}
    {@render slideRow(
      "retain_ratio",
      "retain ratio",
      "fraction of the context kept verbatim on compaction",
      `${Math.round((draft.retain_ratio ?? 0) * 100)}% kept`,
    )}
    {@render numRow("compaction_retries", "compaction retries", "extra attempts when a summary doesn't shrink")}
    {@render numRow("context_skill_budget", "skill context budget", "max chars of skill/memory body in the prompt")}
  </div>

  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">Approval</span>
      <span class="d">what runs on its own, what waits for you &mdash; applies immediately</span>
    </div>
    <div class="cfg-row">
      <div class="label">
        active mode
        <small>manual = ask every time · yolo = approve everything · custom = per-tool rules</small>
      </div>
      <div class="ctrl">
        <span class="k-seg">
          <button type="button" aria-pressed={brain.config.approval_mode === "manual"} onclick={() => void setApproval("manual")}>manual</button>
          <button type="button" aria-pressed={brain.config.approval_mode === "yolo"} onclick={() => void setApproval("yolo")}>yolo</button>
          {#if brain.config.approval_mode !== "manual" && brain.config.approval_mode !== "yolo"}
            <button type="button" aria-pressed={true}>{brain.config.approval_mode}</button>
          {/if}
        </span>
      </div>
    </div>

    <div class="cfg-row block">
      <div class="label">
        custom modes
        <small>
          per-tool rules · <b class="cfg-a">never ask</b> runs silently ·
          <b class="cfg-p">always ask</b> pauses for you
        </small>
      </div>
      <div class="ctrl">
        <div class="cfg-modes">
          {#each Object.entries(customModes) as [name, spec] (name)}
            <div class="cfg-mode" class:on={brain.config.approval_mode === name} class:draft={editingOriginal === name}>
              <div class="hd">
                <span class="nm">{name}</span>
                {#if brain.config.approval_mode === name}<span class="badge">active</span>{/if}
                <span class="sp"></span>
                {#if brain.config.approval_mode !== name}
                  <button type="button" class="k-btn sm" onclick={() => void setApproval(name)}>use</button>
                {/if}
                <button type="button" class="k-btn sm" onclick={() => editMode(name)}>edit</button>
                <button
                  type="button" class="k-btn dgr sm" class:armed={modeConfirm === name}
                  title={modeConfirm === name ? "Click again to confirm" : "Delete"}
                  onclick={() => askRemoveMode(name)}
                >{modeConfirm === name ? "confirm?" : "delete"}</button>
              </div>
              {@render ruleSet("auto", spec.auto)}
              {@render ruleSet("prompt", spec.prompt)}
            </div>
          {/each}

          <!-- the editor is a card too, so a new mode looks like what it becomes -->
          <div class="cfg-mode draft">
            <div class="hd">
              <span class="nm">{editingOriginal ? `editing “${editingOriginal}”` : "new mode"}</span>
              <span class="sp"></span>
            </div>
            <div class="col">
              <input type="text" aria-label="mode name" placeholder="mode name (e.g. semi, readonly)" bind:value={draftName} />
              <div class="set">
                <div class="k-dd">
                  <button
                    type="button" class="k-btn" aria-haspopup="listbox" aria-expanded={openDd === "tool"}
                    onclick={() => (openDd = openDd === "tool" ? null : "tool")}
                  >
                    <span class="v" class:ph={!toolPick}>{toolPick || "choose a tool…"}</span>
                    <Icon name={openDd === "tool" ? "chevron-up" : "chevron-down"} size={12} />
                  </button>
                  {#if openDd === "tool"}
                    <div class="k-dd-pop" role="listbox" aria-label="tool">
                      {#if allTools.length === 0}
                        <div class="grp">no tools loaded</div>
                      {/if}
                      {#each allTools as t (t)}
                        <button
                          type="button" role="option" aria-selected={toolPick === t} class:sel={toolPick === t}
                          onclick={() => { toolPick = t; openDd = null; }}
                        >{t}</button>
                      {/each}
                    </div>
                  {/if}
                </div>
                <button type="button" class="k-btn ok" disabled={!toolPick} onclick={() => assignTool("auto")}>&rarr; never ask</button>
                <button type="button" class="k-btn warn" disabled={!toolPick} onclick={() => assignTool("prompt")}>&rarr; always ask</button>
              </div>
              {@render ruleSet("auto", draftAuto, (t) => dropTool("auto", t))}
              {@render ruleSet("prompt", draftPrompt, (t) => dropTool("prompt", t))}
              {#if modeError}<div class="cfg-err">{modeError}</div>{/if}
              <div class="set">
                <button type="button" class="k-btn pri" onclick={() => void saveDraft()}>
                  {editingOriginal ? "save changes" : "create mode"}
                </button>
                {#if draftDirty || editingOriginal}
                  <button type="button" class="k-btn ghost" onclick={resetDraft}>cancel</button>
                {/if}
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">Subagents</span>
      <span class="d">delegated work: how many at once, and how long each may run</span>
    </div>
    {@render numRow("max_parallel_subagents", "max parallel subagents", "simultaneous synchronous delegates")}
    {@render numRow("job_timeout", "job timeout", "seconds per subagent; blank = never", "never")}
  </div>

  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">Reliability</span>
      <span class="d">what happens when a provider call fails</span>
    </div>
    {@render numRow("retry_max", "retry max", "provider-retry attempts per turn")}
    {@render numRow("retry_interval", "retry interval", "base backoff · grows +2 s each try")}
  </div>

  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">Integrations</span>
      <span class="d">outside services this agent may reach for</span>
    </div>
    <div class="cfg-row">
      <div class="label">
        vision model
        <small>
          used for turns with an attached image, and by inspect_image (blank = session model){allModels().length === 0
            ? " · no models found — fetch in Providers"
            : ""}
        </small>
      </div>
      <div class="ctrl">
        <div class="k-dd">
          <button
            type="button" class="k-btn" aria-haspopup="listbox" aria-label="vision model"
            aria-expanded={openDd === "vision"}
            onclick={() => (openDd = openDd === "vision" ? null : "vision")}
          >
            <span class="v" class:ph={!visionModel}>{visionModel || "normal model (no override)"}</span>
            <Icon name={openDd === "vision" ? "chevron-up" : "chevron-down"} size={12} />
          </button>
          {#if openDd === "vision"}
            <div class="k-dd-pop" role="listbox" aria-label="vision model">
              <button type="button" role="option" aria-selected={visionModel === ""} class:sel={visionModel === ""} onclick={() => void pickVision("")}>
                normal model (no override)
              </button>
              {#each visionOptions() as m (m)}
                <button type="button" role="option" aria-selected={visionModel === m} class:sel={visionModel === m} onclick={() => void pickVision(m)}>{m}</button>
              {/each}
            </div>
          {/if}
        </div>
      </div>
    </div>
    <div class="cfg-row">
      <div class="label">firecrawl<small>web search/extract via firecrawl.dev · off = ddg/reader fallback</small></div>
      <div class="ctrl">
        <span class="state-badge">{firecrawlEnabled ? "ON" : "OFF"}</span>
        <label class="toggle">
          <input
            type="checkbox" aria-label="firecrawl" checked={firecrawlEnabled}
            onchange={(e) => void toggleFirecrawl(e.currentTarget.checked)}
          />
          <span class="track"></span>
          <span class="thumb"></span>
        </label>
      </div>
    </div>
    <div class="cfg-row" class:dirty={changed("firecrawl_key")}>
      <div class="label">
        firecrawl key
        <small>comma-separated keys are used as a pool</small>
        <span class="diff">unsaved change (hidden)</span>
      </div>
      <div class="ctrl">
        <input
          type={showKey ? "text" : "password"} aria-label="firecrawl key"
          placeholder="fclive_…,fclive_…" bind:value={draft.firecrawl_key}
          disabled={!firecrawlEnabled}
        />
        <button type="button" class="k-btn sm" onclick={() => (showKey = !showKey)} disabled={!firecrawlEnabled}>
          {showKey ? "hide" : "show"}
        </button>
      </div>
    </div>
  </div>

</div>
