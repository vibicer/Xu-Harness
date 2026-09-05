/** Shared shell/brain types (contract/methods.md). */

export interface SessionInfo {
  id: string;
  title: string;
  cwd: string;
  model?: string | null;
  persona?: string | null;
  created_at: number;
  updated_at: number;
  message_count: number;
}

export interface Step {
kind: "text" | "tool" | "reasoning" | "compaction";
text?: string;
tool?: string;
args?: string;
status?: "running" | "ok" | "error";
elapsed?: number | null;
output?: string | null;
/** provider tool-call id — settles the right chip when siblings run at once. */
call_id?: string;
/** delegate chips: the sub-agent run this step spawned (opens its activity). */
subagent_run?: string | null;
subagent?: string | null;
/** delegate chips: how many sub-agents this one call spawned (a `tasks` batch
 *  fans out in a single call, so the chip is the only record of the count). */
subagent_count?: number | null;
/** show_image chips: a data-URL image the shell renders under the chip, plus
 *  its alt text (the tool's caption, else the filename). */
image?: string | null;
image_alt?: string | null;
/** compaction rows: outcome + size stats for the transcript divider. */
ok?: boolean;
mode?: "auto" | "manual";
before?: number;
after?: number;
dropped?: number;
error?: string;
count?: number;
ts?: number;
}

/** One delegated sub-agent run — what it was asked and what it did. */
export interface SubagentRun {
id: string;
child: string;
prompt: string;
parent_session: string | null;
/** Parent turn id — siblings of one fan-out share it. Empty on older records. */
group?: string;
status: "running" | "ok" | "error" | "interrupted";
started: number;
finished: number | null;
result: string;
steps: Step[];
reasoning: string;
}

export interface ChatMessage {
  role: "user" | "assistant" | "tool" | "system";
  content: unknown;
  tool_call_id?: string | null;
  tool_name?: string | null;
  reasoning?: string;
  steps?: Step[];
  ts?: number;
  status?: "done" | "interrupted" | "failed";
}

export interface GitStatus {
  repo: boolean;
  branch: string;
  upstream?: string | null;
  ahead?: number;
  behind?: number;
  staged?: number;
  unstaged?: number;
  untracked?: number;
  dirty: boolean;
  last?: { sha: string; subject: string; ts: number | null } | null;
}

export interface GitDetail extends GitStatus {
  cwd: string;
  files: { state: string; path: string }[];
  commits: { sha: string; subject: string; ts: number | null; author: string }[];
}

export interface StatePanel {
  model: string | null;
  persona: string | null;
  rules: string[];
  context: number;
  compress: boolean;
  tokens: number | null;
  /** Last provider-reported `prompt_tokens`; undefined until a turn reports usage. */
  pressure?: number;
  /** `pressure` plus transcript growth since it was sampled — next request's expected cost. */
  projected?: number;
  /** True when the meter is anchored to real provider `prompt_tokens`; false = surface heuristic. */
  calibrated?: boolean;
  cwd: string;
  git?: GitStatus | null;
  preset?: AgentNodeInfo[] | AgentNodeInfo | null;
}

export interface AgentNodeInfo {
  id: string;
  name: string;
  role: "orchestrator" | "agent";
  persona: string;
  job: string;
  model: string | null;
  rules: string[];
  skills: string[] | null;
  tools: string[] | null;
  memory: string[] | null;
  children: AgentNodeInfo[];
}

export interface PresetInfo {
  id: string;
  name: string;
  node_count: number;
  created: number;
  updated: number;
}

export interface PersonaInfo {
  id: string;
  name: string;
}

export interface ProviderInfo {
  id: string;
  name: string;
  type: string;
  base_url: string;
  models: string[];
  key_set: boolean;
  enabled: boolean;
}

export interface MemoryEntry {
  id: string;
  text: string;
  badge: "from-session" | "user-edited";
}

export interface SkillInfo {
  id: string;
  name: string;
  desc: string;
  state: "LOADED" | "DEMAND" | "OFF";
  ambient: boolean; // enabled = always loaded (hard-reserved)
  keywords?: string[];
}

export interface ToolInfo {
  name: string;
  description: string;
  is_dropin?: boolean;
}

export interface DropinInfo {
  name: string;
  description: string;
  toolset: string;
  approval: string | null;
  enabled: boolean;
}
/** A plugin's frontend contribution: an ES module in the plugin dir defining a
 *  custom element the shell mounts at a named spot. `label`/`icon` are the tab
 *  chrome for `mount: "config"`; the shell falls back when either is absent or
 *  names an icon it doesn't ship. */
export interface PluginUI {
  module: string;
  element: string;
  mount: string;
  label?: string;
  icon?: string;
}

/** One setting a plugin's manifest declares, carrying its live value. The host
 *  merges the stored value over the declared default, so `value` is what is in
 *  effect — the shell renders it without a second lookup. */
export interface PluginSetting {
  key: string;
  type: "string" | "boolean" | "integer" | "number" | "list";
  default?: unknown;
  label?: string;
  value: unknown;
}

export interface PluginInfo {
  name: string;
  version: string;
  description: string;
  provides: string[];
  requires: string[];
  /** Required slots nothing currently fills. Reported, not enforced: the
   *  plugin still loads, but the gap is worth surfacing. */
  unmet?: string[];
  enabled: boolean;
  settings?: PluginSetting[];
  ui?: PluginUI | null;
  /** Colour schemes this plugin contributes (the `theme` slot). Offered in
   *  Config → Themes beside the built-ins while the plugin is enabled. */
  themes?: PluginTheme[];
}

export interface PluginTheme {
  id: string;
  name: string;
  /** Which layout the scheme is for; omitted means `default`. */
  layout?: string;
  /** Palette key → hex colour. Written as `--<key>` on the document root. */
  colors: Record<string, string>;
  /** Optional extra stylesheet in the plugin dir, for what custom properties
   *  can't express (fonts, radii, textures). Loaded only while active. */
  css?: string;
}

export interface ToolsetInfo {
  toolset: string;
  enabled: boolean;
  tools: ToolInfo[];
}

export interface ApprovalCard {
  request_id: string;
  turn_id: string;
  session_id?: string;
  tool: string;
  args: string | Record<string, unknown>;
  reason: string;
  /** effective level ("risky" | "always"); "always" hides the always-approve button */
  level?: string;
  resolved?: boolean | null;
  answer?: string; // chosen option or typed reply (ask tool)
}

export interface AppConfig {
  context_length: number;
  compress_threshold: number;
  approval_mode: string;
  approval_modes: Record<string, { auto: string[]; prompt: string[] }>;
  job_timeout: number | null;
  max_parallel_subagents: number;
  retry_max: number;
  retry_interval: number;
  retain_ratio: number;
  compaction_retries: number;
  context_skill_budget: number;
  vision_model: string | null;
  /** Global ordered backup models tried when the active model fails. */
  model_fallbacks: string[];
  firecrawl_enabled: boolean;
  firecrawl_key: string | null;
  prune_keep: number;
}

export interface TodoItem {
  content: string;
  status: "pending" | "done" | "dropped" | "in_progress";
}
export interface TodoPhase {
  phase: string;
  items: TodoItem[];
}
export interface TodoInfo {
  phases: TodoPhase[];
}


export interface SubagentInfo {
  id: string;
  parent_session: string | null;
  prompt: string;
  status: "running" | "done" | "error" | "interrupted";
  result: string;
  error: string | null;
  created: number;
  finished: number | null;
}


/** Layout identity. The layout registry is open (a folder dropped into
 *  lib/layouts/, or an enabled plugin), so this is a plain string with three
 *  id shapes:
 *  - built-in id — a folder under lib/layouts/ (e.g. `default`, `simple`)
 *  - `plugin:<name>` — plugin whose manifest declares `ui.mount: "layout"` owns the whole shell
 *  - `custom:<name>` — a user preset built on top of a built-in base */
export type LayoutId = string;

export interface ThemePreset {
  id: string;
  name: string;
  builtin: boolean;
  layout?: LayoutId;
  colors: Record<string, string>;
  /** Set when a plugin contributed the scheme: the plugin's name. Such a preset
   *  is not editable, not deletable, and not persisted — the plugin owns it. */
  plugin?: string;
  /** Extra stylesheet the plugin ships, loaded only while this scheme is
   *  active (for fonts/radii/textures that custom properties can't carry). */
  css?: string;
}

export interface CustomLayout {
  name: string;
  base: LayoutId;
  sidebar?: string;
  density?: string;
  panels?: string[];
}
