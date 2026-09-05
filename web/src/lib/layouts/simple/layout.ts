import type { LayoutDescriptor } from "../registry";

/** SIMPLE — calm neutral palette for the simple layout. */
const SIMPLE: Record<string, string> = {
  bg: "#0b0d10", "bg-2": "#101318", surface: "#13171c", "surface-2": "#191f26", "surface-3": "#202832",
  border: "#29313a", "border-strong": "#526171", text: "#eef2f5", dim: "#8e9aa6", faint: "#65717d",
  magenta: "#8bc7ff", cyan: "#8bc7ff", amber: "#f2c777", danger: "#ef9292", ok: "#8dd6a5",
  "glow-mag": "#8bc7ff", "glow-cyan": "#8bc7ff", "glow-amber": "#f2c777", "glow-ok": "#8dd6a5",
  "brand-2": "#426b93",
};

const layout: LayoutDescriptor = {
  id: "simple",
  name: "SIMPLE",
  desc: "beginner-friendly sidebar · workspace dashboard",
  title: "Xu — Simple",
  load: () => import("./Layout.svelte"),
  themes: [{ id: "simple", name: "SIMPLE", builtin: true, colors: SIMPLE }],
};

export default layout;
