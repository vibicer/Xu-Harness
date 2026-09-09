<script lang="ts">
  import { tick } from "svelte";
  import { brain } from "../store.svelte";
  import PendingImages from "./chat/PendingImages.svelte";
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

  /** Frames a converging pin may spend chasing a settling height. A row that
   *  just mounted is still at its content-visibility estimate, and a big turn
   *  keeps inflating long after half a second of frames — the budget has to
   *  outlast the settling, not just the switch. */
  const MAX_PIN_FRAMES = 120;

  function scrollToBottom(): void {
    if (!scrollEl) return;
    // Converging pin: with content-visibility rows the transcript keeps
    // drifting for a few frames after a switch (estimate → real height as
    // rows near the viewport render), so a single pass lands short of the
    // newest response on long histories. Pin every frame until the height
    // settles (bounded), and stop chasing if the user scrolls away.
    let last = -1;
    let stable = 0;
    let frames = 0;
    const pass = (): void => {
      if (!scrollEl) return;
      scrollEl.scrollTop = scrollEl.scrollHeight;
      if (thinkingBody) thinkingBody.scrollTop = thinkingBody.scrollHeight;
      const h = scrollEl.scrollHeight;
      if (h === last) stable += 1;
      else {
        stable = 0;
        last = h;
      }
      if (stable < 2 && ++frames < MAX_PIN_FRAMES && followLiveEdge) requestAnimationFrame(pass);
    };
    pass();
  }


  // Follow the live edge, tracked from real scroll events only. Measuring
  // on every streaming delta instead forced a full-transcript layout per
  // chunk (nearBottom reads scrollHeight) — that layout thrash is what made
  // long sessions lag while streaming. A scroll event fires on user scrolls
  // and on our own pins (which land at the bottom and keep following on);
  // content growth alone fires nothing, so follow state stays truthful.
  let followLiveEdge = $state(true);
  function onScroll(): void {
    followLiveEdge = nearBottom();
  }

  // Pin to the bottom at most once per frame: bursts of deltas coalesce into
  // one write, so the forced layout that a pin costs happens once per frame,
  // not once per delta.
  let pinQueued = false;
  let lastSessionId: string | undefined;
  /** True while a draft was streaming on the previous run of this effect, so
   *  the run that finds it gone is the end of a turn. */
  let streaming = false;
  /** Row count on the previous run, so the reload that lands the finished
   *  turn's row is caught even when it arrives after the draft is gone. */
  let lastLen = 0;
  $effect(() => {
    void brain.session?.id;
    const len = brain.messages.length;
    void brain.draft;
    void brain.draft?.steps.length;
    void brain.draft?.reasoning.length;
    void brain.approvals.length;
    const switched = brain.session?.id !== lastSessionId;
    // The end of a turn has two edges and the pin has to catch either: the
    // draft going away, and the authoritative row landing with the reload —
    // which can be the next frame or a second later.
    const turnEnded = streaming && !switched && brain.draft === null;
    const rowLanded = !switched && len !== lastLen;
    streaming = brain.draft !== null;
    lastLen = len;
    // Session switch: the transcript was replaced wholesale — rearm following
    // and pin hard (the double pass in scrollToBottom catches late
    // font/code reflow on the fresh history).
    if (switched) {
      lastSessionId = brain.session?.id;
      followLiveEdge = true;
      void tick().then(() => scrollToBottom());
      return;
    }
    // Only pin when the user was already following the stream; scrolling up
    // means "let me read", so don't yank them back.
    if (!followLiveEdge) return;
    // A finished turn is not another streaming delta: finalizeTurn dropped the
    // draft and mounted the authoritative row in its place, and that row is at
    // its intrinsic-size estimate right now — it inflates to its real height
    // over the frames AFTER this effect. A single write would pin to a height
    // that is about to be wrong and leave the view at the top of the transcript
    // the moment the response ends, so converge like a switch does.
    if (turnEnded || rowLanded) {
      void tick().then(() => scrollToBottom());
      return;
    }
    if (pinQueued) return;
    pinQueued = true;
    requestAnimationFrame(() => {
      pinQueued = false;
      if (!scrollEl) return;
      scrollEl.scrollTop = scrollEl.scrollHeight;
      if (thinkingBody) thinkingBody.scrollTop = thinkingBody.scrollHeight;
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
  <div id="messages" bind:this={scrollEl} onscroll={onScroll}>
    {#if brain.isCompressing(brain.session?.id)}
      <div class="compressing-banner" role="status" aria-live="polite">
        <span class="compressing-spinner" aria-hidden="true"></span>
        <span>compressing history…</span>
      </div>
    {/if}
    <Transcript {dancer} working={thinking} onsub={(id) => void sub.open(id)} bind:thinkingBody />
  </div>

  <PendingImages images={pendingImages} onremove={removeImage} />
  <!-- QueuedTurns now lives inside Composer's #composer-wrap so the todo
       badge floats above it instead of colliding. -->
  <SubRunModal {sub} {dancer} idle={DANCE[0]} />
  <Lightbox />
  <Composer bind:composerEl bind:pendingImages bind:busy onsend={scrollToBottom} />
</div>
