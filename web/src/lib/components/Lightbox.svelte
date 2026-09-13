<script lang="ts">
  import { tick } from "svelte";
  import { lightbox, closeImage } from "../lightbox.svelte";
  /** Full-screen viewer for images in the transcript. Mounted once by the
   *  Chat shell; any image opens it via openImage(). Click backdrop or ESC
   *  to close. `position: fixed` matches .sub-modal, so the body zoom and
   *  scroll containers don't move it. */

  let boxEl: HTMLDivElement | null = $state(null);
  let restoreFocus: HTMLElement | null = null;
  let wasOpen = false;

  // Move focus onto the overlay when it opens, hand it back to the thumbnail
  // on close. A swap from one image to another keeps the same overlay.
  $effect(() => {
    const src = lightbox.src;
    if (src && !wasOpen) {
      wasOpen = true;
      restoreFocus = document.activeElement as HTMLElement | null;
      void tick().then(() => boxEl?.focus());
    } else if (!src && wasOpen) {
      wasOpen = false;
      restoreFocus?.focus?.();
      restoreFocus = null;
    }
  });

  /** ESC closes. Tab is trapped: the overlay has nothing focusable inside, so
   *  without this focus walks the transcript behind it. */
  function onWindowKey(e: KeyboardEvent): void {
    if (!lightbox.src) return;
    if (e.key === "Escape") { e.preventDefault(); closeImage(); }
    else if (e.key === "Tab") { e.preventDefault(); boxEl?.focus(); }
  }
</script>

<svelte:window onkeydown={onWindowKey} />

{#if lightbox.src}
  <!-- click anywhere closes (incl. on the image): the whole overlay reads as
       zoom-out. ESC handled on the window listener above. -->
  <!-- svelte-ignore a11y_click_events_have_key_events -->
  <div
    class="lightbox"
    bind:this={boxEl}
    role="dialog"
    aria-modal="true"
    aria-label={lightbox.alt || "image viewer"}
    tabindex="-1"
    onclick={closeImage}
  >
    <img src={lightbox.src} alt={lightbox.alt || "image from Xu"} />
    {#if lightbox.alt}<div class="lightbox-cap">{lightbox.alt}</div>{/if}
  </div>
{/if}
