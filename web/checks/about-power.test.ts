// Runnable check for the About modal's harness power controls:
// `node checks/about-power.test.ts`
//
// The modal is shared by both layouts (the dock brand-mark and the classic
// sidebar brand open the same panel), so a regression here breaks the only
// in-app way to stop or restart the brain. What this pins:
//
//   * the panel calls the two contract methods, spelled exactly — `app.restart`
//     and `app.shutdown`; a typo is a silent `-32601 method not found`
//   * power is two-step (arm → YES/NO): a single click must not kill the brain
//     mid-turn. The naive `onclick={() => runPower("shutdown")}` is the defect
//     this guards
//   * the buttons are icon-only with the word in `title`/`aria-label` — visible
//     labels push the group onto its own row, which is what we had to undo
//   * the row is only rendered inside the modal, and the state resets on close
//     (a half-armed confirm must not survive a reopen)
//   * the CSS keeps the control right-aligned on the *same* line as the avatar
//     buttons, which is where it was asked to live
//
// Source-text assertions: AboutPanel.svelte is a runes component, so a Node
// test reads the files rather than importing them.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const root = new URL("..", import.meta.url);
const read = (rel: string) => readFileSync(new URL(rel, root), "utf8");

const panel = read("src/lib/components/AboutPanel.svelte");
const theme = read("src/lib/theme.css");
const icons = read("src/lib/icons.ts");
const contract = read("../contract/methods.md");

// ---------- 1. the two contract methods are called, verbatim ----------
for (const method of ["app.restart", "app.shutdown"]) {
  assert.ok(panel.includes(`"${method}"`), `AboutPanel must call "${method}"`);
  // and they must be real, documented methods — not invented here
  assert.match(
    contract,
    new RegExp(`\\|\\s*\`${method.replace(".", "\\.")}\``),
    `contract/methods.md has no row for ${method}`,
  );
}

// ---------- 2. power is two-step: arm, then confirm ----------
assert.match(panel, /powerArmed\s*=\s*\$state/, "AboutPanel needs powerArmed state");
assert.match(panel, /onclick=\{\(\)\s*=>\s*\(powerArmed\s*=\s*"restart"\)\}/, "Restart must arm, not fire");
assert.match(panel, /onclick=\{\(\)\s*=>\s*\(powerArmed\s*=\s*"shutdown"\)\}/, "Shut down must arm, not fire");
// The only place runPower may be called is the armed YES button.
const runPowerCalls = panel.match(/runPower\(/g) ?? [];
assert.equal(runPowerCalls.length, 2, "runPower: one definition + one armed-YES call");
assert.ok(
  /powerArmed\)\s*void\s+runPower\(powerArmed\)/.test(panel),
  "the YES button must dispatch the armed action, not a literal",
);
// A bare, unarmed call site would be the one-click kill this avoids.
assert.ok(
  !/onclick=\{\(\)\s*=>\s*void\s+runPower\(/.test(panel),
  "runPower must never be wired directly to a click handler",
);

// ---------- 3. the control lives in the modal's action row ----------
const actionsAt = panel.indexOf('class="about-actions"');
const powerAt = panel.indexOf('class="about-power"');
assert.ok(actionsAt !== -1, "the modal still has an action row");
assert.ok(powerAt > actionsAt, "the power group must sit inside the action row");

// ---------- 4. closing resets the confirm state ----------
const closeFn = panel.slice(panel.indexOf("function close"), panel.indexOf("function onAvatarPick"));
assert.match(closeFn, /powerArmed\s*=\s*null/, "close() must disarm a pending confirm");
assert.match(closeFn, /powerNote\s*=\s*""/, "close() must clear the status note");

// ---------- 5. the glyphs exist in the registry ----------
assert.match(icons, /\bpower:\s*Power\b/, 'icons.ts must map "power"');
assert.match(icons, /"refresh-cw":\s*RefreshCw/, 'icons.ts must map "refresh-cw"');
for (const name of ["power", "refresh-cw"]) {
  assert.ok(panel.includes(`name="${name}"`), `AboutPanel should use the "${name}" icon`);
}

// ---------- 6. icon-only: the word lives in title/aria-label ----------
// Visible text ("Restart" / "Shut down") widened the group until the row
// wrapped it onto a second line. Each icon button must therefore carry the
// label as an attribute, and render no text of its own.
for (const [icon, label] of [["refresh-cw", "Restart"], ["power", "Shut down"]] as const) {
  const btn = panel.split(`name="${icon}"`)[0]?.split("<button").pop() ?? "";
  assert.match(btn, /aria-label="[^"]*"/, `${label}: icon button needs an aria-label`);
  assert.match(btn, /title="[^"]*"/, `${label}: icon button needs a title tooltip`);
  assert.match(btn, new RegExp(`aria-label="[^"]*${label}[^"]*"`), `${label}: aria-label must name the action`);
  assert.match(btn, new RegExp(`title="[^"]*${label}[^"]*"`), `${label}: title must name the action`);
}
// No visible label text inside the power buttons (the glyph is the only child).
const powerGroup = panel.slice(panel.indexOf('class="about-power"'), panel.indexOf("{#if powerNote}"));
assert.ok(
  !/>\s*(Restart|Shut down)\s*</.test(powerGroup),
  "the power buttons must be icon-only — a visible Restart/Shut down label wraps the row",
);

// ---------- 7. the row is right-aligned on one line, and danger is coloured ----------
assert.match(theme, /\.about-power\s*\{[^}]*margin-left:\s*auto/, ".about-power must push to the right");
assert.match(theme, /\.about-power\s*\{[^}]*flex-shrink:\s*0/, "the power group must not be squeezed");
assert.match(theme, /\.about-btn-danger\s*\{/, "a danger button style must exist");
assert.match(theme, /\.about-power-note\s*\{/, "the outcome note needs a style");
// The row must stay a single flex line (wrap would put power on its own row).
assert.match(theme, /\.about-actions\s*\{[^}]*align-items:\s*center/, "the action row must centre its items");

console.log("about-power: ok");
