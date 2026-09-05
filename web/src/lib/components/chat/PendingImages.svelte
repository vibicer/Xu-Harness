<script lang="ts">
  import Icon from "../Icon.svelte";
  /** Attached-image previews shown between the transcript and the composer.
   *  The data URLs are owned by the composer's send pipeline; this only
   *  renders them and forwards removals. */
  let {
    images,
    onremove,
  }: { images: string[]; onremove: (idx: number) => void } = $props();
</script>

{#if images.length}
  <div class="att-preview">
    {#each images as img, i (img)}
      <div class="att-item">
        <img class="att-thumb" src={img} alt="pending attachment" />
        <button class="att-x" title="remove" aria-label="remove attachment" onclick={() => onremove(i)}><Icon name="x" size={12} /></button>
      </div>
    {/each}
  </div>
{/if}
