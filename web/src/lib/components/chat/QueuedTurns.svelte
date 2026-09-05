<script lang="ts">
  import { brain } from "../../store.svelte";
  import Icon from "../Icon.svelte";
  /** Messages queued while a turn was running, with a send-now button on the
   *  bar (interrupt the turn, queue runs in order) and per-row cancel. Reads
   *  `brain.queued` directly — no prop drilling (store-owned region). */
  const anyConfirmed = $derived(brain.queued.some((m) => m.id));
</script>

{#if brain.queued.length}
  <div class="queued-preview">
    <div class="queued-bar">
      <span class="queued-bar-label">queued {brain.queued.length}</span>
      <button
        class="queued-steer"
        title="send now — interrupt the running turn; queued messages run in order"
        disabled={!anyConfirmed}
        aria-label="send queued messages now"
        onclick={() => void brain.steerQueue()}><Icon name="arrow-up" size={13} /></button>
    </div>
    {#each brain.queued as q, i (i)}
      <div class="queued-msg" title="queued — Xu picks it up at the next tool round">
        <span class="queued-tag">queued</span>
        {#if q.images?.length}{#each q.images as img, j (j)}<img class="queued-thumb" src={img} alt="queued attachment" />{/each}{/if}
        {#if q.text}<span class="queued-text">{q.text}</span>{/if}
        <button
          class="queued-x"
          title="cancel this queued message"
          disabled={!q.id}
          aria-label="cancel this queued message"
          onclick={() => void brain.cancelQueued(q)}><Icon name="x" size={12} /></button>
      </div>
    {/each}
  </div>
{/if}