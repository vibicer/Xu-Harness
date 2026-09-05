/** filebrowser UI — <xu-filebrowser>, mounted by the shell's PluginSlot.
 *
 * A chip that opens a resizable file-explorer modal over the session cwd, and
 * talks to this plugin's own `fs.*` RPCs. Plain custom element (no framework):
 * the shell serves this file verbatim off the plugin dir, so it must run as-is
 * in the browser.
 *
 * Contract with the shell (see PluginSlot.svelte):
 * - this module defines the element named in manifest.ui.element
 * - the shell sets `el.brain` to the live store before/after connecting
 * - styling piggybacks on the shell's global `.dir-modal` classes, so every
 *   layout themes the modal without this plugin knowing which layout is active
 */

const MIN_W = 420;
const MIN_H = 260;
const EDGES = ["n", "s", "e", "w", "ne", "nw", "se", "sw"];

/** Styles for the parts the shell has no class for. Injected once into the
 *  document head — NOT a shadow root: the modal deliberately reuses the shell's
 *  global `.dir-modal` / `.dir-item` / `.cd-btn` chrome, and shadow DOM would
 *  wall those off, costing per-layout theming. Every class is `fc-`-prefixed so
 *  nothing here can leak into the shell's own rules. */
const CSS = `
.fc-chip {
  display: inline-flex; align-items: center; gap: 5px;
  font: inherit; font-size: 11px; letter-spacing: 0.5px;
  background: transparent; border: 0; padding: 2px 6px;
  cursor: pointer; white-space: nowrap; color: var(--fg-dim, #8ea2c0);
}
.fc-chip:hover, .fc-chip.on { color: var(--fg, #dbe6f5); }
.fc-chip:hover { text-decoration: underline dotted; }
.fc-ico { display: inline-flex; width: 12px; height: 12px; }
.fc-ico svg { width: 100%; height: 100%; }

.fc-panel { position: absolute; overflow: hidden; max-width: none; max-height: none; }
.fc-grip { position: absolute; z-index: 2; touch-action: none; }
.fc-grip-n { top: 0; left: 0; right: 0; height: 6px; cursor: ns-resize; }
.fc-grip-s { bottom: 0; left: 0; right: 0; height: 6px; cursor: ns-resize; }
.fc-grip-e { right: 0; top: 0; bottom: 0; width: 6px; cursor: ew-resize; }
.fc-grip-w { left: 0; top: 0; bottom: 0; width: 6px; cursor: ew-resize; }
.fc-grip-ne, .fc-grip-nw, .fc-grip-se, .fc-grip-sw { width: 14px; height: 14px; z-index: 3; }
.fc-grip-ne { top: 0; right: 0; cursor: nesw-resize; }
.fc-grip-nw { top: 0; left: 0; cursor: nwse-resize; }
.fc-grip-se { bottom: 0; right: 0; cursor: nwse-resize; }
.fc-grip-sw { bottom: 0; left: 0; cursor: nesw-resize; }
.dir-close, .fc-foot .cd-btn { position: relative; z-index: 5; }

.fc-error {
  padding: 7px 14px; font-size: 11px; color: var(--danger, #ff6b7a);
  border-bottom: 1px solid var(--border, #1c2740);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.fc-crumb { background: none; border: 0; padding: 0 2px; cursor: pointer; font: inherit; color: var(--cyan, #6fd3e0); }
.fc-crumb:hover { text-decoration: underline; }
.fc-sep { opacity: 0.5; }

.fc-body { flex: 1; display: flex; min-height: 0; }
.fc-tree {
  width: 270px; min-width: 150px; max-width: 60%; flex: none;
  display: flex; flex-direction: column; min-height: 0;
  border-right: 1px solid var(--border, #1c2740);
  resize: horizontal; overflow: hidden;
}
.fc-toolbar { display: flex; gap: 6px; padding: 8px; }
.fc-act {
  font: inherit; font-size: 10px; letter-spacing: 0.5px;
  background: var(--surface-2, #151d2b); color: var(--fg-dim, #8ea2c0);
  border: 1px solid var(--border-strong, #2a3854); padding: 3px 8px; cursor: pointer;
}
.fc-act:hover:not(:disabled) { color: var(--fg, #dbe6f5); border-color: currentColor; }
.fc-act:disabled { opacity: 0.4; cursor: not-allowed; }
.fc-act.danger { color: var(--danger, #ff6b7a); }
.fc-new { display: flex; gap: 6px; padding: 0 8px 8px; }
.fc-new input { flex: 1; min-width: 0; font: inherit; font-size: 11px; }
.fc-list { flex: 1; overflow-y: auto; padding: 0 4px 8px; }
.fc-item { gap: 8px; }
.fc-item.sel { background: var(--surface-3, #1b2536); color: var(--magenta, #c49bff); }
.fc-badge { flex: none; width: 20px; font-size: 9px; opacity: 0.7; text-align: center; letter-spacing: 0; }
.fc-item.dir .fc-badge { color: var(--cyan, #6fd3e0); opacity: 1; }
.fc-name { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.fc-size { flex: none; font-size: 10px; opacity: 0.55; }

.fc-pane { flex: 1; display: flex; flex-direction: column; min-width: 0; min-height: 0; }
.fc-pane-head {
  display: flex; align-items: center; gap: 8px; padding: 8px 10px;
  border-bottom: 1px solid var(--border, #1c2740);
}
.fc-open {
  flex: 1; min-width: 0; font-size: 11px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; color: var(--fg-dim, #8ea2c0);
}
.fc-dirty { font-style: normal; margin-left: 8px; color: var(--amber, #ffd27c); font-size: 10px; }
.fc-search {
  flex: 0 1 170px; min-width: 90px; font: inherit;
  font-family: var(--mono, monospace); font-size: 11px;
  background: var(--bg, #0b0f16); color: var(--text, #dbe6f5);
  border: 1px solid var(--border-strong, #2a3854); padding: 3px 7px;
}
.fc-search:focus { outline: 0; border-color: var(--cyan, #6fd3e0); }
.fc-hits {
  display: flex; flex-direction: column; gap: 1px; max-height: 30%;
  overflow: auto; padding: 6px 10px; background: var(--surface-2, #151d2b);
  border-bottom: 1px solid var(--border, #1c2740);
}
.fc-hits-count { font-size: 10px; color: var(--faint, #5a6b85); }
.fc-hit { display: flex; gap: 8px; font-size: 11px; min-width: 0; }
.fc-hit code { flex: none; width: 42px; text-align: right; color: var(--faint, #5a6b85); }
.fc-hit span { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-family: var(--mono, monospace); }
.fc-confirm {
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 8px 10px;
  font-size: 11px; color: var(--danger, #ff6b7a);
  border-bottom: 1px solid var(--border, #1c2740);
}
.fc-code {
  position: relative; flex: 1; min-height: 0;
  font-family: var(--mono, monospace); font-size: 12px; line-height: 1.55;
  tab-size: 2; background: var(--bg, #0b0f16); color: var(--text, #dbe6f5);
}
.fc-code pre, .fc-code textarea {
  margin: 0; padding: 10px 12px; border: 0; outline: 0;
  font: inherit; font-family: inherit; font-size: inherit; line-height: inherit;
  tab-size: 2; letter-spacing: 0; white-space: pre; word-wrap: normal;
}
.fc-code pre {
  position: absolute; inset: 0; overflow: auto; color: var(--text, #dbe6f5);
}
.fc-code textarea {
  position: absolute; inset: 0; width: 100%; height: 100%;
  resize: none; overflow: auto; background: transparent;
  color: transparent; caret-color: var(--fg, #dbe6f5); z-index: 1;
}
.fc-code textarea::selection {
  background: var(--surface-3, #1b2536); color: transparent;
}
/* token palette — applied to both the highlight layer and md source */
.tk-k { color: var(--magenta, #c49bff); }
.tk-s { color: var(--amber, #ffd27c); }
.tk-c { color: var(--faint, #5a6b85); font-style: italic; }
.tk-n { color: var(--cyan, #6fd3e0); }
.tk-b { color: var(--green, #6fe3a8); }
.tk-f { color: var(--red, #ff6b7a); }
/* markdown source inline tokens */
.tk-e { color: var(--cyan, #6fd3e0); font-style: italic; }
.tk-a { color: var(--cyan, #6fd3e0); }
.tk-m { color: var(--cyan, #6fd3e0); }
.fc-blank {
  flex: 1; display: flex; flex-direction: column; gap: 4px;
  align-items: center; justify-content: center;
  color: var(--faint, #5a6b85); font-size: 12px;
}
.fc-blank p { margin: 0; }
.fc-foot { align-items: center; gap: 10px; }
.fc-note { flex: 1; font-size: 10px; color: var(--faint, #5a6b85); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
`;

function human(n) {
  if (n == null) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function iconFor(name) {
  if (name.endsWith(".py")) return "PY";
  if (name.endsWith(".md")) return "MD";
  if (name.endsWith(".ts")) return "TS";
  if (name.endsWith(".json")) return "{}";
  if (name.endsWith(".toml")) return "TO";
  return "\u00b7\u00b7";
}

/** Escape HTML in raw text so no user file content can become markup. */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/** Pick the view mode for a filename. */
function kindFor(name) {
  const ext = (name.split(".").pop() || "").toLowerCase();
  if (ext === "md" || ext === "markdown") return "md";
  if (/^(py|ts|tsx|js|jsx|mjs|cjs|json|json5|toml|yaml|yml|css|scss|html|htm|sh|bash|zsh|rs|go|java|c|cpp|h|sql|rb|php|swift|kt|lua|r|xml)$/.test(ext)) return "code";
  return "text";
}

/** Regex-per-language token rules. Order matters: first match wins. */
const CODE_RULES = {
  py:    [["c", /^#[^\n]*/], ["s", /^("""[\s\S]*?"""|'''[\s\S]*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')/], ["k", /^(and|as|assert|async|await|break|class|continue|def|del|elif|else|except|finally|for|from|global|if|import|in|is|lambda|nonlocal|not|or|pass|raise|return|try|while|with|yield|True|False|None)\b/], ["n", /^\b\d[\d_]*(\.\d+)?\b/], ["b", /^\b(self|cls|super)\b/]],
  js:    [["c", /^\/\/[^\n]*|\/\*[\s\S]*?\*\//], ["s", /^`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/], ["k", /^(const|let|var|function|return|if|else|for|while|do|switch|case|break|continue|new|delete|typeof|instanceof|in|of|class|extends|super|import|export|from|default|async|await|try|catch|finally|throw|yield|static|this|undefined|null)\b/], ["n", /^\b\d[\d_]*(\.\d+)?\b/], ["b", /^\b(true|false)\b/]],
  json:  [["k", /^"(\\.|[^"\\])*"(?=\s*:)/], ["s", /^"(\\.|[^"\\])*"/], ["n", /^-?\b\d[\d_]*(\.\d+)?([eE][+-]?\d+)?\b/], ["b", /^\b(true|false|null)\b/]],
  toml:  [["c", /^#[^\n]*/], ["k", /^[A-Za-z_][\w.-]*\s*(?==)/], ["s", /^"""[\s\S]*?"""|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/], ["n", /^\b\d[\d_]*(\.\d+)?\b/], ["b", /^\b(true|false)\b/]],
  yaml:  [["c", /^#[^\n]*/], ["k", /^[A-Za-z_][\w.-]*\s*(?=:)/], ["s", /^"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/], ["n", /^\b\d[\d_]*(\.\d+)?\b/], ["b", /^\b(true|false|null|yes|no)\b/]],
  css:   [["c", /^\/\*[\s\S]*?\*\//], ["s", /^"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'/], ["n", /^-?\b\d[\w.]*\b|^#[0-9a-fA-F]{3,8}\b/], ["k", /^([A-Za-z-]+)\s*(?=\()/], ["b", /^@[\w-]+/]],
  html:  [["c", /^<!--[\s\S]*?-->/], ["k", /^<\/?[a-zA-Z][\w-]*/], ["s", /^"[^"]*"|'[^']*'/], ["f", /^[\w-]+(?==)/], ["n", /^\b\d+\b/]],
  sh:    [["c", /^#[^\n]*/], ["s", /^"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`[^`]*`/], ["k", /^(if|then|else|elif|fi|for|while|do|done|case|esac|function|in|return|exit|echo|export|source|set|unset|local|read)\b/], ["n", /^\b\d+\b/]],
  c:     [["c", /^\/\/[^\n]*|\/\*[\s\S]*?\*\//], ["s", /^"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])'|^[Lub]?"(?:\\.|[^"\\])*"/], ["k", /^(int|char|float|double|long|short|unsigned|signed|void|struct|union|enum|typedef|const|static|extern|return|if|else|for|while|do|switch|case|break|continue|goto|sizeof|include|define|class|namespace|template|using|new|delete|public|private|protected|this|true|false|nullptr|auto|try|catch|throw|std|bool)\b/], ["n", /^\b\d[\w]*\b/]],
  go:    [["c", /^\/\/[^\n]*|\/\*[\s\S]*?\*\//], ["s", /^`[^`]*`|"(?:\\.|[^"\\])*"/], ["k", /^(func|package|import|var|const|type|struct|interface|map|chan|go|defer|return|if|else|for|range|switch|case|default|break|continue|fallthrough|select|make|new|len|cap|append|error|nil|true|false)\b/], ["n", /^\b\d[\d_]*(\.\d+)?\b/]],
  rs:    [["c", /^\/\/[^\n]*|\/\*[\s\S]*?\*\//], ["s", /^r#?"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])'|^b?"(?:\\.|[^"\\])*"/], ["k", /^(fn|let|mut|const|struct|enum|impl|trait|use|mod|pub|self|Self|match|if|else|for|while|loop|return|break|continue|where|type|dyn|async|await|move|ref|in|true|false|null|Some|None|Ok|Err|Result|Option|Vec|String)\b/], ["n", /^\b\d[\d_]*(\.\d+)?\b/]],
  sql:   [["c", /^--[^\n]*|\/\*[\s\S]*?\*\//], ["k", /^(SELECT|INSERT|UPDATE|DELETE|FROM|WHERE|JOIN|LEFT|RIGHT|INNER|OUTER|ON|GROUP|BY|ORDER|HAVING|LIMIT|OFFSET|AS|AND|OR|NOT|IN|LIKE|BETWEEN|IS|NULL|CREATE|TABLE|ALTER|DROP|INDEX|PRIMARY|KEY|FOREIGN|REFERENCES|DISTINCT|UNION|ALL|VALUES|SET|CASE|WHEN|THEN|ELSE|END|BEGIN|COMMIT|ROLLBACK|TRUNCATE)\b/i], ["s", /^"(?:\\.|[^"\\])*"|'[^']*'/], ["n", /^\b\d+\b/]],
};
const DEFAULT_RULES = [["c", /^#[^\n]*|\/\/[^\n]*|\/\*[\s\S]*?\*\//], ["s", /^"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`[^`]*`/], ["k", /^[A-Za-z_$][\w$]*\s*(?=\()|^[A-Za-z_$][\w$]*/], ["n", /^\b\d[\d_]*(\.\d+)?\b/]];

/** Map an extension to its code-rule table. */
function langFor(name) {
  const ext = (name.split(".").pop() || "").toLowerCase();
  const map = { py:"py", ts:"js", tsx:"js", js:"js", jsx:"js", mjs:"js", cjs:"js", json:"json", toml:"toml", yaml:"yaml", yml:"yaml", css:"css", scss:"css", html:"html", htm:"html", sh:"sh", bash:"sh", zsh:"sh", rs:"rs", go:"go", java:"c", c:"c", cpp:"c", h:"c", sql:"sql", rb:"c", php:"html", swift:"c", kt:"c", lua:"c", r:"py", xml:"html" };
  return CODE_RULES[map[ext]] || DEFAULT_RULES;
}

/** Tokenize one source line into HTML with per-token classes. */
function highlightLine(src, rules) {
  let out = "";
  let i = 0;
  while (i < src.length) {
    let matched = false;
    for (const [cls, re] of rules) {
      const m = re.exec(src.slice(i));
      if (m && m[0].length > 0) {
        out += `<span class="tk-${cls}">${esc(m[0])}</span>`;
        i += m[0].length;
        matched = true;
        break;
      }
    }
    if (!matched) {
      out += esc(src[i]);
      i++;
    }
  }
  return out || "&nbsp;";
}



/** Color a markdown *source* line (not rendered HTML).
 *
 * Structural markers (heading, list, quote, fence, hr) are colored only at the
 * line start; inline tokens (code, bold, italic, links) are colored anywhere.
 */
function highlightMdLine(line) {
  let out = "";
  let rest = line;
  // Structural markers first (anchored to the start of what remains).
  const struct = rest.match(/^((#{1,6})\s|>\s|(\s*[-*+]|\s*\d+[.)])\s|```+|~~~+|(\s*[-*_])\s*$)/);
  if (struct) {
    const marker = struct[0];
    out += `<span class="tk-m">${esc(marker)}</span>`;
    rest = rest.slice(marker.length);
  }
  // Inline tokens.
  while (rest) {
    const m = rest.match(/^(`[^`]*`|\*\*[^*\n]+\*\*|\*[^*\n]+\*|\[[^\]]*\]\([^)\s]*\))/);
    if (m) {
      const tok = m[0];
      let cls = "tk-s";
      if (tok.startsWith("**")) cls = "tk-b";
      else if (tok.startsWith("*") && !tok.startsWith("**")) cls = "tk-e";
      else if (tok.startsWith("[")) cls = "tk-a";
      out += `<span class="${cls}">${esc(tok)}</span>`;
      rest = rest.slice(tok.length);
      continue;
    }
    out += esc(rest[0]);
    rest = rest.slice(1);
  }
  return out;
}

/** Highlight a source line for the editor's color layer, by file kind. */
function highlightSrc(line, name) {
  return kindFor(name) === "md" ? highlightMdLine(line) : highlightLine(line, langFor(name));
}

function el(tag, props = {}, kids = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) node.setAttribute(k, String(v));
  }
  for (const kid of [].concat(kids)) {
    if (kid) node.append(kid);
  }
  return node;
}

/** One <style> for all instances; the element itself lives in the light DOM. */
function ensureStyles() {
  if (document.getElementById("xu-filebrowser-css")) return;
  const tag = document.createElement("style");
  tag.id = "xu-filebrowser-css";
  tag.textContent = CSS;
  document.head.append(tag);
}

class XuFileBrowser extends HTMLElement {
  #brain = null;
  #open = false;
  #box = { x: 0, y: 0, w: 0, h: 0 };
  // Directory state
  #rel = "";
  #entries = [];
  #parent = null;
  #listing = false;
  // Open-file state
  #selected = null;
  #tag = null;
  #draft = "";
  #original = "";
  #loadingFile = false;
  #saving = false;
  #creating = false;
  #search = "";
  #confirmDelete = false;
  #error = null;
  /** Keeps focus/caret across the re-render an input triggers. */
  #focusKey = null;
  #onKeydown;

  constructor() {
    super();
    ensureStyles();
    this.#onKeydown = (e) => {
      if (this.#open && e.key === "Escape") this.#toggle();
    };
  }

  /** The shell hands over the store; it may arrive before or after connect. */
  set brain(value) {
    this.#brain = value;
    this.#render();
  }

  get brain() {
    return this.#brain;
  }

  connectedCallback() {
    window.addEventListener("keydown", this.#onKeydown);
    this.#render();
  }

  disconnectedCallback() {
    window.removeEventListener("keydown", this.#onKeydown);
    // The shell removed us (plugin disabled, or a layout swap). Drop the modal
    // and every buffer so nothing survives to a later re-mount, and so an open
    // panel cannot outlive the element that owns it.
    this.#open = false;
    this.#closeFile();
    this.#entries = [];
    this.#rel = "";
    this.#parent = null;
    this.#creating = false;
    this.#error = null;
    this.replaceChildren();
  }

  // ---- rpc ---------------------------------------------------------- //

  async #call(method, params = {}) {
    const brain = this.#brain;
    if (!brain?.client) throw new Error("brain store unavailable");
    const sid = brain.session?.id;
    return brain.client.call(method, { ...(sid ? { session_id: sid } : {}), ...params });
  }

  #fail(e) {
    this.#error = e instanceof Error ? e.message : String(e);
  }

  // ---- geometry ----------------------------------------------------- //

  /** The shell sets `zoom: var(--scale)` on the body, so pointer coords arrive
   *  unscaled while our geometry is in the zoomed space. Convert both ways. */
  #scale() {
    const raw = getComputedStyle(document.documentElement).getPropertyValue("--scale");
    const n = Number.parseFloat(raw);
    return Number.isFinite(n) && n > 0 ? n : 1;
  }

  #viewport() {
    const s = this.#scale();
    return { w: window.innerWidth / s, h: window.innerHeight / s };
  }

  #seedBox() {
    const vp = this.#viewport();
    const w = Math.min(920, Math.round(vp.w * 0.94));
    const h = Math.min(620, Math.round(vp.h * 0.82));
    this.#box = { w, h, x: Math.round((vp.w - w) / 2), y: Math.round((vp.h - h) / 2) };
  }

  /** Drag one edge/corner. North and west move the origin as well as the size,
   *  which native `resize` cannot do — hence all 8 handles by hand. */
  #startResize(edge, ev) {
    ev.preventDefault();
    const handle = ev.currentTarget;
    handle.setPointerCapture(ev.pointerId);
    const startX = ev.clientX;
    const startY = ev.clientY;
    const start = { ...this.#box };
    const s = this.#scale();
    const vp = this.#viewport();
    const panel = this.querySelector(".fc-panel");

    const move = (e) => {
      const dx = (e.clientX - startX) / s;
      const dy = (e.clientY - startY) / s;
      const next = { ...start };
      if (edge.includes("e")) next.w = Math.min(Math.max(start.w + dx, MIN_W), vp.w - start.x);
      if (edge.includes("s")) next.h = Math.min(Math.max(start.h + dy, MIN_H), vp.h - start.y);
      if (edge.includes("w")) {
        const right = start.x + start.w;
        next.x = Math.min(Math.max(start.x + dx, 0), right - MIN_W);
        next.w = right - next.x;
      }
      if (edge.includes("n")) {
        const bottom = start.y + start.h;
        next.y = Math.min(Math.max(start.y + dy, 0), bottom - MIN_H);
        next.h = bottom - next.y;
      }
      this.#box = {
        x: Math.round(next.x), y: Math.round(next.y),
        w: Math.round(next.w), h: Math.round(next.h),
      };
      // Style-only update: a full re-render mid-drag would drop the capture.
      if (panel) this.#applyBox(panel);
    };
    const stop = () => {
      handle.removeEventListener("pointermove", move);
      handle.removeEventListener("pointerup", stop);
      handle.removeEventListener("pointercancel", stop);
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", stop);
    handle.addEventListener("pointercancel", stop);
  }

  #applyBox(panel) {
    const b = this.#box;
    panel.style.left = `${b.x}px`;
    panel.style.top = `${b.y}px`;
    panel.style.width = `${b.w}px`;
    panel.style.height = `${b.h}px`;
  }

  // ---- actions ------------------------------------------------------ //

  #toggle() {
    this.#open = !this.#open;
    if (this.#open) {
      const vp = this.#viewport();
      if (!this.#box.w || this.#box.w > vp.w || this.#box.h > vp.h) this.#seedBox();
      void this.#list("");
    } else {
      this.#closeFile();
      this.#creating = false;
      this.#error = null;
    }
    this.#render();
  }
  #closeFile() {
    this.#selected = null;
    this.#tag = null;
    this.#draft = "";
    this.#original = "";
    this.#confirmDelete = false;
    this.#search = "";
  }

  #dirty() {
    return !!this.#selected && this.#draft !== this.#original;
  }

  #join(...parts) {
    return parts.filter(Boolean).join("/");
  }

  async #list(next) {
    this.#listing = true;
    this.#error = null;
    this.#render();
    try {
      const r = await this.#call("fs.tree", { path: next });
      this.#rel = r.rel;
      this.#entries = r.entries;
      this.#parent = r.parent;
    } catch (e) {
      this.#fail(e);
    } finally {
      this.#listing = false;
      this.#render();
    }
  }

  async #enter(entry) {
    if (entry.dir) {
      this.#closeFile();
      await this.#list(this.#join(this.#rel, entry.name));
      return;
    }
    await this.#openFile(entry.name);
  }

  async #openFile(name) {
    this.#loadingFile = true;
    this.#error = null;
    this.#confirmDelete = false;
    this.#search = "";
    this.#render();
    try {
      const r = await this.#call("fs.read", { path: this.#join(this.#rel, name) });
      this.#selected = name;
      this.#draft = r.content;
      this.#original = r.content;
      this.#tag = r.tag;
    } catch (e) {
      this.#fail(e);
      this.#selected = null;
    } finally {
      this.#loadingFile = false;
      this.#render();
    }
  }

  async #save() {
    if (!this.#selected) return;
    this.#saving = true;
    this.#error = null;
    this.#render();
    try {
      const r = await this.#call("fs.write", {
        path: this.#join(this.#rel, this.#selected),
        content: this.#draft,
        ...(this.#tag ? { tag: this.#tag } : {}),
      });
      this.#original = this.#draft;
      this.#tag = r.tag;
      await this.#list(this.#rel);
    } catch (e) {
      this.#fail(e);
    } finally {
      this.#saving = false;
      this.#render();
    }
  }

  async #create(name) {
    const trimmed = (name || "").trim();
    if (!trimmed) return;
    this.#error = null;
    try {
      await this.#call("fs.write", { path: this.#join(this.#rel, trimmed), content: "" });
      this.#creating = false;
      await this.#list(this.#rel);
      await this.#openFile(trimmed);
    } catch (e) {
      this.#fail(e);
      this.#render();
    }
  }

  async #remove() {
    if (!this.#selected) return;
    this.#error = null;
    try {
      await this.#call("fs.remove", { path: this.#join(this.#rel, this.#selected) });
      this.#closeFile();
      await this.#list(this.#rel);
    } catch (e) {
      this.#fail(e);
      this.#render();
    }
  }

  async #up() {
    if (this.#parent === null) return;
    this.#closeFile();
    await this.#list(this.#parent);
  }

  async #jump(i) {
    this.#closeFile();
    await this.#list(this.#rel.split("/").slice(0, i).join("/"));
  }

  #hits() {
    const q = this.#search.trim().toLowerCase();
    if (!q || !this.#selected) return [];
    return this.#draft
      .split("\n")
      .map((text, i) => ({ n: i + 1, text }))
      .filter((row) => row.text.toLowerCase().includes(q))
      .slice(0, 200);
  }

  // ---- render ------------------------------------------------------- //

  #render() {
    this.replaceChildren(this.#chip());
    if (this.#open) this.append(this.#modal());
    this.#restoreFocus();
  }

  #chip() {
    const ico = el("span", { class: "fc-ico", "aria-hidden": "true" });
    ico.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">' +
      '<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';
    const btn = el(
      "button",
      {
        class: `fc-chip${this.#open ? " on" : ""}`,
        type: "button",
        title: "Browse files in the session cwd",
        "aria-expanded": String(this.#open),
        onclick: () => this.#toggle(),
      },
      [ico, document.createTextNode("FILES")],
    );
    return btn;
  }

  #modal() {
    const cwd = this.#brain?.state?.cwd || "~";
    const scrim = el("div", {
      class: "dir-modal",
      role: "dialog",
      "aria-modal": "true",
      "aria-label": "file explorer",
      onclick: (e) => {
        if (e.target === e.currentTarget) this.#toggle();
      },
    });
    const panel = el("div", { class: "dir-modal-panel fc-panel" });
    this.#applyBox(panel);

    for (const edge of EDGES) {
      panel.append(
        el("div", {
          class: `fc-grip fc-grip-${edge}`,
          role: "separator",
          "aria-label": `Resize ${edge}`,
          onpointerdown: (e) => this.#startResize(edge, e),
        }),
      );
    }

    panel.append(
      el("div", { class: "dir-head" }, [
        el("span", { text: "FILE EXPLORER" }),
        el("button", {
          class: "dir-close", type: "button", "aria-label": "close",
          text: "\u00d7", onclick: () => this.#toggle(),
        }),
      ]),
      this.#crumbs(),
    );

    if (this.#error) {
      panel.append(el("div", { class: "fc-error", role: "alert", text: this.#error }));
    }

    panel.append(
      el("div", { class: "fc-body" }, [this.#treePane(), this.#filePane()]),
      el("div", { class: "dir-actions fc-foot" }, [
        el("span", { class: "fc-note", title: cwd, text: cwd }),
        el("button", { class: "cd-btn", type: "button", text: "CLOSE", onclick: () => this.#toggle() }),
      ]),
    );

    scrim.append(panel);
    return scrim;
  }

  #crumbs() {
    const parts = ["cwd", ...(this.#rel ? this.#rel.split("/") : [])];
    const row = el("div", { class: "dir-path" });
    parts.forEach((label, i) => {
      row.append(
        el("button", { class: "fc-crumb", type: "button", text: label, onclick: () => void this.#jump(i) }),
      );
      if (i < parts.length - 1) row.append(el("span", { class: "fc-sep", text: "/" }));
    });
    return row;
  }

  #treePane() {
    const pane = el("div", { class: "fc-tree" });
    pane.append(
      el("div", { class: "fc-toolbar" }, [
        el("button", {
          class: "fc-act", type: "button", text: "+ NEW",
          onclick: () => { this.#creating = !this.#creating; this.#focusKey = this.#creating ? "new" : null; this.#render(); },
        }),
        el("button", {
          class: "fc-act", type: "button", text: "\u2b06 UP",
          disabled: this.#parent === null,
          onclick: () => void this.#up(),
        }),
      ]),
    );

    if (this.#creating) {
      const input = el("input", {
        type: "text", placeholder: "new-file.txt", "data-focus": "new",
        onkeydown: (e) => {
          if (e.key === "Enter") void this.#create(e.currentTarget.value);
          if (e.key === "Escape") { this.#creating = false; this.#render(); }
        },
      });
      pane.append(
        el("div", { class: "fc-new" }, [
          input,
          el("button", { class: "cd-btn", type: "button", text: "ADD", onclick: () => void this.#create(input.value) }),
        ]),
      );
    }

    const list = el("div", { class: "fc-list" });
    if (this.#listing) {
      list.append(el("div", { class: "dir-empty", text: "loading\u2026" }));
    } else if (!this.#entries.length) {
      list.append(el("div", { class: "dir-empty", text: "empty directory" }));
    } else {
      for (const entry of this.#entries) {
        list.append(
          el(
            "button",
            {
              class: `dir-item fc-item${entry.dir ? " dir" : ""}${this.#selected === entry.name ? " sel" : ""}`,
              type: "button",
              onclick: () => void this.#enter(entry),
            },
            [
              el("span", { class: "fc-badge", "aria-hidden": "true", text: entry.dir ? "\u25b8" : iconFor(entry.name) }),
              el("span", { class: "fc-name", text: `${entry.name}${entry.dir ? "/" : ""}` }),
              el("span", { class: "fc-size", text: human(entry.size) }),
            ],
          ),
        );
      }
    }
    pane.append(list);
    return pane;
  }

  #filePane() {
    const pane = el("div", { class: "fc-pane" });

    if (this.#loadingFile) {
      pane.append(el("div", { class: "fc-blank" }, [el("p", { text: "loading\u2026" })]));
      return pane;
    }
    if (!this.#selected) {
      pane.append(
        el("div", { class: "fc-blank" }, [
          el("p", { text: "no file open" }),
          el("small", { text: "pick a file on the left" }),
        ]),
      );
      return pane;
    }

    const openPath = this.#join(this.#rel, this.#selected);
    const label = el("span", { class: "fc-open", title: openPath, text: openPath });
    if (this.#dirty()) label.append(el("em", { class: "fc-dirty", text: "\u25cf unsaved" }));

    pane.append(
      el("div", { class: "fc-pane-head" }, [
        label,
        el("button", {
          class: "fc-act", type: "button",
          text: this.#saving ? "SAVING\u2026" : "SAVE",
          disabled: !this.#dirty() || this.#saving,
          onclick: () => void this.#save(),
        }),
        el("input", {
          class: "fc-search", type: "search", placeholder: "search in file\u2026",
          "aria-label": "search in file", value: this.#search, "data-focus": "search",
          oninput: (e) => { this.#search = e.currentTarget.value; this.#focusKey = "search"; this.#render(); },
          onkeydown: (e) => {
            if (e.key === "Escape") { this.#search = ""; this.#focusKey = "search"; this.#render(); }
          },
        }),
        el("button", {
          class: "fc-act danger", type: "button", text: "DELETE",
          onclick: () => { this.#confirmDelete = !this.#confirmDelete; this.#render(); },
        }),
      ]),
    );

    if (this.#search.trim()) {
      const hits = this.#hits();
      const box = el("div", { class: "fc-hits" });
      if (!hits.length) {
        box.append(el("span", { class: "fc-hits-count", text: "no match" }));
      } else {
        box.append(el("span", { class: "fc-hits-count", text: `${hits.length} line${hits.length === 1 ? "" : "s"}` }));
        for (const h of hits) {
          box.append(
            el("div", { class: "fc-hit" }, [
              el("code", { text: String(h.n) }),
              el("span", { text: h.text.trim() }),
            ]),
          );
        }
      }
      pane.append(box);
    }

    if (this.#confirmDelete) {
      pane.append(
        el("div", { class: "fc-confirm" }, [
          document.createTextNode("delete "),
          el("code", { text: this.#selected }),
          document.createTextNode("? this cannot be undone \u2014 "),
          el("button", { class: "fc-act danger", type: "button", text: "CONFIRM", onclick: () => void this.#remove() }),
          el("button", {
            class: "fc-act", type: "button", text: "CANCEL",
            onclick: () => { this.#confirmDelete = false; this.#render(); },
          }),
        ]),
      );
    }

    const box = el("div", { class: "fc-code" });
    const glow = el("pre", { "aria-hidden": "true" });
    const editor = el("textarea", {
      class: "fc-editor", spellcheck: "false",
      "aria-label": `contents of ${this.#selected}`, "data-focus": "editor",
      oninput: (e) => {
        const wasDirty = this.#dirty();
        this.#draft = e.currentTarget.value;
        // Repaint only the color layer — rebuilding the textarea would jump
        // the caret. Dirty/search re-render stays a pane-level decision.
        if (this.#selected) glow.innerHTML = this.#highlightHTML(this.#draft, this.#selected);
        if (this.#dirty() !== wasDirty || this.#search.trim()) {
          this.#focusKey = "editor";
          this.#render();
        }
      },
      onscroll: (e) => { glow.scrollTop = e.currentTarget.scrollTop; glow.scrollLeft = e.currentTarget.scrollLeft; },
    });
    editor.value = this.#draft;
    glow.innerHTML = this.#highlightHTML(this.#draft, this.#selected);
    box.append(glow, editor);
    pane.append(box);
    return pane;
  }

  /** Colored, XSS-safe HTML for one file, used by the editor's color layer. */
  #highlightHTML(src, name) {
    return String(src).replace(/\r\n/g, "\n").split("\n")
      .map((l) => highlightSrc(l, name))
      .join("\n");
  }

  /** Re-focus (and restore the caret of) whatever input triggered a re-render. */
  #restoreFocus() {
    if (!this.#focusKey) return;
    const node = this.querySelector(`[data-focus="${this.#focusKey}"]`);
    this.#focusKey = null;
    if (!node) return;
    node.focus();
    if (typeof node.selectionStart === "number" && node.tagName === "TEXTAREA") {
      const end = node.value.length;
      node.setSelectionRange(end, end);
    }
  }
}

if (!customElements.get("xu-filebrowser")) {
  customElements.define("xu-filebrowser", XuFileBrowser);
}

export default XuFileBrowser;
