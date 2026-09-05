/** Drag-and-drop wiring for a session tab strip.
 *
 *  The four shells each render their own strip, so the *behaviour* lives here
 *  and only the markup differs: one factory per strip, spread onto each tab.
 *  Native HTML5 drag events — no dependency, and the platform already handles
 *  the pointer work.
 *
 *  The strip reorders *live*, as the pointer crosses each tab's midpoint, rather
 *  than computing a landing slot on release. Drop-on-release needed the pointer
 *  to be over the right tab at the right moment; sliding tabs meant a one-slot
 *  nudge was frequently a no-op. Now the release only commits what is already on
 *  screen — dropping anywhere on the strip is enough.
 *
 *  Reorder maths lives in `tab-order.ts` (see its runnable check).
 */
import { brain } from "./store.svelte";

export function createTabDrag() {
  let dragging = $state<string | null>(null);

  function end(): void {
    if (dragging !== null) brain.commitSessionTabs();
    dragging = null;
  }

  return {
    get dragging() { return dragging; },
    /** Spread onto each tab element. */
    tab(id: string) {
      return {
        draggable: true,
        ondragstart: (event: DragEvent) => {
          dragging = id;
          // Text payload keeps the drag valid in Firefox, which ignores a drag
          // with no data attached.
          event.dataTransfer?.setData("text/plain", id);
          if (event.dataTransfer) event.dataTransfer.effectAllowed = "move";
        },
        ondragover: (event: DragEvent) => {
          if (dragging === null || dragging === id) return;
          // preventDefault marks this a valid drop target, so the pointer never
          // shows "no drop" mid-strip.
          event.preventDefault();
          if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
          const el = event.currentTarget as HTMLElement | null;
          if (!el) return;
          const box = el.getBoundingClientRect();
          brain.dragSessionTabOver(dragging, id, event.clientX, box);
        },
        ondrop: (event: DragEvent) => {
          if (dragging === null) return;
          event.preventDefault();
          end();
        },
        ondragend: end,
      };
    },
    /** Spread onto the strip itself: the whole strip accepts the drop, so
     *  releasing between tabs or past the last one keeps the live order instead
     *  of cancelling the drag. */
    strip() {
      return {
        ondragover: (event: DragEvent) => {
          if (dragging === null) return;
          event.preventDefault();
          if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
        },
        ondrop: (event: DragEvent) => {
          if (dragging === null) return;
          event.preventDefault();
          end();
        },
      };
    },
    /** Alt+←/→ moves a focused tab — reordering must not be drag-only. */
    onkeydown(event: KeyboardEvent, id: string): void {
      if (!event.altKey) return;
      const delta = event.key === "ArrowLeft" ? -1 : event.key === "ArrowRight" ? 1 : 0;
      if (!delta) return;
      event.preventDefault();
      brain.shiftSessionTab(id, delta);
    },
  };
}
