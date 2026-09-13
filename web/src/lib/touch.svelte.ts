/**
 * "Is this a touch-first device?" — one reactive answer for the whole shell.
 *
 * `@media (pointer: coarse)` answers it for CSS, but the composer needs it in
 * script: Enter means *send* on a physical keyboard and *newline* on a soft one,
 * and a chat box that steals Enter from a phone user can never write two lines.
 * Duplicating the query in JS is the only way to keep the two in step, so it
 * lives here once, in the file that documents why they must agree.
 *
 * Read `touch.coarse` from a component to subscribe. It is `$state`, so a device
 * that changes input mode (a laptop with a touchscreen, an iPad with a Magic
 * Keyboard) re-renders whoever asked. The subscription rules are the pure
 * `watchCoarsePointer` in coarse-pointer.ts, which is what the checks test.
 */
import { watchCoarsePointer } from "./coarse-pointer";

let coarse = $state(false);
let detach: () => void = () => {};

if (typeof matchMedia === "function") {
  detach = watchCoarsePointer(matchMedia("(pointer: coarse)"), (v) => (coarse = v));
}

export const touch = {
  get coarse(): boolean {
    return coarse;
  },
  /** Stop tracking. Only for teardown paths; the shell never calls it. */
  stop(): void {
    detach();
    detach = () => {};
  },
};
