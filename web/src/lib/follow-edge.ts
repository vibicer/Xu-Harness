/** Keep a scroll box pinned to the live edge while the user is at the bottom.
 *
 *  Why watch the DOM instead of model state: a streamed sub-agent run grows by
 *  appending text to the *last existing step*, so `steps.length` and `result`
 *  never change and an effect keyed on them never fires. A MutationObserver
 *  catches every growth — token deltas, tool output, markdown that renders a
 *  frame late — and the only thing that ever clears "following" is a real
 *  scroll event, which is the only honest signal of user intent.
 *
 *  Runnable check: `node checks/follow-edge.test.ts`
 */

/** The slice of an element this needs — kept structural so the check can run
 *  without a DOM. */
export interface ScrollBox {
  scrollTop: number;
  readonly scrollHeight: number;
  readonly clientHeight: number;
  addEventListener(type: "scroll", handler: () => void): void;
  removeEventListener(type: "scroll", handler: () => void): void;
}

/** Watch a box for content growth; returns a disposer. */
export type Observe = (box: ScrollBox, onGrow: () => void) => () => void;

const domObserve: Observe = (box, onGrow) => {
  if (typeof MutationObserver !== "function") return () => {};
  const mo = new MutationObserver(onGrow);
  mo.observe(box as unknown as Node, { childList: true, characterData: true, subtree: true });
  return () => mo.disconnect();
};

/** Pin `box` to the bottom until the user scrolls away, and resume when they
 *  scroll back. Call from an `$effect` and return the disposer.
 *
 *  `slack` is how far from the bottom still counts as following. */
export function followEdge(box: ScrollBox, slack = 40, observe: Observe = domObserve): () => void {
  let following = true;
  const pin = (): void => {
    box.scrollTop = box.scrollHeight;
  };
  // Our own pin also fires this, which lands at gap 0 and keeps following true.
  const onScroll = (): void => {
    following = box.scrollHeight - box.scrollTop - box.clientHeight <= slack;
  };
  const onGrow = (): void => {
    if (following) pin();
  };
  box.addEventListener("scroll", onScroll);
  const stop = observe(box, onGrow);
  pin();
  return () => {
    box.removeEventListener("scroll", onScroll);
    stop();
  };
}
