<script lang="ts">
  import ToolChip from "../ToolChip.svelte";
  import Icon from "../Icon.svelte";
  import { runLabel } from "../../subrun-tabs";
  import type { SubRunView } from "../../subrun-view.svelte";
  /** Sub-agent activity modal — markup only; behaviour (which run, polling,
   *  live-edge scroll) lives in subrun-view.svelte.ts and is created by the
   *  Chat shell, which also opens runs from transcript tool chips. */
  let {
    sub,
    /** live dancer glyph, cycled by the shell while a turn streams */
    dancer,
    /** static glyph for finished reasoning steps */
    idle,
  }: { sub: SubRunView; dancer: string; idle: string } = $props();
</script>

{#if sub.id}
<div
class="sub-modal"
role="dialog"
aria-modal="true"
aria-label="sub-agent activity"
tabindex="-1"
onclick={(e) => e.target === e.currentTarget && sub.close()}
onkeydown={(e) => e.key === "Escape" && sub.close()}
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
{#each sub.tabs as t (t.id)}
<button
class="sub-tab {t.status}"
class:active={t.id === sub.id}
role="tab"
aria-selected={t.id === sub.id}
title="{runLabel(t)} · {t.status}"
onclick={() => sub.select(t.id)}
>
<span class="sub-tab-dot"></span>{runLabel(t)}
</button>
{/each}
</div>
{/if}

{#if sub.loading}
<div class="sub-empty">loading…</div>
{:else if sub.error}
<div class="sub-empty err">{sub.error}</div>
{:else if sub.run}
{@const run = sub.run}
<div class="sub-body" {@attach sub.follow}>
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
<!-- Live while it is the streaming tail: `run` drives the LED label and the
     dancer cycles. `.tk-body` is its own scroll box, so it needs its own
     live-edge follower — the panel's follower cannot reach inside it. -->
<div class="think {live ? 'run' : 'ok'}">
<div class="tk-head">
<span class="tk-tool"><span class="dancer">{live ? dancer : idle}</span> thinking</span>
</div>
<div class="tk-body" {@attach sub.follow}>{st.text}</div>
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
