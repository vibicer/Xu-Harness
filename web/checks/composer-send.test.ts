// Runnable check for the composer send pipeline: `node --test checks/composer-send.test.ts`
//
// Three real bugs this pins, all in Composer.svelte / PendingImages.svelte:
//   1. a failed brain.send cleared the box and only console.error'd — the
//      user's text and attachments were gone with no visible reason;
//   2. a second Enter inside the in-flight window wiped the box and then hit
//      the `busy` early-return, so the text was silently dropped;
//   3. attaching the same image twice produced two identical data URLs, and
//      the keyed each-blocks threw `each_key_duplicate` — taking the preview
//      strip (and the transcript's attachment strip) down with it.
// The components need a DOM, so the contract is asserted on the source text,
// same approach as transcript-perf.test.ts / config-panels.test.ts.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const read = (rel: string): string => readFileSync(new URL(rel, import.meta.url), "utf8");

// 1. The `busy` guard must run BEFORE the box is cleared. Otherwise a send
//    inside the in-flight window clears `input` / `pendingImages` and then
//    returns without sending — the draft is lost.
{
  const src = read("../src/lib/components/chat/Composer.svelte");
  const send = src.slice(
    src.indexOf("async function send"),
    src.indexOf("async function onFilesChosen"),
  );
  const busyAt = send.indexOf("if (busy) return;");
  const clearAt = send.indexOf('input = "";');
  assert.ok(busyAt >= 0, "the busy guard is present");
  assert.ok(clearAt >= 0, "a real send still clears the box");
  assert.ok(busyAt < clearAt, "the busy check precedes clearing anything");
}

// 2. A rejected send restores the text + attachments and surfaces a visible
//    error that clears on the next attempt. The cause is still logged.
{
  const src = read("../src/lib/components/chat/Composer.svelte");
  const send = src.slice(
    src.indexOf("async function send"),
    src.indexOf("async function onFilesChosen"),
  );
  assert.match(
    send,
    /catch \(e\) \{[\s\S]*?input = text;[\s\S]*?pendingImages = images;[\s\S]*?sendError =/,
    "a failed send restores the draft and records an error",
  );
  assert.ok(send.includes('console.error("send failed", e)'), "the cause is still logged for the operator");
  assert.match(send, /sendError = null;/, "the error clears on the next send attempt");
  assert.match(
    src,
    /\{#if sendError\}[\s\S]{0,160}role="alert"/,
    "the error is rendered, visibly, near the composer",
  );
}

// 3. Attaching the same image twice is deduped on insert: against what is
//    already attached AND within one pick.
{
  const src = read("../src/lib/components/chat/Composer.svelte");
  const fn = src.slice(src.indexOf("async function onFilesChosen"));
  assert.ok(fn.includes("new Set(pendingImages)"), "onFilesChosen dedupes against the current strip");
  assert.ok(fn.includes("seen.add(img)"), "…and within the incoming pick");
}

// 4. Both preview strips key by index, never by the (possibly duplicate) URL,
//    so remove-by-index stays correct.
{
  const pending = read("../src/lib/components/chat/PendingImages.svelte");
  assert.match(pending, /\{#each images as img, i \(i\)\}/, "the pending strip keys by index");
  assert.ok(
    !/\{#each images as img, i \(img\)\}/.test(pending),
    "…and no longer keys by the data URL",
  );
  const transcript = read("../src/lib/components/chat/Transcript.svelte");
  assert.match(
    transcript,
    /\{#each contentImages\(t\.user\.content\) as img, i \(i\)\}/,
    "the transcript attachment strip keys by index",
  );
}
