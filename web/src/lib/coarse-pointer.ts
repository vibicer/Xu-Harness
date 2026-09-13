/**
 * The pure half of touch detection: keep a callback current with a
 * `(pointer: coarse)` media query.
 *
 * Split out of `touch.svelte.ts` so it can be exercised by `node --test` —
 * that runner does not compile Svelte, so nothing importable from a test may
 * use runes. `touch.svelte.ts` owns the reactive state; this owns the
 * subscription rules, which are where the bugs are (no `matchMedia`, legacy
 * `addListener`, a listener that must not outlive the module).
 */

/** The bits of `MediaQueryList` this uses — optional legacy spellings included. */
export interface MediaLike {
  readonly matches: boolean;
  addEventListener?(type: "change", cb: () => void): void;
  removeEventListener?(type: "change", cb: () => void): void;
  /** Deprecated Safari ≤13 spelling. */
  addListener?(cb: () => void): void;
  removeListener?(cb: () => void): void;
}

/**
 * Push `mq.matches` into `set`, then keep it current. Returns a detach.
 *
 * A missing `mq` (no `matchMedia`, or a query the platform cannot answer) is
 * not an error: the caller keeps whatever it started with, which is `false` —
 * desktop behaviour, the conservative default.
 */
export function watchCoarsePointer(
  mq: MediaLike | undefined,
  set: (coarse: boolean) => void,
): () => void {
  if (!mq) return () => {};
  set(mq.matches);
  const onChange = (): void => set(mq.matches);
  if (mq.addEventListener) mq.addEventListener("change", onChange);
  else mq.addListener?.(onChange);
  return () => {
    if (mq.removeEventListener) mq.removeEventListener("change", onChange);
    else mq.removeListener?.(onChange);
  };
}
