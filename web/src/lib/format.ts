/** Shared number formatting, so a duration reads the same in a tool chip, a
 *  thinking row and the live clock instead of drifting across three call sites. */

/** Seconds → one decimal with its unit, e.g. `4.2s`. Truncated, not rounded:
 *  a 4.96s call must not claim to be shorter than it ran. */
export function dur(seconds: number): string {
  return `${(Math.trunc(seconds * 10) / 10).toFixed(1)}s`;
}

/** Seconds → `4.2s` under a minute, then `2:17`. A long think still has to fit
 *  the right-hand slot without the row jittering as the digits grow. */
export function clock(seconds: number): string {
  if (seconds < 60) return dur(seconds);
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return `${m}:${String(s).padStart(2, "0")}`;
}
