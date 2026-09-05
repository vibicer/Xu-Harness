<script lang="ts">
  import { brain } from "../store.svelte";
  import Icon from "./Icon.svelte";
  import type { GitDetail } from "../types";

  let open = $state(false);
  let detail = $state<GitDetail | null>(null);
  let loading = $state(false);

  const git = $derived(brain.state.git ?? null);
  const hasRepo = $derived(git?.repo !== false && !!git?.branch);

  /** Compact one-line summary: branch ↑↓ + change counts. */
  const label = $derived.by<string>(() => {
    if (!git || git.repo === false) return "";
    let s = git.branch;
    if ((git.ahead ?? 0) > 0) s += ` ↑${git.ahead}`;
    if ((git.behind ?? 0) > 0) s += ` ↓${git.behind}`;
    const parts: string[] = [];
    if (git.staged) parts.push(`+${git.staged}`);
    if (git.unstaged) parts.push(`~${git.unstaged}`);
    if (git.untracked) parts.push(`?${git.untracked}`);
    return parts.length ? `${s} · ${parts.join(" ")}` : s;
  });

  async function toggle(): Promise<void> {
    open = !open;
    if (open) await refresh();
  }

  async function refresh(): Promise<void> {
    loading = true;
    try {
      detail = await brain.client.call<GitDetail>("git.detail", {
        ...(brain.session?.id ? { session_id: brain.session.id } : {}),
      });
    } catch {
      detail = null;
    } finally {
      loading = false;
    }
  }

  function ago(ts: number | null | undefined): string {
    if (!ts) return "";
    const s = Math.max(1, Math.floor(Date.now() / 1000 - ts));
    if (s < 60) return `${s}s ago`;
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  }

  const titleText = $derived.by<string>(() => {
    if (!git) return "";
    let s = `git — ${git.branch}`;
    if (git.upstream) s += ` → ${git.upstream}`;
    s += git.dirty ? " · dirty" : " · clean";
    if (git.last) s += ` · ${git.last.sha} ${git.last.subject}`;
    return s;
  });
</script>

{#if hasRepo}
  <button
    class="gc-chip"
    class:dirty={git?.dirty}
    class:synced={!git?.dirty && !(git?.ahead ?? 0) && !(git?.behind ?? 0)}
    type="button"
    title={titleText}
    onclick={() => void toggle()}
    aria-expanded={open}
  >
    <span class="gc-dot" aria-hidden="true"></span><Icon name="git-branch" size={12} />{label}
  </button>

  {#if open}
    <div class="gc-panel" role="dialog" aria-label="git detail">
      <div class="gc-head">
        <strong>{git?.branch}</strong>
        {#if git?.upstream}
          <span class="gc-up">→ {git.upstream} ({#if (git.ahead ?? 0) || (git.behind ?? 0)}↑{git.ahead ?? 0} ↓{git.behind ?? 0}{:else}synced{/if})</span>
        {/if}
        <button class="gc-close" type="button" aria-label="Close" onclick={() => (open = false)}><Icon name="x" size={14} /></button>
      </div>

      {#if git?.last}
        <div class="gc-last">
          <code>{git.last.sha}</code>
          <span class="gc-subject">{git.last.subject}</span>
          <span class="gc-ago">{ago(git.last.ts)}</span>
        </div>
      {/if}

      <div class="gc-counts">
        <span class:hot={!!git?.staged}>+{git?.staged ?? 0} staged</span>
        <span class:hot={!!git?.unstaged}>~{git?.unstaged ?? 0} modified</span>
        <span class:hot={!!git?.untracked}>?{git?.untracked ?? 0} untracked</span>
      </div>

      {#if loading}
        <div class="gc-loading">loading…</div>
      {:else if detail?.files?.length}
        <div class="gc-files">
          {#each detail.files.slice(0, 12) as f (f.path)}
            <span class="gc-file {f.state}" title={f.state}>{f.path}</span>
          {/each}
          {#if detail.files.length > 12}<span class="gc-more">+{detail.files.length - 12} more</span>{/if}
        </div>
      {:else if detail}
        <div class="gc-empty">no changes</div>
      {/if}

      {#if detail?.commits?.length}
        <div class="gc-commits">
          {#each detail.commits.slice(0, 5) as c (c.sha)}
            <div class="gc-commit">
              <code>{c.sha}</code>
              <span class="gc-subject">{c.subject}</span>
              <span class="gc-ago">{ago(c.ts)}</span>
            </div>
          {/each}
        </div>
      {/if}

      <button class="gc-refresh" type="button" onclick={() => void refresh()}><Icon name="refresh-cw" size={12} /> refresh</button>
    </div>
  {/if}
{/if}

<style>
  .gc-chip {
    display: inline-flex; align-items: center; gap: 6px;
    font: inherit; font-size: 11px; letter-spacing: 0.5px;
    background: transparent; border: 0; padding: 2px 6px;
    cursor: pointer; white-space: nowrap;
    color: var(--fg-dim, #8ea2c0);
  }
  .gc-chip:hover { color: var(--fg, #dbe6f5); text-decoration: underline dotted; }
  .gc-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ok, #7ce38b); flex: none; }
  .gc-chip.dirty .gc-dot { background: var(--amber, #ffd27c); }
  .gc-chip.dirty { color: var(--amber, #ffd27c); }

  .gc-panel {
    position: absolute; bottom: calc(100% + 8px); left: 0; z-index: 40;
    min-width: 280px; max-width: 380px;
    background: var(--bg-raised, #10161f);
    border: 1px solid var(--border-strong, #2a3854);
    border-radius: 8px; padding: 10px 12px;
    display: flex; flex-direction: column; gap: 8px;
    box-shadow: 0 8px 24px rgba(0 0 0 / 0.4);
    font-size: 12px; text-align: left;
  }
  .gc-head { display: flex; align-items: center; gap: 8px; }
  .gc-head strong { font-size: 13px; }
  .gc-up { color: var(--fg-dim, #8ea2c0); font-size: 11px; }
  .gc-close {
    margin-left: auto; background: transparent; border: 0; color: var(--fg-dim, #8ea2c0);
    cursor: pointer; padding: 0 2px; display: grid; place-items: center;
  }
  .gc-close:hover { color: var(--fg, #dbe6f5); }

  .gc-last, .gc-commit { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
  .gc-last code, .gc-commit code { color: var(--magenta, #c49bff); flex: none; font-size: 11px; }
  .gc-subject { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .gc-ago { color: var(--fg-dim, #8ea2c0); font-size: 10px; flex: none; }

  .gc-counts { display: flex; gap: 12px; font-size: 11px; color: var(--fg-dim, #8ea2c0); }
  .gc-counts .hot { color: var(--amber, #ffd27c); }

  .gc-files { display: flex; flex-direction: column; gap: 2px; max-height: 132px; overflow: auto; }
  .gc-file {
    flex: none;
    font-size: 11px; font-family: var(--mono, monospace);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    color: var(--fg-dim, #8ea2c0);
  }
  .gc-file.modified, .gc-file.staged { color: var(--amber, #ffd27c); }
  .gc-file.added { color: var(--ok, #7ce38b); }
  .gc-file.untracked { color: var(--fg-dim, #8ea2c0); font-style: italic; }
  .gc-more { font-size: 10px; color: var(--fg-dim, #8ea2c0); }
  .gc-empty, .gc-loading { font-size: 11px; color: var(--fg-dim, #8ea2c0); font-style: italic; }

  .gc-commits { display: flex; flex-direction: column; gap: 3px; border-top: 1px solid var(--border, #1c2740); padding-top: 8px; }

  .gc-refresh {
    align-self: flex-end; background: transparent; border: 1px solid var(--border-strong, #2a3854);
    color: var(--fg-dim, #8ea2c0); font-size: 10px; padding: 2px 8px; border-radius: 4px; cursor: pointer;
    display: inline-flex; align-items: center; gap: 5px;
  }
  .gc-refresh:hover { color: var(--fg, #dbe6f5); }
</style>
