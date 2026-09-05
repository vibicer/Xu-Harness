<script lang="ts">
  import { brain } from "../store.svelte";

  interface AppStatus {
    brain: string;
    providers?: string[];
    rss_mb?: number | null;
  }

  let status = $state<AppStatus>({ brain: "off" });

  const ram = $derived(status.rss_mb != null ? `${Math.round(status.rss_mb)}MB` : "—");

  $effect(() => {
    if (!brain.connected) return;
    let alive = true;
    const poll = async () => {
      try {
        const s = await brain.client.call<AppStatus>("app.status");
        if (alive) status = s;
      } catch { /* keep last */ }
    };
    void poll();
    const t = setInterval(() => void poll(), 5000);
    return () => { alive = false; clearInterval(t); };
  });
</script>

<div id="ws-bar">
  <span class="id">XU.SYS</span>
  <span class="val">v0.1.0</span>
  <span style="width:2px;height:18px;background:var(--border);display:inline-block;"></span>
  <span class="id">SESS</span>
  <span class="val">{brain.session ? brain.session.id : "—"}</span>
  <span class="id">OPEN</span>
  <span class="val">{brain.openSessionIds.length}</span>
  <span class="id">RAM</span>
  <span class="val">{ram}</span>
  <span class="spacer"></span>
  <span class="st">
    <span class="sq" class:mag={brain.connected} class:off={!brain.connected}></span>
    <span class="l">{brain.connected ? "BRAIN OK" : "BRAIN DOWN"}</span>
  </span>
</div>
