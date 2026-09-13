// Runnable check for the reasoning-effort picker:
//
//   node checks/effort-picker.test.ts
//
// The picker lives in the agent-state panel next to MODEL and PERSONA, and it
// has one design rule worth pinning: the ladder is *not* written down in the
// frontend. It is fetched from the brain's `app.info`, so the shell can only
// offer levels the wire actually accepts and the two lists cannot drift.
//
// The second rule is the one that is easy to get wrong later: "default" is a
// real option, distinct from every level. It means *send no effort field*, so
// the provider (or the router in front of it) applies its own default. A
// picker that only listed levels would make "you decide" unreachable.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const ROOT = new URL("../", import.meta.url);
const AGENT_STATE = "src/lib/components/AgentState.svelte";
const PROVIDERS = "src/lib/store/providers.svelte.ts";

const agentState = readFileSync(new URL(AGENT_STATE, ROOT), "utf8");
const providers = readFileSync(new URL(PROVIDERS, ROOT), "utf8");

let groups = 0;
const group = (name: string, fn: () => void) => { fn(); groups++; void name; };

group("ladder-comes-from-the-brain", () => {
  assert.match(agentState, /call<\{[^}]*reasoning_efforts[^}]*\}>\("app\.info"\)/,
    "the picker must read the ladder from app.info, not keep its own copy");
  // The seven levels must not appear as a literal list in the component. One
  // occurrence of a level as a *string* would mean the copy crept back in.
  for (const level of ["minimal", "xhigh"]) {
    assert.doesNotMatch(agentState, new RegExp(`["'\`]${level}["'\`]`),
      `"${level}" is hardcoded in ${AGENT_STATE}; the ladder belongs to the brain`);
  }
});

group("default-is-not-a-level", () => {
  assert.match(agentState, /pickEffort\(null\)/,
    "the picker must offer a null choice — sending no field is what lets the provider decide");
  assert.match(providers, /async setEffort\(effort: string \| null\)/,
    "setEffort must accept null, or 'default' is unreachable");
});

group("picker-sends-the-pick", () => {
  assert.match(providers, /client\.call\("state\.set_effort"/,
    "setEffort must call the state.set_effort RPC");
  assert.match(agentState, /void brain\.setEffort\(effort\)/,
    "the picker must route through the store, not call the client directly");
});

group("row-is-bound-to-session-state", () => {
  assert.match(agentState, /class="l">EFFORT</,
    "the panel must render an EFFORT row");
  assert.match(agentState, /brain\.state\.reasoning_effort/,
    "the row must read the session's current effort from store state");
  assert.match(agentState, /bind:this=\{epickEl\}/,
    "the row needs its own element ref for the outside-click handler");
  assert.match(agentState, /epickEl && !epickEl\.contains/,
    "the EFFORT menu must close on an outside click like its siblings");
});

console.log(`effort-picker: ${groups} groups passed`);
void fileURLToPath;
