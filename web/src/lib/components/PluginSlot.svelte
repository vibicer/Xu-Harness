<script lang="ts">
  /** Generic mount point for plugin-contributed UI.
   *
   * Knows nothing about any specific plugin: it reads `brain.plugins`, and for
   * every *enabled* plugin whose manifest declares a `ui` for this `mount`, it
   * imports the module off `/plugins/<name>/<module>` and renders the custom
   * element it defines.
   *
   * The contract with a plugin module:
   * - it is an ES module served from the plugin's own directory
   * - importing it must define `ui.element` via `customElements.define`
   * - the element receives a `brain` property (the live store) so it can call
   *   RPCs, and is otherwise responsible for its own DOM
   *
   * Renders *nothing at all* — no wrapper element — when no plugin contributes
   * here, so a host layout's chrome (borders, padding, flex gaps) leaves no
   * empty slot behind. Layouts pass their own wrapper class via `chrome`.
   */
  import { brain } from "../store.svelte";
  import Icon from "./Icon.svelte";
  import type { PluginInfo } from "../types";

  type Contribution = PluginInfo & { ui: NonNullable<PluginInfo["ui"]> };

  /** `only` narrows the slot to one plugin, for a mount where each contributor
   *  needs its own wrapper (a Config pane is shown/hidden per tab). Omitted =
   *  every contributor for this mount, side by side. */
  let { mount = "statusbar", chrome = "", only = "" }: {
    mount?: string;
    chrome?: string;
    only?: string;
  } = $props();

  /** Modules already imported this session, so re-renders don't re-fetch.
   *  A custom element cannot be un-defined, so this is intentionally sticky
   *  across disable/enable — only the *rendering* is gated. */
  const loaded = new Set<string>();
  let ready = $state<string[]>([]);
  let failed = $state<Record<string, string>>({});

  const contributions = $derived(
    brain.plugins.filter(
      (p): p is Contribution =>
        !!p.enabled && !!p.ui?.element && (p.ui.mount ?? "statusbar") === mount
        && (!only || p.name === only),
    ),
  );

  /** Only what actually has something to show: a defined element, or an error
   *  worth surfacing. Anything mid-import contributes no markup, so the slot
   *  does not flash an empty box while a module loads. */
  const visible = $derived(
    contributions.filter((p) => ready.includes(p.ui.element) || failed[p.name]),
  );

  /** Import each contributor's module once. A failure is recorded, not thrown:
   *  one broken plugin must not take the shell's status bar down with it. */
  $effect(() => {
    for (const p of contributions) {
      const key = `${p.name}/${p.ui.module}`;
      if (loaded.has(key)) continue;
      loaded.add(key);
      import(/* @vite-ignore */ `/plugins/${p.name}/${p.ui.module}`)
        .then(() => {
          if (customElements.get(p.ui.element)) {
            ready = [...ready, p.ui.element];
          } else {
            failed = { ...failed, [p.name]: `module defined no <${p.ui.element}>` };
          }
        })
        .catch((e: unknown) => {
          failed = { ...failed, [p.name]: e instanceof Error ? e.message : String(e) };
        });
    }
  });

  /** Drop stale errors for plugins that are no longer contributing here, so a
   *  disabled plugin cannot keep a warning chip (and its chrome) on screen. */
  $effect(() => {
    const live = new Set(contributions.map((p) => p.name));
    const stale = Object.keys(failed).filter((name) => !live.has(name));
    if (stale.length) {
      failed = Object.fromEntries(
        Object.entries(failed).filter(([name]) => live.has(name)),
      );
    }
  });

  /** Hand the store to the element once it is in the DOM — plugin UI talks to
   *  the brain through the same client as everything else. */
  function bind(node: HTMLElement): void {
    (node as HTMLElement & { brain?: unknown }).brain = brain;
  }
</script>

{#if visible.length}
  <span class={chrome}>
    {#each visible as p (p.name)}
      {#if ready.includes(p.ui.element)}
        <svelte:element this={p.ui.element} {@attach bind} />
      {:else}
        <span class="plugin-ui-error" title={`${p.name}: ${failed[p.name]}`}><Icon name="triangle-alert" size={12} /> {p.name}</span>
      {/if}
    {/each}
  </span>
{/if}

<style>
  .plugin-ui-error {
    display: inline-flex; align-items: center; gap: 4px;
    font-size: 10px; letter-spacing: 0.5px;
    color: var(--danger, #ff6b7a); white-space: nowrap;
  }
</style>
