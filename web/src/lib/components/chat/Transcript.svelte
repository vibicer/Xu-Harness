<script lang="ts">
  import { slide } from "svelte/transition";
  import { brain } from "../../store.svelte";
  import ToolChip from "../ToolChip.svelte";
  import Icon from "../Icon.svelte";
  import ApprovalCard from "./ApprovalCard.svelte";
  import { openImage } from "../../lightbox.svelte";
  import { renderMarkdown } from "../../markdown";
  import { collapseDelegates } from "../../delegate-round";
  import {
    compactionDetail,
    compactionLabel,
    isCompactionFailure,
    contentImages,
    contentText,
    displayTurns,
    segments,
    subagentResultParts,
  } from "./messages";

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
  {#each displayTurns(brain.messages) as t, tIdx (t.assistantIdx >= 0 ? t.assistantIdx : `u${tIdx}`)}
    {#if t.divider}
      {@const d = t.divider}
      {@const failed = isCompactionFailure(d)}
      <div class="compaction" class:failed>
        <button
          class="compaction-bar"
          aria-expanded={compactionOpen === tIdx}
          onclick={() => (compactionOpen = compactionOpen === tIdx ? -1 : tIdx)}
        >
          <span class="rule"></span>
          <span class="label"><Icon name={failed ? "circle-x" : "fold-vertical"} size={12} /> {compactionLabel(d)}</span>
          <span class="rule"></span>
        </button>
        {#if compactionOpen === tIdx}
          <div class="compaction-body" transition:slide={{ duration: 120 }}>
            <pre>{compactionDetail(d)}</pre>
          </div>
        {/if}
      </div>
    {/if}
    {#if t.subagent}
      {@const s = subagentResultParts(t.subagent)}
      <div class="compaction subagent" data-idx={tIdx}>
        <div class="compaction-bar">
          <span class="rule"></span>
          <span class="label"><Icon name="git-branch" size={12} /> {s.label}</span>
          <span class="rule"></span>
        </div>
        <div class="compaction-body"><pre>{s.body}</pre></div>
      </div>
    {/if}
    {#if t.user}
      <div class="msg user" data-idx={tIdx}>
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
      <div class="msg assistant" data-idx={tIdx}>
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
              aria-expanded={!!histOpen[tIdx]}
              onclick={() => toggleHist(tIdx)}
              onkeydown={(e) => histKey(e, tIdx)}
            >
              <div class="tk-head">
                <span class="tk-icon"><Icon name="check" size={12} /></span>
                <span class="tk-tool"><span class="dancer">{idle}</span> thinking</span>
                <span class="tk-state"><Icon name={histOpen[tIdx] ? "chevron-down" : "chevron-right"} size={11} /> {histOpen[tIdx] ? "hide" : "show"}</span>
              </div>
              {#if histOpen[tIdx]}
                <div class="tk-body">{a.reasoning}</div>
              {/if}
            </div>
          {/if}
          {#if a.steps && a.steps.length > 0}
            {#each collapseDelegates(a.steps) as step, j (j)}
              {#if step.kind === "tool"}
                <ToolChip {step} {onsub} />
              {:else if step.kind === "reasoning"}
                {@const rk = `${tIdx}:${j}`}
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
