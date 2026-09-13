<!-- Tool chip: renders a tool step as the thing the tool *is* — a terminal pane
     for shell work, a note tab for file work, a query line for searches, and
     the plain log line for everything else.

     One component for all four shells: the root keeps the legacy `.toollog`
     classes (so existing theme CSS still lands) and adds `shape-*` / `v-*` for
     the new panes. Skins tune the panes through the `--chip-*` custom
     properties below rather than by duplicating markup.

     Owns its own open/closed state, so callers no longer track a keyed map. -->
<script lang="ts">
  import type { Step } from "../types";
  import { openImage } from "../lightbox.svelte";
  import { toolShape, isReadOnly, headline, displayLocation, displayDescription, fileTab, splitExit, lineCount } from "../toolshape";
  import { dur } from "../format";
  import Icon from "./Icon.svelte";

  let {
    step,
    variant = "default",
    startOpen = false,
    subLabel = "↳ activity",
    onsub,
  }: {
    step: Step;
    /** Which shell is rendering: adds `v-<variant>` so a layout's scoped CSS can
     *  reach this markup. Kept as a single-value union so callers stay valid. */
    variant?: "default";
    startOpen?: boolean;
    subLabel?: string;
    onsub?: (runId: string) => void;
  } = $props();

  // `startOpen` is a fixed per-call-site default (the sub-run panel wants its
  // chips expanded), so capturing the initial value is the intent.
  // svelte-ignore state_referenced_locally
  let open = $state(startOpen);

  // Braille spinner for the running icon — ticks only while this chip runs.
  const FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];
  let frame = $state(0);
  $effect(() => {
    if (step.status !== "running") return;
    const t = setInterval(() => (frame = (frame + 1) % FRAMES.length), 80);
    return () => clearInterval(t);
  });

  const shape = $derived(toolShape(step.tool));
  const status = $derived(step.status === "ok" ? "ok" : step.status === "error" ? "err" : "run");
  // Location describes the actual execution target; only the model-written
  // note adds intent. Old history keeps literal command/query fallbacks.
  const head = $derived(headline(step.tool, step.args));
  // A resolved delegate name beats the requested label. Multi-agent rounds
  // keep their count in the head and individual names in activity/details.
  const location = $derived(
    (step.tool === "delegate" && step.subagent) || displayLocation(step.tool, step.args, step.cwd),
  );
  const description = $derived(displayDescription(step.tool, step.args, step.note, step.cwd));
  /** How many sub-agents this delegate round spawned. The brain sums the whole
   *  round, so sibling calls and a `tasks` batch both report the real total. */
  const spawned = $derived(
    step.tool === "delegate" ? Math.max(step.subagent_count ?? 1, 1) : 0,
  );
  /** One name is misleading while several run — the count carries the round and
   *  the activity tabs carry the individual names. */
  const showLocation = $derived(Boolean(location) && spawned < 2);
  const tab = $derived(fileTab(head));
  const term = $derived(splitExit(step.output));
  const lines = $derived(lineCount(step.output));
  const hasBody = $derived(step.output != null && step.output !== "");
  // The backend caps output; never discard any of that available payload here.
  const bodyLines = $derived(term.body.split("\n"));

  function toggle(): void {
    if (hasBody) open = !open;
  }
  // A native <button> already fires click on Enter/Space — no key handler.
</script>

<!-- The compact row carries location and action. Expanding shows tool output
     only, with no repeated metadata form. The activity button stays a sibling
     of the head — nested buttons are invalid HTML. -->
<div class="toollog {status} shape-{shape} v-{variant}" class:open class:ro={isReadOnly(step.tool)}>
  <div class="tl-row">
    <button
      class="tl-head"
      type="button"
      disabled={!hasBody}
      aria-expanded={hasBody ? open : undefined}
      onclick={toggle}
    >
      <span class="tl-tool" title={step.tool}>{step.tool}</span>
      {#if showLocation}<span class="tl-what tl-location" title={location}>{location}</span>{/if}
      {#if description}<span class="tl-description" title={description}>{description}</span>{/if}
      {#if spawned}
        <span class="tl-spawn">{spawned} agent{spawned === 1 ? "" : "s"} spawned</span>
      {/if}
      <span class="tl-icon" title={step.status ?? "running"} aria-label={step.status ?? "running"}>
        {#if step.status === "ok"}<Icon name="check" size={13} />
        {:else if step.status === "error"}<Icon name="x" size={13} />
        {:else}{FRAMES[frame]}{/if}
      </span>
      <span class="tl-state">
        {#if step.status === "running"}RUNNING{:else if step.elapsed != null}{dur(step.elapsed)}{:else}{(step.status ?? "ok").toUpperCase()}{/if}
      </span>
      {#if hasBody}<span class="tl-caret" aria-hidden="true"><Icon name={open ? "chevron-down" : "chevron-right"} size={12} /></span>{/if}
    </button>
    {#if step.subagent_run && onsub}
      <button
        class="tl-sub"
        type="button"
        title="show what {step.subagent ?? 'the sub-agent'} did"
        onclick={() => onsub!(step.subagent_run!)}
      >{subLabel}</button>
    {/if}
  </div>

  <!-- show_image: the picture is the point of the chip, so it renders open,
       outside the disclosure. `alt` is the tool's caption (else the filename). -->
  {#if step.image}
    <!-- clickable: opens the shared lightbox (data: URLs can't open in a tab) -->
    <figure class="tl-img">
      <button class="tl-img-btn" title="zoom image" onclick={() => openImage(step.image!, step.image_alt || "")}>
        <img src={step.image} alt={step.image_alt || "image sent by Xu"} loading="lazy" />
      </button>
      {#if step.image_alt}<figcaption>{step.image_alt}</figcaption>{/if}
    </figure>
  {/if}

  {#if open && hasBody}
    {#if shape === "terminal"}
      <div class="tl-term">
        <div class="tl-term-bar"><span class="tl-dots"><i></i><i></i><i></i></span><span class="tl-term-title">{step.tool}</span></div>
        <div class="tl-term-out">{#each bodyLines as l, i (i)}<span class="tl-line">{l || " "}</span>{/each}</div>
        {#if term.exit}<div class="tl-term-exit" class:bad={status === "err"}>{term.exit}</div>{/if}
      </div>
    {:else if shape === "note"}
      <div class="tl-note">
        <div class="tl-note-tab">{tab.name}{#if tab.dir}<span class="tl-dir">{tab.dir}</span>{/if}{#if isReadOnly(step.tool)}<span class="tl-ro">read-only</span>{/if}</div>
        <div class="tl-note-page">{#each bodyLines as l, i (i)}<span class="tl-nline"><em>{i + 1}</em>{l || " "}</span>{/each}</div>
      </div>
    {:else}
      {#if shape === "search" && lines > 0}<div class="tl-detail"><span class="tl-hits">{lines} {lines === 1 ? "hit" : "hits"}</span></div>{/if}
      <div class="tl-body">{step.output}</div>
    {/if}
  {/if}
</div>

<style>
  /* Panes read four tokens. A theme sets `--chip-term` / `--chip-note` /
     `--chip-rule` on `.toollog` (or any ancestor) to restyle both panes
     without touching this markup; these are only the fallbacks.
     NB: aliases are resolved at each use site, not redeclared on `.toollog` —
     component styles out-specify plain stylesheets, which would shadow a
     theme's own `--chip-*` values. */
  .toollog {
    --chip-term-bg: var(--chip-term, #07090e);
    --chip-term-fg: var(--chip-term-text, var(--ok, #7ee0a8));
    --chip-note-bg: var(--chip-note, #14120c);
    --chip-note-fg: var(--chip-note-text, var(--text, #e6e6e6));
  }

  /* ---- head: shared skeleton, shape-specific middle ----
     `.tl-head` is a <button>, so reset the UA chrome and let it fill the row;
     the shells' `.toollog .tl-head` overrides (font-size, color) still apply. */
  .tl-row { display: flex; align-items: center; gap: 8px; min-width: 0; }
  .tl-head {
    flex: 1; min-width: 0;
    display: flex; align-items: center; gap: 8px;
    appearance: none; background: none; border: 0; padding: 0; margin: 0;
    text-align: left; cursor: pointer;
    font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, monospace);
    font-size: 11.5px; color: var(--dim, #8c9ab1);
  }
  .tl-head:disabled { cursor: default; }
  .tl-icon { margin-left: auto; width: 14px; display: inline-flex; align-items: center; justify-content: center; flex-shrink: 0; color: var(--cyan, #68e0cc); }
  .ok .tl-icon { color: var(--ok, #68e0cc); }
  .err .tl-icon { color: var(--danger, #ff7886); }
  .tl-tool { color: var(--cyan, #68e0cc); font-weight: bold; min-width: 0; flex-shrink: 0; max-width: 25%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  /* Independent one-line fields; full strings remain in hover titles. */
  .tl-location {
    min-width: 0; max-width: 40%; flex: 0 1 auto; color: var(--dim, #8c9ab1);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .tl-description {
    min-width: 0; flex: 0 1 auto; color: var(--text, #e6e6e6);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  /* delegate: how many sub-agents this call spawned */
  .tl-spawn {
    flex-shrink: 0; color: var(--magenta, #c49bff);
    font-size: 10.5px; white-space: nowrap;
  }
  .tl-state {
    flex-shrink: 0;
    font-family: var(--pixel, var(--mono, monospace)); font-size: 10.5px; padding: 1px 5px;
  }
  .run .tl-state { color: var(--amber, #ffd27c); }
  .ok .tl-state { color: var(--ok, #68e0cc); }
  .err .tl-state { color: var(--danger, #ff7886); }
  .tl-caret { flex-shrink: 0; display: inline-flex; align-items: center; color: var(--faint, #4f5d74); }
  /* `.tl-sub` is left to the themes — each shell already skins it globally. */

  /* ---- image (show_image) ----
     Sized like a chat attachment: fits the bubble, never taller than a
     screenful, never upscaled past its own pixels. */
  .tl-img { margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 4px; align-items: flex-start; }
  .tl-img img {
    display: block; max-width: 100%; max-height: 380px; width: auto; height: auto;
    border: 1px solid var(--chip-rule, var(--border, #2a3854));
    background: var(--chip-term-bg);
    object-fit: contain;
  }
  .tl-img figcaption {
    font-family: var(--mono, ui-monospace, SFMono-Regular, Menlo, monospace);
    font-size: 10.5px; color: var(--dim, #8c9ab1); max-width: 100%;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }

  /* ---- terminal shape ---- */
  .tl-term { margin-top: 6px; border: 1px solid var(--chip-rule, var(--border, #2a3854)); background: var(--chip-term-bg); overflow: hidden; }
  .tl-term-bar {
    display: flex; align-items: center; gap: 7px; padding: 3px 8px;
    border-bottom: 1px solid var(--chip-rule, var(--border, #2a3854)); background: color-mix(in srgb, var(--chip-term-bg) 70%, #fff 6%);
  }
  .tl-dots { display: flex; gap: 4px; }
  .tl-dots i { width: 7px; height: 7px; border-radius: 50%; background: var(--chip-rule, var(--border, #2a3854)); }
  .tl-term-title {
    font-family: var(--mono, monospace); font-size: 9.5px; letter-spacing: .08em;
    text-transform: uppercase; color: var(--faint, #4f5d74);
  }
  .tl-term-out {
    display: block; padding: 8px 10px; max-height: 260px; overflow: auto;
    font-family: var(--mono, monospace); font-size: 11.5px; line-height: 1.5;
    color: var(--chip-term-fg);
  }
  .tl-line { display: block; white-space: pre-wrap; word-break: break-word; }
  .tl-term-exit {
    padding: 2px 10px; border-top: 1px solid var(--chip-rule, var(--border, #2a3854));
    font-family: var(--mono, monospace); font-size: 10px; color: var(--ok, #68e0cc);
  }
  .tl-term-exit.bad { color: var(--danger, #ff7886); }

  /* ---- note shape ---- */
  .tl-dir {
    color: var(--faint, #4f5d74); min-width: 0;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; direction: rtl; text-align: left;
  }
  .tl-note { margin-top: 6px; }
  .tl-note-tab {
    display: inline-flex; align-items: center; gap: 8px;
    padding: 2px 12px; border: 1px solid var(--chip-rule, var(--border, #2a3854)); border-bottom: none;
    background: var(--chip-note-bg);
    font-family: var(--mono, monospace); font-size: 10.5px; color: var(--text, #e6e6e6);
  }
  .tl-ro { font-size: 9px; letter-spacing: .06em; text-transform: uppercase; color: var(--faint, #4f5d74); }
  .tl-note-page {
    display: block; padding: 8px 10px 8px 0; border: 1px solid var(--chip-rule, var(--border, #2a3854));
    background: var(--chip-note-bg); max-height: 260px; overflow: auto;
    font-family: var(--mono, monospace); font-size: 11.5px; line-height: 1.6;
    color: var(--chip-note-fg);
  }
  .tl-nline { display: block; white-space: pre-wrap; word-break: break-word; padding-left: 46px; text-indent: 0; position: relative; }
  .tl-nline em {
    position: absolute; left: 0; width: 34px; text-align: right;
    font-style: normal; color: var(--faint, #4f5d74); user-select: none;
    border-right: 1px solid var(--chip-rule, var(--border, #2a3854)); padding-right: 5px;
  }

  /* ---- search shape: query + hit count live in the detail line ---- */
  .tl-detail {
    display: flex; align-items: center; gap: 8px; margin-top: 6px; min-width: 0;
    font-family: var(--mono, monospace); font-size: 11px;
  }
  .tl-hits {
    flex-shrink: 0; font-size: 9.5px; padding: 0 5px;
    color: var(--bg, #0b0f16); background: var(--cyan, #68e0cc);
  }

  /* ---- plain fallback body ---- */
  .tl-body {
    margin-top: 6px; padding: 8px 10px;
    border: 1px solid var(--chip-rule, var(--border, #2a3854)); border-left: 2px solid var(--border-strong, var(--chip-rule, var(--border, #2a3854)));
    background: var(--bg, #0b0f16);
    font-family: var(--mono, monospace); font-size: 11.5px; color: var(--dim, #8c9ab1);
    white-space: pre-wrap; word-break: break-word; max-height: 220px; overflow: auto;
  }
</style>
