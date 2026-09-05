<script lang="ts">
  import AboutPanel from "./AboutPanel.svelte";
  import Icon from "./Icon.svelte";
  import { VIEW_ICONS } from "../icons";
  import { brain } from "../store.svelte";
  import type { ViewName } from "../store.svelte";

  function handleKey(view: ViewName, e: KeyboardEvent): void {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      brain.setView(view);
    }
  }

  let aboutOpen = $state(false);

  function openAbout(): void { aboutOpen = true; }

  const ORDER: ViewName[] = ["workspace", "sessions", "config", "logs"];
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

  <AboutPanel open={aboutOpen} onclose={() => aboutOpen = false} />
</div>
