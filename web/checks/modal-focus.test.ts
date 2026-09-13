// Runnable check for modal keyboard handling and composer focus ownership:
// `node checks/modal-focus.test.ts`
//
// Why source assertions: these bugs are invisible to a type-checker and need a
// real browser + focus to reproduce, so the guard has to read the source. Four
// invariants, one per fixed component:
//
//   * AboutPanel / SubRunModal / Lightbox — Escape and Tab live on
//     `<svelte:window>`, focus moves into the dialog on open and back to the
//     trigger on close. Bound to the element, the handler never fired: the
//     trigger kept focus, so the keydown never reached the dialog.
//   * Chat.svelte — the composer-focus effect must key off view/session, not
//     `brain.messages` (replaced every turn), and must stand down while a
//     `[aria-modal="true"]` dialog is up.
//   * AgentState.svelte — PERSONA and PRESET each own their element ref, and
//     both are in the outside-click guard.
//
// The lesson is not shrug: before writing files, confirm the exact rule the
// check will read.

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const ROOT = new URL("../", import.meta.url);
const read = (p: string) => readFileSync(new URL(p, ROOT), "utf8");

const about = read("src/lib/components/AboutPanel.svelte");
const subrun = read("src/lib/components/chat/SubRunModal.svelte");
const lightbox = read("src/lib/components/Lightbox.svelte");
const chat = read("src/lib/components/Chat.svelte");
const agentState = read("src/lib/components/AgentState.svelte");

let groups = 0;
const group = (name: string, fn: () => void) => { fn(); groups++; void name; };

group("about-panel: window-level escape, focus in/out, tab trap", () => {
  assert.match(about, /<svelte:window onkeydown=\{onWindowKey\}/,
    "AboutPanel must handle keys on <svelte:window> — the backdrop never receives them");
  assert.ok(!/class="about-backdrop"[^>]*onkeydown/.test(about),
    "the backdrop's dead onkeydown is gone");
  assert.match(about, /bind:this=\{panelEl\}[\s\S]{0,120}?role="dialog"/,
    "the dialog element is bound so focus can be moved into it");
  assert.match(about, /tabindex="-1"/, "the dialog must be focusable for initial focus");
  assert.match(about, /restoreFocus = document\.activeElement/,
    "the trigger's focus must be captured on open");
  assert.match(about, /restoreFocus\?\.focus\?\.\(\)/,
    "focus must be handed back to the trigger on close");
  assert.match(about, /e\.key !== "Tab"/, "Tab must be trapped inside the dialog");
});

group("sub-run modal: window escape, focus in/out, tab trap", () => {
  assert.match(subrun, /<svelte:window onkeydown=\{onWindowKey\}/,
    "SubRunModal must handle Escape on the window, not on the element");
  assert.match(subrun, /bind:this=\{modalEl\}/, "the dialog element is bound for initial focus");
  assert.match(subrun, /restoreFocus = document\.activeElement/);
  assert.match(subrun, /restoreFocus\.focus\?\.\(\)/);
  assert.match(subrun, /e\.key !== "Tab"/, "Tab must be trapped inside the dialog");
});

group("sub-run tabs: arrow keys, roving tabindex, tabpanel wiring", () => {
  assert.match(subrun, /function onTabKey\(e: KeyboardEvent, idx: number\)/,
    "the tablist needs an arrow-key handler");
  assert.match(subrun, /onkeydown=\{\(e\) => onTabKey\(e, i\)\}/, "each tab wires the handler");
  assert.match(subrun, /ArrowRight/, "ArrowRight must move to the next tab");
  assert.match(subrun, /ArrowLeft/, "ArrowLeft must move to the previous tab");
  assert.match(subrun, /tabindex=\{t\.id === sub\.id \? 0 : -1\}/,
    "roving tabindex keeps one tab in the page tab order");
  assert.match(subrun, /aria-controls="sub-tabpanel"/, "each tab must point at its tabpanel");
  assert.match(subrun, /role="tabpanel"/, "the body must be the tabpanel");
});

group("lightbox: escape + tab trapped on the window, focus restored", () => {
  assert.match(lightbox, /<svelte:window onkeydown=\{onWindowKey\}/,
    "Lightbox must handle Escape on the window");
  assert.match(lightbox, /bind:this=\{boxEl\}/, "the overlay is bound for initial focus");
  assert.match(lightbox, /restoreFocus = document\.activeElement/);
  assert.match(lightbox, /restoreFocus\?\.focus\?\.\(\)/, "focus must return to the thumbnail");
  assert.match(lightbox, /e\.key === "Tab"[\s\S]{0,80}?preventDefault/,
    "Tab must not walk the transcript behind the overlay");
});

group("chat: composer focus keys off view/session, and defers to a modal", () => {
  const effect = chat.match(/\$effect\(\(\) => \{[\s\S]*?void brain\.view[\s\S]*?\n  \}\);/)?.[0] ?? "";
  assert.ok(effect, "the composer-focus effect must still exist");
  // Comments may name brain.messages; the code must not read it.
  const code = effect.replace(/\/\/[^\n]*/g, "");
  assert.ok(!code.includes("brain.messages"),
    "the focus effect must not depend on brain.messages — replaced every turn");
  assert.match(effect, /brain\.session\?\.id/, "it must refocus on a session switch");
  assert.match(effect, /isContentEditable/, "it must stand down while an editable element has focus");
  assert.match(chat, /document\.querySelector\('\[aria-modal="true"\]'\)\)\s*return/,
    "the global key handler must bail while a modal is open");
});

group("agent-state: persona and preset own separate refs", () => {
  assert.match(agentState, /let presetEl = \$state<HTMLDivElement \| null>\(null\)/,
    "PRESET needs its own ref — sharing ppickEl ran the outside-click test against the wrong element");
  const presetRow = agentState.slice(agentState.indexOf('class="l">PRESET'));
  assert.match(presetRow, /bind:this=\{presetEl\}/, "the PRESET picker must bind presetEl");
  assert.match(agentState, /!modelOpen && !effortOpen && !personaOpen && !presetOpen/,
    "the outside-click effect must also run while the PRESET menu is open");
  assert.match(agentState, /presetEl && !presetEl\.contains/,
    "an outside click must close the PRESET menu");
});

console.log(`modal-focus: ${groups} groups passed`);
