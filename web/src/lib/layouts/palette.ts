import type { LayoutId } from "../types";

/** One editable CSS var of a color scheme. Glow vars are stored as hex and
 *  converted to rgb triplets when applied (rgba(var(--glow-*), a) usage). */
export interface PaletteEntry {
  var: string;
  label: string;
  layouts?: LayoutId[];
}

/** Canonical palette: every CSS var a custom scheme can override, UI order. */
export const PALETTE: PaletteEntry[] = [
  { var: "bg", label: "background" },
  { var: "bg-2", label: "background 2" },
  { var: "surface", label: "surface" },
  { var: "surface-2", label: "surface 2" },
  { var: "surface-3", label: "surface 3" },
  { var: "border", label: "border" },
  { var: "border-strong", label: "border strong" },
  { var: "text", label: "text" },
  { var: "dim", label: "dim text" },
  { var: "faint", label: "faint text" },
  { var: "magenta", label: "primary accent" },
  { var: "cyan", label: "secondary accent" },
  { var: "amber", label: "warning" },
  { var: "danger", label: "danger" },
  { var: "ok", label: "ok" },
  { var: "glow-mag", label: "glow primary" },
  { var: "glow-cyan", label: "glow secondary" },
  { var: "glow-amber", label: "glow warning" },
  { var: "glow-ok", label: "glow ok" },
  { var: "brand-2", label: "brand gradient" },
];
