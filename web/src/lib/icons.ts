/** The icon registry — one place where a glyph is chosen, so 36 icons across
 *  15 files cannot drift into three different visual languages (which is what
 *  the unicode carets, hand-inlined `{@html}` SVG strings and 📎 emoji were).
 *
 *  Deep imports (`@lucide/svelte/icons/<name>`), never the barrel: the barrel
 *  re-exports ~7700 icon components, so importing it makes the dev server
 *  compile the whole set before first paint. A type-only import of the barrel
 *  is fine — it is erased.
 *
 *  Size and stroke come from `setLucideProps` in App.svelte, colour from
 *  `currentColor`, so every theme's palette drives the icons for free. Only
 *  override `size` at a call site when that one spot genuinely differs.
 */
import type { PluginInfo } from "./types";
import type { Component } from "svelte";
import type { LucideProps } from "@lucide/svelte";
import type { BuiltinViewName, ViewName } from "./store.svelte";
import { pluginViewName } from "./plugin-views.svelte";

import ArrowRight from "@lucide/svelte/icons/arrow-right";
import ArrowUp from "@lucide/svelte/icons/arrow-up";
import Bell from "@lucide/svelte/icons/bell";
import Bot from "@lucide/svelte/icons/bot";
import Check from "@lucide/svelte/icons/check";
import ChevronDown from "@lucide/svelte/icons/chevron-down";
import ChevronLeft from "@lucide/svelte/icons/chevron-left";
import ChevronRight from "@lucide/svelte/icons/chevron-right";
import ChevronUp from "@lucide/svelte/icons/chevron-up";
import CircleAlert from "@lucide/svelte/icons/circle-alert";
import CircleStop from "@lucide/svelte/icons/circle-stop";
import CircleX from "@lucide/svelte/icons/circle-x";
import Copy from "@lucide/svelte/icons/copy";
import Database from "@lucide/svelte/icons/database";
import FoldVertical from "@lucide/svelte/icons/fold-vertical";
import GitBranch from "@lucide/svelte/icons/git-branch";
import GraduationCap from "@lucide/svelte/icons/graduation-cap";
import Hash from "@lucide/svelte/icons/hash";
import ListTodo from "@lucide/svelte/icons/list-todo";
import House from "@lucide/svelte/icons/house";
import MemoryStick from "@lucide/svelte/icons/memory-stick";
import MessageSquare from "@lucide/svelte/icons/message-square";
import MessageSquarePlus from "@lucide/svelte/icons/message-square-plus";
import MessagesSquare from "@lucide/svelte/icons/messages-square";
import Network from "@lucide/svelte/icons/network";
import Palette from "@lucide/svelte/icons/palette";
import Paperclip from "@lucide/svelte/icons/paperclip";
import Plug from "@lucide/svelte/icons/plug";
import Plus from "@lucide/svelte/icons/plus";
import Puzzle from "@lucide/svelte/icons/puzzle";
import RefreshCw from "@lucide/svelte/icons/refresh-cw";
import Rocket from "@lucide/svelte/icons/rocket";
import ScrollText from "@lucide/svelte/icons/scroll-text";
import Send from "@lucide/svelte/icons/send";
import Settings from "@lucide/svelte/icons/settings";
import TriangleAlert from "@lucide/svelte/icons/triangle-alert";
import Wrench from "@lucide/svelte/icons/wrench";
import X from "@lucide/svelte/icons/x";

/** Keys are the Lucide icon slugs, so a name is checkable against the package
 *  (see `checks/icons.test.ts`) and searchable on lucide.dev. */
export const ICONS = {
  "arrow-right": ArrowRight,
  "arrow-up": ArrowUp,
  bell: Bell,
  bot: Bot,
  check: Check,
  "chevron-down": ChevronDown,
  "chevron-left": ChevronLeft,
  "chevron-right": ChevronRight,
  "chevron-up": ChevronUp,
  "circle-alert": CircleAlert,
  "circle-stop": CircleStop,
  "circle-x": CircleX,
  copy: Copy,
  database: Database,
  "fold-vertical": FoldVertical,
  "git-branch": GitBranch,
  "graduation-cap": GraduationCap,
  hash: Hash,
  house: House,
  "list-todo": ListTodo,
  "memory-stick": MemoryStick,
  "message-square": MessageSquare,
  "message-square-plus": MessageSquarePlus,
  "messages-square": MessagesSquare,
  network: Network,
  palette: Palette,
  paperclip: Paperclip,
  plug: Plug,
  plus: Plus,
  puzzle: Puzzle,
  "refresh-cw": RefreshCw,
  rocket: Rocket,
  "scroll-text": ScrollText,
  send: Send,
  settings: Settings,
  "triangle-alert": TriangleAlert,
  wrench: Wrench,
  x: X,
} satisfies Record<string, Component<LucideProps>>;

export type IconName = keyof typeof ICONS;

/** One icon per built-in view, shared by every layout: the same destination
 *  must not be a chat bubble in the dock and a house in the sidebar. Plugin
 *  views name their own glyph, resolved through `viewIcon`. */
export const VIEW_ICONS: Record<BuiltinViewName, IconName> = {
  workspace: "message-square",
  sessions: "messages-square",
  config: "settings",
  logs: "scroll-text",
  onboarding: "rocket",
};

const isBuiltinView = (view: ViewName): view is BuiltinViewName => view in VIEW_ICONS;

/** Glyph for any view, built-in or plugin-contributed. A plugin names a Lucide
 *  slug it cannot verify, and `Icon` renders nothing for a key it doesn't
 *  have — so an absent or renamed slug falls back to the puzzle. */
export function viewIcon(view: ViewName, plugins: PluginInfo[]): IconName {
  if (isBuiltinView(view)) return VIEW_ICONS[view];
  const icon = plugins.find((p) => p.enabled && p.name === pluginViewName(view))?.ui?.icon;
  return icon && icon in ICONS ? (icon as IconName) : "puzzle";
}
