// Runnable check for the transcript-perf memoization: `node checks/transcript-perf.test.ts`
//
// Long sessions lagged for two reasons this pins: (1) every history reload
// (whole messages array replaced after each turn) re-ran `renderMarkdown` /
// `segments` for every message — these must now be cached by input text, and
// `segments` must return a stable identity so Svelte skips the inner
// each-block effects; (2) the scroll-follow measured layout per delta — that
// lives in Chat.svelte, which cannot be imported here, so the source is
// asserted textually (same approach as config-panels.test.ts): no
// `$effect.pre` layout reads, an onscroll-driven follow flag, and a
// rAF-coalesced pin.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { renderMarkdown } from "../src/lib/markdown.ts";
import { segments } from "../src/lib/components/chat/messages.ts";

// 1. renderMarkdown: same input → same output (behavior unchanged) and a
//    cache hit on the second call (identity equality proves no re-parse).
{
  const src = "# Title\n\nsome **bold** and `code`\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n";
  const a = renderMarkdown(src);
  const b = renderMarkdown(src);
  assert.equal(a, b);
  assert.ok(a.includes("<h1") || a.includes("md-h"), "headings still render");
  assert.ok(a.includes("<strong>bold</strong>"));
  assert.ok(a.includes('class="md-table"'));
  assert.equal(a, b, "cache hit returns the identical string");
}

// 2. Different inputs never collide.
{
  assert.notEqual(renderMarkdown("one"), renderMarkdown("two"));
  assert.equal(renderMarkdown("one"), renderMarkdown("one"));
}

// 3. segments: same behavior — error splitting still works, empty text still
//    falls back to a single md segment, and the array identity is stable so
//    re-renders skip the inner each-block effects.
{
  const withErr = "before\n[error] {\"message\": \"boom\"}";
  const segs = segments(withErr);
  assert.deepEqual(
    segs.map((s) => s.kind),
    ["md", "error"],
  );
  assert.ok(segs[1].text.includes("boom"), "tidyError keeps the message");
  assert.equal(segments(withErr), segs, "stable identity on re-render");

  const plain = "just prose";
  assert.deepEqual(
    segments(plain).map((s) => s.kind),
    ["md"],
  );
  assert.equal(segments(plain), segments(plain));
}

// 4. Chat.svelte: the layout-thrash pattern is gone — no per-delta
//    scrollHeight measurement ($effect.pre + nearBottom), follow state comes
//    from onscroll, and the pin is coalesced through one rAF per frame.
{
  const src = readFileSync(new URL("../src/lib/components/Chat.svelte", import.meta.url), "utf8");
  assert.ok(!src.includes("$effect.pre"), "no pre-effect layout reads remain");
  assert.ok(src.includes("onscroll={onScroll}"), "follow state driven by scroll events");
  assert.ok(src.includes("pinQueued"), "pin is rAF-coalesced");
}

// 5. The pin itself must stay single-write (scrollEl.scrollTop assignment in
//    the rAF), never the old double scrollToBottom per delta.
{
  const src = readFileSync(new URL("../src/lib/components/Chat.svelte", import.meta.url), "utf8");
  const rafPin = src.match(/requestAnimationFrame\(\(\) => \{[\s\S]*?\}\)/g) ?? [];
  assert.ok(
    rafPin.some((block) => block.includes("pinQueued = false")),
    "the coalescing rAF pin is present",
  );
}

// 6. Transcript.svelte: the session-switch freeze is gone. A big session
//    carries ~2000 tool steps, and mounting every ToolChip in one synchronous
//    pass is what froze the shell for seconds. The contract:
//      a) history renders through a tail-first window (`turns`), never the
//         full array directly in the each block
//      b) the window arms in $effect.pre — arming in a post effect would let
//         one full render mount the whole transcript before clamping
//      c) arming waits for the session's HISTORY (len > 0), not just its id
//         (a restored tab reports the id while messages are still empty)
//      d) older turns backfill in idle batches, holding the reader's place
//         with a reference-row correction: capture the row under the
//         scroller's top edge, restore its offset after the prepend. That
//         residual is ~0 where native scroll anchoring exists (composes
//         safely) and is the full correction where it doesn't. A
//         scrollHeight-delta compensation instead double-counts where
//         anchoring exists (yank = tremble), and no compensation slides the
//         reader to the top on engines without anchoring.
//      e) open-state keys are the ORIGINAL turn index, so backfill
//         prepends don't re-key open thinking blocks
{
  const src = readFileSync(new URL("../src/lib/components/chat/Transcript.svelte", import.meta.url), "utf8");
  assert.ok(!src.includes("displayTurns(brain.messages) as t"), "each block renders the window, not the full history");
  assert.ok(src.includes("{#each turns as t"), "window-backed each block present");
  const preEffect = src.match(/\$effect\.pre\(\(\) => \{[\s\S]*?\}\);/) ?? [];
  assert.ok(
    preEffect.some((block) => block.includes("readyFor = sid") && block.includes("len === 0")),
    "window arms pre-paint and waits for the history (len > 0)",
  );
  assert.ok(src.includes("requestIdleCallback"), "backfill yields to idle time");
  assert.ok(
    src.includes("captureRef") && src.includes("holdRef"),
    "reference-row compensation holds the reader's place across prepends",
  );
  assert.ok(
    !src.includes("scrollHeight - before"),
    "no scrollHeight-delta compensation (double-counts against anchoring)",
  );
  assert.ok(src.includes("allTurns.length - turns.length + tIdx"), "open-state keys are original-index stable");
}

// 7. mountBatch: batches are bounded by turn count AND total tool steps.
//    A step-dense history must not let "20 turns" mount 600 ToolChips.
{
  const { mountBatch } = await import("../src/lib/components/chat/messages.ts");
  const turn = (steps: number) =>
    ({ user: null, assistant: { steps: Array.from({ length: steps }, () => ({})) }, assistantIdx: -1 });
  const all = [turn(200), turn(50), turn(50), turn(50), turn(1)];

  // step budget caps the batch: walks back from the tail [1,50,...]: 1 + 50 = 51 ≤ 300 fits, + 50 = 101 > 100 busts it
  assert.equal(mountBatch(all, 5, 20, 100), 2, "step budget stops the walk");
  // turn budget caps even when steps are cheap
  assert.equal(mountBatch(all, 5, 2, 10_000), 2, "turn cap stops the walk");
  // the always-include-first rule: one giant turn alone still mounts
  assert.equal(mountBatch([turn(500)], 1, 20, 80), 1, "single oversized turn mounts");
  // small history mounts whole
  assert.equal(mountBatch(all, 4, 20, 160), 3, "150 fits 160, the 200-step 4th busts it");
}

// 8. The mount window must never make history look deleted. Real regression,
//    from the user's own session: 4 turns with step counts [2, 14, 171, 14].
//    Walking back off the tail with a 160-step budget clamps that to ONE turn
//    (14 + 171 busts it), and once backfill became demand-driven the other
//    three turns simply never appeared — "when I sent a message all old
//    responses disappeared". The window exists to defuse ~2000-step sessions;
//    a session whose whole history is cheap must mount whole on the first
//    paint, and the eager decision is made on the TOTAL, not one batch.
{
  const { armMount, totalSteps } = await import("../src/lib/components/chat/messages.ts");
  const turn = (steps: number) =>
    ({ user: null, assistant: { steps: Array.from({ length: steps }, () => ({})) }, assistantIdx: -1 });
  const hellow = [turn(2), turn(14), turn(171), turn(14)];

  assert.equal(totalSteps(hellow), 201, "the whole session is 201 steps");
  assert.equal(armMount(hellow, 40, 160), 4, "a cheap session arms fully, not at one turn");
  assert.equal(totalSteps([]), 0, "empty history has no steps");
  assert.equal(armMount([], 40, 160), 0, "nothing to mount");

  // a genuinely heavy session still windows, so the freeze stays fixed
  const heavy = Array.from({ length: 120 }, (_, i) => turn(i < 40 ? 40 : 20));
  assert.ok(totalSteps(heavy) > 600, "the heavy session busts the eager budget");
  const armed = armMount(heavy, 40, 160);
  assert.ok(armed >= 1 && armed < heavy.length, `heavy history arms as a window (${armed})`);
  assert.ok(armed <= 40, "the arm still respects the turn cap");
}

// 9. Backfill is demand-driven, but demand has two shapes, and a prepend needs
//    the matching correction or the view moves under the reader:
//      "edge" — the reader is on the newest response (this is where opening a
//         session or refreshing leaves them). Mounting above MUST re-pin to the
//         bottom; the one-shot reference correction that ran here before left
//         scrollTop alone while scrollHeight grew, so batch after batch the
//         viewport marched to the top of the transcript.
//      "hold" — the reader scrolled back into the window and is about to run
//         out of history; keep their reference row steady instead.
//      null   — mid-window: neither; don't interrupt, and don't yank.
{
  const { backfillMode } = await import("../src/lib/components/chat/messages.ts");
  const box = (scrollTop: number, scrollHeight: number, clientHeight: number) => ({
    scrollTop,
    scrollHeight,
    clientHeight,
  });
  // a 20000px window in a 900px viewport
  assert.equal(backfillMode(box(19100, 20000, 900)), "edge", "on the newest response");
  assert.equal(backfillMode(box(19050, 20000, 900)), "edge", "inside the edge slack");
  assert.equal(backfillMode(box(0, 20000, 900)), "hold", "at the top of the mounted window");
  assert.equal(backfillMode(box(899, 20000, 900)), "hold", "one viewport from the top");
  assert.equal(backfillMode(box(4000, 20000, 900)), null, "mid-window: reading, not loading");
  assert.equal(backfillMode(box(0, 500, 900)), "edge", "short session: everything already visible");
  assert.equal(backfillMode(box(0, 900, 900)), "edge", "exactly one viewport");
  assert.equal(backfillMode(box(0, 0, 0)), null, "unmeasured box: do nothing");
}

// 10. The hold must converge, not correct once. A batch's rows enter at their
//     contain-intrinsic-size estimate and inflate as they settle, so the offset
//     measured in the tick after the prepend is already stale. Re-apply the
//     same reference row's correction across frames, and bail if the reader
//     returns to the live edge mid-hold (never fight a scroll).
{
  const src = readFileSync(new URL("../src/lib/components/chat/Transcript.svelte", import.meta.url), "utf8");
  assert.match(src, /HOLD_FRAMES/, "the ref hold is frame-bounded, not one-shot");
  assert.ok(src.includes("armMount"), "arming mounts a cheap session whole");
  assert.ok(
    /requestAnimationFrame\(\(\) => hold/.test(src),
    "the correction re-applies on later frames to absorb late row inflation",
  );
  assert.ok(!src.includes("firstElementChild"), "the row walk ignores #msg-col's siblings (e.g. the compressing banner)");
  assert.ok(src.includes('getElementById("msg-col")'), "reference rows are read from the rows container");
}

// 11. Chat.svelte: the END of a turn needs the converging pin, not the
//     mid-stream single write. finalizeTurn drops the streaming draft and puts
//     the authoritative history row in its place; with content-visibility that
//     row enters at its 120px intrinsic estimate and inflates to its real
//     height over the frames AFTER the pin ran — their last turn in `hellow`
//     was 171 steps, tens of thousands of px. One write pins to a height that
//     is about to be wrong, so the view lands at the top of the transcript the
//     moment the response finishes ("good while responding, then back to the
//     first message when it ends"). The single write stays for streaming
//     deltas, where a layout read per token was the original thrash.
{
  const src = readFileSync(new URL("../src/lib/components/Chat.svelte", import.meta.url), "utf8");
  assert.match(src, /const turnEnded = /, "a finished turn is recognised as its own case");
  assert.match(
    src,
    /if \(turnEnded \|\| rowLanded\)[\s\S]{0,160}scrollToBottom\(\)/,
    "a finished turn converges on both edges of it",
  );
  assert.match(src, /const rowLanded = /, "the reload landing the row is caught too");
  const frames = src.match(/MAX_PIN_FRAMES = (\d+)/);
  assert.ok(frames && Number(frames[1]) >= 60, "the converge budget outlasts a slow, huge row");
  assert.match(
    src,
    /requestAnimationFrame\(\(\) => \{[\s\S]*?scrollEl\.scrollTop = scrollEl\.scrollHeight/,
    "streaming deltas still pin once per frame, not once per token",
  );
  assert.ok(!/if \(turnEnded\)\s*return;/.test(src), "the finished turn is pinned, not skipped");
}
