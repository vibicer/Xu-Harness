// Runnable check for mobile keyboard inset: `node checks/keyboard.test.ts`
import assert from "node:assert/strict";
import { keyboardInset, type ViewportLike } from "../src/lib/keyboard.ts";

const vv = (height: number, offsetTop = 0): ViewportLike => ({ height, offsetTop });

// No visual viewport (older desktop Safari) => never invent an inset.
assert.equal(keyboardInset(null, 800), 0);
assert.equal(keyboardInset(undefined, 800), 0);

// Desktop: the visual viewport is the whole window.
assert.equal(keyboardInset(vv(800), 800), 0);

// Phone with the keyboard up: 800px window, 460px visible => 340px covered.
assert.equal(keyboardInset(vv(460), 800), 340);

// Browser panned the page up to reveal the focused composer: the visible band
// starts 40px below the layout top, so only 300px of the bottom is really gone.
assert.equal(keyboardInset(vv(460, 40), 800), 300);

// A panned-further page must not produce a negative inset (would grow the shell).
assert.equal(keyboardInset(vv(700, 200), 800), 0);

// Sub-pixel viewports round to whole CSS px — the var feeds a calc().
assert.equal(keyboardInset(vv(460.4), 800), 340);

console.log("keyboard inset: ok");
