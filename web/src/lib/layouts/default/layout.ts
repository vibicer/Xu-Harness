import type { LayoutDescriptor } from "../registry";

const ARCANE: Record<string, string> = {
  bg: "#0a0714", "bg-2": "#0e0a1a", surface: "#150f26", "surface-2": "#1c1433", "surface-3": "#241a42",
  border: "#32205a", "border-strong": "#4a2f7d", text: "#f2ecff", dim: "#b3a5d9", faint: "#6f5f96",
  magenta: "#ff4fd8", cyan: "#5fd4ff", amber: "#ffb454", danger: "#ff5c6c", ok: "#7ee8a2",
  "glow-mag": "#ff4fd8", "glow-cyan": "#5fd4ff", "glow-amber": "#ffb454", "glow-ok": "#7ee8a2",
  "brand-2": "#7b2fd0",
};

/** HORIZON — Color Hunt #000000 #233D4D #FE7F2D #EADECF (dark, dusk-on-the-horizon). */
const HORIZON: Record<string, string> = {
  bg: "#000000", "bg-2": "#080d12", surface: "#0d1419", "surface-2": "#152029", "surface-3": "#1c2a35",
  border: "#233D4D", "border-strong": "#FE7F2D", text: "#EADECF", dim: "#b3a08a", faint: "#7a6f60",
  magenta: "#FE7F2D", cyan: "#EADECF", amber: "#EADECF", danger: "#FE7F2D", ok: "#EADECF",
  "glow-mag": "#FE7F2D", "glow-cyan": "#EADECF", "glow-amber": "#EADECF", "glow-ok": "#EADECF",
  "brand-2": "#233D4D",
};

const MINT: Record<string, string> = {
  bg: "#000000", "bg-2": "#0a0a0a", surface: "#111212", "surface-2": "#171a1a", "surface-3": "#1e2323",
  border: "#222222", "border-strong": "#1DCD9F", text: "#ecfaf5", dim: "#9fc7ba", faint: "#5d7d74",
  magenta: "#1DCD9F", cyan: "#48e0b5", amber: "#ffd166", danger: "#ff5858", ok: "#1DCD9F",
  "glow-mag": "#1DCD9F", "glow-cyan": "#48e0b5", "glow-amber": "#ffd166", "glow-ok": "#1DCD9F",
  "brand-2": "#169976",
};

const HARVEST: Record<string, string> = {
  bg: "#1e2533", "bg-2": "#2D3C59", surface: "#2D3C59", "surface-2": "#37486b", "surface-3": "#41547c",
  border: "#46597f", "border-strong": "#94A378", text: "#F2EFE2", dim: "#ccd0b6", faint: "#8c9478",
  magenta: "#E5BA41", cyan: "#94A378", amber: "#E5BA41", danger: "#D1855C", ok: "#94A378",
  "glow-mag": "#E5BA41", "glow-cyan": "#94A378", "glow-amber": "#E5BA41", "glow-ok": "#94A378",
  "brand-2": "#94A378",
};

/** DEEP — Color Hunt #073059 #2866AB #5FBDC5 #D8D95C (dark, ocean navy). */
const DEEP: Record<string, string> = {
  bg: "#031225", "bg-2": "#041a33", surface: "#052040", "surface-2": "#073059", "surface-3": "#0a3d6e",
  border: "#2866AB", "border-strong": "#5FBDC5", text: "#e8f4fc", dim: "#9cc3de", faint: "#5a8aaa",
  magenta: "#5FBDC5", cyan: "#D8D95C", amber: "#D8D95C", danger: "#ff5c6c", ok: "#5FBDC5",
  "glow-mag": "#5FBDC5", "glow-cyan": "#D8D95C", "glow-amber": "#D8D95C", "glow-ok": "#5FBDC5",
  "brand-2": "#2866AB",
};

const layout: LayoutDescriptor = {
  id: "default",
  name: "DEFAULT",
  desc: "classic dock · workspace shell",
  title: "Xu — agent harness",
  load: () => import("./Layout.svelte"),
  themes: [
    { id: "arcane", name: "ARCANE", builtin: true, colors: ARCANE },
    { id: "horizon", name: "HORIZON", builtin: true, colors: HORIZON },
    { id: "mint", name: "MINT", builtin: true, colors: MINT },
    { id: "harvest", name: "HARVEST", builtin: true, colors: HARVEST },
    { id: "deep", name: "DEEP", builtin: true, colors: DEEP },
  ],
};

export default layout;
