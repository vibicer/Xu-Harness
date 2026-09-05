<script lang="ts">
  import { brain } from "../store.svelte";
  import Icon from "./Icon.svelte";

  /** About modal + avatar picker, shared by every layout (Dock's brand-mark
   *  and the classic sidebar brand open the same panel). */
  let { open, onclose }: { open: boolean; onclose: () => void } = $props();

  let avatarInput: HTMLInputElement | undefined = $state(undefined);
  let avatarPreview: string | null = $state(null);

  function close(): void {
    avatarPreview = null;
    onclose();
  }

  /** Read a chosen image/GIF → data URL; preview, apply on confirm. */
  function onAvatarPick(e: Event): void {
    const file = (e.target as HTMLInputElement).files?.[0];
    if (!file) return;
    if (!file.type.startsWith("image/")) { alert("Pick an image file (PNG/JPEG/GIF/WebP)."); return; }
    if (file.size > 2 * 1024 * 1024) { alert("Image too large — keep it under 2 MB."); return; }
    const reader = new FileReader();
    reader.onload = () => { avatarPreview = String(reader.result); };
    reader.readAsDataURL(file);
  }

  function saveAvatar(): void {
    brain.setAvatar(avatarPreview);
    close();
  }

  function clearAvatar(): void { brain.setAvatar(null); avatarPreview = null; }

  function avatarSource(): string {
    return avatarPreview ?? brain.avatar ?? "";
  }
</script>

{#if open}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
  <div class="about-backdrop" onclick={close} onkeydown={(e) => e.key === "Escape" && close()} aria-hidden="true"></div>
  <div class="about-panel" role="dialog" aria-modal="true" aria-labelledby="about-title">
    <button type="button" class="about-close" onclick={close} aria-label="Close"><Icon name="x" size={15} /></button>
    <div class="about-preview-wrap">
      {#if avatarSource()}
        <img class="about-avatar" src={avatarSource()} alt="Xu avatar" />
      {:else}
        <div class="about-avatar about-avatar-placeholder">X</div>
      {/if}
    </div>
    <div class="about-head">
      <div>
        <div id="about-title" class="about-title">XU</div>
        <div class="about-ver">v0.1.0 · Svelte 5 SPA · local brain</div>
      </div>
    </div>
    <div class="about-body">
      <p><b>Xu</b> — a lightweight personal AI harness.</p>
      <p>Shell: Svelte 5 webui · Brain: Python 3.12 daemon · Contract: localhost WebSocket + JSON-RPC.</p>
      <p>
        This app is served on <code>http://127.0.0.1:1421</code>.
      </p>
    </div>
    <div class="about-actions">
      <input
        bind:this={avatarInput}
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp"
        class="about-file"
        onchange={onAvatarPick}
      />
      <button type="button" class="about-btn" onclick={() => avatarInput?.click()} title="PNG/JPEG/GIF/WebP, under 2 MB">
        Change avatar
      </button>
      {#if brain.avatar || avatarPreview}
        <button type="button" class="about-btn" onclick={clearAvatar}>Remove avatar</button>
      {/if}
      {#if avatarPreview}
        <button type="button" class="about-btn about-btn-primary" onclick={saveAvatar}>Apply avatar</button>
      {/if}
    </div>
  </div>
{/if}
