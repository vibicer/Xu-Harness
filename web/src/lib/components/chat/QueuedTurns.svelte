<script lang="ts">
  import { brain } from "../../store.svelte";
  import Icon from "../Icon.svelte";
  /** Messages queued while a turn was running, with cancel buttons. Reads
   *  `brain.queued` directly — no prop drilling (store-owned region). */
</script>

{#if brain.queued.length}
  <div class="queued-preview">
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
