// Runnable check for live-edge auto-follow: `node checks/follow-edge.test.ts`
import assert from "node:assert/strict";
import { followEdge, type Observe, type ScrollBox } from "../src/lib/follow-edge.ts";

/** Fake scroll box: `grow` adds content height and fires the observer, like a
 *  streamed token appending to the last step. */
function box(clientHeight = 100): ScrollBox & {
  grow(px: number): void;
  userScrollTo(top: number): void;
  height: number;
  fire(): void;
} {
  let handler: (() => void) | null = null;
  let onGrow: (() => void) | null = null;
  const b = {
    scrollTop: 0,
    height: clientHeight,
    get scrollHeight() {
      return b.height;
    },
    get clientHeight() {
      return clientHeight;
    },
    addEventListener(_t: "scroll", h: () => void) {
      handler = h;
    },
    removeEventListener() {
      handler = null;
    },
    fire() {
      handler?.();
    },
    grow(px: number) {
      b.height += px;
      onGrow?.();
    },
    userScrollTo(top: number) {
      b.scrollTop = top;
      b.fire();
    },
  };
  const observe: Observe = (_box, grow) => {
    onGrow = grow;
    return () => {
      onGrow = null;
    };
  };
  return Object.assign(b, { observe });
}

// 1. Content growth pins to the bottom (the reported bug: it did not).
{
  const b = box();
  const stop = followEdge(b, 40, (b as any).observe);
  b.grow(400);
  assert.equal(b.scrollTop, 500, "should pin after growth");
  b.grow(300);
  assert.equal(b.scrollTop, 800, "should keep pinning on every growth");
  stop();
}

// 2. Text-only growth still follows — this is what token deltas look like, and
//    what a steps.length/result-keyed effect missed entirely.
{
  const b = box();
  const stop = followEdge(b, 40, (b as any).observe);
  for (let i = 0; i < 20; i++) b.grow(12);
  assert.equal(b.scrollTop, b.scrollHeight, "many small text growths still follow");
  stop();
}

// 3. Scrolling up stops the pin — reading is not interrupted.
{
  const b = box();
  const stop = followEdge(b, 40, (b as any).observe);
  b.grow(900); // scrollTop = 1000
  b.userScrollTo(200);
  b.grow(100);
  assert.equal(b.scrollTop, 200, "must not yank a reader back to the bottom");
  stop();
}

// 4. Scrolling back to the bottom resumes following.
{
  const b = box();
  const stop = followEdge(b, 40, (b as any).observe);
  b.grow(900);
  b.userScrollTo(200);
  b.grow(100); // still parked
  b.userScrollTo(b.scrollHeight - b.clientHeight); // back to the edge
  b.grow(50);
  assert.equal(b.scrollTop, b.scrollHeight, "returning to the edge resumes follow");
  stop();
}

// 5. Within `slack` of the bottom counts as following (a stray wheel tick of a
//    few px must not permanently unpin the stream — the old bug's mechanism).
{
  const b = box();
  const stop = followEdge(b, 40, (b as any).observe);
  b.grow(900);
  b.userScrollTo(970); // 30px gap, inside slack
  b.grow(100);
  assert.equal(b.scrollTop, b.scrollHeight, "small gap still follows");
  stop();
}

// 6. Disposing detaches everything: no pinning after unmount.
{
  const b = box();
  const stop = followEdge(b, 40, (b as any).observe);
  stop();
  b.scrollTop = 0;
  b.grow(500);
  assert.equal(b.scrollTop, 0, "disposed follower must not touch scrollTop");
}

console.log("follow-edge: 6/6 checks passed");
