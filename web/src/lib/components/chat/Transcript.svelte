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

  let {
    /** live dancer glyph cycled by the shell while a turn streams */
    dancer,
    /** streaming bubble is "hot" (shell LED state) */
    working,
    /** open a sub-agent run modal (behaviour lives in the shell's subrun view) */
    onsub,
    /** the live reasoning scroll box, bound up so the shell can follow the stream */
    thinkingBody = $bindable<HTMLDivElement | null>(null),
  }: {
    dancer: string;
    working: boolean;
    onsub: (id: string) => void;
    thinkingBody: HTMLDivElement | null;
  } = $props();

  const idle = "╰(°▽°)╯"; // static glyph for finished reasoning steps

  // live reasoning body ref: keep its inner scroll following the stream
  // collapsed draft timeline; a reasoning step is "live" only while it is the tail
  const draftSteps = $derived(brain.draft ? collapseDelegates(brain.draft.steps) : []);

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
    const len = allTurns.length; // start once the history lands
    if (!backfill || len <= MOUNT_CHUNK) return; // else: nothing wants older history
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
              {#each contentImages(t.user.content) as img (img)}
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
              class="think ok"
              role="button"
              tabindex="0"
              aria-expanded={!!histOpen[origIdx]}
              onclick={() => toggleHist(origIdx)}
              onkeydown={(e) => histKey(e, origIdx)}
            >
              <div class="tk-head">
                <span class="tk-icon"><Icon name="check" size={12} /></span>
                <span class="tk-tool"><span class="dancer">{idle}</span> thinking</span>
                <span class="tk-state"><Icon name={histOpen[origIdx] ? "chevron-down" : "chevron-right"} size={11} /> {histOpen[origIdx] ? "hide" : "show"}</span>
              </div>
              {#if histOpen[origIdx]}
                <div class="tk-body">{a.reasoning}</div>
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
                  class="think ok"
                  role="button"
                  tabindex="0"
                  aria-expanded={!!histStepOpen[rk]}
                  onclick={() => toggleStepReasoning(rk)}
                  onkeydown={(e) => stepReasoningKey(e, rk)}
                >
                  <div class="tk-head">
                    <span class="tk-icon"><Icon name="check" size={12} /></span>
                    <span class="tk-tool"><span class="dancer">{idle}</span> thinking</span>
                    <span class="tk-state"><Icon name={histStepOpen[rk] ? "chevron-down" : "chevron-right"} size={11} /> {histStepOpen[rk] ? "hide" : "show"}</span>
                  </div>
                  {#if histStepOpen[rk]}
                    <div class="tk-body">{step.text}</div>
                  {/if}
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
      <div class="box">
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
        {#if brain.draft.steps.length === 0 && !brain.draft.reasoning && !brain.draft.notice}
          <div class="text stream-idle">waiting for response…</div>
        {/if}
        {#each draftSteps as step, i (i)}
          {#if step.kind === "tool"}
            <ToolChip {step} {onsub} />
          {:else if step.kind === "reasoning"}
            {@const live = i === draftSteps.length - 1}
            <div class="think {live ? 'run' : 'ok'}">
              <div class="tk-head">
                <span class="tk-tool"><span class="dancer">{live ? dancer : idle}</span> thinking</span>
              </div>
              <div class="tk-body" bind:this={thinkingBody}>{step.text}</div>
            </div>
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
        {/each}
        {#each brain.approvals.filter((card) => !card.session_id || card.session_id === brain.session?.id) as card (card.request_id)}
          <ApprovalCard {card} />
        {/each}
      </div>
    </div>
  {/if}
</div>
