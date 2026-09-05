<script lang="ts">
  /** `<Icon name="settings" />` — the only way icons enter the UI.
   *
   *  Thin on purpose: defaults live in `setLucideProps` (App.svelte) and the
   *  glyph choice lives in `icons.ts`, so this component is just the lookup.
   *  Any Lucide prop (`size`, `strokeWidth`, `class`, `aria-label`…) passes
   *  through. Without an a11y prop Lucide marks the svg `aria-hidden`, which is
   *  what an icon beside a label wants. */
  import type { LucideProps } from "@lucide/svelte";
  import { ICONS, type IconName } from "../icons";

  let { name, ...rest }: { name: IconName } & LucideProps = $props();

  const Glyph = $derived(ICONS[name]);
</script>

<Glyph {...rest} />
