<script lang="ts">
  import { brain } from "../store.svelte";
  import Icon from "./Icon.svelte";
  import type { PresetInfo } from "../types";
  type AstateTab = "rules" | "skills" | "tools";

  let tab = $state<AstateTab>("rules");
  let compressing = $state(false);
  let compressMsg = $state("");
  // Kind is tracked separately so the badge can carry the right glyph *and* the
  // right colour — it used to render every outcome, failures included, as `ok`.
  let compressOk = $state(false);

  async function runCompress(): Promise<void> {
    compressing = true;
    compressMsg = "";
    compressOk = false;
    brain.compactionResult = null;
    try {
      const ack = await brain.compressSession();
      if (!ack.ok) {
        compressMsg = ack.error ?? "failed";
        compressing = false;
        return;
      }
      // The real result arrives async via the `compaction.done` event; poll the
      // store's compactionResult until it lands (the UI stays responsive — this
      // is a web-side await, not a blocked brain websocket).
      const deadline = Date.now() + 60_000;
      while (!brain.compactionResult && Date.now() < deadline) {
        await new Promise((r) => setTimeout(r, 200));
      }
      const r = brain.compactionResult as { ok: boolean; error?: string; before?: number; after?: number; dropped?: number } | null;
      if (r) {
        compressMsg = r.ok ? `${r.before} → ${r.after} chars` : (r.error ?? "failed");
        compressOk = r.ok;
        brain.compactionResult = null;
      } else {
        compressMsg = "timed out";
      }
    } catch (e) {
      compressMsg = e instanceof Error ? e.message : String(e);
    } finally {
      compressing = false;
      setTimeout(() => (compressMsg = ""), 4000);
    }
  }
  let modelOpen = $state(false);
  let personaOpen = $state(false);
  let mpickEl = $state<HTMLDivElement | null>(null);
  let ppickEl = $state<HTMLDivElement | null>(null);

  let providerModels = $derived(
    brain.providers
      .filter((p) => p.enabled)
      .flatMap((p) => p.models.map((m) => ({ model: m, provider: p.id }))),
  );

  function pickModel(entry: { model: string; provider: string } | null): void {
    modelOpen = false;
    if (!entry) return void brain.setModel("");
    void brain.setModel(entry.model, entry.provider);
  }

  function pickPersona(p: string | null): void {
    personaOpen = false;
    void brain.setPersona(p);
  }

  let presetOpen = $state(false);
  let presetsList = $state<PresetInfo[]>([]);
  $effect(() => {
    if (presetOpen || tab === "rules") void brain.refreshPresets().then((ps) => (presetsList = ps));
  });
  const activePreset = $derived(brain.state.preset as Record<string, unknown> | null);
  const presetActive = $derived(!!activePreset);
  function pickPreset(id: string | null): void {
    presetOpen = false;
    void brain.setSessionPreset(id);
  }
  // tools tab: which toolsets are expanded to show per-tool descriptions
  let openToolsets = $state<Record<string, boolean>>({});
  function toggleToolset(name: string): void {
    openToolsets = { ...openToolsets, [name]: !openToolsets[name] };
  }
  // skills tab: same collapse pattern — show/hide the description
  let openSkills = $state<Record<string, boolean>>({});
  function toggleSkill(id: string): void {
    openSkills = { ...openSkills, [id]: !openSkills[id] };
  }
  // tools tab: collapsible panels (built-in toolsets vs custom tools)
  let openPanels = $state<Record<string, boolean>>({ builtin: true, custom: true });
  function togglePanel(key: string): void {
    openPanels = { ...openPanels, [key]: !openPanels[key] };
  }

  const blocks = 10;
  const filled = $derived(Math.round((brain.state.context / 100) * blocks));
  const amber = $derived(brain.state.context >= 60);

  // skills/tools tab: client-side search
  let skillQ = $state("");
  let toolQ = $state("");
  function matches(q: string, ...fields: (string | undefined)[]): boolean {
    const needle = q.trim().toLowerCase();
    if (!needle) return true;
    return fields.some((f) => f?.toLowerCase().includes(needle));
  }
  const filteredSkills = $derived(
    brain.skills.filter((s) => matches(skillQ, s.name, s.id, s.desc, (s.keywords ?? []).join(" "))),
  );
  const builtinToolsets = $derived(
    brain.toolsets
      .map((ts) => ({ ...ts, tools: ts.tools.filter((t) => !t.is_dropin) }))
      .filter((ts) => ts.tools.length > 0),
  );
  const filteredToolsets = $derived(
    builtinToolsets
      .map((ts) => ({ ...ts, tools: ts.tools.filter((t) => matches(toolQ, ts.toolset, t.name, t.description)) }))
      .filter((ts) => ts.tools.length > 0),
  );
  const filteredDropins = $derived(
    brain.dropins.filter((d) => matches(toolQ, d.name, d.toolset, d.description)),
  );

  // While searching, expand every matching row so results are visible.
  $effect(() => {
    if (skillQ.trim()) {
      const next: Record<string, boolean> = {};
      for (const s of filteredSkills) next[s.id] = true;
      openSkills = next;
    }
    if (toolQ.trim()) {
      const next: Record<string, boolean> = {};
      for (const ts of filteredToolsets) next[ts.toolset] = true;
      for (const d of filteredDropins) next["__d:" + d.name] = true;
      openToolsets = next;
    }
  });

  let cwdOpen = $state(false);
  let cwdPath = $state(brain.state.cwd || "/");
  let cwdDirs = $state<string[]>([]);
  let cwdLoading = $state(false);
  let cwdJump = $state("");
  function jumpToPath(): void {
    const t = cwdJump.trim();
    if (t) {
      cwdPath = t;
      void refreshDirs();
    }
  }

  async function refreshDirs(): Promise<void> {
    cwdLoading = true;
    try {
      const r = await brain.listDirs(cwdPath);
      cwdPath = r.path;
      cwdDirs = r.dirs;
    } finally {
      cwdLoading = false;
    }
  }

  function openCwdPicker(): void {
    cwdPath = brain.state.cwd || "/";
    cwdOpen = true;
    void refreshDirs();
  }
  function enterDir(name: string): void {
    cwdPath = cwdPath === "/" ? `/${name}` : `${cwdPath}/${name}`;
    void refreshDirs();
  }
  function upDir(): void {
    const parts = cwdPath.split("/").filter(Boolean);
    cwdPath = parts.length ? `/${parts.slice(0, -1).join("/")}` : "/";
    void refreshDirs();
  }
  function pickCwd(): void {
    cwdOpen = false;
    void brain.setCwd(cwdPath);
  }
  function closeCwd(): void {
    cwdOpen = false;
  }
  function goRoot(): void {
    cwdPath = "/";
    void refreshDirs();
  }
  function goHome(): void {
    cwdPath = "~";
    void refreshDirs();
  }

  let rulesText = $state(brain.state.rules.join("\n"));

  // Load the active session's rules whenever the active session changes,
  // so the box reflects that session (not a stale one) on tab/switch.
  $effect(() => {
    const sid = brain.session?.id;
    rulesText = (sid ? brain.state.rules : []).join("\n");
  });

  let rulesSaved = $state(false);

  // Persist; keep the textarea in sync with what the server normalized.
  async function saveRules(): Promise<void> {
    const rules = rulesText.split("\n").map((rule) => rule.trim()).filter(Boolean);
    await brain.setRules(rules);
    rulesText = rules.join("\n");
    rulesSaved = true;
    setTimeout(() => (rulesSaved = false), 2000);
  }
  // ---- rule presets: named, reusable rule sets saved by the user ----
  type RulePreset = { id: string; name: string; rules: string[] };
  const PRESETS_KEY = "xu.rule-presets";
  let rulePresets = $state<RulePreset[]>(loadPresets());
  let presetSel = $state<string>("");
  let presetName = $state("");
  let presetsOpen = $state(false);
  let namingPreset = $state(false);

  function loadPresets(): RulePreset[] {
    try {
      const raw = localStorage.getItem(PRESETS_KEY);
      if (raw) {
        const parsed: unknown = JSON.parse(raw);
        if (Array.isArray(parsed)) {
          return parsed.filter(
            (p): p is RulePreset =>
              !!p && typeof p === "object" &&
              typeof (p as RulePreset).id === "string" &&
              typeof (p as RulePreset).name === "string" &&
              Array.isArray((p as RulePreset).rules),
          );
        }
      }
    } catch { /* corrupt browser state is ignored */ }
    return [];
  }

  function persistPresets(): void {
    try { localStorage.setItem(PRESETS_KEY, JSON.stringify(rulePresets)); }
    catch { /* storage can be unavailable */ }
  }

  function applyRulePreset(id: string): void {
    const p = rulePresets.find((x) => x.id === id);
    if (p) {
      rulesText = p.rules.join("\n");
      rulesSaved = false;
    }
  }

  function saveRulePreset(): void {
    const rules = rulesText.split("\n").map((r) => r.trim()).filter(Boolean);
    const name = presetName.trim();
    if (!name || rules.length === 0) return;
    const existing = rulePresets.find((p) => p.name === name);
    if (existing) {
      existing.rules = rules; // overwrite/update
    } else {
      rulePresets = [...rulePresets, { id: crypto.randomUUID(), name, rules }];
    }
    persistPresets();
    presetSel = existing ? existing.id : rulePresets[rulePresets.length - 1].id;
    presetName = "";
    namingPreset = false;
  }

  function deleteRulePreset(id: string): void {
    rulePresets = rulePresets.filter((p) => p.id !== id);
    if (presetSel === id) presetSel = "";
    persistPresets();
  }

  // Fetch live data for whichever tab is active.
  $effect(() => {
    if (tab === "skills") void brain.refreshSkills();
    else if (tab === "tools") void brain.refreshToolsets();
  });

  // close the model/persona menus on any outside click
  $effect(() => {
    if (!modelOpen && !personaOpen) return;
    const onDoc = (e: MouseEvent) => {
      if (mpickEl && !mpickEl.contains(e.target as Node)) modelOpen = false;
      if (ppickEl && !ppickEl.contains(e.target as Node)) personaOpen = false;
    };
    document.addEventListener("mousedown", onDoc, true);
    return () => document.removeEventListener("mousedown", onDoc, true);
  });
</script>

<div id="astate">
  <div id="astate-head">
    <span class="dot" ></span>
    AGENT STATE
  </div>

  <div id="astate-status">
    <div class="cu-row">
      <span class="l">CONTEXT</span>
      <div class="cu-bar" title="context fill">{#each Array.from({ length: blocks }) as _, i (i)}
        {@const on = i < filled}
        {@const cls = on ? (amber ? "cu amber" : "cu on") : i === filled && amber ? "cu pend" : "cu"}
        <span class={cls} ></span>
      {/each}</div>
      <span class="pct" class:amber>{brain.state.context}%</span>
      <button
        class="btn sm compress-btn"
        disabled={compressing}
        onclick={() => void runCompress()}
        title="compress context now"
      >
        {compressing ? "…" : "Compress"}
      </button>
      {#if brain.state.compress}<span class="badge amber compress">COMPRESSING</span>{/if}
      {#if compressMsg}<span class="badge" class:ok={compressOk} class:danger={!compressOk}><Icon name={compressOk ? "check" : "x"} size={11} /> {compressMsg}</span>{/if}
    </div>
    <div class="cwd-row">
      <span class="l">CWD</span>
      <span id="cwd-path" title={brain.state.cwd}>{brain.state.cwd || "~"}</span>
      <button class="cd-btn" id="cwd-edit" title="choose working directory" onclick={openCwdPicker}>
        CD <Icon name="chevron-right" size={12} />
      </button>
    </div>
    <div class="model-row">
      <span class="l">MODEL</span>
      <div class="mpick" bind:this={mpickEl}>
        <button
          type="button"
          class="mpick-btn"
          aria-haspopup="listbox"
          aria-expanded={modelOpen}
          onclick={() => (modelOpen = !modelOpen)}
          >
          <span class="mpick-val" class:placeholder={!brain.state.model}>{brain.state.model ?? "— none —"}</span>
          <span class="mpick-caret"><Icon name={modelOpen ? "chevron-up" : "chevron-down"} size={12} /></span>
          </button>
          {#if modelOpen}
          <div class="mpick-menu" role="listbox" aria-label="model">
            <button
              type="button"
              role="option"
              aria-selected={!brain.state.model}
              class="mpick-opt"
              class:sel={!brain.state.model}
              onclick={() => pickModel(null)}
            >— none —</button>
            {#each providerModels as e (e.provider + "/" + e.model)}
              <button
                type="button"
                role="option"
                aria-selected={brain.state.model === e.model}
                class="mpick-opt"
                class:sel={brain.state.model === e.model}
                onclick={() => pickModel(e)}
              >{e.model}<span class="mpick-prov">{brain.providers.find((pp) => pp.id === e.provider)?.name ?? e.provider}</span></button>
            {/each}
          </div>
        {/if}
      </div>
    </div>
    <div class="model-row">
      <span class="l">PERSONA</span>
      <div class="mpick" bind:this={ppickEl}>
        <button
          type="button"
          class="mpick-btn"
          aria-haspopup="listbox"
          aria-expanded={personaOpen}
          disabled={presetActive}
          onclick={() => (personaOpen = !personaOpen)}
          >
          <span class="mpick-val" class:placeholder={!brain.state.persona}>
          {brain.personas.find((p) => p.id === brain.state.persona)?.name ?? brain.state.persona ?? "— default —"}
          </span>
          <span class="mpick-caret"><Icon name={personaOpen ? "chevron-up" : "chevron-down"} size={12} /></span>
          </button>
          {#if personaOpen && !presetActive}
          <div class="mpick-menu" role="listbox" aria-label="persona">
            <button
              type="button"
              role="option"
              aria-selected={!brain.state.persona}
              class="mpick-opt"
              class:sel={!brain.state.persona}
              onclick={() => pickPersona(null)}
            >— default —</button>
            {#each brain.personas as p (p.id)}
              <button
                type="button"
                role="option"
                aria-selected={brain.state.persona === p.id}
                class="mpick-opt"
                class:sel={brain.state.persona === p.id}
                onclick={() => pickPersona(p.id)}
              >{p.name}</button>
            {/each}
          </div>
        {/if}
      </div>
    </div>
    <div class="model-row">
      <span class="l">PRESET</span>
      <div class="mpick" bind:this={ppickEl}>
        <button
          type="button"
          class="mpick-btn"
          aria-haspopup="listbox"
          aria-expanded={presetOpen}
          onclick={() => (presetOpen = !presetOpen)}
        >
          <span class="mpick-val" class:placeholder={!activePreset}>
            {(activePreset && (activePreset as { name?: string }).name) ?? "— none —"}
          </span>
          <span class="mpick-caret"><Icon name={presetOpen ? "chevron-up" : "chevron-down"} size={12} /></span>
        </button>
        {#if presetOpen}
          <div class="mpick-menu" role="listbox" aria-label="orchestration preset">
            <button
              type="button"
              role="option"
              aria-selected={!activePreset}
              class="mpick-opt"
              class:sel={!activePreset}
              onclick={() => pickPreset(null)}
            >— none (no orchestration) —</button>
            {#each presetsList as p (p.id)}
              <button
                type="button"
                role="option"
                aria-selected={activePreset?.id === p.id}
                class="mpick-opt"
                class:sel={activePreset?.id === p.id}
                onclick={() => pickPreset(p.id)}
              >{p.name}<small> · {p.node_count} agents</small></button>
            {/each}
          </div>
        {/if}
      </div>
    </div>
  </div>

  <div id="astate-tabs">
    <button class="stab" class:active={tab === "rules"} onclick={() => (tab = "rules")}>RULES</button>
    <button class="stab" class:active={tab === "skills"} onclick={() => (tab = "skills")}>SKILLS</button>
    <button class="stab" class:active={tab === "tools"} onclick={() => (tab = "tools")}>TOOLS</button>
  </div>

  <div id="astate-body">
    <div id="st-rules" class="stpane" class:active={tab === "rules"}>
      <textarea class="rules-input" aria-label="Session rules" placeholder="One rule per line, e.g.&#10;Don't commit/push&#10;Use tests"
        bind:value={rulesText} disabled={presetActive}></textarea>
      <div class="rules-save">
        <button class="btn sm primary" onclick={() => void saveRules()} disabled={presetActive}>SAVE</button>
        {#if presetActive}<span class="badge">preset-managed</span>{/if}
        {#if rulesSaved}<span class="badge ok">saved</span>{/if}
      </div>
      <div class="rules-presets">
        <button type="button" class="rules-presets-toggle" aria-expanded={presetsOpen}
          onclick={() => (presetsOpen = !presetsOpen)}>
          <span>PRESETS <em>{rulePresets.length}</em></span>
          <span class="rules-presets-caret">{presetsOpen ? "\u25b4" : "\u25be"}</span>
        </button>
        {#if presetsOpen}
          {#if rulePresets.length === 0}
            <div class="rules-presets-empty">No presets yet &mdash; press SAVE AS PRESET below.</div>
          {:else}
            <div class="rules-presets-list">
              {#each rulePresets as p (p.id)}
                <div class="rules-preset-card-wrap">
                  <button type="button" class="rules-preset-card" class:active={presetSel === p.id}
                    title="Load these rules into the editor"
                    onclick={() => { presetSel = p.id; applyRulePreset(p.id); }}>
                    <span class="rules-preset-meta">
                      <strong>{p.name}</strong>
                      <em>{p.rules.length} rule{p.rules.length === 1 ? "" : "s"}</em>
                    </span>
                    <span class="rules-preset-snippet">{[p.rules[0], p.rules[1]].filter(Boolean).join(" \u00b7 ") || "\u2014"}</span>
                  </button>
                  <button type="button" class="rules-preset-del" title="Delete preset" aria-label="Delete preset {p.name}"
                    onclick={(e) => deleteRulePreset(p.id)}>&times;</button>
                </div>
              {/each}
            </div>
          {/if}
        {/if}
        {#if !namingPreset}
          <button type="button" class="btn sm" onclick={() => (namingPreset = true)}>SAVE AS PRESET</button>
        {:else}
          <div class="rules-presets-add">
            <input class="rules-presets-name" bind:value={presetName} placeholder="preset name&hellip;"
              aria-label="preset name" />
            <button class="btn sm primary" onclick={() => saveRulePreset()} disabled={!presetName.trim()}>SAVE</button>
            <button class="btn sm" onclick={() => { namingPreset = false; presetName = ""; }}>CANCEL</button>
          </div>
        {/if}
      </div>
      <div class="stfoot">SESSION RULES · injected below each user message</div>
    </div>
    <div id="st-skills" class="stpane" class:active={tab === "skills"}>
      <input class="mini-input" type="search" placeholder="search skills…" bind:value={skillQ} aria-label="search skills" />
      {#if brain.skills.length === 0}
        <div class="strow"><div class="t">No skills installed</div></div>
      {:else if filteredSkills.length === 0}
        <div class="strow"><div class="t">No skills match "{skillQ}"</div></div>
      {:else}
        {#each filteredSkills as s (s.id)}
          <div class="strow skill-row">
            <button class="t tool-exp" onclick={() => toggleSkill(s.id)} aria-expanded={!!openSkills[s.id]}>
              <span class="exp-caret"><Icon name={openSkills[s.id] ? "chevron-down" : "chevron-right"} size={12} /></span>
              {s.name}
            </button>
            {#if openSkills[s.id] && s.desc}
              <div class="t" style="font-size:14px;color:var(--faint);margin:4px 0 0 16px">{s.desc}</div>
            {/if}
            <div class="meta">
              <span class="chip-tag {s.ambient ? 'on' : 'off'}">{s.ambient ? "ON" : "OFF"}</span>
              <label class="toggle" title={s.ambient ? 'disable (stop always-loading)' : 'enable (always load)'}>
                <input
                  type="checkbox"
                  checked={s.ambient}
                  disabled={presetActive}
                  onchange={() => void brain.setSkill(s.id, !s.ambient)}
                />
                <span class="track"></span>
                <span class="thumb"></span>
              </label>
            </div>
          </div>
        {/each}
      {/if}
      <div class="stfoot">{filteredSkills.length}/{brain.skills.length} known</div>
    </div>
    <div id="st-tools" class="stpane" class:active={tab === "tools"}>
      <input class="mini-input" type="search" placeholder="search tools…" bind:value={toolQ} aria-label="search tools" />
      <button class="panel-hdr" type="button" onclick={() => togglePanel("builtin")}>
          <span class="exp-caret"><Icon name={openPanels.builtin ? "chevron-down" : "chevron-right"} size={12} /></span>
        Toolsets
        <small>{filteredToolsets.length} sets · {filteredToolsets.reduce((n, ts) => n + ts.tools.length, 0)} tools</small>
      </button>
      {#if openPanels.builtin}
        <div class="panel-body">
          {#each filteredToolsets as ts (ts.toolset)}
            <div class="toolrow">
              <button class="n tool-exp" onclick={() => toggleToolset(ts.toolset)}>
                <span class="exp-caret"><Icon name={openToolsets[ts.toolset] ? "chevron-down" : "chevron-right"} size={12} /></span>
                {ts.toolset}<small>{ts.tools.length}</small>
              </button>
              <label class="toggle">
                <input
                  type="checkbox"
                  checked={ts.enabled}
                  disabled={presetActive}
                  onchange={() => void brain.setToolEnabled(ts.toolset, !ts.enabled)}
                />
                <span class="track" ></span>
                <span class="thumb" ></span>
              </label>
            </div>
            {#if openToolsets[ts.toolset]}
              <div class="tooldetail">
                {#each ts.tools as t (t.name)}
                  <div class="td-row">
                    <span class="td-name">{t.name}</span>
                    {#if t.description}<span class="td-desc">{t.description}</span>{/if}
                  </div>
                {/each}
              </div>
            {/if}
          {/each}
          {#if builtinToolsets.length === 0}
            <div class="strow"><div class="t">No toolsets.</div></div>
          {:else if filteredToolsets.length === 0}
            <div class="strow"><div class="t">No tools match "{toolQ}"</div></div>
          {/if}
        </div>
      {/if}

      <button class="panel-hdr" type="button" onclick={() => togglePanel("custom")}>
        <span class="exp-caret"><Icon name={openPanels.custom ? "chevron-down" : "chevron-right"} size={12} /></span>
        Custom Tools
        <small>{filteredDropins.length} {filteredDropins.length === 1 ? "tool" : "tools"} · drop-in</small>
      </button>
      {#if openPanels.custom}
        <div class="panel-body">
          {#each filteredDropins as d (d.name)}
            <div class="toolrow">
              <button class="n tool-exp" onclick={() => toggleToolset("__d:" + d.name)}>
                <span class="exp-caret"><Icon name={openToolsets["__d:" + d.name] ? "chevron-down" : "chevron-right"} size={12} /></span>
                {d.name}<small>1</small>
              </button>
              <label class="toggle">
                <input
                  type="checkbox"
                  checked={d.enabled}
                  disabled={presetActive}
                  onchange={() => void brain.setDropinEnabled(d.name, !d.enabled)}
                />
                <span class="track" ></span>
                <span class="thumb" ></span>
              </label>
            </div>
            {#if openToolsets["__d:" + d.name]}
              <div class="tooldetail">
                <div class="td-row">
                  <span class="td-name">{d.name}</span>
                  {#if d.approval}<span class="badge">{d.approval}</span>{/if}
                  {#if d.toolset}<span class="td-desc">toolset {d.toolset}</span>{/if}
                  {#if d.description}<span class="td-desc">{d.description}</span>{/if}
                </div>
              </div>
            {/if}
          {/each}
          {#if brain.dropins.length === 0}
            <div class="strow"><div class="t">No custom tools — author one with tool_create.</div></div>
          {:else if filteredDropins.length === 0}
            <div class="strow"><div class="t">No custom tools match "{toolQ}"</div></div>
          {/if}
        </div>
      {/if}
    </div>
  </div>
</div>

  {#if cwdOpen}
    <div class="dir-modal" role="dialog" aria-modal="true" aria-label="choose directory" tabindex="-1"
      onclick={(e) => e.target === e.currentTarget && closeCwd()}
      onkeydown={(e) => e.key === "Escape" && closeCwd()}>
      <div class="dir-modal-panel">
        <div class="dir-head">
          <span>CHOOSE DIRECTORY</span>
          <button class="dir-close" onclick={closeCwd} aria-label="close"><Icon name="x" size={15} /></button>
        </div>
        <div class="dir-path" title={cwdPath}>{cwdPath}</div>
        <div class="dir-jump">
          <input
            type="text"
            bind:value={cwdJump}
            placeholder="or type a path…"
            onkeydown={(e) => {
              if (e.key === "Enter") jumpToPath();
              if (e.key === "Escape") { cwdJump = cwdPath; }
            }}
          />
          <button class="cd-btn" onclick={jumpToPath}>GO</button>
        </div>
        <div class="dir-list">
          <button class="dir-item up" onclick={goRoot}>/  root</button>
          <button class="dir-item up" onclick={goHome}><Icon name="house" size={13} /> home</button>
          <button class="dir-item up" onclick={upDir}><Icon name="arrow-up" size={13} /> ..</button>
          {#if cwdLoading}
            <div class="dir-empty">loading…</div>
          {:else if cwdDirs.length === 0}
            <div class="dir-empty">no subdirectories</div>
          {:else}
            {#each cwdDirs as d (d)}
              <button class="dir-item" onclick={() => enterDir(d)}>{d}<span class="dir-slash">/</span></button>
            {/each}
          {/if}
        </div>
        <div class="dir-actions">
          <button class="btn sm primary" disabled={cwdLoading} onclick={pickCwd}>SELECT</button>
          <button class="btn sm" onclick={closeCwd}>CANCEL</button>
        </div>
      </div>
    </div>
  {/if}
