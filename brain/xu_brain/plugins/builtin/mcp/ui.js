/** mcp UI — <xu-mcp-config>, a Config pane mounted by the shell's PluginSlot.
 *
 * The server list, connect/disconnect, add/remove, and the two per-server
 * switches (autostart, trusted), all over this plugin's own `mcp.*` RPCs.
 * Plain custom element, no framework: the shell serves this file verbatim off
 * the plugin dir, so it must run as-is in the browser.
 *
 * Contract with the shell (see PluginSlot.svelte):
 * - this module defines the element named in manifest.ui.element
 * - the shell sets `el.brain` to the live store, before or after connecting
 * - the shell owns the `.cfg-pane` wrapper and shows/hides it per tab, so this
 *   element renders bare pane content and never asks which tab is open
 *
 * Styling is the shell's own `.cfg-*` / `.k-*` / `.chip-tag` / `.toggle`
 * classes in light DOM (no shadow root), so every theme and layout restyles
 * this pane without the plugin knowing which one is active. The handful of
 * `mcp-`-prefixed rules below cover only what the shell has no class for.
 */

const CSS = `
.mcp-form { display: grid; grid-template-columns: 110px minmax(0, 1fr); gap: 10px 14px; align-items: start; padding: 12px 2px 4px; }
.mcp-form label { font-family: var(--mono); font-size: 12px; color: var(--dim); padding-top: 8px; }
.mcp-form label small { display: block; font-family: var(--crt); font-size: 11px; color: var(--faint); }
.mcp-form input, .mcp-form textarea { width: 100%; box-sizing: border-box; }
.mcp-form textarea { min-height: 56px; resize: vertical; font-family: var(--mono); font-size: 12px; padding: 7px 9px; }
.mcp-foot { grid-column: 2; display: flex; gap: 9px; align-items: center; }
.mcp-wrap { white-space: pre-wrap; word-break: break-word; }
.mcp-bad { color: var(--danger); }
`;

let cssDone = false;
function injectCSS() {
  if (cssDone) return;
  cssDone = true;
  const el = document.createElement("style");
  el.dataset.plugin = "mcp";
  el.textContent = CSS;
  document.head.appendChild(el);
}

/** Escape text so nothing off the brain — a server name, a tool name, an error
 *  string from someone else's MCP server — can become markup. */
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

/** `KEY=value` per line → `{KEY: value}`. Blank lines and `#` comments skipped.
 *  A value may be a literal or `${VAR}`; the brain expands the latter, so a
 *  secret can stay in the environment instead of in `mcp.json`. */
function parseEnv(text) {
  const env = {};
  for (const line of String(text || "").split("\n")) {
    const s = line.trim();
    if (!s || s.startsWith("#")) continue;
    const i = s.indexOf("=");
    if (i < 1) continue;
    env[s.slice(0, i).trim()] = s.slice(i + 1).trim();
  }
  return env;
}

const STATE_CHIP = { on: "on", off: "off", error: "warn" };

/** One server row. Pure on purpose: the element owns the state, this owns the
 *  markup, and `_check.js` can assert the escaping without a DOM.
 *  `ui` = `{open, busy, armed}` for this row. */
function row(s, ui) {
  const count = s.tools?.length ?? 0;
  const sub = [
    [s.command, ...(s.args || [])].join(" "),
    s.state === "on" ? `${count} tool${count === 1 ? "" : "s"}` : "",
    s.transport !== "stdio" ? s.transport : "",
    s.configured ? "" : "not in mcp.json",
    s.enabled ? "" : "autostart off",
  ].filter(Boolean).join(" \u00b7 ");

  return `
    <div class="cfg-row">
      <button type="button" class="label row-exp" data-act="fold" data-name="${esc(s.name)}" aria-expanded="${ui.open}">
        <span class="exp-caret">${ui.open ? "\u25be" : "\u25b8"}</span>${esc(s.name)}
        <small class="mcp-wrap">${esc(sub)}</small>
      </button>
      <div class="ctrl">
        ${s.trusted ? `<span class="chip-tag on" title="tools run without an approval prompt">trusted</span>` : ""}
        <span class="chip-tag ${STATE_CHIP[s.state] || "off"}">${esc(s.state)}</span>
        <button type="button" class="k-btn sm" data-act="${s.state === "on" ? "disconnect" : "connect"}"
                data-name="${esc(s.name)}"${ui.busy ? " disabled" : ""}>
          ${ui.busy ? "\u2026" : s.state === "on" ? "Disconnect" : "Connect"}
        </button>
        <button type="button" class="k-btn dgr sm${ui.armed ? " armed" : ""}"
                data-act="remove" data-name="${esc(s.name)}"${ui.busy || !s.configured ? " disabled" : ""}
                title="${s.configured ? "delete from mcp.json" : "not in mcp.json \u2014 nothing to delete"}">
          ${ui.armed ? "Sure?" : "Remove"}
        </button>
      </div>
    </div>
    ${ui.open ? detail(s) : ""}`;
}

/** The expanded half of a row: what it runs, what it reported, its tools, and
 *  the two switches. Every value here is third-party (an MCP server names its
 *  own tools), so nothing reaches the markup unescaped. */
function detail(s) {
  const rows = [
    ["command", esc([s.command, ...(s.args || [])].join(" ")) || "\u2014"],
    s.cwd ? ["cwd", esc(s.cwd)] : null,
    // Key names only — the payload carries no values, by design.
    s.env_keys?.length ? ["env", esc(s.env_keys.join(" \u00b7 "))] : null,
    s.server?.name ? ["reported", esc(`${s.server.name} ${s.server.version || ""}`)] : null,
    s.error ? ["error", `<span class="mcp-bad">${esc(s.error)}</span>`] : null,
    s.tools?.length
      ? ["tools", `<span class="k-chips">${s.tools.map((t) => `<span class="chip-tag">${esc(t)}</span>`).join("")}</span>`]
      : null,
  ].filter(Boolean);

  return `
    <div class="tooldetail">
      ${rows.map(([k, v]) => `<div class="td-row"><span class="td-name">${k}</span><span class="td-desc mcp-wrap">${v}</span></div>`).join("")}
      <div class="td-row">
        <span class="td-name">autostart</span>
        <span class="td-desc">connect on boot</span>
        <label class="toggle" title="autostart">
          <input type="checkbox" data-act="enabled" data-name="${esc(s.name)}"${s.enabled ? " checked" : ""} />
          <span class="track"></span><span class="thumb"></span>
        </label>
      </div>
      <div class="td-row">
        <span class="td-name">trusted</span>
        <span class="td-desc">run this server's tools without an approval prompt</span>
        <label class="toggle" title="trusted">
          <input type="checkbox" data-act="trusted" data-name="${esc(s.name)}"${s.trusted ? " checked" : ""} />
          <span class="track"></span><span class="thumb"></span>
        </label>
      </div>
    </div>`;
}
class XuMcpConfig extends HTMLElement {
  #brain = null;
  #snap = null;
  #open = new Set();
  #armed = "";
  #busy = "";
  #error = "";
  #list = null;
  #note = null;
  #msg = null;
  #io = null;

  /** The shell hands over the store; it may arrive before or after connect. */
  set brain(value) {
    this.#brain = value;
    void this.#load();
  }

  get brain() {
    return this.#brain;
  }

  connectedCallback() {
    injectCSS();
    if (!this.#list) this.#mount();
    // A hidden `.cfg-pane` has no box, so this fires exactly when the tab is
    // opened — state that went stale while the pane was closed (a server that
    // died, a hand-edit to mcp.json) refreshes on the way in.
    this.#io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) void this.#load();
    });
    this.#io.observe(this);
  }

  disconnectedCallback() {
    // The shell removed us (plugin disabled, or a layout swap). Drop everything
    // so no stale row or armed Remove survives to a later re-mount.
    this.#io?.disconnect();
    this.#io = null;
    this.#snap = null;
    this.#armed = "";
    this.#error = "";
    this.#open.clear();
    this.#list = null;
    this.replaceChildren();
  }

  // ---- rpc ----------------------------------------------------------- //

  async #call(method, params = {}) {
    if (!this.#brain?.client) throw new Error("brain store unavailable");
    return this.#brain.client.call(method, params);
  }

  async #load() {
    if (!this.#brain?.client || !this.#list) return;
    try {
      this.#snap = await this.#call("mcp.list");
      this.#error = "";
    } catch (e) {
      // -32601: brain up, plugin not loaded. Anything else is worth showing.
      this.#error = e?.code === -32601 ? "" : e?.message || String(e);
    }
    this.#paint();
  }

  /** Run one mutation, then reload: every RPC returns a row but the toolset and
   *  the other rows can move too (trusting a server re-registers its tools). */
  async #act(name, method, params) {
    this.#busy = name;
    this.#error = "";
    this.#paint();
    let failed = "";
    try {
      await this.#call(method, params);
    } catch (e) {
      failed = e?.message || String(e);
    } finally {
      this.#busy = "";
    }
    // `#load` clears the banner it owns, so the failure is restored after it.
    await this.#load();
    if (failed) {
      this.#error = failed;
      this.#paint();
    }
  }

  // ---- dom ----------------------------------------------------------- //

  /** Build the skeleton once. The add form is deliberately outside the part
   *  that re-renders: repainting it would wipe whatever is half-typed. */
  #mount() {
    this.innerHTML = `
      <div class="cfg-msg err" style="display:none"></div>
      <div class="cfg-sec">
        <div class="cfg-sec-hd">
          <span class="t">MCP Servers</span>
          <span class="d"></span>
          <span class="sp"></span>
          <button type="button" class="k-btn ghost sm" data-act="reload">Refresh</button>
        </div>
        <div class="mcp-list"></div>
      </div>
      <div class="cfg-sec">
        <div class="cfg-sec-hd">
          <span class="t">Add Server</span>
          <span class="d">stdio only — the command runs on this machine, with your permissions</span>
        </div>
        <form class="mcp-form" autocomplete="off">
          <label for="mcp-f-name">name</label>
          <input id="mcp-f-name" name="name" class="k-in" type="text" placeholder="context7" required />
          <label for="mcp-f-cmd">command</label>
          <input id="mcp-f-cmd" name="command" class="k-in" type="text" placeholder="npx" required />
          <label for="mcp-f-args">args<small>one line, quotes ok</small></label>
          <input id="mcp-f-args" name="args" class="k-in" type="text" placeholder="-y @upstash/context7-mcp" />
          <label for="mcp-f-cwd">cwd<small>optional</small></label>
          <input id="mcp-f-cwd" name="cwd" class="k-in" type="text" placeholder="/home/you/project" />
          <label for="mcp-f-env">env<small>KEY=value per line</small></label>
          <textarea id="mcp-f-env" name="env" placeholder="API_KEY=\${MY_KEY}"></textarea>
          <div class="mcp-foot">
            <button type="submit" class="k-btn pri">Add &amp; connect</button>
            <span class="d k-empty">saved to mcp.json</span>
          </div>
        </form>
      </div>`;
    this.#list = this.querySelector(".mcp-list");
    this.#note = this.querySelector(".cfg-sec-hd .d");
    this.#msg = this.querySelector(".cfg-msg");
    this.addEventListener("click", this.#onClick);
    this.addEventListener("change", this.#onChange);
    this.querySelector("form").addEventListener("submit", this.#onSubmit);
  }

  #paint() {
    if (!this.#list) return;
    const snap = this.#snap;
    const servers = snap?.servers ?? [];
    const on = servers.filter((s) => s.state === "on").length;
    const tools = servers.reduce((n, s) => n + (s.tools?.length ?? 0), 0);
    this.#note.textContent = snap
      ? `${on}/${servers.length} connected · ${tools} tool${tools === 1 ? "" : "s"} in ${snap.toolset} · ${snap.config}`
      : "loading…";

    const problem = this.#error || snap?.config_error || "";
    this.#msg.textContent = problem;
    // `hidden` doesn't work on `.cfg-msg` (its `display:flex` rule beats the
    // UA `[hidden]`), so hide with an inline display instead.
    this.#msg.style.display = problem ? "" : "none";

    this.#list.innerHTML = servers.length
      ? servers.map((s) => row(s, {
          open: this.#open.has(s.name),
          busy: this.#busy === s.name,
          armed: this.#armed === s.name,
        })).join("")
      : `<div class="k-empty">No MCP servers yet — add one below.</div>`;
  }

  // ---- events -------------------------------------------------------- //

  /** One delegated handler: the list is replaced wholesale on every paint, so
   *  per-node listeners would leak with it. */
  #onClick = (ev) => {
    const btn = ev.target.closest("[data-act]");
    if (!btn || !this.contains(btn) || btn.tagName !== "BUTTON") return;
    const { act, name } = btn.dataset;
    if (act !== "remove") this.#armed = "";
    switch (act) {
      case "reload":
        void this.#load();
        break;
      case "fold":
        this.#open.has(name) ? this.#open.delete(name) : this.#open.add(name);
        this.#paint();
        break;
      case "connect":
      case "disconnect":
        void this.#act(name, `mcp.${act}`, { name });
        break;
      case "remove":
        // Two clicks: killing a server and editing mcp.json is not undoable.
        if (this.#armed !== name) {
          this.#armed = name;
          this.#paint();
        } else {
          this.#armed = "";
          this.#open.delete(name);
          void this.#act(name, "mcp.remove", { name });
        }
        break;
    }
  };

  #onChange = (ev) => {
    const box = ev.target.closest("input[data-act]");
    if (!box || !this.contains(box)) return;
    const { act, name } = box.dataset;
    if (act === "enabled" || act === "trusted") {
      void this.#act(name, "mcp.set", { name, [act]: box.checked });
    }
  };

  #onSubmit = (ev) => {
    ev.preventDefault();
    // Always via `elements`: a form's own `name`/`autocomplete` IDL properties
    // shadow same-named fields, so `f.name` would be the form, not the input.
    const form = ev.target;
    const f = form.elements;
    const env = parseEnv(f.env.value);
    void this.#act("add", "mcp.add", {
      name: f.name.value.trim(),
      command: f.command.value.trim(),
      // The brain shlex-splits a one-line string, so quoting works here.
      args: f.args.value.trim(),
      cwd: f.cwd.value.trim(),
      ...(Object.keys(env).length ? { env } : {}),
      connect: true,
    }).then(() => {
      // Keep a rejected draft on screen to be fixed; clear it once it landed.
      if (!this.#error) form.reset();
    });
  };
}

if (!customElements.get("xu-mcp-config")) {
  customElements.define("xu-mcp-config", XuMcpConfig);
}
