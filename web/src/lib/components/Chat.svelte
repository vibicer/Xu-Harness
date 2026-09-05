<script lang="ts">
  import { tick } from "svelte";
  import { brain } from "../store.svelte";
  import PendingImages from "./chat/PendingImages.svelte";
  import QueuedTurns from "./chat/QueuedTurns.svelte";
  import Composer from "./chat/Composer.svelte";
  import SubRunModal from "./chat/SubRunModal.svelte";
  import Lightbox from "./Lightbox.svelte";
  import Transcript from "./chat/Transcript.svelte";
  import { createSubRunView } from "../subrun-view.svelte";
  let scrollEl: HTMLDivElement;

  // ---- composer handle: focus targets (state owned by the composer) ----
  let composerEl: HTMLTextAreaElement | null = $state(null);
  let busy = $state(false);
  // Attached image data URLs, sent with the next message. Added by the
  // composer's attach pipeline; rendered by the strip below the transcript.
  let pendingImages: string[] = $state([]);

  // Focus composer on mount / view change, and when any printable key is
  // pressed while focus isn't already in an editable element.
  $effect(() => {
    void brain.view; // refocus when switching views
    void brain.messages; // refocus after a session (re)loads
    if (brain.view === "workspace") void tick().then(() => composerEl?.focus());
  });

  $effect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const t = e.target as HTMLElement | null;
      const inEditable =
        t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA" || t.tagName === "SELECT" || t.isContentEditable);
      if (inEditable) return;
      if (e.key.length === 1 || e.key === "Enter") {
        composerEl?.focus();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  function nearBottom(): boolean {
    if (!scrollEl) return true;
    // within ~80px of the bottom counts as "following the stream"
    return scrollEl.scrollHeight - scrollEl.scrollTop - scrollEl.clientHeight < 80;
  }

  function scrollToBottom(): void {
    if (!scrollEl) return;
    scrollEl.scrollTop = scrollEl.scrollHeight;
    // second pass after paint catches late markdown/code reflow (e.g. on resume)
    requestAnimationFrame(() => {
      if (scrollEl) scrollEl.scrollTop = scrollEl.scrollHeight;
    });
  }

  // Capture whether the user was following the live edge BEFORE Svelte
  // applies the new message/tool/approval DOM. Checking after the update
  // sees the new height at the old scrollTop and incorrectly reports that
  // the user scrolled away.
  let followLiveEdge = $state(true);
  $effect.pre(() => {
    void brain.session?.id;
    void brain.messages.length;
    void brain.draft;
    void brain.draft?.steps.length;
    void brain.draft?.reasoning.length;
    void brain.approvals.length;
    followLiveEdge = nearBottom();
  });

  $effect(() => {
    void brain.session?.id;
    void brain.messages.length;
    void brain.draft;
    void brain.draft?.steps.length;
    void brain.draft?.reasoning.length;
    void brain.approvals.length;
    // Only pin when the user was already following the stream; scrolling up
    // means "let me read", so don't yank them back.
    if (!followLiveEdge) return;
    void tick().then(() => {
      scrollToBottom();
      if (thinkingBody) thinkingBody.scrollTop = thinkingBody.scrollHeight;
      requestAnimationFrame(() => scrollToBottom());
    });
  });
  const thinking = $derived(busy || brain.isBusy(brain.session?.id ?? "") || brain.draft !== null);

  // dancing stick figure for the live "thinking" label (tool chips own their
  // own spinner — see ToolChip.svelte)
  const DANCE = ["╰(°▽°)╯", "┐(°▽°)┌", "┌(°▽°)┐", "╮(°▽°)╭"];
  let danceFrame = $state(0);
  $effect(() => {
    if (!thinking) return;
    const d = setInterval(() => (danceFrame = (danceFrame + 1) % DANCE.length), 160);
    return () => clearInterval(d);
  });
  const dancer = $derived(DANCE[danceFrame]);

  // live reasoning body ref: keep its inner scroll following the stream
  let thinkingBody: HTMLDivElement | null = $state(null);

  // ---- sub-agent activity modal (delegate chips) ----
  // Behaviour (which run, which sibling tabs, polling, live-edge scroll) lives
  // in subrun-view.svelte.ts so all four shells share one copy; the modal
  // markup itself lives in chat/SubRunModal.svelte.
  const sub = createSubRunView();

  function removeImage(idx: number): void {
    pendingImages = pendingImages.filter((_, i) => i !== idx);
  }
</script>

<div id="chat-main" class:working={thinking}>
  <div id="messages" bind:this={scrollEl}>
    {#if brain.isCompressing(brain.session?.id)}
      <div class="compressing-banner" role="status" aria-live="polite">
        <span class="compressing-spinner" aria-hidden="true"></span>
        <span>compressing history…</span>
      </div>
    {/if}
    <Transcript {dancer} working={thinking} onsub={(id) => void sub.open(id)} bind:thinkingBody />
  </div>

  <PendingImages images={pendingImages} onremove={removeImage} />
  <QueuedTurns />
  <SubRunModal {sub} {dancer} idle={DANCE[0]} />
  <Lightbox />
  <Composer bind:composerEl bind:pendingImages bind:busy onsend={scrollToBottom} />
</div>
