// Runnable check for touch detection: `node checks/touch.test.ts`
import assert from "node:assert/strict";
import { watchCoarsePointer, type MediaLike } from "../src/lib/coarse-pointer.ts";

/** Fake MediaQueryList: records what was subscribed so detach can be asserted. */
function fakeMq(initial: boolean, legacy = false): MediaLike & { fire(): void; subs: number } {
  const fns = new Set<() => void>();
  const mq = {
    matches: initial,
    subs: 0,
    fire() {
      for (const fn of fns) fn();
    },
  } as MediaLike & { fire(): void; subs: number };
  if (legacy) {
    mq.addListener = (fn) => { fns.add(fn); mq.subs++; };
    mq.removeListener = (fn) => { fns.delete(fn); mq.subs--; };
  } else {
    mq.addEventListener = (_t, fn) => { fns.add(fn); mq.subs++; };
    mq.removeEventListener = (_t, fn) => { fns.delete(fn); mq.subs--; };
  }
  return mq;
}

const seen: boolean[] = [];
const collect = (v: boolean): void => { seen.push(v); };

// No matchMedia at all (older desktop, or a non-browser host): stay desktop.
seen.length = 0;
const detachNone = watchCoarsePointer(undefined, collect);
assert.deepEqual(seen, [], "must not report anything without a query");
detachNone(); // and the returned detach must be callable

// Modern browser: the current value is pushed immediately, then on change.
seen.length = 0;
const touch = fakeMq(true);
const detach = watchCoarsePointer(touch, collect);
assert.deepEqual(seen, [true], "initial match reported");
touch.fire();
assert.deepEqual(seen, [true, true], "change re-reads matches");

// The value, not the event, is what matters — a flip to fine pointer propagates.
seen.length = 0;
touch.matches = false;
touch.fire();
assert.deepEqual(seen, [false], "mouse reattached (iPad + Magic Keyboard)");

// Detach must actually unsubscribe: a leak would keep writing to dead state.
assert.equal(touch.subs, 1);
detach();
assert.equal(touch.subs, 0);
seen.length = 0;
touch.fire();
assert.deepEqual(seen, [], "no writes after detach");

// Legacy Safari ≤13: only addListener/removeListener exist, same contract.
seen.length = 0;
const old = fakeMq(true, true);
const detachOld = watchCoarsePointer(old, collect);
assert.deepEqual(seen, [true], "legacy path reports initial");
assert.equal(old.subs, 1, "legacy path subscribed");
detachOld();
assert.equal(old.subs, 0, "legacy path unsubscribed");

console.log("touch detection: ok");
