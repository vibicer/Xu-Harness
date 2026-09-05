<script lang="ts">
  import { lightbox, closeImage } from "../lightbox.svelte";
  /** Full-screen viewer for images in the transcript. Mounted once by the
   *  Chat shell; any image opens it via openImage(). Click backdrop or ESC
   *  to close. `position: fixed` matches .sub-modal, so the body zoom and
   *  scroll containers don't move it. */
</script>

<svelte:window
  onkeydown={(e) => {
    if (lightbox.src && e.key === "Escape") closeImage();
  }}
/>

{#if lightbox.src}
  <!-- backdrop: click-to-close. ESC handled on the window listener above.
       Focus stays in place: the dialog is display-only, no focusable content. -->
  <!-- svelte-ignore a11y_click_events_have_key_events a11y_no_noninteractive_element_interactions a11y_interactive_supports_focus -->
  <div
    class="lightbox"
    role="dialog"
    aria-modal="true"
    aria-label={lightbox.alt || "image viewer"}
    onclick={(e) => e.target === e.currentTarget && closeImage()}
  >
    <img src={lightbox.src} alt={lightbox.alt || "image from Xu"} />
    {#if lightbox.alt}<div class="lightbox-cap">{lightbox.alt}</div>{/if}
  </div>
{/if}