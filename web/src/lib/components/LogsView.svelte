<script lang="ts">
  import { brain } from "../store.svelte";
  import Icon from "./Icon.svelte";

  interface ActivityLog {
    id: number;
    ts: number;
    level: "debug" | "info" | "warn" | "error";
    source: string;
    message: string;
    detail?: string;
    payload?: Record<string, unknown>;
  }

  let logs = $state<ActivityLog[]>([]);
  let loading = $state(false);
  let level = $state("info");
  let active = $state<ActivityLog | null>(null);
  let copyState = $state<"idle" | "copied" | "failed">("idle");
  const LEVELS = ["all", "debug", "info", "warn", "error"];

  $effect(() => {
    if (brain.view !== "logs" || !brain.connected) return;
    void refresh();
    const timer = setInterval(() => void refresh(), 2000);
    return () => clearInterval(timer);
  });

  $effect(() => {
    // ESC closes the details modal.
    if (active === null) return;
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === "Escape") close();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  async function refresh(): Promise<void> {
    if (loading) return;
    loading = true;
    try {
      const params: Record<string, unknown> = { limit: 200 };
      if (level !== "all") params.level = level;
      const result = await brain.client.call<{ logs: ActivityLog[] }>("logs.list", params);
      logs = result.logs;
    } finally {
      loading = false;
    }
  }

  function clock(ts: number): string {
    return new Date(ts * 1000).toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    });
  }

  function open(entry: ActivityLog): void {
    if (!entry.payload) return;
    active = entry;
    copyState = "idle";
  }

  function close(): void {
    active = null;
  }

  async function copy(): Promise<void> {
    if (!active?.payload) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(active.payload, null, 2));
      copyState = "copied";
      setTimeout(() => { if (copyState === "copied") copyState = "idle"; }, 1500);
    } catch {
      copyState = "failed";
      setTimeout(() => { if (copyState === "failed") copyState = "idle"; }, 1500);
    }
  }

  const errors = $derived(logs.filter((l) => l.level === "error").length);
  const jsonText = $derived(
    active?.payload ? JSON.stringify(active.payload, null, 2) : "",
  );
</script>

<div class="view-inner">
  <div class="page-title">Logs</div>
  <div class="page-sub">
    All brain and plugin activity — turns, RPC calls, tool runs, errors. In-memory only, newest first, capped at 1000 entries. Click a row to view its full input/output payload (credentials are redacted).
  </div>

  <div class="panel logs-panel">
    <div class="logs-head">
      <div>
        <h3>ACTIVITY LOG</h3>
        <div class="gw-log-sub">
          {logs.length} entries · {errors} error{errors === 1 ? "" : "s"}
        </div>
      </div>
      <div class="logs-tools">
        <label class="logs-filter">
          LEVEL
          <select bind:value={level} onchange={() => void refresh()}>
            {#each LEVELS as lvl}
              <option value={lvl}>{lvl}</option>
            {/each}
          </select>
        </label>
      </div>
    </div>
    <div class="gw-logs logs-full">
      {#if logs.length === 0}
        <div class="gw-log-empty">No activity yet.</div>
      {:else}
        {#snippet row(entry: ActivityLog)}
          <span class="gw-log-time">{clock(entry.ts)}</span>
          <span class="gw-log-source">{entry.source}</span>
          <span class={`gw-log-level ${entry.level}`}>{entry.level}</span>
          <span class="gw-log-message">{entry.message}</span>
          {#if entry.detail}<span class="gw-log-detail">{entry.detail}</span>{/if}
          {#if entry.payload}<span class="gw-log-chevron" aria-hidden="true">›</span>{/if}
        {/snippet}
        {#each logs as entry (entry.id)}
          <!-- Rows with a payload open a details modal, so they are real
               buttons (keyboard + focus for free). Rows without one are inert
               text and stay plain divs. -->
          {#if entry.payload}
            <button
              type="button"
              class="gw-log-entry gw-log-clickable"
              class:error={entry.level === "error"}
              class:warn={entry.level === "warn"}
              onclick={() => open(entry)}
            >
              {@render row(entry)}
            </button>
          {:else}
            <div
              class="gw-log-entry"
              class:error={entry.level === "error"}
              class:warn={entry.level === "warn"}
            >
              {@render row(entry)}
            </div>
          {/if}
        {/each}
      {/if}
    </div>
  </div>
</div>

{#if active}
  <!-- Backdrop closes on a click that lands on the backdrop itself, so the
       panel needs no stopPropagation handler (which would put a click on a
       non-interactive element). ESC closes too — see the $effect above. -->
  <div
    class="logs-modal-backdrop"
    role="presentation"
    onclick={(e) => { if (e.target === e.currentTarget) close(); }}
  >
    <div
      class="logs-modal-panel"
      role="dialog"
      aria-modal="true"
      aria-labelledby="logs-modal-title"
    >
      <div class="logs-modal-head">
        <div>
          <div id="logs-modal-title" class="logs-modal-title">API call details</div>
          <div class="logs-modal-sub">
            <span class={`gw-log-level ${active.level}`}>{active.level}</span>
            <span class="logs-modal-source">{active.source}</span>
            <span class="logs-modal-time">{clock(active.ts)}</span>
          </div>
          <div class="logs-modal-msg">{active.message}</div>
        </div>
        <div class="logs-modal-actions">
          <button type="button" class="logs-modal-btn" onclick={copy}>
            {copyState === "copied" ? "Copied" : copyState === "failed" ? "Copy failed" : "Copy JSON"}
          </button>
          <button type="button" class="logs-modal-btn logs-modal-close" onclick={close} aria-label="Close"><Icon name="x" size={14} /></button>
        </div>
      </div>
      <pre class="logs-modal-json">{jsonText}</pre>
    </div>
  </div>
{/if}
