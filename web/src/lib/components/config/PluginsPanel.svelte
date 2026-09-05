<script lang="ts">
  import { brain } from "../../store.svelte";
  import Icon from "../Icon.svelte";
  let { activeModule = "plugins" }: { activeModule?: string } = $props();
  let openPlugins = $state<Record<string, boolean>>({});
  function togglePlugin(name: string): void { openPlugins = { ...openPlugins, [name]: !openPlugins[name] }; }
  let pluginReloading = $state(false);
  async function reloadPlugins(): Promise<void> {
    pluginReloading = true;
    try { await brain.reloadPlugins(); } finally { pluginReloading = false; }
  }
</script>

<div class="cfg-pane" class:active={activeModule === "plugins"}>
  <div class="cfg-sec">
    <div class="cfg-sec-hd">
      <span class="t">PLUGINS</span>
      <span class="d">{brain.plugins.filter(a => a.enabled).length}/{brain.plugins.length} on · plugin packages in slots</span>
      <span class="sp"></span>
      <button type="button" class="k-btn ghost sm" disabled={pluginReloading} onclick={() => void reloadPlugins()}>
        <Icon name="refresh-cw" size={12} /> Reload
      </button>
    </div>
    {#if brain.plugins.length === 0}
      <div class="k-empty">No plugins installed — drop a package into ~/.xu/plugins/ and hit Reload.</div>
    {:else}
      {#each brain.plugins as a (a.name)}
        {@const detail = (a.settings?.length ?? 0) + (a.themes?.length ?? 0)}
        <div class="cfg-row">
          <!-- The name is the fold trigger when there is anything to fold, and
               plain text otherwise: a caret that expands nothing is a lie. -->
          <svelte:element
            this={detail > 0 ? "button" : "div"}
            class="label"
            class:row-exp={detail > 0}
            type={detail > 0 ? "button" : undefined}
            role={detail > 0 ? "button" : undefined}
            aria-expanded={detail > 0 ? !!openPlugins[a.name] : undefined}
            onclick={detail > 0 ? () => togglePlugin(a.name) : undefined}
          >
            {#if detail > 0}<span class="exp-caret"><Icon name={openPlugins[a.name] ? "chevron-down" : "chevron-right"} size={12} /></span>{/if}
            {a.name}<small>v{a.version}{a.description ? ` · ${a.description}` : ""}</small>
            {#if a.provides?.length}
              <div class="k-chips">
                {#each a.provides as p (p)}<span class="chip-tag on">+{p}</span>{/each}
              </div>
            {/if}
            {#if a.requires?.length}
              <div class="k-chips">
                <!-- A requirement nothing fills is the one worth flagging: the
                     plugin still loaded, so a silent chip would read as met. -->
                {#each a.requires as r (r)}
                  <span class="chip-tag" class:warn={a.unmet?.includes(r)}
                        title={a.unmet?.includes(r) ? "nothing currently fills this slot" : "provided"}
                  >needs {r}{#if a.unmet?.includes(r)} <Icon name="x" size={10} />{/if}</span>
                {/each}
              </div>
            {/if}
            <!-- Folded away, the settings still have to be countable: a plugin
                 whose only affordance is hidden looks like it has none. -->
            {#if detail > 0 && !openPlugins[a.name]}
              <small class="plug-count">
                {[
                  a.settings?.length ? `${a.settings.length} setting${a.settings.length > 1 ? "s" : ""}` : "",
                  a.themes?.length ? `${a.themes.length} scheme${a.themes.length > 1 ? "s" : ""}` : "",
                ].filter(Boolean).join(" · ")}
              </small>
            {/if}
          </svelte:element>
          <div class="ctrl">
            <label class="toggle" title={a.enabled ? "disable (takes effect on next reload/boot)" : "enable"}>
              <input type="checkbox" checked={a.enabled} onchange={() => void brain.setPluginEnabled(a.name, !a.enabled)} />
              <span class="track"></span>
              <span class="thumb"></span>
            </label>
          </div>
        </div>
        {#if openPlugins[a.name]}
          <!-- Manifest-declared settings, rendered generically. The host sends
               the schema with each key's live value, so no plugin needs its own
               RPC (or its own panel) just to be configurable. -->
          {#each a.settings ?? [] as s (s.key)}
            <div class="cfg-row sub">
              <div class="label">
                {s.label || s.key}<small>{s.key} · {s.type}</small>
              </div>
              <div class="ctrl">
                {#if s.type === "boolean"}
                  <label class="toggle" title={s.key}>
                    <input type="checkbox" checked={s.value === true}
                           onchange={(e) => void brain.setPluginSetting(a.name, s.key, e.currentTarget.checked)} />
                    <span class="track"></span>
                    <span class="thumb"></span>
                  </label>
                {:else if s.type === "integer" || s.type === "number"}
                  <!-- change, not input: committing per keystroke would write a
                       config file (and reject a half-typed "-") on every key. -->
                  <input type="number" value={String(s.value ?? "")}
                         step={s.type === "integer" ? 1 : "any"}
                         onchange={(e) => void brain.setPluginSetting(a.name, s.key, e.currentTarget.value)} />
                {:else if s.type === "list"}
                  <textarea rows="3" title="one per line"
                            value={Array.isArray(s.value) ? (s.value as unknown[]).join("\n") : String(s.value ?? "")}
                            onchange={(e) => void brain.setPluginSetting(a.name, s.key, e.currentTarget.value)}
                  ></textarea>
                {:else}
                  <input type="text" value={String(s.value ?? "")}
                         onchange={(e) => void brain.setPluginSetting(a.name, s.key, e.currentTarget.value)} />
                {/if}
              </div>
            </div>
          {/each}
          <!-- Contributed schemes: named here so an enabled theme plugin is
               discoverable from this pane, with a pointer to where it applies. -->
          {#if a.themes?.length}
            <div class="cfg-row sub">
              <div class="label">
                colour schemes<small>pick these in Themes → COLOR SCHEME</small>
              </div>
              <div class="ctrl">
                <div class="k-chips">
                  {#each a.themes as t (t.id)}
                    <span class="chip-tag" class:on={brain.theme === `plugin:${a.name}:${t.id}`}
                          title={`for the ${t.layout ?? "default"} layout`}>{t.name}</span>
                  {/each}
                </div>
              </div>
            </div>
          {/if}
        {/if}
      {/each}
    {/if}
  </div>
</div>
