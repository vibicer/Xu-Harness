<script lang="ts">
  import type { Component } from "svelte";
  import { brain } from "./lib/store.svelte";
  import { resolveLayout } from "./lib/layouts/registry";
  import PluginLayout from "./lib/components/PluginLayout.svelte";
  import { setLucideProps } from "@lucide/svelte";

  // Icon defaults for the whole shell, set once: 16px reads as a peer of the
  // 12–14px UI type, and 1.75 stroke keeps a small icon from going muddy at
  // Lucide's default 2. Colour stays `currentColor`, so an icon is themed by
  // whatever palette var its container already uses. Per-site overrides are a
  // CSS width/height (which beats the svg attributes) or an explicit `size`.
  setLucideProps({ size: 16, strokeWidth: 1.75 });

  // Every layout is a whole separate UI — its own markup *and* its own
  // stylesheet — so static imports would ship all of them to everyone. Each
  // registry folder's `load()` is one lazy chunk; the first paint pulls only
  // the layout in use, switching costs one localhost fetch, then it's cached.
  const descriptor = $derived(resolveLayout(brain.layout));

  /** A layout can also come from a plugin: `plugin:<name>` means the shell hands
   *  the whole UI to that plugin's `mount: "layout"` custom element. Kept out of
   *  the registry because there is no module to import here — PluginLayout fetches it
   *  from the plugin's own directory at runtime. */
  const pluginName = $derived(
    brain.layout.startsWith("plugin:") ? brain.layout.slice(7) : null,
  );

  let Layout = $state<Component | null>(null);

  $effect(() => {
    if (pluginName) return; // a plugin owns the shell; no built-in to load
    const desc = descriptor;
    let live = true;
    // Switching layouts twice quickly can resolve the imports out of order, so
    // a stale module must not overwrite the current one.
    void desc.load().then((mod) => {
      if (live) Layout = mod.default;
    });
    return () => {
      live = false;
    };
  });
</script>

<svelte:head>
  <title>{pluginName ? `Xu // ${pluginName}` : descriptor.title}</title>
</svelte:head>

<!-- Blank for the one frame a chunk is in flight; theme.css paints the bg. -->
{#if pluginName}
  <PluginLayout name={pluginName} />
{:else if Layout}
  <Layout />
{/if}
