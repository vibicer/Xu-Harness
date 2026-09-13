<script lang="ts">
  import { tick } from "svelte";
  import { brain } from "../store.svelte";
  import Icon from "./Icon.svelte";

  /** About modal + avatar picker, shared by every layout (Dock's brand-mark
   *  and the classic sidebar brand open the same panel). */
  let { open, onclose }: { open: boolean; onclose: () => void } = $props();

  let avatarInput: HTMLInputElement | undefined = $state(undefined);
  let avatarPreview: string | null = $state(null);
  let version = $state<string>("");
  /** The dialog itself, and whatever had focus when it opened. The panel is
   *  launched from a brand-mark that keeps focus, so Escape and Tab have to be
   *  handled on the window and focus moved in by hand. */
  let panelEl: HTMLDivElement | null = $state(null);
  let restoreFocus: HTMLElement | null = null;

  /** Power controls. A destructive action is two-step (arm → confirm) rather
   *  than a browser `confirm()`: the modal is already a dialog, and an inline
   *  YES/NO keeps the whole interaction inside it. `powerBusy` locks the row
   *  while the RPC is in flight; `powerNote` reports the outcome in place,
   *  since the default layout has no toast stack. */
  let powerArmed = $state<"restart" | "shutdown" | null>(null);
  let powerBusy = $state<"restart" | "shutdown" | null>(null);
  let powerNote = $state<string>("");

  $effect(() => {
    if (!open) return;
    let alive = true;
    brain.client
      .call<{ version: string }>("app.info")
      .then((i) => { if (alive) version = i.version; })
      .catch(() => { /* keep last */ });
    return () => { alive = false; };
  });

  // Move focus into the dialog on open, hand it back to the trigger on close.
  $effect(() => {
    if (!open) return;
    restoreFocus = document.activeElement as HTMLElement | null;
    void tick().then(() => panelEl?.focus());
    return () => {
      restoreFocus?.focus?.();
      restoreFocus = null;
    };
  });

  /** Escape closes; Tab cycles inside the dialog instead of walking the page
   *  behind it. On the window, not the backdrop: keydown targets whatever has
   *  focus, and nothing inside the panel has it until we put it there. */
  function onWindowKey(e: KeyboardEvent): void {
    if (!open) return;
    if (e.key === "Escape") { e.preventDefault(); close(); return; }
    if (e.key !== "Tab" || !panelEl) return;
    const items = panelEl.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (items.length === 0) { e.preventDefault(); panelEl.focus(); return; }
    const first = items[0];
    const last = items[items.length - 1];
    const active = document.activeElement;
    if (e.shiftKey && (active === first || active === panelEl)) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && (active === last || !panelEl.contains(active))) { e.preventDefault(); first.focus(); }
  }

  function close(): void {
    avatarPreview = null;
    // Drop any half-armed power confirmation so reopening never resumes one.
    powerArmed = null;
    powerNote = "";
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

  /** Ask the brain to stop, or to stop and relaunch itself. The brain flushes
   *  the RPC reply *before* it tears down (see `app.shutdown` in the contract),
   *  so the promise resolves on success instead of rejecting on a closed
   *  socket; a reconnect then happens on its own. */
  async function runPower(action: "restart" | "shutdown"): Promise<void> {
    powerArmed = null;
    powerBusy = action;
    powerNote = action === "restart" ? "Restarting brain…" : "Shutting down…";
    try {
      await brain.client.call(action === "restart" ? "app.restart" : "app.shutdown");
      powerNote = action === "restart"
        ? "Brain restarting — it reconnects in a moment."
        : "Brain stopped. Run `xu start` to bring it back.";
    } catch (err) {
      powerNote = `Could not ${action === "restart" ? "restart" : "shut down"}: ${
        err instanceof Error ? err.message : String(err)
      }`;
    } finally {
      powerBusy = null;
    }
  }
</script>

<svelte:window onkeydown={onWindowKey} />

{#if open}
  <!-- svelte-ignore a11y_click_events_have_key_events, a11y_no_noninteractive_element_interactions -->
  <div class="about-backdrop" onclick={close} aria-hidden="true"></div>
  <div class="about-panel" bind:this={panelEl} role="dialog" aria-modal="true" aria-labelledby="about-title" tabindex="-1">
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
        <div class="about-ver">{version ? `v${version}` : "…"} · Svelte 5 SPA · local brain</div>
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

      <!-- Harness power, on the same line as the avatar actions. Icon-only so
           it never wraps to its own row; the label is the hover tooltip (and
           the aria-label for screen readers). Destructive, so it is two-step:
           arm → YES/NO, never a single stray click that kills a running turn. -->
      <div class="about-power" role="group" aria-label="Xu harness power">
        {#if powerArmed}
          <button
            type="button"
            class="about-btn about-btn-danger"
            disabled={powerBusy !== null}
            aria-label={powerArmed === "restart" ? "Confirm restart" : "Confirm shut down"}
            title={powerArmed === "restart" ? "Confirm restart" : "Confirm shut down"}
            onclick={() => { if (powerArmed) void runPower(powerArmed); }}
          >YES</button>
          <button
            type="button"
            class="about-btn"
            disabled={powerBusy !== null}
            aria-label="Cancel"
            title="Cancel"
            onclick={() => (powerArmed = null)}
          >NO</button>
        {:else}
          <button
            type="button"
            class="about-btn about-power-btn"
            disabled={powerBusy !== null}
            aria-label="Restart the Xu harness (brain daemon)"
            title="Restart the Xu harness (brain daemon)"
            onclick={() => (powerArmed = "restart")}
          ><Icon name="refresh-cw" size={14} /></button>
          <button
            type="button"
            class="about-btn about-power-btn about-btn-danger"
            disabled={powerBusy !== null}
            aria-label="Shut down the Xu harness (brain daemon)"
            title="Shut down the Xu harness (brain daemon)"
            onclick={() => (powerArmed = "shutdown")}
          ><Icon name="power" size={14} /></button>
        {/if}
      </div>
    </div>
    {#if powerNote}
      <div class="about-power-note" role="status">{powerNote}</div>
    {/if}
  </div>
{/if}
