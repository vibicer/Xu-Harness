<script lang="ts">
  import { tick } from "svelte";
  import { flip } from "svelte/animate";
  import { fly, slide } from "svelte/transition";
  import { quintOut } from "svelte/easing";
  import { brain } from "../../store.svelte";
  import { readAttachments } from "../../attach";
  import GitChip from "../GitChip.svelte";
  import PluginSlot from "../PluginSlot.svelte";
  import Icon from "../Icon.svelte";
  import { touch } from "../../touch.svelte";
  import QueuedTurns from "./QueuedTurns.svelte";

  // ---- composer send pipeline ----
  let input = $state("");
  let sendError = $state<string | null>(null);
  let fileInputEl: HTMLInputElement | null = null;
  // Attached image data URLs, sent with the next message. The pending-images
  // strip sits between transcript and composer, so the shell renders it from
  // this shared state (bound up via bind:pendingImages).
  let {
    /** the <textarea>, bound up so the shell can focus it on view change / keypress */
    composerEl = $bindable<HTMLTextAreaElement | null>(null),
    pendingImages = $bindable<string[]>([]),
    /** set while the send RPC is in flight — the shell's "thinking" LED reads it */
    busy = $bindable(false),
    /** "take me to the live edge" — the shell owns the scroll container */
    onsend,
  }: {
    composerEl: HTMLTextAreaElement | null;
    pendingImages: string[];
    busy: boolean;
    onsend: () => void;
  } = $props();

  function autoresize(el: HTMLTextAreaElement) {
el.style.height = "0px";
el.style.height = el.scrollHeight + "px";
}
  function shrinkComposer() {
    if (composerEl) { composerEl.style.height = "0px"; composerEl.style.height = composerEl.scrollHeight + "px"; }
  }

  async function send(): Promise<void> {
    const text = input.trim();
    const images = [...pendingImages];
    if (!text && images.length === 0) return;
    // Order matters: check `busy` BEFORE clearing anything. A second Enter
    // inside the in-flight window used to wipe the box and then return, so the
    // text was silently dropped. The RPC already running owns this turn — leave
    // the draft in the box (a send while the brain is mid-turn is queued
    // server-side on a later round, not this one).
    if (busy) return;
    input = "";
    pendingImages = [];
    sendError = null;
    // sending is an explicit "take me to the live edge" — override nearBottom
    void tick().then(onsend);
    busy = true;
    try {
      await brain.send(text, images.length ? images : undefined);
    } catch (e) {
      // The brain rolls back its optimistic row and rethrows: a failed send
      // must be visible and must not eat what the user typed. Put the text and
      // attachments back and say so inline; the error clears on the next send.
      input = text;
      pendingImages = images;
      sendError = e instanceof Error ? e.message : String(e);
      console.error("send failed", e);
    } finally {
      busy = false;
      void tick().then(shrinkComposer);
    }
  }

  async function onFilesChosen(files: FileList | null): Promise<void> {
    if (!files) return;
    const { images, texts, skipped } = await readAttachments(files);
    if (images.length) {
      // Dedupe on insert: the same attachment twice yields two identical data
      // URLs, and the keyed each-blocks in PendingImages / Transcript then
      // throw `each_key_duplicate` — taking the strip (and the transcript)
      // down. Guard here so a duplicate never enters the strip at all.
      const seen = new Set(pendingImages);
      const fresh = images.filter((img) => {
        if (seen.has(img)) return false;
        seen.add(img);
        return true;
      });
      if (fresh.length) pendingImages = [...pendingImages, ...fresh];
    }
    if (texts.length) input = input ? `${input}\n${texts.join("")}` : texts.join("");
    if (skipped.length) console.warn("skipped attachments:", skipped.join(", "));
    void tick().then(() => { if (composerEl) { autoresize(composerEl); composerEl.focus(); } });
  }

  /** A queued message the user pulled back to edit. Its queue row is already
   *  gone (the caller cancelled it server-side), so this only restores the
   *  content into the box — and appends rather than clobbers, so text typed
   *  since the send is never lost. */
  function restoreQueued(q: { text: string; images?: string[] }): void {
    input = input.trim() ? `${input}\n${q.text}` : q.text;
    if (q.images?.length) {
      const seen = new Set(pendingImages);
      const fresh = q.images.filter((img) => !seen.has(img));
      if (fresh.length) pendingImages = [...pendingImages, ...fresh];
    }
    void tick().then(() => { if (composerEl) { autoresize(composerEl); composerEl.focus(); } });
  }

  // ---- task list strip ----
  // Task list starts collapsed to a badge; the user expands it on demand.
  let todoOpen = $state(false);
  const todoItems = $derived(brain.todos.phases.flatMap((ph) => ph.items));
  const todoTotal = $derived(todoItems.length);
  const todoDone = $derived(
    todoItems.filter((it) => it.status === "done" || it.status === "dropped").length,
  );
  const todoRunning = $derived(todoItems.some((it) => it.status === "in_progress"));
  const todoSummary = $derived(
    todoItems.find((it) => it.status === "in_progress")?.content ??
      `${todoDone}/${todoTotal} tasks done`,
  );

  // ---- context meter ----
  // Context meter: est. tokens in play / window size (the brain reports a
  // percentage of context_length, so derive the absolute numbers from it).
  const ctxTokens = $derived(
    Math.round((brain.state.context / 100) * brain.config.context_length),
  );
  // New provider-anchored values (optional, fall back to transcript helpers)
  const pressureTokens = $derived(brain.state.pressure ?? ctxTokens);
  const projectedTokens = $derived(brain.state.projected ?? ctxTokens);
  function fmtTokens(n: number): string {
    if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
    if (n >= 1000) return `${Math.round(n / 1000)}k`;
    return String(n);
  }
</script>

<div id="composer-wrap">
  <QueuedTurns onedit={restoreQueued} />
  {#if brain.todos.phases.length}
    <!-- Collapsed by default: a small badge, not a full-width bar. The badge
         shows done/total and turns amber while an item is in progress. -->
    <div id="todo-strip" class:open={todoOpen} aria-label="tasks">
      <button
        type="button"
        id="todo-toggle"
        class:running={todoRunning}
        onclick={() => (todoOpen = !todoOpen)}
        aria-expanded={todoOpen}
        title={todoSummary}
      >
        <span class="todo-icon" aria-hidden="true"><Icon name="list-todo" size={14} /></span>
        <!-- Keyed on the count so each completion remounts the span and
             replays the pop — a visible tick of progress. -->
        {#key todoDone}
          <span class="todo-count">{todoDone}/{todoTotal}</span>
        {/key}
        <span class="sr-only">{todoSummary}</span>
      </button>
      {#if todoOpen}
        <div
          id="todo-body"
          transition:slide={{ duration: 220, easing: quintOut }}
        >
          {#each brain.todos.phases as ph (ph.phase)}
            <div class="todo-phase">
              {#if ph.phase}<div class="todo-phase-name">{ph.phase}</div>{/if}
              {#each ph.items as it, i (it.content)}
                <div class="todo-row"
                  class:done={it.status === "done"}
                  class:dropped={it.status === "dropped"}
                  class:active={it.status === "in_progress"}
                  animate:flip={{ duration: 260, easing: quintOut }}
                  in:fly={{ x: -6, duration: 200, delay: i * 28, easing: quintOut }}
                  out:slide={{ duration: 140, easing: quintOut }}
                >
                  <span class="todo-dot" aria-hidden="true"></span>
                  <span class="todo-txt">{it.content}</span>
                </div>
              {/each}
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
  <div id="composer">
  {#if sendError}
    <div class="composer-error" role="alert">
      <Icon name="circle-alert" size={12} />
      <span>send failed — {sendError} — your message was kept; press Enter to retry</span>
    </div>
  {/if}
  <div id="composer-box">
    <span class="prompt">&gt;_</span>
    <textarea
      id="composer-input"
      bind:this={composerEl}
      rows="1"
      placeholder={touch.coarse ? "Message…" : "Message… (Enter to send, Shift+Enter for newline)"}
      bind:value={input}
      oninput={(e) => autoresize(e.currentTarget)}
      onkeydown={(e) => {
        /* Enter sends on a keyboard; on a soft keyboard it must stay a newline.
           A phone has no Shift+Enter, so sending Enter there would make a
           two-line message impossible to write — the send button is the
           sender. Matches the `(pointer: coarse)` query in theme.css. */
        if (e.key === "Enter" && !e.shiftKey && !touch.coarse) {
          e.preventDefault();
          void send();
        }
      }}
    ></textarea>
    <div id="composer-side">
      <button id="att-btn" title="attach image or text file" aria-label="attach image or text file" onclick={() => fileInputEl?.click()}><Icon name="paperclip" size={15} /></button>
      <input
        type="file"
        accept="image/*,.txt,.md,.markdown,.json,.yaml,.yml,.toml,.ini,.cfg,.csv,.tsv,.log,.py,.js,.jsx,.ts,.tsx,.svelte,.vue,.html,.htm,.css,.scss,.sh,.bash,.zsh,.fish,.rs,.go,.java,.kt,.c,.h,.cpp,.hpp,.cs,.rb,.php,.sql,.xml,.svg,.diff,.patch,.env"
        multiple
        bind:this={fileInputEl}
        style="display:none"
        onchange={(e) => { onFilesChosen((e.currentTarget as HTMLInputElement).files); }}
      />
      <button
        id="send-btn"
        title="Send message"
        aria-label="Send message"
        disabled={input.trim() === "" && pendingImages.length === 0}
        onclick={() => void send()}
      >
        <Icon name="arrow-right" size={16} />
      </button>
      {#if brain.draft !== null}
        <button
          id="stop-btn"
          title="Stop turn"
          aria-label="Stop turn"
          onclick={() => void brain.stop()}
        >
          <Icon name="square" size={13} fill="currentColor" />
        </button>
      {/if}
    </div>
  </div>
  <div id="composer-foot" aria-label="Composer status">
    <span class="foot-group approvals">
      <span class="foot-label">approvals</span>
      <button
        class="mode yolo-toggle"
        class:on={brain.config.approval_mode !== "manual"}
        aria-pressed={brain.config.approval_mode !== "manual"}
        title={`approval mode: ${brain.config.approval_mode} — click to cycle (${brain.approvalModeCycle.join(" → ")})`}
        onclick={() => void brain.cycleApprovalMode()}
      >{brain.config.approval_mode}</button>
    </span>
    <span class="context-metrics" title="context meter — calibrated to the API when a sample exists, surface heuristic before">
      <span class="metric ctx" class:amber={brain.state.compress} title={brain.state.calibrated ? "calibrated: last real prompt_tokens + growth since" : "heuristic estimate — no API sample yet"}>
        <span class="label">context</span>
        <span class="value">{fmtTokens(ctxTokens)}</span>
      </span>
      {#if brain.state.pressure !== undefined}
        <span class="metric api" title="provider-reported prompt_tokens from last call">
          <span class="label">API</span>
          <span class="value">{fmtTokens(pressureTokens)}</span>
        </span>
      {/if}
      {#if brain.state.projected !== undefined && !brain.state.calibrated}
        <span class="metric projected" title="projected next request cost">
          <span class="arrow"><Icon name="arrow-right" size={11} /></span>
          <span class="value">{fmtTokens(projectedTokens)}</span>
        </span>
      {/if}
      <span class="metric window" title="configured context window">
        <span class="value">{fmtTokens(brain.config.context_length)}</span>
      </span>
    </span>
    <span class="foot-status git-wrap" style="position:relative">
      <GitChip />
    </span>
    <PluginSlot mount="statusbar" chrome="foot-status" />
  </div>
</div>
</div>

<style>
  /* Context meter group in the composer foot. Theme tokens only — the palette
     is per-layout, so a raw hex here would ignore the active theme. */
  .context-metrics {
    min-height: 24px;
    display: inline-flex;
    align-items: center;
    gap: 7px;
    white-space: nowrap;
  }
  .context-metrics .metric {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
  .context-metrics .label {
    color: var(--dim);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .context-metrics .metric.ctx .value { color: var(--text); }
  .context-metrics .metric.ctx.amber .value,
  .context-metrics .metric.ctx.amber .label { color: var(--amber); }
  .context-metrics .metric.api .value { color: var(--ok); }
  .context-metrics .metric.projected .value { color: var(--cyan); }
  .context-metrics .metric.window .value { color: var(--faint); }
  .context-metrics .arrow { display: inline-flex; align-items: center; color: var(--faint); }
  /* A failed send: inline, above the box, so it is visible without a toast
     system. Theme tokens only. */
  .composer-error {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 0 0 8px;
    font-family: var(--mono);
    font-size: 11px;
    color: var(--danger);
    min-width: 0;
    word-break: break-word;
  }
</style>
