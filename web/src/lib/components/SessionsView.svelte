<script lang="ts">
  import { brain } from "../store.svelte";
  import Icon from "./Icon.svelte";

  let editingId = $state<string | null>(null);
  let titleDraft = $state("");
  let confirmDeleteId = $state<string | null>(null);
  let limitDraft = $state(30);
  let pageSize = $state(10);
  let page = $state(1);

  const PAGE_SIZES = [10, 30, 50, 100];

  $effect(() => { limitDraft = brain.config.prune_keep; });

  async function applyLimit(): Promise<void> {
    const n = Math.max(1, Math.floor(Number(limitDraft) || 1));
    limitDraft = n;
    await brain.saveConfig("prune_keep", n);
  }

  // pagination over the current, sorted session list
  const totalPages = $derived(Math.max(1, Math.ceil(brain.sessions.length / pageSize)));
  const paged = $derived(brain.sessions.slice((page - 1) * pageSize, page * pageSize));

  // numbered pages, windowed to 7 around the current page
  const pageList = $derived((() => {
    const n = totalPages;
    if (n <= 7) return Array.from({ length: n }, (_, i) => i + 1);
    const lo = Math.max(1, Math.min(page - 3, n - 6));
    const hi = Math.min(n, lo + 6);
    return Array.from({ length: hi - lo + 1 }, (_, i) => lo + i);
  })());

  $effect(() => { if (page > totalPages) page = totalPages; });

  function startEdit(id: string, current: string): void {
    editingId = id;
    titleDraft = current;
    confirmDeleteId = null;
  }

  function cancelEdit(): void {
    editingId = null;
    titleDraft = "";
  }

  async function commitEdit(id: string): Promise<void> {
    const t = titleDraft.trim();
    if (t) await brain.renameSession(id, t);
    cancelEdit();
  }

  async function confirmDelete(id: string): Promise<void> {
    await brain.deleteSession(id);
    confirmDeleteId = null;
  }
</script>

<div class="view-inner">
  <div class="page-title">Sessions</div>
  <div class="page-sub">
    Every conversation is a session. Resume any of them; only the newest {brain.config.prune_keep} are kept — the oldest is replaced when the limit is exceeded.
  </div>
  <div style="display:flex;justify-content:flex-end;align-items:center;gap:8px;margin-bottom:10px;font-size:13px;">
    <label style="display:flex;align-items:center;gap:6px;opacity:.85;">
      Sessions Limit
      <input type="number" min="1" step="1" value={limitDraft} style="width:64px;" oninput={(e) => (limitDraft = Number(e.currentTarget.value))} onkeydown={(e) => { if (e.key === "Enter") void applyLimit(); }} />
      <button class="btn sm" onclick={() => void applyLimit()}>Apply</button>
    </label>
    <span style="opacity:.5;">{brain.sessions.length} saved</span>
  </div>
  <div class="panel" style="padding:0;overflow:hidden;">
    {#if brain.sessions.length === 0}
      <div style="padding:18px;color:var(--faint);text-align:center;">No sessions yet — start one from the workspace.</div>
    {:else}

      <table>
        <thead>
          <tr>
            <th>Title</th>
            <th>Model</th>
            <th>Msgs</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          {#each paged as s (s.id)}
            <tr>
              <td>
                {#if editingId === s.id}
                  <span class="tedit">
                    <input
                      type="text"
                      bind:value={titleDraft}
                      style="font-size:13px;padding:4px 7px;width:220px;"
                      onkeydown={(e) => {
                        if (e.key === "Enter") void commitEdit(s.id);
                        if (e.key === "Escape") cancelEdit();
                      }}
                    />
                    <button class="btn sm primary" onclick={() => void commitEdit(s.id)}>OK</button>
                    <button class="btn sm" onclick={cancelEdit} aria-label="Cancel rename"><Icon name="x" size={13} /></button>
                  </span>
                {:else}
                  <button
                    type="button"
                    class="tcell"
                    title="click to rename"
                    onclick={() => startEdit(s.id, s.title || s.id)}
                  >{s.title || s.id}</button>
                {/if}
              </td>
              <td class="mono">{s.model ?? "—"}</td>
              <td>{s.message_count}</td>
              <td>
                {#if confirmDeleteId === s.id}
                  <span class="del-confirm">
                    <span class="del-q">delete?</span>
                    <button class="btn sm danger" onclick={() => void confirmDelete(s.id)}>YES</button>
                    <button class="btn sm" onclick={() => (confirmDeleteId = null)}>NO</button>
                  </span>
                {:else}
                  <button class="btn sm primary" onclick={() => void brain.openSession(s.id)}>Resume</button>
                  <button class="btn sm danger" style="margin-left:6px;" onclick={() => { confirmDeleteId = s.id; editingId = null; }}>Del</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>
  <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:12px;font-size:12px;">
    <button class="btn primary" onclick={() => void brain.newSession()}>New session</button>
    <button class="btn">Export…</button>
    <span style="opacity:.55;margin:0 4px;">|</span>
    <select bind:value={pageSize} onchange={() => (page = 1)} title="Rows per page" style="width:auto;font-size:12px;">
      {#each PAGE_SIZES as n (n)}<option value={n}>{n}</option>{/each}
    </select>
    <button class="btn sm" disabled={page <= 1} onclick={() => (page = page - 1)} aria-label="Previous page"><Icon name="chevron-left" size={14} /></button>
    {#each pageList as n (n)}<button class="btn sm" class:primary={n === page} onclick={() => (page = n)}>{n}</button>{/each}
    <button class="btn sm" disabled={page >= totalPages} onclick={() => (page = page + 1)} aria-label="Next page"><Icon name="chevron-right" size={14} /></button>
  </div>
</div>
