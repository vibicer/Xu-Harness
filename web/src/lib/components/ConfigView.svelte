<script lang="ts">
  import { brain } from "../store.svelte";
  import ProvidersPanel from "./config/ProvidersPanel.svelte";
  import AgentPanel from "./config/AgentPanel.svelte";
  import NotificationsPanel from "./config/NotificationsPanel.svelte";
  import ToolsPanel from "./config/ToolsPanel.svelte";
  import SkillsPanel from "./config/SkillsPanel.svelte";
  import PluginsPanel from "./config/PluginsPanel.svelte";
  import OrchestratePanel from "./config/OrchestratePanel.svelte";
  import DataPanel from "./config/DataPanel.svelte";
  import ThemesPanel from "./config/ThemesPanel.svelte";
  import Icon from "./Icon.svelte";
  import { ICONS, type IconName } from "../icons";
  import { notifySupported } from "../notify";
  import { builtinToolsetsFrom, enabledToolsetsCount } from "./config/shared.svelte";
  import PluginSlot from "./PluginSlot.svelte";

import type { PresetInfo } from "../types";

/** Built-in panes, plus `plugin:<name>` for a pane a plugin contributes via
 *  `manifest.ui.mount: "config"`. */
type CfgModule = "providers" | "agent" | "toolsets" | "skills" | "data" | "themes" | "orchestrate" | "notifications" | "plugins" | `plugin:${string}`;
/** One entry in the tab strip. */
type CfgTab = { id: CfgModule; n: string; d: string; stamp: string; icon: IconName };

  /** Layouts that embed this view in their own chrome pass `embed` to drop
   *  the page title/subtitle (they already render a sheet header). */
  let { embed = false }: { embed?: boolean } = $props();

  let activeModule = $state<CfgModule>("providers");
// An appearance-perfecting reload stashes its return target in
// sessionStorage; consume it once so the reload reopens this pane.
try {
const ret = sessionStorage.getItem("cfg-return") as CfgModule | null;
if (ret) { activeModule = ret; sessionStorage.removeItem("cfg-return"); }
} catch { /* storage unavailable */ }
  let msg = $state("");
  let msgKind = $state<"ok" | "err" | "info">("info");

  $effect(() => {
    if (brain.view === "config") void refresh().catch(() => {});
  });

  function setMsg(text: string, kind: "ok" | "err" | "info" = "info"): void {
    msg = text;
    msgKind = kind;
  }

  /** Re-fetch everything the panes read. Each pane keeps its own editable
   *  mirrors and reseeds off `brain.config`, so this only refreshes stores. */
  async function refresh(): Promise<void> {
    await brain.refreshProviders();  // keep chat model dropdown in sync
    await Promise.all([
      brain.refreshToolsets(),
      brain.refreshMemory(),
      brain.refreshConfig(),
      brain.refreshSkills(),
      brain.refreshPersonas(),
      loadPresets(),
    ]);
  }


  let presets = $state<PresetInfo[]>([]);
  async function loadPresets(): Promise<void> {
    try { presets = await brain.refreshPresets(); } catch { /* not ready */ }
  }
  const notifySettings = $derived(brain.notify);
  const notifyPerm = $derived(brain.notifyPerm);
  function notifyStamp(): string {
    if (!notifySupported()) return "N/A";
    if (notifyPerm === "granted") return notifySettings.enabled ? "ON" : "OFF";
    return notifyPerm === "denied" ? "BLOCKED" : "ASK";
  }
  const builtinToolsets = $derived(builtinToolsetsFrom(brain.toolsets));
  const enabledTools = $derived(enabledToolsetsCount(builtinToolsets));

  /** Enabled plugins contributing a pane here. The shell knows nothing about
   *  any of them: it renders a tab from the manifest and hands the pane to
   *  PluginSlot, which owns the element. */
  const pluginPanes = $derived(
    brain.plugins.filter((p) => p.enabled && p.ui?.element && p.ui.mount === "config"),
  );
  const MODULES = $derived<CfgTab[]>(([
    { id: "agent", n: "Agent", d: "context, compression, approvals, timeout", stamp: "4", icon: "bot" },
    { id: "data", n: "Data", d: "data home · personas · memory", stamp: `${brain.personas.length}P · ${brain.memories.length}M`, icon: "database" },
    { id: "notifications", n: "Notifications", d: "desktop alerts — response done · run failed", stamp: notifyStamp(), icon: "bell" },
    { id: "orchestrate", n: "Orchestration", d: "agent-team presets", stamp: `${presets.length}P`, icon: "network" },
    { id: "plugins", n: "Plugins", d: "plugin packages in slots — toggle · hot-reload", stamp: `${brain.plugins.filter((a) => a.enabled).length}/${brain.plugins.length}`, icon: "puzzle" },
    { id: "providers", n: "Providers", d: "openai / anthropic compatible endpoints, keys, models", stamp: `${brain.providers.length} SET`, icon: "plug" },
    { id: "skills", n: "Skills", d: "progressive-disclosure skill catalog on/off", stamp: `${brain.skills.filter(s => s.ambient).length}/${brain.skills.length}`, icon: "graduation-cap" },
    { id: "themes", n: "Themes", d: "dark color schemes — built-ins + your presets", stamp: (brain.allThemes.find((t) => t.id === brain.theme)?.name ?? brain.theme).toUpperCase(), icon: "palette" },
    { id: "toolsets", n: "Toolsets", d: "built-in + custom tools in show/hide panels", stamp: `${enabledTools}/${builtinToolsets.length}`, icon: "wrench" },
    ...pluginPanes.map((p) => ({
      id: `plugin:${p.name}` as CfgModule,
      n: p.ui?.label ?? p.name,
      d: p.description || `${p.name} plugin`,
      stamp: "",
      // Plugins name an icon; the shell only draws one it actually ships.
      icon: (p.ui?.icon && p.ui.icon in ICONS ? p.ui.icon : "puzzle") as IconName,
    })),
  ] as CfgTab[]).sort((a, b) => a.n.localeCompare(b.n)));
  /** The tab keeps only name + stamp; the module's blurb moves to a single
   *  note line under the strip, for whichever tab is open. */
  const activeNote = $derived(MODULES.find((m) => m.id === activeModule)?.d ?? "");

  /** A plugin pane can vanish under the open tab (disabled, or uninstalled).
   *  Fall back rather than showing a strip with nothing selected. */
  $effect(() => {
    if (brain.plugins.length && !MODULES.some((m) => m.id === activeModule)) {
      activeModule = "providers";
    }
  });
</script>

<div class="view-inner">
  {#if !embed}
    <div class="page-title">Config</div>
    <div class="page-sub">
      Providers, agent behavior, toolsets, and data. Changes hot-apply — no restart needed.
    </div>
  {/if}

  <nav class="cfg-tabs" aria-label="Config modules">
    {#each MODULES as m (m.id)}
      <button
        type="button"
        class="cfg-tab"
        class:active={activeModule === m.id}
        aria-pressed={activeModule === m.id}
        onclick={() => (activeModule = m.id)}
      >
        <Icon name={m.icon} size={14} />
        <span class="n">{m.n}</span>
        <span class="stamp">{m.stamp}</span>
      </button>
    {/each}
  </nav>
  <div class="cfg-tabnote">{activeNote}</div>

  {#if msg}
    <div class="cfg-msg" class:ok={msgKind === "ok"} class:err={msgKind === "err"}>
      {#if msgKind !== "info"}<Icon name={msgKind === "ok" ? "check" : "circle-x"} size={12} />{/if}
      {msg}
    </div>
  {/if}

  <ProvidersPanel {activeModule} {setMsg} {refresh} />
  <AgentPanel {activeModule} />
  <NotificationsPanel {activeModule} />
  <ToolsPanel {activeModule} />
  <SkillsPanel {activeModule} />
  <PluginsPanel {activeModule} />
  <OrchestratePanel {activeModule} {presets} {loadPresets} />
  <DataPanel {activeModule} />
  <ThemesPanel {activeModule} />

  <!-- Plugin panes. The wrapper is the shell's own `.cfg-pane`, so a plugin
       neither knows nor cares which tab is open — it just renders content. -->
  {#each pluginPanes as p (p.name)}
    <PluginSlot
      mount="config"
      only={p.name}
      chrome={`cfg-pane${activeModule === `plugin:${p.name}` ? " active" : ""}`}
    />
  {/each}
</div>
