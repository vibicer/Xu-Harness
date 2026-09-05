<script lang="ts">
  import { brain } from "../../store.svelte";
  import {
    approvalReason,
    approvalSummary,
    askOptions,
    askQuestion,
  } from "./messages";
  import type { ApprovalCard } from "../../types";
  /** One approval / clarification card, rendered inside the streaming draft
   *  bubble. Free-text ask replies are owned here (one input per card). */
  let { card }: { card: ApprovalCard } = $props();

  // ask tool: per-card reply state (free-text input owned by this component)
  let askInput = $state("");
  function setAskInput(value: string): void {
    askInput = value;
  }
  function submitAsk(): void {
    const answer = askInput.trim();
    if (answer) void brain.replyAsk(card.request_id, answer);
  }

  const reason = $derived(approvalReason(card.reason));
  const isAsk = $derived(card.tool === "ask");
  const opts = $derived(isAsk ? askOptions(card) : []);
  // ALWAYS-gated actions (destructive shell, durable memory writes) re-prompt
  // every time by design, so offering "always" there would be a lie.
  const canRemember = $derived(!isAsk && card.level !== "always");
</script>
<div class="approval" class:done={card.resolved !== null}>
  <div class="title">
    {#if card.resolved === null}
      {isAsk ? "CLARIFICATION REQUESTED" : `APPROVAL REQUIRED · ${card.tool}`}
    {:else if card.resolved === true}
      {isAsk ? "ANSWERED" : `APPROVED · ${card.tool}`}
    {:else}
      {isAsk ? "DISMISSED" : `DENIED · ${card.tool}`}
    {/if}
  </div>
  {#if !isAsk || card.resolved !== null}
    <div class="approval-summary">
      <span class="approval-label">{isAsk ? "Asks" : "Wants to"}</span>
      <span>{approvalSummary(card)}</span>
    </div>
    {#if reason}
      <div class="why"><span class="approval-label">Why</span>{reason}</div>
    {/if}
  {/if}
  {#if card.resolved !== null}
    <div class="btns">
      <span class="chip-tag" style="color:{card.resolved ? 'var(--ok)' : 'var(--danger)'}">
        {card.resolved ? (isAsk ? "answered" : "approved") : isAsk ? "dismissed" : "denied"} · logged
      </span>
    </div>
    {#if isAsk}
      <div class="ask-result">
        <div class="ask-q">{askQuestion(card)}{card.answer ? ` \u2014 ${card.answer}` : ""}</div>
      </div>
    {/if}
  {:else if isAsk}
    {@const qtext = askQuestion(card)}
    {#if qtext}
      <div class="ask-q">{qtext}</div>
    {/if}
    <div class="ask-options">
      {#if opts.length}
        {#each opts as opt, i (i)}
          <button type="button" class="ask-opt" onclick={() => void brain.replyAsk(card.request_id, opt)}>
            <span class="ask-idx">{i + 1}</span>
            <span class="ask-val">{opt}</span>
          </button>
        {/each}
        <span class="ask-label">or type a custom reply</span>
      {/if}
      <div class="askbox">
        <input
          type="text"
          placeholder="Type a reply…"
          value={askInput}
          oninput={(e) => setAskInput(e.currentTarget.value)}
          onkeydown={(e) => { if (e.key === "Enter") { e.preventDefault(); submitAsk(); } }}
        />
        <button type="button" class="btn primary" onclick={() => submitAsk()}>Send</button>
      </div>
    </div>
  {:else}
    <div class="btns">
      <button class="btn primary" onclick={() => void brain.resolveApproval(card.request_id, true)}>Approve</button>
      {#if canRemember}
        <button
          class="btn"
          title={`approve, and stop asking for "${card.tool}" for the rest of this session`}
          onclick={() => void brain.resolveApproval(card.request_id, true, true)}
        >Always</button>
      {/if}
      <button class="btn danger" onclick={() => void brain.resolveApproval(card.request_id, false)}>Deny</button>
    </div>
  {/if}
</div>
