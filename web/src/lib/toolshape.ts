/** Tool chips render as the thing the tool *is*: a terminal for shell work, a
 *  note tab for file work, a query line for searches. One classifier, shared by
 *  every layout so the four shells stay in sync.
 *
 *  Input is the flat `args` string the brain emits (`registry.summarize_args`):
 *  `command=ls -la path=/tmp` — space-joined `key=value`, values capped.
 *  Runnable check: `node checks/toolshape.test.ts`
 */

export type ToolShape = "terminal" | "note" | "search" | "plain";

const SHAPES: Record<string, ToolShape> = {
  bash: "terminal", eval: "terminal", debug: "terminal",
  write: "note", edit: "note", ast_edit: "note", read: "note",
  grep: "search", glob: "search", ast_grep: "search",
  web_search: "search", web_extract: "search", browse: "search",
};

/** Which visual shape a tool chip takes. Unknown tools stay `plain`. */
export function toolShape(tool: string | undefined): ToolShape {
  return (tool && SHAPES[tool]) || "plain";
}

/** `read` and `grep` only look; `write`/`edit` mutate. Drives the tint. */
export function isReadOnly(tool: string | undefined): boolean {
  return tool === "read" || tool === "ast_grep" || tool === "grep";
}

/** Pull one `key=` value out of the flat args string. Values may contain
 *  spaces, so a value runs until the next ` key=` or end of string. */
export function argValue(args: string | undefined, key: string): string {
  if (!args) return "";
  const m = new RegExp(`(?:^|\\s)${key}=(.*?)(?=\\s[a-z_]+=|$)`).exec(args);
  return m ? m[1].trim() : "";
}

/** The headline for a chip: the command run, the file touched, the pattern
 *  searched. Key priority follows the shape — a search chip leads with its
 *  pattern, a note chip with its path. Falls back to the whole args string. */
const HEADLINE_KEYS: Record<ToolShape, string[]> = {
  terminal: ["command", "expression", "program", "file"],
  note: ["path", "file", "pattern"],
  search: ["pattern", "query", "url", "path"],
  // `label`/`child` is delegate's squad-member name — the one thing worth
  // naming on an otherwise shapeless chip. `prompt` is absent: too long.
  plain: ["command", "path", "file", "pattern", "query", "url", "label", "child", "id"],
};

export function headline(tool: string | undefined, args: string | undefined): string {
  for (const k of HEADLINE_KEYS[toolShape(tool)]) {
    const v = argValue(args, k);
    if (v) return v;
  }
  return (args ?? "").replace(/\s+/g, " ");
}

/** Basename + parent dir of a path, for the note tab: `lib/store.ts`. */
export function fileTab(path: string): { name: string; dir: string } {
  const clean = path.replace(/:\d+(-\d+)?$/, "").replace(/\/+$/, "");
  const i = clean.lastIndexOf("/");
  return i < 0 ? { name: clean, dir: "" } : { name: clean.slice(i + 1), dir: clean.slice(0, i) };
}

/** The one short thing to show next to the tool name on a closed chip: what
 *  the tool actually touched. File paths collapse to their last two segments
 *  (`lib/store.ts`) — the full dir is on the note tab once opened. Commands,
 *  patterns and URLs pass through and get ellipsized by CSS. Empty when there
 *  is nothing worth naming. */
export function target(tool: string | undefined, args: string | undefined): string {
  const head = headline(tool, args);
  if (!head || head.includes("=")) return ""; // fallback arg dump, not a target
  if (toolShape(tool) !== "note") return head; // a command is its own target
  const { name, dir } = fileTab(head);
  const parent = dir.slice(dir.lastIndexOf("/") + 1);
  return parent ? `${parent}/${name}` : name;
}

/** Trailing `[exit N · Ts]` line that the bash tool appends, split off so the
 *  terminal pane can show it as a status bar instead of body text. */
export function splitExit(output: string | null | undefined): { body: string; exit: string } {
  const text = output ?? "";
  const m = /\n?(\[exit [^\]]*\])\s*$/.exec(text);
  return m ? { body: text.slice(0, m.index), exit: m[1].slice(1, -1) } : { body: text, exit: "" };
}

/** Non-empty line count of a tool's output — the badge on search/note chips. */
export function lineCount(output: string | null | undefined): number {
  if (!output) return 0;
  return output.split("\n").filter((l) => l.trim() !== "").length;
}
