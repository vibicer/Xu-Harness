<script lang="ts">
  import { tick } from "svelte";
  import ToolChip from "../ToolChip.svelte";
  import Icon from "../Icon.svelte";
  import { runDuration, runLabel } from "../../subrun-tabs";
  import ThinkClock from "./ThinkClock.svelte";
  import type { SubRunView } from "../../subrun-view.svelte";
  /** Sub-agent activity modal — markup only; behaviour (which run, polling,
   *  live-edge scroll) lives in subrun-view.svelte.ts and is created by the
   *  Chat shell, which also opens runs from transcript tool chips. */
  let { sub }: { sub: SubRunView } = $props();

  /** The dialog and the element that had focus when it opened. The panel is
   *  opened from a transcript chip that keeps focus, so Escape/Tab are handled
   *  on the window and focus is moved in by hand. */
  let modalEl: HTMLDivElement | null = $state(null);
  let restoreFocus: HTMLElement | null = null;

  // Focus the dialog when a run opens, hand focus back when it closes. Runs
  // change id while open (tab switches) — only the closed↔open edge moves
  // focus.
  $effect(() => {
    if (sub.id) {
      if (!restoreFocus) restoreFocus = document.activeElement as HTMLElement | null;
      void tick().then(() => modalEl?.focus());
    } else if (restoreFocus) {
      restoreFocus.focus?.();
      restoreFocus = null;
    }
  });

  /** Escape closes; Tab cycles inside the dialog instead of walking the page
   *  behind it. */
  function onWindowKey(e: KeyboardEvent): void {
    if (!sub.id) return;
    if (e.key === "Escape") { e.preventDefault(); sub.close(); return; }
    if (e.key !== "Tab" || !modalEl) return;
    const items = modalEl.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (items.length === 0) { e.preventDefault(); modalEl.focus(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === modalEl)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (active === last || !modalEl.contains(active))) { e.preventDefault(); first.focus(); }
  }

  /** WAI-ARIA tablist: arrows (and Home/End) move selection, and focus follows
   *  it. Roving tabindex keeps one tab in the page tab order. */
  function onTabKey(e: KeyboardEvent, idx: number): void {
    const tabs = sub.tabs;
    if (tabs.length < 2) return;
    let next = -1;
    if (e.key === "ArrowRight" || e.key === "ArrowDown") next = (idx + 1) % tabs.length;
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") next = (idx - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = tabs.length - 1;
    if (next < 0) return;
    e.preventDefault();
    sub.select(tabs[next].id);
    document.getElementById(`sub-tab-${tabs[next].id}`)?.focus();
  }
</script>

<svelte:window onkeydown={onWindowKey} />

{#if sub.id}
<!-- svelte-ignore a11y_click_events_have_key_events -->
<div
class="sub-modal"
bind:this={modalEl}
role="dialog"
aria-modal="true"
aria-label="sub-agent activity"
tabindex="-1"
onclick={(e) => e.target === e.currentTarget && sub.close()}
>
<div class="sub-panel">
<div class="sub-head">
<span class="sub-title">
SUB-AGENT ACTIVITY
{#if sub.run}<em>{sub.run.child}</em>{/if}
</span>
{#if sub.run}
<span class="sub-badge {sub.run.status}">
{sub.run.status === "running" ? "RUNNING" : sub.run.status.toUpperCase()} · {sub.duration(sub.run)}
</span>
{/if}
<button class="sub-close" title="close" aria-label="close" onclick={() => sub.close()}><Icon name="x" size={15} /></button>
</div>

{#if sub.tabs.length > 1}
<div class="sub-tabs" role="tablist" aria-label="sub-agents in this fan-out">
{#each sub.tabs as t, i (t.id)}
<button
id={`sub-tab-${t.id}`}
class="sub-tab {t.status}"
class:active={t.id === sub.id}
role="tab"
aria-selected={t.id === sub.id}
aria-controls="sub-tabpanel"
tabindex={t.id === sub.id ? 0 : -1}
title="{runLabel(t)} · {t.status}"
onclick={() => sub.select(t.id)}
onkeydown={(e) => onTabKey(e, i)}
>
<span class="sub-tab-dot"></span>{runLabel(t)}
</button>
{/each}
</div>
{/if}

<!-- The tabpanel id is on whichever body state is mounted — only one renders
     at a time, so the tabs' aria-controls always resolves. -->
{#if sub.loading}
<div class="sub-empty" id="sub-tabpanel" role="tabpanel" aria-labelledby={`sub-tab-${sub.id}`}>loading…</div>
{:else if sub.error}
<div class="sub-empty err" id="sub-tabpanel" role="tabpanel" aria-labelledby={`sub-tab-${sub.id}`}>{sub.error}</div>
{:else if sub.run}
{@const run = sub.run}
<div class="sub-body" id="sub-tabpanel" role="tabpanel" aria-labelledby={`sub-tab-${sub.id}`} {@attach sub.follow}>
<div class="sub-section">
<div class="sub-label">TASK</div>
<div class="sub-prompt">{run.prompt}</div>
</div>

<div class="sub-section">
<div class="sub-label">STEPS <span class="sub-count">{run.steps.length}</span></div>
{#if run.steps.length === 0}
<div class="sub-empty">no steps yet…</div>
{:else}
{#each run.steps as st, k (k)}
{@const live = run.status === "running" && k === run.steps.length - 1}
{#if st.kind === "tool"}
<ToolChip step={st} startOpen />
{:else if st.kind === "reasoning"}
<!-- Ledger row, same as the transcript's. A sub-agent run carries real
     start/finish times, so the settled state can show how long it thought —
     unlike a main-session history row, which only knows the reasoning text.
     `.lg-body` is its own scroll box, so it needs its own live-edge follower:
     the panel's follower cannot reach inside it. -->
<div class="lg" class:live>
<div class="lg-row">
<span class="lg-led" aria-hidden="true"></span>
<span>{live ? "THINKING" : "THOUGHT"}</span>
<span class="lg-tail">{#if live && run.started}<ThinkClock startMs={run.started * 1000} />{:else}<span class="lg-meta">{runDuration(run)}</span>{/if}</span>
</div>
<div class="lg-rail" aria-hidden="true"><i></i></div>
<div class="lg-body" {@attach sub.follow}>{st.text}</div>
</div>
{:else}
<div class="sub-text">{st.text}</div>
{/if}
{/each}
{/if}
</div>

{#if run.result}
<div class="sub-section">
<div class="sub-label">RESULT</div>
<div class="sub-result">{run.result}</div>
</div>
{/if}
</div>
{/if}
</div>
</div>
{/if}
