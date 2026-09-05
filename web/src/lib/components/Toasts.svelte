<script module lang="ts">
  // Module-level universal reactivity so any layout action can call toast().
  let toasts = $state<{ id: number; text: string }[]>([]);
  let seq = 0;
  export function toast(text: string): void {
    const id = ++seq;
    toasts = [...toasts, { id, text }];
    setTimeout(() => {
      toasts = toasts.filter((t) => t.id !== id);
    }, 1800);
  }
</script>

<script lang="ts"></script>

{#if toasts.length}
  <div class="toasts" aria-live="polite">
    {#each toasts as t (t.id)}
      <div class="toast" role="status">{t.text}</div>
    {/each}
  </div>
{/if}