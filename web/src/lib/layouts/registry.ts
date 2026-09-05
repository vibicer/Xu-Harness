/// <reference types="vite/client" />
import type { Component } from "svelte";
import type { LayoutId, ThemePreset } from "../types";
import { PALETTE, type PaletteEntry } from "./palette";
export { PALETTE, type PaletteEntry } from "./palette"; // re-exported for store.svelte.ts back-compat surface

/** Everything one layout owns. Dropping a folder into this directory with a
 *  `layout.ts` (default export) + `Layout.svelte` is the whole registration —
 *  no edits in App.svelte, the store, or Config. */
export interface LayoutDescriptor {
  id: string;
  /** Config → Themes label. */
  name: string;
  /** Config → Themes sublabel. */
  desc: string;
  /** document.title while the layout is active. */
  title: string;
  /** One-chunk lazy loader: the shell fetches a layout only when in use. */
  load: () => Promise<{ default: Component }>;
  /** Built-in color schemes this layout owns; the registry stamps `layout`. */
  themes: Omit<ThemePreset, "layout">[];
  /** Extra CSS vars this layout's schemes may set, beyond the shared PALETTE. */
  palette?: PaletteEntry[];
}

/** Self-registration: every subfolder's `layout.ts` default-exports one. */
const modules = import.meta.glob<{ default: LayoutDescriptor }>("./*/layout.ts", {
  eager: true,
});

/** Stable ordering so the Config list and fallbacks are deterministic:
 *  `default` first, then alphabetical. */
export const LAYOUTS: LayoutDescriptor[] = Object.values(modules)
  .map((m) => m.default)
  .sort((a, b) => (a.id === "default" ? -1 : b.id === "default" ? 1 : a.id < b.id ? -1 : 1));

export function layoutById(id: string): LayoutDescriptor | undefined {
  return LAYOUTS.find((l) => l.id === id);
}

/** Any id — unknown, stale persisted, `plugin:*`, `custom:*` — resolves to a
 *  real layout; unknown/stale falls back to `default`, which always ships as a
 *  built-in folder. (`plugin:*` shell handoff is handled by the caller.) */
export function resolveLayout(id: string): LayoutDescriptor {
  return layoutById(id) ?? layoutById("default")!;
}

/** Built-in schemes flat-mapped from each owning layout folder, each stamped
 *  with its owner's id. */
export const BUILTIN_THEMES: ThemePreset[] = LAYOUTS.flatMap((l) =>
  l.themes.map((t) => ({ ...t, layout: l.id })),
);

/** Shared palette plus a layout's own extras. */
export function paletteFor(layoutId: LayoutId): PaletteEntry[] {
  return [...PALETTE, ...(layoutById(layoutId)?.palette ?? [])];
}
