// Runnable check for the brain store's public surface:
//
//   node checks/store-surface.test.ts
//
// The store is split across src/lib/store.svelte.ts (composition root) and
// concern mixins in src/lib/store/*.svelte.ts. Components import `brain` and
// do `brain.<member>` reads, so two things are pinned here:
//
//   1. The FULL set of public names (module exports of the entry file + every
//      public member of the composed class, wherever it now lives) must match
//      the hardcoded list below — no renames, no vanishings, no additions.
//   2. Every pinned class member resolves in EXACTLY ONE of the store files,
//      so a future split (D2) cannot silently drop or duplicate a member.
//
// These checks run as plain node --test and cannot import a runes module, so
// extraction is source-text based. Strings, comments and template literals are
// neutralised first, then a brace-depth walk tags each block as class-body or
// not; only lines sitting directly inside a class body count as members. The
// block context is what keeps module-level helpers (`pushText`), interface
// bodies (`TabState`) and object literals (`DEFAULT_STATE`) out of the set,
// whatever their indentation.
import assert from "node:assert/strict";
import { readFileSync, readdirSync, existsSync } from "node:fs";
import { join } from "node:path";

const lib = join(import.meta.dirname, "..", "src", "lib");
const entry = join(lib, "store.svelte.ts");
const storeDir = join(lib, "store");

const storeFiles = [
  entry,
  ...(existsSync(storeDir)
    ? readdirSync(storeDir).filter((f) => f.endsWith(".svelte.ts")).sort().map((f) => join(storeDir, f))
    : []),
];
assert.ok(storeFiles.length >= 1, "store.svelte.ts must exist");

/** Replace string, comment and template-literal *text* with spaces (same
 *  length, newlines kept) so nothing inside them can move a brace counter.
 *  Code inside a template's `${ ... }` hole stays live, but the hole's own
 *  `${` and `}` are blanked too — keeping one and dropping the other is what
 *  unbalances a brace walk. */
function neutralize(src: string): string {
  const out = src.split("");
  let mode: "code" | "line" | "block" | "str" | "tpl" = "code";
  let quote = "";
  const holeDepths: number[] = []; // one entry per open `${ ... }` hole
  let depth = 0;
  const blank = (i: number, n = 1) => {
    for (let k = i; k < i + n; k++) if (out[k] !== "\n") out[k] = " ";
  };
  for (let i = 0; i < src.length; i++) {
    const c = src[i];
    if (mode === "line") {
      blank(i);
      if (c === "\n") mode = "code";
      continue;
    }
    if (mode === "block") {
      if (c === "*" && src[i + 1] === "/") {
        blank(i, 2);
        i++;
        mode = "code";
        continue;
      }
      blank(i);
      continue;
    }
    if (mode === "str") {
      if (c === "\\") {
        blank(i, 2);
        i++;
        continue;
      }
      blank(i);
      if (c === quote) mode = "code";
      continue;
    }
    if (mode === "tpl") {
      if (c === "\\") {
        blank(i, 2);
        i++;
        continue;
      }
      if (c === "$" && src[i + 1] === "{") {
        // expression hole: live code until the `}` that closes it. Both the
        // `${` and its `}` are blanked, so the hole contributes no braces.
        holeDepths.push(depth);
        blank(i, 2);
        i++;
        mode = "code";
        continue;
      }
      blank(i);
      if (c === "`") mode = "code";
      continue;
    }
    // code
    if (c === "/" && src[i + 1] === "/") {
      blank(i, 2);
      i++;
      mode = "line";
      continue;
    }
    if (c === "/" && src[i + 1] === "*") {
      blank(i, 2);
      i++;
      mode = "block";
      continue;
    }
    if (c === '"' || c === "'") {
      blank(i);
      quote = c;
      mode = "str";
      continue;
    }
    if (c === "`") {
      blank(i);
      mode = "tpl";
      continue;
    }
    if (c === "{") {
      depth++;
      continue; // structural brace: kept
    }
    if (c === "}") {
      if (holeDepths.length && depth === holeDepths[holeDepths.length - 1]) {
        // closes a template hole, not a code block
        holeDepths.pop();
        blank(i);
        mode = "tpl";
        continue;
      }
      depth--;
      continue;
    }
  }
  return out.join("");
}

/** Push/pop the block stack over one neutralised line.
 *
 *  A `{` opens a class body when a `class` keyword appeared since the previous
 *  brace. That window (`since`) carries across lines, because the composed
 *  `extends A(B(C))` heritage clause is wrapped — the `class` keyword and the
 *  `{` are on different lines — and it resets at every brace, so a method body
 *  inside a class is correctly "other", not another class body.
 *
 *  Also returns the line's net paren delta: a member declaration is only
 *  recognised at paren depth 0, which is what keeps the continuation lines of a
 *  multi-line parameter list (`dragged`, `target`, ...) from reading as
 *  members. */
function walkLine(
  line: string,
  stackIn: ("class" | "other")[],
  sinceIn: string,
): { stack: ("class" | "other")[]; parens: number; since: string } {
  const stack = [...stackIn];
  let since = sinceIn;
  let parens = 0;
  for (let i = 0; i < line.length; i++) {
    const c = line[i];
    if (c === "{") {
      // `classList` does not match: \b needs a non-word char after "class"
      stack.push(/\bclass\b/.test(since) ? "class" : "other");
      since = "";
    } else if (c === "}") {
      stack.pop();
      since = "";
    } else {
      if (c === "(") parens++;
      else if (c === ")") parens--;
      since += c;
    }
  }
  return { stack, parens, since };
}

/** Names of public members declared directly inside a class body. `private`
 *  members are excluded, and `protected` ones too: those are mixin seams, not
 *  part of `brain`'s surface. Indentation is not required — parts of the store
 *  are currently written flush-left inside the class body. */
function classMemberNames(src: string): Set<string> {
  const names = new Set<string>();
  const memberRe =
    /^\s*(?:(?:readonly|public|static|override)\s+)*(?:async\s+)?(?:get\s+|set\s+)?([A-Za-z_$][A-Za-z0-9_$]*)\s*[=(<:]/;
  let stack: ("class" | "other")[] = [];
  let parens = 0;
  let since = "";
  for (const line of neutralize(src).split("\n")) {
    const container = stack.length ? stack[stack.length - 1] : "module";
    const m = memberRe.exec(line);
    if (
      parens === 0 &&
      container === "class" &&
      m &&
      m[1] !== "constructor" &&
      !/\b(?:private|protected|abstract|declare)\b/.test(line)
    ) {
      names.add(m[1]);
    }
    const walked = walkLine(line, stack, since);
    stack = walked.stack;
    parens += walked.parens;
    since = walked.since;
  }
  return names;
}

/** Module-level export names of the entry file (the compatibility contract
 *  other modules import from "./store"). */
function moduleExports(src: string): Set<string> {
  const out = new Set<string>();
  for (const m of src.matchAll(/^export (?:abstract )?(?:class|const|function|interface|type) ([A-Za-z_$][A-Za-z0-9_$]*)/gm)) {
    out.add(m[1]);
  }
  for (const m of src.matchAll(/^export (?:type )?\{([^}]+)\} from/gm)) {
    for (const name of m[1].split(",")) {
      const clean = name.trim().replace(/^type\s+/, "").split(/\s+as\s+/).pop();
      if (clean) out.add(clean);
    }
  }
  return out;
}

const declared = new Map<string, Set<string>>();
for (const f of storeFiles) {
  declared.set(f, classMemberNames(readFileSync(f, "utf8")));
}
declared.set(entry, new Set([...declared.get(entry)!, ...moduleExports(readFileSync(entry, "utf8"))]));

// 1. Full pinned surface of `brain` as of D1. If you must change this list,
//    you are changing the public contract ~20 components depend on — that is
//    a D2-scale decision, not a side effect.
const PINNED = [
  "activeCustomLayout", "activePersona", "addMemory", "addProvider", "allThemes",
  "applyNow", "approvalModeCycle", "approvals", "avatar", "bgfx", "BUILTIN_THEMES",
  "BuiltinViewName", "brain", "cancelQueued", "client", "closeSession",
  "commitSessionTabs", "compactionResult", "compressingSessions", "compressSession",
  "config", "connected", "createTheme",
  "CustomLayout", "customLayouts", "customThemes", "cycleApprovalMode", "deleteMemory",
  "deletePersona", "deletePreset", "deleteProvider", "deleteSession", "deleteTheme",
  "draft", "dragSessionTabOver", "dropins", "EMPTY_DRAFT", "getPersona", "glass", "grantNotify",
  "interruptSubagent", "isBusy", "isCompressing", "layout", "LayoutId", "listDirs",
  "listSessions", "loadSkillBody", "memories", "messages", "models", "moveSessionTab",
  "newSession", "notify", "notifyPerm", "onboarded", "openSession", "openSessionIds",
  "PALETTE", "pendingApply", "personas", "plugins", "pluginThemes", "providers",
  "queued", "refreshConfig", "refreshMemory", "refreshPersonas", "refreshPlugins",
  "replaceMemories",
  "refreshPresets", "refreshProviders", "refreshSessionSkills", "refreshSessionToolsets",
  "refreshSkills", "refreshSubagents",
  "refreshTodo", "refreshToolsets", "reloadPlugins", "renameSession", "renameTheme",
  "replyAsk", "requestSubRunId", "resolveApproval", "saveConfig", "savePersona",
  "send", "session", "sessionDropins", "sessionSkills", "sessionToolsets", "sessions", "setActivePersona", "setApprovalMode", "setAvatar",
  "setBgfx", "setCustomLayout", "setCwd", "setDropinEnabled", "setGlass", "setLayout", "setModel",
  "setNotify", "setPersona", "setPluginEnabled", "setPluginSetting",
  "setProviderEnabled", "setRules", "setSessionDropinEnabled", "setSessionPreset", "setSessionSkill", "setSessionToolEnabled", "setSkill", "setTheme",
  "setToolEnabled", "setView", "shiftSessionTab", "skills", "spawnSubagent", "state",
  "status", "steerQueue", "stop", "subagentActivity", "subagentActivityUpdate", "subagents",
  "subRuns", "switchSession", "testNotify", "testProvider", "theme", "ThemePreset",
  "themes", "todos", "toggleYolo", "toolsets", "TurnDraft", "turns", "updateMemory",
  "updateThemeColors", "upsertPreset", "view", "ViewName", "XuBrainStore",
];

// 2. Exact set equality: nothing missing, nothing silently added.
const found = new Set<string>();
for (const names of declared.values()) for (const n of names) found.add(n);
assert.deepEqual(
  [...found].sort(),
  [...PINNED].sort(),
  "public surface drifted: " +
    JSON.stringify({
      missing: PINNED.filter((n) => !found.has(n)),
      added: [...found].filter((n) => !PINNED.includes(n)),
    }),
);

// 3. Each member resolves in exactly one store file (no dupes across mixins, no
//    member that vanished on the way into a mixin file) — and in the file the
//    map below names. The map is the D2 record of which concern owns what: a
//    member that drifts to another file is a deliberate edit here, not a
//    silent side effect of a copy-paste.
const HOMES: Record<string, string[]> = {
  "store.svelte.ts": [
    "BUILTIN_THEMES", "BuiltinViewName", "CustomLayout", "EMPTY_DRAFT", "LayoutId", "PALETTE",
    "ThemePreset", "TurnDraft", "ViewName", "XuBrainStore",
    "approvalModeCycle", "avatar", "brain", "cancelQueued",
    "closeSession", "commitSessionTabs", "compactionResult",
    "compressSession", "compressingSessions", "config", "connected",
    "cycleApprovalMode", "deleteSession", "draft", "dragSessionTabOver",
    "grantNotify", "isBusy", "isCompressing", "listDirs", "listSessions",
    "messages", "moveSessionTab", "newSession", "notify", "notifyPerm",
    "onboarded", "openSession", "openSessionIds", "queued", "refreshConfig",
    "renameSession", "saveConfig", "send",
    "session", "sessions", "setApprovalMode", "setAvatar", "setCwd",
    "setNotify", "setPersona", "setRules", "setView", "shiftSessionTab",
    "state", "status", "steerQueue", "stop", "switchSession", "testNotify", "toggleYolo",
    "turns", "view",
  ],
  "store/appearance.svelte.ts": [
    "activeCustomLayout", "allThemes", "applyNow", "bgfx", "createTheme",
    "customLayouts", "customThemes", "deleteTheme", "glass", "layout", "pendingApply",
    "pluginThemes", "renameTheme", "setBgfx", "setCustomLayout", "setGlass", "setLayout", "setTheme",
    "theme", "themes", "updateThemeColors",
  ],
  "store/approvals.svelte.ts": ["approvals", "replyAsk", "resolveApproval"],
  "store/core.svelte.ts": ["client"],
  // The onEvent router (phase D2). Every member it declares is a `declare`,
  //  `private` or `protected` mixin seam, so it owns no public name — but the
  //  every-file-is-a-key check below still wants it listed.
  "store/events.svelte.ts": [],
  "store/memory.svelte.ts": [
    "addMemory", "deleteMemory", "memories", "refreshMemory", "replaceMemories", "updateMemory",
  ],
  "store/personas.svelte.ts": [
    "activePersona", "deletePersona", "getPersona", "personas",
    "refreshPersonas", "savePersona", "setActivePersona",
  ],
  "store/plugins.svelte.ts": [
    "plugins", "refreshPlugins", "reloadPlugins", "setPluginEnabled",
    "setPluginSetting",
  ],
  "store/presets.svelte.ts": [
    "deletePreset", "refreshPresets", "setSessionPreset", "upsertPreset",
  ],
  "store/providers.svelte.ts": [
    "addProvider", "deleteProvider", "models", "providers",
    "refreshProviders", "setModel", "setProviderEnabled", "testProvider",
  ],
  "store/skills.svelte.ts": [
    "loadSkillBody", "refreshSessionSkills", "refreshSkills", "sessionSkills",
    "setSessionSkill", "setSkill", "skills",
  ],
  "store/subagents.svelte.ts": [
    "interruptSubagent", "refreshSubagents", "refreshTodo", "requestSubRunId",
    "spawnSubagent", "subRuns", "subagentActivity", "subagentActivityUpdate",
    "subagents", "todos",
  ],
  "store/tools.svelte.ts": [
    "dropins", "refreshSessionToolsets", "refreshToolsets", "sessionDropins",
    "sessionToolsets", "setDropinEnabled", "setSessionDropinEnabled",
    "setSessionToolEnabled", "setToolEnabled", "toolsets",
  ],
};

/** `src/lib`-relative key for a store file, the shape HOMES is written in. */
const relKey = (f: string) => f.slice(f.indexOf(join("src", "lib")) + join("src", "lib").length + 1);

// Every pinned name is mapped, and every mapped name is pinned.
const mapped = Object.values(HOMES).flat();
assert.deepEqual(
  [...mapped].sort(),
  [...PINNED].sort(),
  "HOMES and PINNED disagree: " +
    JSON.stringify({
      unmapped: PINNED.filter((n) => !mapped.includes(n)),
      unpinned: mapped.filter((n) => !PINNED.includes(n)),
    }),
);
// Every store file on disk is a key (a new mixin must declare its members).
assert.deepEqual(
  storeFiles.map(relKey).sort(),
  Object.keys(HOMES).sort(),
  "HOMES must name every file in src/lib/store plus the entry",
);

for (const name of PINNED) {
  const home = storeFiles.filter((f) => declared.get(f)!.has(name));
  assert.equal(home.length, 1, `${name} resolves in ${home.length} files: ${home.join(", ")}`);
  assert.ok(
    HOMES[relKey(home[0])].includes(name),
    `${name} now lives in ${relKey(home[0])}; HOMES says ${
      Object.keys(HOMES).find((k) => HOMES[k].includes(name))
    }`,
  );
}

// 4. The entry's re-export contract lines stay intact (other modules import
//    these names from "./store", not from their new homes).
const entrySrc = readFileSync(entry, "utf8");
assert.match(entrySrc, /export type \{ CustomLayout, LayoutId, ThemePreset \} from "\.\/types";/);
assert.match(entrySrc, /export \{ BUILTIN_THEMES, PALETTE \} from "\.\/layouts\/registry";/);
// EMPTY_DRAFT's definition moved to the store/shared leaf (it broke an import
// cycle with store/events); the entry must still re-export the name.
assert.match(entrySrc, /export \{ EMPTY_DRAFT \} from "\.\/store\/shared";/);
assert.match(entrySrc, /export const brain = new XuBrainStore\(\);/);

// 5. The composition itself: the entry composes every mixin in store/ (a file
//    added there but never wired in would otherwise pass silently), and the
//    seams StoreCore declares are implemented on the composed class.
for (const f of storeFiles.slice(1)) {
  const base = f.split("/").pop()!.replace(".svelte.ts", "");
  if (base === "core") continue;
  const factory = new RegExp(`export function (\\w+)`).exec(readFileSync(f, "utf8"))?.[1];
  assert.ok(factory, `${base}.svelte.ts must export a mixin factory`);
  assert.ok(
    entrySrc.includes(`import { ${factory} } from "./store/${base}.svelte"`),
    `${factory} must be imported by store.svelte.ts`,
  );
  assert.match(entrySrc, new RegExp(`extends[\\s\\S]{0,400}\\b${factory}\\(`), `${factory} must be in the chain`);
}
// 6. Every seam StoreCore declares is implemented by exactly one concern file.
//    The stubs in core throw, so an unimplemented seam is a runtime crash the
//    moment the concern that needs it runs — and two implementations mean the
//    composition order silently decides which one wins.
const coreSrc = readFileSync(join(storeDir, "core.svelte.ts"), "utf8");
const seams = [...coreSrc.matchAll(/^  protected (?:get )?(\w+)/gm)].map((m) => m[1]);
assert.ok(seams.length > 0, "core.svelte.ts declares no seams — the regex stopped matching");
for (const seam of seams) {
  // a real declaration: line-initial, modifiers only — never a `this.x()` call
  const decl = new RegExp(
    `^\\s*(?:protected\\s+|public\\s+|override\\s+|static\\s+)*(?:async\\s+)?(?:get\\s+)?${seam}\\s*\\(`,
    "m",
  );
  const impls = storeFiles.filter(
    (f) => f !== join(storeDir, "core.svelte.ts") && decl.test(readFileSync(f, "utf8")),
  );
  assert.equal(
    impls.length,
    1,
    `StoreCore declares the ${seam} seam; exactly one concern file must implement it, saw ${impls.length}: ${impls.join(", ")}`,
  );
}

console.log(`store-surface: ${PINNED.length} names pinned across ${storeFiles.length} files`);
