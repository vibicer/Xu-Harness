<script lang="ts">
  import { slide } from "svelte/transition";
  import { tick } from "svelte";
  import { brain } from "../../store.svelte";
  import ToolChip from "../ToolChip.svelte";
  import Icon from "../Icon.svelte";
  import { renderMarkdown } from "../../markdown";
  import { openImage } from "../../lightbox.svelte";
  import ApprovalCard from "./ApprovalCard.svelte";
  import { collapseDelegates } from "../../delegate-round";
  import {
    compactionDetail,
    compactionLabel,
    isCompactionFailure,
    contentImages,
    contentText,
    displayTurns,
    mountBatch,
    segments,
    subagentResultParts,
    armMount,
    backfillMode,
  } from "./messages";
  import type { BackfillMode } from "./messages";
  import ThinkClock from "./ThinkClock.svelte";
  import { dur } from "../../format";

  let {
    /** streaming bubble is "hot" (shell LED state) */
    working,
    /** open a sub-agent run modal (behaviour lives in the shell's subrun view) */
    onsub,
    /** the live reasoning scroll box, bound up so the shell can follow the stream */
    thinkingBody = $bindable<HTMLDivElement | null>(null),
  }: {
    working: boolean;
    onsub: (id: string) => void;
    thinkingBody: HTMLDivElement | null;
  } = $props();

  // live reasoning body ref: keep its inner scroll following the stream
  // collapsed draft timeline; a reasoning step is "live" only while it is the tail
  const draftSteps = $derived(brain.draft ? collapseDelegates(brain.draft.steps) : []);
  /* Nothing real has landed in the live bubble yet — no thought, no tool, no
     text. A layout that shows a whole-turn status *line* (simple's "Processing")
     uses this to retire the line once there is something to read; a layout that
     shows it as a border (the default LED ring) ignores it and keeps the ring. */
  const pending = $derived(draftSteps.length === 0 && !brain.draft?.notice);

  // ---- tail-first mount window ----
  // A big session carries thousands of tool steps; mounting every ToolChip in
  // one synchronous pass is what froze the shell for seconds on session
  // switch. Land the live edge first, then prepend older turns in idle
  // batches, restoring the scroll offset so the viewport never jumps.
  // Batches are bounded by BOTH turn count and total tool steps (mountBatch):
  // a turn-only bound lets "20 turns" mean 600 ToolChips and freezes again.
  const MOUNT_CHUNK = 20;
  const STEP_BUDGET = 80;
  /** Frames a reference-row hold keeps correcting for. A batch's rows enter at
   *  their `contain-intrinsic-size` estimate and inflate as they settle, so the
   *  offset read in the tick right after the prepend is already stale. */
  const HOLD_FRAMES = 6;

  let mountCount = $state(Number.POSITIVE_INFINITY);
  /** The session whose history the window was initialized for. The window
   * must arm on this session's HISTORY, not its id: a freshly opened or
   * restored tab reports the id while its messages are still empty, and
   * arming at len 0 would treat a big session as small. */
  let readyFor: string | undefined;
  let tailLocked = true; // window tracks the live edge (everything mounted)
  /** Where the reader is, measured from real scroll events only — the same
   *  rule the live-edge follow uses: content growth fires nothing, a scroll is
   *  intent. Decides whether older history mounts at all, and which correction
   *  keeps the view still while it does. */
  let backfill = $state<BackfillMode>(null);

  const allTurns = $derived(displayTurns(brain.messages));
  const turns = $derived(
    mountCount >= allTurns.length ? allTurns : allTurns.slice(-mountCount),
  );

  // Arming runs PRE-paint: on a switch the first render would otherwise read
  // the previous session's mountCount (usually fully mounted) and mount the
  // whole transcript once before this could clamp it — one full pass of a
  // 2000-step history is the freeze the window exists to remove. armMount
  // decides the size: everything when the session fits the eager budget, else
  // one step-aware batch off the tail. Sizing it per batch instead hid a 4-turn
  // session behind a 171-step turn, which read as deleted history.
  $effect.pre(() => {
    const sid = brain.session?.id;
    const len = allTurns.length;
    if (!sid || len === 0 || sid === readyFor) return;
    readyFor = sid;
    // A switch (or a refresh) lands the reader on the newest response, so the
    // window arms at the live edge — never mid-history.
    backfill = null;
    mountCount = armMount(allTurns, 2 * MOUNT_CHUNK, 2 * STEP_BUDGET);
    tailLocked = mountCount >= len;
    // …and a partial window is therefore already AT that edge, so say so
    // rather than leaving it to the sample the pin's scroll would supply.
    // `backfill === null` means "reader is mid-window, mount nothing", so a
    // window that never scrolls — a transcript shorter than the viewport —
    // could never start its backfill chain and would stay partial forever,
    // showing only whatever little it happened to arm with.
    if (!tailLocked) backfill = "edge";
  });

  $effect(() => {
    const sid = brain.session?.id;
    const len = allTurns.length;
    if (!sid || sid !== readyFor) return;
    // Same session, history changed (turn finished, compaction): when the
    // window already reached the tail, keep it there so new turns mount
    // immediately; mid-backfill the window slides forward on its own.
    if (tailLocked) mountCount = len;
  });

  // Backfill driver: prepend older turns one idle batch at a time, never
  // mid-window. A prepend adds height ABOVE the viewport, so the view only
  // stays still if it is corrected — and the right correction depends on where
  // the reader is, which is what `backfillMode` decides (see messages.ts):
  // re-pin to the bottom for a reader on the newest response, restore a
  // reference row for one who scrolled back, mount nothing for one in between.
  //
  // The reference row: remember the row under the scroller's top edge before
  // the prepend, then restore that row's exact viewport-relative offset after
  // it. Engines with native scroll anchoring
  // already did (most of) this during the forced layout, so the residual we
  // correct is ~0 and nothing trembles; engines WITHOUT anchoring (WebKitGTK,
  // older Safari) get the full correction instead of sliding from the newest
  // response to the top of the history mid-backfill. (A scrollHeight-delta
  // correction can't do this: it double-counts where anchoring exists.)
  const scroller = (): HTMLElement | null => document.getElementById("messages");
  const captureRef = (): { el: Element; rel: number } | null => {
    const box = scroller();
    // The rows are #msg-col's children. #messages' first child is not one of
    // them: the compressing banner shares that parent.
    const list = document.getElementById("msg-col")?.children ?? [];
    if (!box) return null;
    const stop = box.getBoundingClientRect().top + 2;
    for (const row of Array.from(list)) {
      const r = row.getBoundingClientRect();
      if (r.bottom > stop) return { el: row, rel: r.top - stop };
    }
    return null;
  };
  const holdRef = (ref: { el: Element; rel: number } | null, frame = 0): void => {
    const box = scroller();
    // backfill !== "hold" means the reader moved mid-hold: never fight a
    // scroll, drop the correction and let them be.
    if (!ref || !box || !ref.el.isConnected || backfill !== "hold") return;
    const stop = box.getBoundingClientRect().top + 2;
    const drift = ref.el.getBoundingClientRect().top - stop - ref.rel;
    // Under half a pixel is subpixel noise; writing scrollTop for it trembles.
    if (Math.abs(drift) > 0.5) box.scrollTop += drift;
    if (frame < HOLD_FRAMES) requestAnimationFrame(() => holdRef(ref, frame + 1));
  };
  /** The reader is on the newest response, so that is the thing that must not
   *  move: re-pin to the bottom while the batch mounted above them settles.
   *  Stopping the moment they scroll away is the point — the pin is for a
   *  reader who is following, not a leash on one who isn't. */
  const holdEdge = (frame = 0): void => {
    const box = scroller();
    if (!box || backfill !== "edge") return;
    box.scrollTop = box.scrollHeight;
    if (frame < HOLD_FRAMES) requestAnimationFrame(() => holdEdge(frame + 1));
  };

  // Chat owns the scroller element and its `onscroll`; the window owns what to
  // do about it. No initial measurement: at arm time the fresh window has not
  // been pinned yet, so its geometry can read "top of a tall transcript" when
  // the reader is about to be at the bottom. The pin Chat schedules fires a
  // scroll of its own, and that is the first honest sample.
  $effect(() => {
    void brain.session?.id; // re-attach for the freshly armed window
    const box = scroller();
    if (!box) return;
    const measure = (): void => {
      backfill = backfillMode(box);
    };
    box.addEventListener("scroll", measure, { passive: true });
    return () => box.removeEventListener("scroll", measure);
  });

  $effect(() => {
    void brain.session?.id; // restart the chain per session
    // Start once the history lands AND the window wants older turns. The old
    // `len <= MOUNT_CHUNK` test was a turn-count proxy for "everything is
    // mounted", which a step-heavy history breaks: 9 turns of 300 steps each
    // arm to ONE turn, and the chain then never ran — 8 turns unreachable with
    // nothing on screen to scroll. `step` below already stops when the window
    // reaches the end, so the only thing to test here is intent.
    if (!backfill) return;
    let alive = true;
    const schedule = (fn: () => void): void => {
      if ("requestIdleCallback" in window) requestIdleCallback(fn);
      else setTimeout(fn, 16);
    };
    const step = (): void => {
      // Bail if the effect was disposed (session switched) or if the reader
      // moved somewhere that wants no correction — a chain must never pull
      // them, and mid-window there is nothing to prepare for them.
      if (!alive || !backfill) return;
      if (mountCount >= allTurns.length) {
        tailLocked = true;
        return;
      }
      const ref = backfill === "hold" ? captureRef() : null;
      mountCount += mountBatch(allTurns, allTurns.length - mountCount, MOUNT_CHUNK, STEP_BUDGET);
      const next = (): void => {
        if (!alive) return;
        if (mountCount >= allTurns.length) tailLocked = true;
        else if (backfill) schedule(step);
      };
      void tick().then(() => {
        if (ref) holdRef(ref);
        else holdEdge();
        // Mount one batch per settle, not one per idle slice: the rows just
        // mounted are still intrinsic-size placeholders, their real heights (and
        // so the hold's reference point) land a frame later. Give them that
        // frame before the next batch goes in above them.
        requestAnimationFrame(() => requestAnimationFrame(next));
      });
    };
    schedule(step);
    return () => {
      alive = false;
    };
  });
  // history reasoning blocks: collapsed by default, per-message index toggle
  let histOpen = $state<Record<number, boolean>>({});
  // compaction dividers: which one is expanded (turn index, -1 = none)
  let compactionOpen = $state(-1);
  function toggleHist(i: number): void {
    histOpen = { ...histOpen, [i]: !histOpen[i] };
  }
  function histKey(e: KeyboardEvent, i: number): void {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggleHist(i);
    }
  }
  // inline history reasoning steps: collapsed by default, keyed "msgIdx:stepIdx"
  let histStepOpen = $state<Record<string, boolean>>({});
  function toggleStepReasoning(key: string): void {
    histStepOpen = { ...histStepOpen, [key]: !histStepOpen[key] };
  }
  function stepReasoningKey(e: KeyboardEvent, key: string): void {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggleStepReasoning(key);
    }
  }

  let copied: number | null = $state(null);
  let copyTimer: ReturnType<typeof setTimeout> | undefined;
  async function copy(index: number, text: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
    }
    copied = index;
    clearTimeout(copyTimer);
    copyTimer = setTimeout(() => {
      copied = null;
    }, 900);
  }

  /** Concatenate the draft's text steps for copy. */
  function draftText(): string {
    return (brain.draft?.steps ?? [])
      .filter((s) => s.kind === "text")
      .map((s) => s.text ?? "")
      .join("");
  }
</script>

<div id="msg-col">
  {#if brain.messages.length === 0 && !brain.draft}
    <div class="msg assistant">
      <div class="box">
        <div class="text stream-idle">
          <span class="idle-dot"></span>The agent is ready. Pick a model in the agent panel and start typing.
        </div>
      </div>
    </div>
  {/if}
  {#each turns as t, tIdx (t.assistantIdx >= 0 ? t.assistantIdx : `u${allTurns.length - turns.length + tIdx}`)}
    {@const origIdx = allTurns.length - turns.length + tIdx}
    {#if t.divider}
      {@const d = t.divider}
      {@const failed = isCompactionFailure(d)}
      <div class="compaction" class:failed>
        <button
          class="compaction-bar"
          aria-expanded={compactionOpen === origIdx}
          onclick={() => (compactionOpen = compactionOpen === origIdx ? -1 : origIdx)}
        >
          <span class="rule"></span>
          <span class="label"><Icon name={failed ? "circle-x" : "fold-vertical"} size={12} /> {compactionLabel(d)}</span>
          <span class="rule"></span>
        </button>
        {#if compactionOpen === origIdx}
          <div class="compaction-body" transition:slide={{ duration: 120 }}>
            <pre>{compactionDetail(d)}</pre>
          </div>
        {/if}
      </div>
    {/if}
    {#if t.subagent}
      {@const s = subagentResultParts(t.subagent)}
      <div class="compaction subagent" data-idx={origIdx}>
        <div class="compaction-bar">
          <span class="rule"></span>
          <span class="label"><Icon name="git-branch" size={12} /> {s.label}</span>
          <span class="rule"></span>
        </div>
        <div class="compaction-body"><pre>{s.body}</pre></div>
      </div>
    {/if}
    {#if t.user}
      <div class="msg user" data-idx={origIdx}>
        <div class="box">
          {#if contentImages(t.user.content).length}
            <div class="att-thumbs">
              {#each contentImages(t.user.content) as img, i (i)}
                <button class="att-thumb-btn" title="zoom image" onclick={() => openImage(img, "attachment")}>
                  <img class="att-thumb" src={img} alt="attachment" loading="lazy" />
                </button>
              {/each}
            </div>
          {/if}
          {#each segments(contentText(t.user.content)) as seg, j (j)}
            {#if seg.kind === "error"}
              <div class="err-block">
                <div class="err-head"><Icon name="circle-alert" size={12} /> ERROR</div>
                <div class="err-body">{seg.text}</div>
              </div>
            {:else}
              <div class="text md user-md">{@html renderMarkdown(seg.text)}</div>
            {/if}
          {/each}
        </div>
      </div>
    {/if}
    {#if t.assistant}
      {@const a = t.assistant}
      {@const hasReasoningBlock = !!a.reasoning && !(a.steps ?? []).some((s) => s.kind === "reasoning")}
      <div class="msg assistant" data-idx={origIdx}>
        <div class="box">
          <button
            class="copy"
            class:done={copied === t.assistantIdx}
            title="copy message"
            onclick={() => void copy(t.assistantIdx, typeof a.content === "string" ? a.content : JSON.stringify(a.content))}
          >
            <span class="ok"><Icon name="check" size={12} /></span>
            <Icon name="copy" size={12} />
          </button>
          {#if hasReasoningBlock}
            <div
              class="lg done"
              role="button"
              tabindex="0"
              aria-expanded={!!histOpen[origIdx]}
              onclick={() => toggleHist(origIdx)}
              onkeydown={(e) => histKey(e, origIdx)}
            >
              <div class="lg-row">
                <span class="lg-led" aria-hidden="true"></span>
                <span>THOUGHT</span>
                <span class="lg-tail">
                  <span class="lg-caret" aria-hidden="true"><Icon name={histOpen[origIdx] ? "chevron-down" : "chevron-right"} size={12} /></span>
                </span>
              </div>
              {#if histOpen[origIdx]}
                <div class="lg-body">{a.reasoning}</div>
              {/if}
            </div>
          {/if}
          {#if a.steps && a.steps.length > 0}
            {#each collapseDelegates(a.steps) as step, j (j)}
              {#if step.kind === "tool"}
                <ToolChip {step} {onsub} />
              {:else if step.kind === "reasoning"}
                {@const rk = `${origIdx}:${j}`}
                <div
                  class="lg done"
                  role="button"
                  tabindex="0"
                  aria-expanded={!!histStepOpen[rk]}
                  onclick={() => toggleStepReasoning(rk)}
                  onkeydown={(e) => stepReasoningKey(e, rk)}
                >
                  <div class="lg-row">
                    <span class="lg-led" aria-hidden="true"></span>
                    <span>THOUGHT</span>
                    <span class="lg-tail">
                      {#if step.elapsed != null}<span class="lg-meta">{dur(step.elapsed)}</span>{/if}
                      <span class="lg-caret" aria-hidden="true"><Icon name={histStepOpen[rk] ? "chevron-down" : "chevron-right"} size={12} /></span>
                    </span>
                  </div>
                  {#if histStepOpen[rk]}
                    <div class="lg-body">{step.text}</div>
                  {/if}
                </div>
              {:else if step.kind === "guard"}
                <div class="guard-note" class:hot={(step.tier ?? 1) > 1}>
                  <span class="gn-flag">loop guard</span>
                  <span class="gn-text">{step.text}</span>
                </div>
              {:else}
                {#each segments(step.text ?? "") as seg, k (k)}
                  {#if seg.kind === "error"}
                    <div class="err-block">
                      <div class="err-head"><Icon name="circle-alert" size={12} /> PROVIDER ERROR</div>
                      <div class="err-body">{seg.text}</div>
                    </div>
                  {:else if seg.text.trim() !== ""}
                    <div class="text md">{@html renderMarkdown(seg.text)}</div>
                  {/if}
                {/each}
              {/if}
            {/each}
          {:else}
            {#each segments(typeof a.content === "string" ? a.content : JSON.stringify(a.content)) as seg, j (j)}
              {#if seg.kind === "error"}
                <div class="err-block">
                  <div class="err-head"><Icon name="circle-alert" size={12} /> PROVIDER ERROR</div>
                  <div class="err-body">{seg.text}</div>
                </div>
              {:else if seg.text.trim() !== ""}
                <div class="text md">{@html renderMarkdown(seg.text)}</div>
              {/if}
            {/each}
          {/if}
          {#if a.status === "interrupted"}
            <div class="interrupted"><Icon name="circle-stop" size={12} /> Interrupted</div>
          {:else if a.status === "failed"}
            <div class="interrupted"><Icon name="circle-x" size={12} /> Turn failed</div>
          {/if}
        </div>
      </div>
    {/if}
  {/each}
  {#if brain.draft}
    <div class="msg assistant" class:working={working}>
      <div class="box" class:pending>
        <button
          class="copy"
          class:done={copied === -1}
          title="copy message"
          onclick={() => void copy(-1, draftText())}
        >
          <span class="ok"><Icon name="check" size={12} /></span>
          <Icon name="copy" size={12} />
        </button>
        {#if brain.draft.notice}
          <div class="turn-notice">{brain.draft.notice}</div>
        {/if}
        {#if pending}
          <div class="text stream-idle">waiting for response…</div>
        {/if}
        {#each draftSteps as step, i (i)}
          {@const live = i === draftSteps.length - 1}
          {#if step.kind === "tool"}
            <ToolChip {step} {onsub} />
          {:else if step.kind === "reasoning"}
            <div class="lg" class:live>
              <div class="lg-row">
                <span class="lg-led" aria-hidden="true"></span>
                <span>{live ? "THINKING" : "THOUGHT"}</span>
                <ThinkClock startMs={live ? step.t0 ?? brain.draft?.startedAt ?? Date.now() : undefined} />
              </div>
              <div class="lg-rail" aria-hidden="true"><i></i></div>
              <div class="lg-body" bind:this={thinkingBody}>{step.text}</div>
            </div>
          {:else if step.kind === "guard"}
            <div class="guard-note" class:hot={(step.tier ?? 1) > 1}>
              <span class="gn-flag">loop guard</span>
              <span class="gn-text">{step.text}</span>
            </div>
          {:else}
            {#if live}
              <!-- The tail step is still growing, so render it as escaped
                   plain text. A full markdown parse per streamed token is
                   O(n²) — the cache key is the whole accumulating string, so
                   every token misses and re-parses — and the {@html} write
                   re-assigns the bubble's innerHTML each token, thrashing the
                   cache and dropping text selection. Plain text is O(1) per
                   token; the markdown render takes over the moment this step
                   stops being the tail (a later step appended, or the turn
                   settling into history). -->
              <div class="text md live">{step.text ?? ""}</div>
            {:else}
              {#each segments(step.text ?? "") as seg, j (j)}
                {#if seg.kind === "error"}
                  <div class="err-block">
                    <div class="err-head"><Icon name="circle-alert" size={12} /> PROVIDER ERROR</div>
                    <div class="err-body">{seg.text}</div>
                  </div>
                {:else}
                  <div class="text md">{@html renderMarkdown(seg.text)}</div>
                {/if}
              {/each}
            {/if}
          {/if}
        {/each}
        {#each brain.approvals.filter((card) => !card.session_id || card.session_id === brain.session?.id) as card (card.request_id)}
          <ApprovalCard {card} />
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  /* The live (still-growing) tail step renders as escaped plain text — see the
     note at its use site. pre-wrap keeps the streamed line breaks that the
     markdown render would otherwise supply via <p>/<br>. */
  .text.md.live {
    white-space: pre-wrap;
    word-break: break-word;
  }
</style>
