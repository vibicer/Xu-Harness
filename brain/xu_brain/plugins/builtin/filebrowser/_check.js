/** Runnable check for the filebrowser live color editor.
 *
 * The highlight helpers live inside ui.js (not exported). This loads the file

 * in a Node VM with browser globals stubbed, calls the pure functions directly,
 * and asserts their output — covering markdown/code source coloring + XSS
 * escaping, the two things that matter for a color layer fed by arbitrary
 * file contents.
 *
 * Run: `node ~/.xu/plugins/filebrowser/_check.js`
 */
import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(join(here, "ui.js"), "utf8");

// ---- browser stubs so the module's top level can load ------------- //
const noop = () => {};
globalThis.document = {
  createElement: () => ({ setAttribute(){}, addEventListener(){}, append(){}, appendChild(){}, focus(){}, value:"" }),
  createTextNode: (t) => ({ textContent: t }),
  head: { append(){} }, getElementById: () => null,
};
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => null, define: () => {} };
globalThis.window = { addEventListener: noop, removeEventListener: noop };

// Evaluate, dropping the module export line, and grab the pure helpers.
const body = src.replace(/^export default XuFileBrowser;/m, "");
const { esc, kindFor, highlightLine, highlightMdLine, highlightSrc } =
  new Function(body + "return { esc, kindFor, highlightLine, highlightMdLine, highlightSrc };")();

let pass = 0, fail = 0;
const check = (name, got, want) => {
  if (got === want) { pass++; console.log("ok", name); }
  else { fail++; console.log("FAIL", name, "\n  got :", JSON.stringify(got), "\n  want:", JSON.stringify(want)); }
};

// ---- escaping (XSS: raw content must never become markup) --------- //
check("esc HTML", esc("<b>&\""), "&lt;b&gt;&amp;&quot;");

// ---- file-kind detection ------------------------------------------- //
check("md", kindFor("README.md"), "md");
check("md uppercase", kindFor("A.MARKDOWN"), "md");
check("code py", kindFor("app.py"), "code");
check("code ts", kindFor("x.ts"), "code");
check("text", kindFor("notes.txt"), "text");

// ---- code source coloring ------------------------------------------ //
const py = highlightSrc("def hello(x):  # note", "a.py");
check("code keyword", py.includes("tk-k"), true);
check("code comment", py.includes("tk-c"), true);
check("code comment text", py.includes("# note"), true);
check("code keeps text", py.includes("hello"), true);
const js = highlightSrc('const s = "hi";', "b.js");
check("code string", js.includes("tk-s"), true);
check("code no raw lt", highlightSrc("<img>", "x.js").includes("<img>"), false);
check("code xss escaped", highlightSrc("<img>", "x.js").includes("&lt;img&gt;"), true);

// ---- markdown source coloring -------------------------------------- //
const mdHead = highlightSrc("# Title here", "R.md");
check("md heading marker", mdHead.includes("tk-m"), true);
const mdBold = highlightSrc("some **bold** text", "R.md");
check("md bold", mdBold.includes("tk-b"), true);
const mdCode = highlightSrc("run `npm i` now", "R.md");
check("md inline code", mdCode.includes("tk-s"), true);
const mdList = highlightSrc("- item one", "R.md");
check("md list marker", mdList.includes("tk-m"), true);
const mdXss = highlightSrc("<script>", "R.md");
check("md xss escaped", mdXss.includes("&lt;script&gt;"), true);
check("md xss no raw", mdXss.includes("<script>"), false);

// ---- whitespace/trailing blank lines stay present ------------------ //
check("empty line", highlightMdLine(""), "");

console.log(`\n${pass} passed, ${fail} failed`);
process.exit(fail ? 1 : 0);
