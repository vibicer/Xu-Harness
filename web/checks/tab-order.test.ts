// Runnable check for session-tab reordering: `node checks/tab-order.test.ts`
import assert from "node:assert/strict";
import { moveAcross, moveBefore, shiftBy } from "../src/lib/tab-order.ts";

const ids = () => ["a", "b", "c", "d"];
/** Each tab is 100px wide, laid out left to right: a=0, b=100, c=200, d=300. */
const rect = (id: string) => ({ left: "abcd".indexOf(id) * 100, width: 100 });

// 1. Drag right: insert before the target, not after it
assert.deepEqual(moveBefore(ids(), "a", "c"), ["b", "a", "c", "d"]);

// 2. Drag left
assert.deepEqual(moveBefore(ids(), "d", "b"), ["a", "d", "b", "c"]);

// 3. Onto itself → same reference (no persist, no re-render)
{
  const before = ids();
  assert.equal(moveBefore(before, "b", "b"), before);
}

// 4. Onto the tab already behind it → no-op. Removing first would shift the
//    target and land the tab one slot too far right.
{
  const before = ids();
  assert.equal(moveBefore(before, "a", "b"), before);
}

// 5. Dropping past the end parks it last; already-last is a no-op
assert.deepEqual(moveBefore(ids(), "b", null), ["a", "c", "d", "b"]);
{
  const before = ids();
  assert.equal(moveBefore(before, "d", null), before);
}

// 6. Unknown ids never mutate the strip (a tab closed mid-drag)
{
  const before = ids();
  assert.equal(moveBefore(before, "zz", "b"), before);
  assert.equal(moveBefore(before, "a", "zz"), before);
}

// 7. First and last stay reachable in both directions
assert.deepEqual(moveBefore(ids(), "a", null), ["b", "c", "d", "a"]);
assert.deepEqual(moveBefore(ids(), "d", "a"), ["d", "a", "b", "c"]);

// 8. Keyboard: one slot at a time, clamped at both ends
assert.deepEqual(shiftBy(ids(), "b", -1), ["b", "a", "c", "d"]);
assert.deepEqual(shiftBy(ids(), "b", 1), ["a", "c", "b", "d"]);
{
  const before = ids();
  assert.equal(shiftBy(before, "a", -1), before, "already first");
  assert.equal(shiftBy(before, "d", 1), before, "already last");
  assert.equal(shiftBy(before, "zz", 1), before, "unknown id");
}

// 9. A single tab cannot move anywhere
{
  const one = ["a"];
  assert.equal(shiftBy(one, "a", 1), one);
  assert.equal(moveBefore(one, "a", null), one);
}

// ---- live reorder while dragging (moveAcross) ----

// 10. Travelling right over the immediate neighbour: nothing until the pointer
//     passes b's midpoint (150), then a lands after b. This is the case that
//     insert-before could not express at all — a one-slot nudge right.
{
  const before = ids();
  assert.equal(moveAcross(before, "a", "b", 120, rect("b")), before, "before the midpoint");
  assert.deepEqual(moveAcross(ids(), "a", "b", 160, rect("b")), ["b", "a", "c", "d"]);
}

// 11. Travelling left is mirrored: past c's midpoint going left lands before c
{
  const before = ids();
  assert.equal(moveAcross(before, "d", "c", 280, rect("c")), before, "not there yet");
  assert.deepEqual(moveAcross(ids(), "d", "c", 240, rect("c")), ["a", "b", "d", "c"]);
}

// 12. Exactly on the midpoint commits the move (>= / <=, never a dead pixel)
assert.deepEqual(moveAcross(ids(), "a", "b", 150, rect("b")), ["b", "a", "c", "d"]);

// 13. Crossing several tabs in one gesture keeps landing in the right slot
{
  let live = ids();
  live = moveAcross(live, "a", "b", 160, rect("b"));   // -> b a c d
  live = moveAcross(live, "a", "c", 260, rect("c"));   // -> b c a d
  live = moveAcross(live, "a", "d", 360, rect("d"));   // -> b c d a
  assert.deepEqual(live, ["b", "c", "d", "a"]);
}

// 14. Same tab, or an unknown one, never mutates
{
  const before = ids();
  assert.equal(moveAcross(before, "b", "b", 150, rect("b")), before);
  assert.equal(moveAcross(before, "zz", "b", 160, rect("b")), before);
  assert.equal(moveAcross(before, "a", "zz", 160, rect("b")), before);
}

// 15. No oscillation: after a swap the pointer sits still while the tabs move
//     under it, and the direction guard must refuse to swap back.
{
  const swapped = moveAcross(ids(), "a", "b", 160, rect("b")); // ["b","a","c","d"]
  // `a` is now at index 1, `b` at 0 — b's rect is where a used to be.
  assert.equal(
    moveAcross(swapped, "a", "b", 160, rect("a")),
    swapped,
    "pointer right of b's new midpoint must not swap back",
  );
}

console.log("tab-order: 15/15 checks passed");
