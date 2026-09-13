/**
 * Mobile on-screen keyboard inset.
 *
 * A phone browser does NOT shrink the *layout* viewport when the keyboard
 * opens — `100dvh` keeps reporting the full window height while the keyboard
 * covers the bottom third of it. This shell is a fixed-height flex column with
 * `body { overflow: hidden }` and the composer pinned at the bottom, so with no
 * compensation the composer simply sits under the keyboard: the one thing you
 * must tap to type.
 *
 * `VisualViewport` is the truth about what's actually visible. Publish the
 * covered height as `--kb` on the root; `theme.css` subtracts it from the shell
 * height, which pushes the composer up to the keyboard's edge. Desktop has no
 * visual-viewport API mismatch (`vv.height === innerHeight`), so `--kb` stays 0
 * and nothing moves.
 */

/** The subset of `VisualViewport` we read — keeps this testable with a fake. */
export interface ViewportLike {
  readonly height: number;
  readonly offsetTop: number;
}

/**
 * Height in CSS px the keyboard (plus any browser UI that scrolled into the
 * layout) hides at the bottom of the window.
 *
 * `offsetTop` is subtracted as well as `height`: when the browser scrolls the
 * focused input into view, the visible band starts below the layout top, so the
 * uncovered bottom edge is `vv.height + vv.offsetTop`, not `vv.height`.
 */
export function keyboardInset(
  vv: ViewportLike | null | undefined,
  innerHeight: number,
): number {
  if (!vv || !(innerHeight > 0)) return 0;
  return Math.max(0, Math.round(innerHeight - vv.height - vv.offsetTop));
}

/**
 * Wire `--kb` to the visual viewport. Returns a detach function.
 *
 * Both `resize` (keyboard opens/closes) and `scroll` (browser pans the page to
 * reveal a focused control) change the inset, so both are bound.
 */
export function installKeyboardInset(win: Window = window): () => void {
  // `visualViewport` is typed non-optional on Window but absent in older Safari.
  // Typed as the DOM interface, not `ViewportLike`: the listeners need
  // EventTarget, and a VisualViewport satisfies ViewportLike structurally.
  const vv: VisualViewport | null = win.visualViewport ?? null;
  const root = win.document.documentElement;

  const paint = (): void => {
    root.style.setProperty("--kb", `${keyboardInset(vv, win.innerHeight)}px`);
  };

  paint();
  if (!vv) return () => {};
  vv.addEventListener("resize", paint);
  vv.addEventListener("scroll", paint);
  return () => {
    vv.removeEventListener("resize", paint);
    vv.removeEventListener("scroll", paint);
  };
}
