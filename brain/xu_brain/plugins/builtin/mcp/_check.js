/** Runnable check for the mcp Config pane.
 *
 * The render helpers live inside ui.js (not exported). This loads the file with
 * browser globals stubbed, calls the pure functions directly, and asserts their
 * output — covering the two things that matter for markup built from strings a
 * third party chose: escaping (an MCP server names its own tools) and the
 * `KEY=value` env parser.
 *
 * Run: `node xu_brain/plugins/builtin/mcp/_check.js`
 */
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "ui.js"), "utf8");

// ---- browser stubs so the module's top level can load -------------- //
globalThis.document = { createElement: () => ({ dataset: {} }), head: { appendChild() {} } };
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => null, define: () => {} };
globalThis.IntersectionObserver = class { observe() {} disconnect() {} };

const { esc, parseEnv, row, detail } =
  new Function(src + "return { esc, parseEnv, row, detail };")();

let pass = 0, fail = 0;
const check = (name, got, want) => {
  if (got === want) { pass++; console.log("ok", name); }
  else { fail++; console.log("FAIL", name, "\n  got :", JSON.stringify(got), "\n  want:", JSON.stringify(want)); }
};

const server = (over = {}) => ({
  name: "ctx7", command: "npx", args: ["-y", "pkg"], cwd: "", env_keys: [],
  transport: "stdio", enabled: true, configured: true, state: "on",
  trusted: false, error: null, tools: ["search"], server: null, ...over,
});

// ---- escaping ------------------------------------------------------- //
check("esc HTML", esc("<b>&\""), "&lt;b&gt;&amp;&quot;");
check("esc null", esc(null), "");

const evil = "<img src=x onerror=alert(1)>";
const badName = row(server({ name: evil }), { open: false, busy: false, armed: false });
check("row escapes name", badName.includes("<img"), false);
check("row keeps name text", badName.includes("&lt;img"), true);

const badTool = detail(server({ tools: [evil] }));
check("detail escapes tool name", badTool.includes("<img"), false);
const badErr = detail(server({ error: '"><script>x</script>' }));
check("detail escapes error", badErr.includes("<script>"), false);

// A name lands in an attribute too, so a quote must not close it early.
const quoted = row(server({ name: 'a" onclick="x' }), { open: false, busy: false, armed: false });
check("row escapes attribute", quoted.includes('" onclick="x'), false);

// ---- row state ------------------------------------------------------ //
const off = row(server({ state: "off" }), { open: false, busy: false, armed: false });
check("off offers Connect", off.includes("Connect"), true);
check("off has no Disconnect", off.includes("Disconnect"), false);
const on = row(server(), { open: false, busy: false, armed: false });
check("on offers Disconnect", on.includes('data-act="disconnect"'), true);
check("error state chips warn", row(server({ state: "error" }), {}).includes('chip-tag warn'), true);

// Remove is two clicks, and only for something that is in mcp.json.
check("armed asks", row(server(), { armed: true }).includes("Sure?"), true);
check("unarmed does not", row(server(), {}).includes("Sure?"), false);
check("unconfigured cannot be removed",
  row(server({ configured: false }), {}).includes('data-act="remove" data-name="ctx7" disabled'), true);

// Folding is what shows the detail block; a closed row must not carry it.
check("closed row hides detail", row(server(), { open: false }).includes("tooldetail"), false);
check("open row shows detail", row(server(), { open: true }).includes("tooldetail"), true);

// ---- detail --------------------------------------------------------- //
check("detail shows command", detail(server()).includes("npx -y pkg"), true);
check("detail shows env keys", detail(server({ env_keys: ["API_KEY"] })).includes("API_KEY"), true);
check("autostart reflects enabled", detail(server({ enabled: false })).includes('data-act="enabled" data-name="ctx7" />'), true);
check("trusted reflects trusted", detail(server({ trusted: true })).includes('data-act="trusted" data-name="ctx7" checked'), true);

// ---- env parser ----------------------------------------------------- //
check("env one pair", JSON.stringify(parseEnv("A=1")), '{"A":"1"}');
check("env trims", JSON.stringify(parseEnv("  A = 1  ")), '{"A":"1"}');
check("env keeps = in value", JSON.stringify(parseEnv("A=b=c")), '{"A":"b=c"}');
check("env skips comments", JSON.stringify(parseEnv("# note\nA=1")), '{"A":"1"}');
check("env skips blanks", JSON.stringify(parseEnv("\n\nA=1\n\n")), '{"A":"1"}');
check("env skips junk", JSON.stringify(parseEnv("nope\n=x")), "{}");
check("env passes ${VAR} through", JSON.stringify(parseEnv("K=${SECRET}")), '{"K":"${SECRET}"}');

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
