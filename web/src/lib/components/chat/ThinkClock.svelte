<script lang="ts">
  /** The Ledger thinking counter, isolated in its own component on purpose: the
   *  old dancer was a 160ms ticker whose glyph was passed down as a prop, so
   *  every tick re-rendered the whole transcript. Here the tick re-renders this
   *  one span. Reads a real start time handed down by the caller rather than
   *  its own mount time, so switching views and back mid-turn keeps counting
   *  instead of restarting at zero. */
  import { clock } from "../../format";

  let { startMs }: { startMs?: number } = $props();

  let now = $state(Date.now());
  $effect(() => {
    if (startMs === undefined) return; // not the growing segment — nothing to count
    const id = setInterval(() => (now = Date.now()), 100);
    return () => clearInterval(id);
  });

  const text = $derived(startMs === undefined ? "" : clock(Math.max(0, (now - startMs) / 1000)));
</script>

{#if text}<span class="lg-meta">{text}</span>{/if}
