<script lang="ts">
  import AboutPanel from "./AboutPanel.svelte";
  import Icon from "./Icon.svelte";
  import { VIEW_ICONS, viewIcon } from "../icons";
  import { brain } from "../store.svelte";
  import type { BuiltinViewName, ViewName } from "../store.svelte";
  import { pluginViews } from "../plugin-views.svelte";

  function handleKey(view: ViewName, e: KeyboardEvent): void {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      brain.setView(view);
    }
  }

  let aboutOpen = $state(false);

  function openAbout(): void { aboutOpen = true; }

  // Built-ins first (fixed order), plugin-contributed views after — a plugin
  // view is a dock peer, not a guest, so it gets the same button and keys.
  const ORDER: BuiltinViewName[] = ["workspace", "sessions", "config", "logs"];
</script>

<div id="dock">
  <div
    class="brand-mark"
    role="button"
    tabindex="0"
    onclick={openAbout}
    onkeydown={(e) => { if (e.key === "Enter" || e.key === " ") openAbout(); }}
    title="About Xu"
  >
    {#if brain.avatar}
      <img class="brand-avatar" src={brain.avatar} alt="Xu avatar" draggable="false" />
    {:else}
      X
    {/if}
  </div>
  {#each ORDER as name (name)}
    <div
      class="dock-icon"
      class:active={brain.view === name}
      role="button"
      tabindex="0"
      onclick={() => brain.setView(name)}
      onkeydown={(e) => handleKey(name, e)}
    >
      <Icon name={VIEW_ICONS[name]} />
      <span class="tip">{name.toUpperCase()}</span>
    </div>
  {/each}
  {#each pluginViews(brain.plugins) as p (p.name)}
    {@const view = `plugin:${p.name}` as ViewName}
    <div
      class="dock-icon"
      class:active={brain.view === view}
      role="button"
      tabindex="0"
      onclick={() => brain.setView(view)}
      onkeydown={(e) => handleKey(view, e)}
    >
      <Icon name={viewIcon(view, brain.plugins)} />
      <span class="tip">{(p.ui?.label ?? p.name).toUpperCase()}</span>
    </div>
  {/each}

  <AboutPanel open={aboutOpen} onclose={() => aboutOpen = false} />
</div>
