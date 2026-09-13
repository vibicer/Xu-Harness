import type { IconName } from "../../icons";

export interface ConfigSearchItem {
  id: string;
  moduleId: string;
  moduleName: string;
  title: string;
  desc: string;
  keywords: string;
  icon: IconName;
  anchorId?: string;
}

export const CONFIG_CATALOG: ConfigSearchItem[] = [
  // Providers
  {
    id: "prov-endpoints",
    moduleId: "providers",
    moduleName: "Providers",
    title: "Provider Endpoints & API Keys",
    desc: "Configure OpenAI / Anthropic compatible endpoints, base URLs, and secret keys",
    keywords: "openai anthropic api key base url endpoint host token custom model credentials",
    icon: "plug",
  },
  {
    id: "prov-models",
    moduleId: "providers",
    moduleName: "Providers",
    title: "Model Fetch & Selection",
    desc: "Fetch available models from providers, test connection latency, and select active models",
    keywords: "models test fetch connection ping latency select active model",
    icon: "plug",
  },

  // Agent
  {
    id: "agent-context",
    moduleId: "agent",
    moduleName: "Agent",
    title: "Context Length",
    desc: "Hard token cap for prompt construction before compaction triggers",
    keywords: "tokens context window length max prompt size limit",
    icon: "bot",
  },
  {
    id: "agent-compaction",
    moduleId: "agent",
    moduleName: "Agent",
    title: "Compression Threshold & Retain Ratio",
    desc: "Configure when conversation history compresses and what fraction is kept verbatim",
    keywords: "compress compaction threshold history retain ratio summarize summary",
    icon: "bot",
  },
  {
    id: "agent-budget",
    moduleId: "agent",
    moduleName: "Agent",
    title: "Skill Context Budget",
    desc: "Maximum character budget allocated for skill and memory bodies in prompts",
    keywords: "skill budget memory prompt chars characters limit",
    icon: "bot",
  },
  {
    id: "agent-subagents",
    moduleId: "agent",
    moduleName: "Agent",
    title: "Subagent Concurrency & Timeout",
    desc: "Maximum parallel sub-agents allowed and execution timeout per job",
    keywords: "parallel subagent concurrency delegate workers job timeout kill runtime",
    icon: "bot",
  },
  {
    id: "agent-retries",
    moduleId: "agent",
    moduleName: "Agent",
    title: "Network Retry & Backoff",
    desc: "Maximum retry attempts and interval delay for failed model API calls",
    keywords: "network retry backoff interval rate limit delay 429 500",
    icon: "bot",
  },
  {
    id: "agent-approvals",
    moduleId: "agent",
    moduleName: "Agent",
    title: "Approval Mode & Custom Rules",
    desc: "Autonomous vs manual confirmation (yolo, manual, custom per-tool auto/prompt rules)",
    keywords: "approval mode gate confirm prompt manual yolo permission safe auto ask",
    icon: "bot",
  },
  {
    id: "agent-loop-guard",
    moduleId: "agent",
    moduleName: "Agent",
    title: "Loop Guard & Reasoning Steps",
    desc: "Detection and escalation when the agent repeats actions or cycles indefinitely",
    keywords: "loop guard loop-guard repetitive cycle trip runaway reasoning thinking",
    icon: "bot",
  },
  {
    id: "agent-vision",
    moduleId: "agent",
    moduleName: "Agent",
    title: "Vision Model",
    desc: "Designated multimodal model for image inputs and inspect_image tool calls",
    keywords: "vision image inspect photo screenshot multimodal eye camera",
    icon: "bot",
  },
  {
    id: "agent-firecrawl",
    moduleId: "agent",
    moduleName: "Agent",
    title: "Firecrawl Web Scraper Key",
    desc: "API key for deep web extraction and crawling via Firecrawl",
    keywords: "firecrawl crawl scraper web extract markdown search key",
    icon: "bot",
  },

  // Themes & Appearance
  {
    id: "themes-layout",
    moduleId: "themes",
    moduleName: "Themes",
    title: "Layout Style",
    desc: "Switch between Default pixel/dock shell, Simple sidebar, and custom layouts",
    keywords: "layout style simple default appearance shell dock sidebar custom",
    icon: "palette",
  },
  {
    id: "themes-schemes",
    moduleId: "themes",
    moduleName: "Themes",
    title: "Color Schemes & Custom Presets",
    desc: "Built-in dark color themes, plus create, edit, or delete custom theme presets",
    keywords: "color scheme theme palette dark preset colors accent hex",
    icon: "palette",
  },
  {
    id: "themes-glass",
    moduleId: "themes",
    moduleName: "Themes",
    title: "Glassmorphism & Blur FX",
    desc: "Translucent chrome, backdrop blur radius, and opacity dials for bars, panels, and modals",
    keywords: "glass translucent blur transparency backdrop glassmorphism chrome panel",
    icon: "palette",
  },
  {
    id: "themes-wallpaper",
    moduleId: "themes",
    moduleName: "Themes",
    title: "Wallpaper Background",
    desc: "Custom wallpaper image behind the shell with adjustable image softening blur",
    keywords: "wallpaper background image photo wall blur bgfx backdrop",
    icon: "palette",
  },

  // Orchestration
  {
    id: "orchestrate-fallbacks",
    moduleId: "orchestrate",
    moduleName: "Orchestration",
    title: "Model Fallback Chain",
    desc: "Ordered chain of backup models automatically tried when a primary model fails",
    keywords: "fallback backup models chain failover order chain secondary",
    icon: "network",
  },
  {
    id: "orchestrate-presets",
    moduleId: "orchestrate",
    moduleName: "Orchestration",
    title: "Orchestration Presets",
    desc: "Create and edit multi-agent squad compositions with custom personas, models, and tools",
    keywords: "orchestrate preset squad multi-agent team delegate subagent tree composition",
    icon: "network",
    anchorId: "preset-editor",
  },

  // Memory
  {
    id: "memory-mnemosyne",
    moduleId: "memory",
    moduleName: "Memory",
    title: "Mnemosyne Long-Term Memory",
    desc: "Long-term ranked memory digest injection, semantic vector embeddings, and storage settings",
    keywords: "mnemosyne long-term memory recall digest prompt inject vector semantic sqlite",
    icon: "memory-stick",
  },
  {
    id: "memory-autocapture",
    moduleId: "memory",
    moduleName: "Memory",
    title: "Auto-Capture Facts",
    desc: "Automatic turn-tail extractor that captures durable user facts into memory",
    keywords: "auto capture facts extractor durable learn remember automatic",
    icon: "memory-stick",
  },
  {
    id: "memory-editor",
    moduleId: "memory",
    moduleName: "Memory",
    title: "MEMORY.md Document Editor",
    desc: "Review, edit, and organize durable Markdown memory entries paragraph by paragraph",
    keywords: "memory.md facts editor raw entries paragraph notes durable",
    icon: "memory-stick",
  },

  // Data
  {
    id: "data-home-path",
    moduleId: "data",
    moduleName: "Data",
    title: "Data Home Path",
    desc: "Directory on disk where sessions, memory, personas, skills, and configuration are stored",
    keywords: "data home path storage directory filesystem folder ~/.xu",
    icon: "database",
  },
  {
    id: "data-personas",
    moduleId: "data",
    moduleName: "Data",
    title: "Personas Library & SOUL.md",
    desc: "System prompt personas library, active default selector, and persona editor",
    keywords: "persona soul soul.md system prompt role custom instructions default",
    icon: "database",
  },

  // Toolsets
  {
    id: "toolsets-builtin",
    moduleId: "toolsets",
    moduleName: "Toolsets",
    title: "Built-in Toolsets",
    desc: "Toggle tool groups: file, bash, web, browse, search, git, inspect_image, eval, lsp, etc.",
    keywords: "tools toolset file bash web browse search git mcp inspect eval lsp debug",
    icon: "wrench",
  },
  {
    id: "toolsets-custom",
    moduleId: "toolsets",
    moduleName: "Toolsets",
    title: "Custom Drop-in Tools",
    desc: "Manage user-authored tools created live via the tool_create tool",
    keywords: "custom dropin tools tool_create drop-ins python authoring",
    icon: "wrench",
  },

  // Skills
  {
    id: "skills-catalog",
    moduleId: "skills",
    moduleName: "Skills",
    title: "Skills Catalog",
    desc: "Enable ambient loading (always loaded) or on-demand loading for progressive disclosure skills",
    keywords: "skill ambient load catalog instruction capabilities progressive disclosure",
    icon: "graduation-cap",
  },

  // Notifications
  {
    id: "notifications-alerts",
    moduleId: "notifications",
    moduleName: "Notifications",
    title: "Desktop Alerts",
    desc: "Browser notification permissions and audio/visual alerts when runs finish or wait for input",
    keywords: "notifications alerts sound desktop browser notify alert bell",
    icon: "bell",
  },

  // Plugins
  {
    id: "plugins-manager",
    moduleId: "plugins",
    moduleName: "Plugins",
    title: "Plugins & Slot Extensions",
    desc: "Manage plugin packages in slots, enable/disable, hot-reload, and adjust plugin settings",
    keywords: "plugins extensions slots reload manifest settings hot-reload packages",
    icon: "puzzle",
  },
];

export function filterConfigCatalog(query: string): ConfigSearchItem[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const words = q.split(/\s+/);

  return CONFIG_CATALOG.filter((item) => {
    const haystack = `${item.title} ${item.desc} ${item.keywords} ${item.moduleName}`.toLowerCase();
    return words.every((w) => haystack.includes(w));
  }).sort((a, b) => {
    const aTitle = a.title.toLowerCase();
    const bTitle = b.title.toLowerCase();
    if (aTitle === q && bTitle !== q) return -1;
    if (bTitle === q && aTitle !== q) return 1;
    if (aTitle.startsWith(q) && !bTitle.startsWith(q)) return -1;
    if (bTitle.startsWith(q) && !aTitle.startsWith(q)) return 1;
    return 0;
  });
}
