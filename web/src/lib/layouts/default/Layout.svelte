<script lang="ts">
  import { brain } from "../../store.svelte";
  import Dock from "../../components/Dock.svelte";
  import SessionBar from "../../components/SessionBar.svelte";
  import Icon from "../../components/Icon.svelte";
  import Chat from "../../components/Chat.svelte";
  import AgentState from "../../components/AgentState.svelte";
  import SessionsView from "../../components/SessionsView.svelte";
  import ConfigView from "../../components/ConfigView.svelte";
  import LogsView from "../../components/LogsView.svelte";
  import OnboardingView from "../../components/OnboardingView.svelte";
  import PluginSlot from "../../components/PluginSlot.svelte";
  import { pluginViewGuard, pluginViewName } from "../../plugin-views.svelte";
  import { createTabDrag } from "../../tab-drag.svelte";
  import { flip } from "svelte/animate";
  import { quintOut } from "svelte/easing";

  // Tab order is a user preference, so the strip is draggable (Alt+←/→ too).
  const drag = createTabDrag();

  /** The open view's plugin, or null for a built-in. `only` the open view's
   *  module is imported — a null `only` would mount every view plugin hidden. */
  const pluginView = $derived(pluginViewName(brain.view));

  // A plugin view can vanish while it is open (disabled, uninstalled) — land
  // on the chat rather than sit on a blank screen.
  $effect(() => pluginViewGuard(brain));
</script>

<div class="app-root">
  <Dock />
  <div id="main">
    {#if brain.view === "onboarding"}
      <OnboardingView />
    {:else}
      <SessionBar />
      <div id="session-tabs" aria-label="Open sessions" {...drag.strip()}>
        {#each brain.openSessionIds as id (id)}
          {@const tab = brain.sessions.find((item) => item.id === id)}
          <div
            class="session-tab"
            class:active={brain.session?.id === id}
            class:dragging={drag.dragging === id}
            animate:flip={{ duration: 220, easing: quintOut }}
            {...drag.tab(id)}
          >
            <button
              class="session-tab-name"
              onclick={() => void brain.switchSession(id)}
              onkeydown={(e) => drag.onkeydown(e, id)}
              title={`${tab?.title ?? id} — drag to reorder (Alt+←/→)`}
            >
              {#if brain.isBusy(id)}<span class="tab-busy" title="Running" aria-label="Running"></span>{/if}
              {tab?.title || id}
            </button>
            <button class="session-tab-close" aria-label={`Close ${tab?.title || id}`} onclick={() => brain.closeSession(id)}><Icon name="x" size={13} /></button>
          </div>
        {/each}
        <button class="session-tab-add" onclick={() => void brain.newSession()} aria-label="New session"><Icon name="plus" size={15} /></button>
      </div>
      <div id="view-workspace" class="view" class:active={brain.view === "workspace"}><div id="ws-row"><Chat /><AgentState /></div></div>
      <div id="view-sessions" class="view" class:active={brain.view === "sessions"}><SessionsView /></div>
      <div id="view-config" class="view" class:active={brain.view === "config"}><ConfigView /></div>
      <div id="view-logs" class="view" class:active={brain.view === "logs"}><LogsView /></div>
      <div id="view-plugin" class="view" class:active={pluginView !== null}>
        {#if pluginView !== null}
          <PluginSlot mount="view" only={pluginView} chrome="plugin-view" />
        {/if}
      </div>
    {/if}
  </div>
</div>
