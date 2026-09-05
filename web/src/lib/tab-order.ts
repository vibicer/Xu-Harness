/** Reordering the open-session tab strip.
 *
 *  Tab order is frontend-only state (`openSessionIds`, persisted to
 *  localStorage) — the brain neither knows nor cares about it. Drag-and-drop and
 *  the keyboard shortcut both route through `moveBefore` so the four shells
 *  cannot drift apart on the off-by-one that insert-before invites.
 *
 *  Runnable check: `node checks/tab-order.test.ts`
 */

/** Move `dragged` so it sits immediately before `target`.
 *
 *  Insert-before is what browsers and editors do when a tab is dropped onto
 *  another. Dropping past the end of the strip (`target` absent) parks the tab
 *  last. Returns the same array reference when nothing would change, so callers
 *  can skip a persist + re-render.
 */
export function moveBefore(ids: string[], dragged: string, target: string | null): string[] {
  const from = ids.indexOf(dragged);
  if (from < 0) return ids;
  // Dropping a tab on itself, or onto the tab already behind it, is a no-op —
  // removing first would otherwise shift the target and move it one too far.
  if (dragged === target) return ids;
  const rest = ids.filter((id) => id !== dragged);
  if (target === null) {
    if (from === ids.length - 1) return ids;
    return [...rest, dragged];
  }
  const to = rest.indexOf(target);
  if (to < 0) return ids;
  if (to === from) return ids;
  return [...rest.slice(0, to), dragged, ...rest.slice(to)];
}

/** Reorder live while the pointer travels: move `dragged` to `target`'s slot as
 *  soon as the pointer crosses `target`'s midpoint.
 *
 *  Insert-before alone made dragging onto the *immediate* neighbour a no-op, so
 *  a one-slot move needed an overshoot onto the tab beyond it. The midpoint
 *  decides the side instead: past it going right lands after, past it going left
 *  lands before.
 *
 *  The direction guard is what stops oscillation — once swapped, the tabs move
 *  under a stationary pointer, and without it they would flip back and forth
 *  forever. Returns the same array reference when nothing changes.
 */
export function moveAcross(
  ids: string[],
  dragged: string,
  target: string,
  pointerX: number,
  rect: { left: number; width: number },
): string[] {
  const from = ids.indexOf(dragged);
  const at = ids.indexOf(target);
  if (from < 0 || at < 0 || from === at) return ids;
  const mid = rect.left + rect.width / 2;
  if (from < at && pointerX < mid) return ids; // travelling right, not there yet
  if (from > at && pointerX > mid) return ids; // travelling left, not there yet
  const rest = ids.filter((id) => id !== dragged);
  const to = rest.indexOf(target);
  return from < at
    ? [...rest.slice(0, to + 1), dragged, ...rest.slice(to + 1)]
    : [...rest.slice(0, to), dragged, ...rest.slice(to)];
}

/** Shift a tab one slot left or right — the keyboard path, so reordering is not
 *  drag-only. Clamped at both ends. */
export function shiftBy(ids: string[], id: string, delta: number): string[] {
  const from = ids.indexOf(id);
  if (from < 0) return ids;
  const to = from + delta;
  if (to < 0 || to >= ids.length) return ids;
  const next = [...ids];
  next.splice(from, 1);
  next.splice(to, 0, id);
  return next;
}
