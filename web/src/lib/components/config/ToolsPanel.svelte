<script lang="ts">
  import { brain } from "../../store.svelte";
  import Icon from "../Icon.svelte";
  let { activeModule = "toolsets" }: { activeModule?: string } = $props();
  let openToolsets = $state<Record<string, boolean>>({});
  function toggleToolset(name: string): void { openToolsets = { ...openToolsets, [name]: !openToolsets[name] }; }
  const builtinToolsets = $derived(brain.toolsets.map((ts) => ({ ...ts, tools: ts.tools.filter((t) => !t.is_dropin) })).filter((ts) => ts.tools.length > 0));
  const totalTools = $derived(builtinToolsets.reduce((n, ts) => n + ts.tools.length, 0));
  const enabledToolsets = $derived(builtinToolsets.filter((ts) => ts.enabled).length);
  const enabledDropins = $derived(brain.dropins.filter((d) => d.enabled).length);
</script>

  <!-- ========== TOOLS ========== -->
  <div class="cfg-pane" class:active={activeModule === "toolsets"}>
    <div class="cfg-sec">
      <div class="cfg-sec-hd">
        <span class="t">TOOLSETS</span>
        <span class="d">built-in tools, switched a whole set at a time — {enabledToolsets}/{builtinToolsets.length} enabled · {totalTools} tools</span>
      </div>
      {#each builtinToolsets as ts (ts.toolset)}
        <div class="cfg-row">
          <button type="button" class="label tool-exp" aria-expanded={!!openToolsets[ts.toolset]} onclick={() => toggleToolset(ts.toolset)}>
            <span class="exp-caret"><Icon name={openToolsets[ts.toolset] ? "chevron-down" : "chevron-right"} size={12} /></span>
            {ts.toolset}<small>{ts.tools.length} tools</small>
          </button>
          <div class="ctrl">
            <label class="toggle">
              <input type="checkbox" aria-label="enable toolset {ts.toolset}" checked={ts.enabled} onchange={() => void brain.setToolEnabled(ts.toolset, !ts.enabled)} />
              <span class="track"></span>
              <span class="thumb"></span>
            </label>
          </div>
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
        <div class="k-empty">No toolsets loaded.</div>
      {/if}
    </div>

    <div class="cfg-sec">
      <div class="cfg-sec-hd">
        <span class="t">CUSTOM TOOLS</span>
        <span class="d">drop-ins you authored — {enabledDropins}/{brain.dropins.length} enabled</span>
      </div>
      {#each brain.dropins as d (d.name)}
        <div class="cfg-row">
          <button type="button" class="label tool-exp" aria-expanded={!!openToolsets["__d:" + d.name]} onclick={() => toggleToolset("__d:" + d.name)}>
            <span class="exp-caret"><Icon name={openToolsets["__d:" + d.name] ? "chevron-down" : "chevron-right"} size={12} /></span>
            {d.name}<small>1 tool</small>
          </button>
          <div class="ctrl">
            <label class="toggle">
              <input type="checkbox" aria-label="enable custom tool {d.name}" checked={d.enabled} onchange={() => void brain.setDropinEnabled(d.name, !d.enabled)} />
              <span class="track"></span>
              <span class="thumb"></span>
            </label>
          </div>
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
        <div class="k-empty">No custom tools — author one with tool_create.</div>
      {/if}
    </div>
  </div>
