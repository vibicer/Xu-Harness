<script lang="ts">
  import { brain } from "../../store.svelte";
  import Icon from "../Icon.svelte";
  /** Messages queued while a turn was running. Every row carries its own three
   *  actions — edit, send now, remove — so a queued message is never a bubble
   *  you can only watch. Reads `brain.queued` directly (store-owned region, no
   *  prop drilling); only `onedit` comes from the composer, which owns the box. */
  type Queued = { text: string; images?: string[]; id?: string; key?: string };

  let { onedit }: { onedit: (q: Queued) => void } = $props();

  /** Pull this message out of the queue and back into the composer. Waits for
   *  the server to confirm the drop: when the brain already spliced the row
   *  into the live turn it has reached the model, and handing it back would
   *  send it twice. */
  async function edit(q: Queued): Promise<void> {
    if (await brain.cancelQueued(q)) onedit(q);
  }
</script>

{#if brain.queued.length}
  <div class="queued-preview">
    <div class="queued-bar">
      <span class="queued-bar-label">queued {brain.queued.length}</span>
    </div>
    {#each brain.queued as q, i (i)}
      <div class="queued-msg" title="queued — Xu picks it up at the next tool round">
        <span class="queued-tag">queued</span>
        {#if q.images?.length}{#each q.images as img, j (j)}<img class="queued-thumb" src={img} alt="queued attachment" />{/each}{/if}
        {#if q.text}<span class="queued-text">{q.text}</span>{/if}
        <div class="queued-acts">
          <button
            class="queued-act is-edit"
            title="edit — take it out of the queue and back into the box"
            disabled={!q.id}
            aria-label="edit this queued message"
            onclick={() => void edit(q)}><Icon name="square-pen" size={12} /></button>
          <button
            class="queued-act is-send"
            title="send now — interrupt the running turn; this message runs first, the rest follow in order"
            disabled={!q.id}
            aria-label="send this queued message now"
            onclick={() => void brain.steerQueued(q)}><Icon name="arrow-up" size={13} /></button>
          <button
            class="queued-act is-x"
            title="remove this queued message"
            disabled={!q.id}
            aria-label="remove this queued message"
            onclick={() => void brain.cancelQueued(q)}><Icon name="x" size={12} /></button>
        </div>
      </div>
    {/each}
  </div>
{/if}
