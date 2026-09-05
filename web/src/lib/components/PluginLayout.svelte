<script lang="ts">
  /** Whole-shell mount point for a plugin-contributed layout.
   *
   * The sibling of PluginSlot: same import-and-define contract, but this one
   * renders the plugin's custom element *as the entire UI* instead of as a chip
   * inside someone else's chrome. A plugin opts in with
   * `"ui": { "module": "ui.js", "element": "xu-…", "mount": "layout" }`, and
   * the shell shows it when `brain.layout === "plugin:<name>"`.
   *
   * Why a separate component: PluginSlot renders *every* contributor for a
   * mount and wraps them in a `<span>` — correct for status chips, wrong for a
   * layout. Only one layout can be on screen, and it must not sit inside an
   * inline wrapper.
   *
   * On failure this renders a plain readable message rather than nothing: a
   * blank page would look like the app died, and the user still needs a way
   * back to a working layout.
   */
  import { brain } from "../store.svelte";

  let { name }: { name: string } = $props();

  const plugin = $derived(
    brain.plugins.find((p) => p.name === name && !!p.enabled && !!p.ui?.element) ?? null,
  );

  let ready = $state(false);
  let error = $state<string | null>(null);

  /** Custom elements cannot be un-defined, so a module is imported at most once
   *  per page load even if the layout is switched away and back. */
  const loaded = new Set<string>();

  $effect(() => {
    const p = plugin;
    if (!p?.ui) return;
    const element = p.ui.element;
    if (customElements.get(element)) {
      ready = true;
      return;
    }
    const key = `${p.name}/${p.ui.module}`;
    if (loaded.has(key)) return;
    loaded.add(key);
    import(/* @vite-ignore */ `/plugins/${p.name}/${p.ui.module}`)
      .then(() => {
        if (customElements.get(element)) ready = true;
        else error = `module defined no <${element}>`;
      })
      .catch((e: unknown) => {
        error = e instanceof Error ? e.message : String(e);
      });
  });

  let host = $state<(HTMLElement & { sync?: () => void }) | null>(null);

  /** Hand over the live store, and keep a handle for the reactivity bridge
   *  below. The element reads state and calls RPCs through the store, exactly
   *  as a statusbar plugin does. */
  function bind(node: HTMLElement): () => void {
    (node as HTMLElement & { brain?: unknown }).brain = brain;
    host = node as HTMLElement & { sync?: () => void };
    return () => { host = null; };
  }

  /** Reactivity bridge. A plain custom element cannot subscribe to runes, so the
   *  shell pushes instead: this effect *reads* every store field a layout paints
   *  from — reading is what subscribes it — then calls `sync()` on the element.
   *  Omitting a field means the layout silently stops updating for that kind of
   *  change, so this list is load-bearing, not decorative. */
  $effect(() => {
    void brain.view;
    void brain.connected;
    void brain.messages.length;
    void brain.draft;
    void brain.turns;
    void brain.approvals.length;
    void brain.sessions.length;
    void brain.openSessionIds.length;
    void brain.session?.id;
    void brain.state;
    void brain.status;
    void brain.todos;
    void brain.subRuns;
    void brain.compressingSessions.length;
    void brain.config;
    void brain.skills.length;
    void brain.plugins.length;
    void brain.models.length;
    void brain.memories.length;
    void brain.personas.length;
    // `allThemes`, not `themes`: a plugin layout may render the scheme picker
    // itself, and enabling a *theme* plugin only changes the derived list.
    void brain.allThemes.length;
    void brain.theme;
    void brain.layout;
    void brain.customLayouts.length;
    void brain.activePersona;
    void brain.avatar;
    // A layout may render the agent state itself (model/persona pickers, a
    // toolset list), so provider and tool changes have to reach it too.
    void brain.providers;
    void brain.toolsets;
    void brain.dropins;
    // A layout may render the notification panel: settings live on the store
    // (the localStorage mirror) and the permission beside them.
    void brain.notify;
    void brain.notifyPerm;
    const el = host;
    if (el && typeof el.sync === "function") el.sync();
  });
</script>

<!-- `plugins` is empty until `plugin.list` returns, so "not enabled" must not be
     claimed before the list has actually arrived — otherwise every cold start
     flashes a scary error for the plugin that is working fine. Empty list =
     unknown, and unknown renders nothing. -->
{#if plugin?.ui && ready}
  <svelte:element this={plugin.ui.element} {@attach bind} />
{:else if error}
  <div class="pl-fail" role="alert">
    <strong>{name}</strong> could not provide a layout: {error}
    <button type="button" onclick={() => brain.setLayout("default")}>Use the default layout</button>
  </div>
{:else if !plugin && brain.plugins.length > 0}
  <div class="pl-fail" role="alert">
    Layout plugin <strong>{name}</strong> is not enabled. Turn it on in Config → Plugins,
    or pick another layout.
    <button type="button" onclick={() => brain.setLayout("default")}>Use the default layout</button>
  </div>
{/if}

<style>
  /* Deliberately self-contained: the failing case is exactly when the plugin's
     own stylesheet may be missing, so this cannot rely on layout tokens. */
  .pl-fail {
    margin: 40px auto; max-width: 52ch;
    font: 13px/1.6 ui-monospace, Menlo, Consolas, monospace;
    color: #dbe6f5; background: #10161f;
    border: 1px solid #2a3854; padding: 18px 20px;
  }
  .pl-fail strong { color: #ff9179; }
  .pl-fail button {
    display: block; margin-top: 14px; cursor: pointer;
    font: inherit; font-size: 11px;
    color: #10161f; background: #8ba4ff; border: 0; padding: 7px 12px;
  }
</style>
