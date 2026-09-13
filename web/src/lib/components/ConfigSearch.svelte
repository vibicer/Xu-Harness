<script lang="ts">
  import Icon from "./Icon.svelte";
  import { filterConfigCatalog, type ConfigSearchItem } from "./config/search";

  let {
    onselect,
    oncounts,
  }: {
    onselect: (item: ConfigSearchItem) => void;
    oncounts?: (query: string, counts: Record<string, number>) => void;
  } = $props();

  let query = $state("");
  let open = $state(false);
  let wrapEl = $state<HTMLElement | null>(null);
  const results = $derived(filterConfigCatalog(query));

  $effect(() => {
    const q = query.trim();
    const map: Record<string, number> = {};
    if (q) {
      for (const item of results) {
        map[item.moduleId] = (map[item.moduleId] ?? 0) + 1;
      }
    }
    oncounts?.(q, map);
  });

  function pick(item: ConfigSearchItem): void {
    open = false;
    onselect(item);
  }

  function onInput(): void {
    open = true;
  }

  function onFocus(): void {
    if (query.trim()) open = true;
  }

  function onKeydown(e: KeyboardEvent): void {
    if (e.key === "Escape") {
      open = false;
      query = "";
    } else if (e.key === "Enter" && results.length > 0) {
      pick(results[0]);
    }
  }

  $effect(() => {
    if (!open) return;
    const onDocClick = (e: MouseEvent) => {
      if (wrapEl && !wrapEl.contains(e.target as Node)) {
        open = false;
      }
    };
    document.addEventListener("mousedown", onDocClick, true);
    return () => document.removeEventListener("mousedown", onDocClick, true);
  });
</script>

<div class="cfg-search-wrap" bind:this={wrapEl}>
  <div class="cfg-search-bar">
    <Icon name="search" size={13} />
    <input
      type="text"
      class="cfg-search-input"
      placeholder="Search settings…"
      bind:value={query}
      oninput={onInput}
      onfocus={onFocus}
      onkeydown={onKeydown}
      aria-label="Search configuration settings"
    />
    {#if query}
      <button
        type="button"
        class="cfg-search-clear"
        aria-label="Clear search"
        onclick={() => { query = ""; open = false; }}
      >
        <Icon name="x" size={13} />
      </button>
    {/if}
  </div>

  {#if open && query.trim()}
    <div class="cfg-search-results" role="region" aria-label="Search results">
      {#if results.length === 0}
        <div class="cfg-search-empty">No settings match "{query}"</div>
      {:else}
        <div class="cfg-search-head">
          <span>{results.length} setting{results.length === 1 ? "" : "s"} found</span>
          <span class="sp"></span>
          <span class="hint">click to jump</span>
        </div>
        {#each results as item (item.id)}
          <button
            type="button"
            class="cfg-search-item"
            onclick={() => pick(item)}
          >
            <span class="cfg-search-badge"><Icon name={item.icon} size={11} /> {item.moduleName}</span>
            <div class="cfg-search-info">
              <span class="cfg-search-title">{item.title}</span>
              <span class="cfg-search-desc">{item.desc}</span>
            </div>
            <Icon name="arrow-right" size={13} />
          </button>
        {/each}
      {/if}
    </div>
  {/if}
</div>
