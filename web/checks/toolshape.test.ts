// Runnable check for tool-chip shaping: `node checks/toolshape.test.ts`
import assert from "node:assert/strict";
import { toolShape, isReadOnly, argValue, headline, target, fileTab, splitExit, lineCount } from "../src/lib/toolshape.ts";

// 1. Shape classification
assert.equal(toolShape("bash"), "terminal");
assert.equal(toolShape("eval"), "terminal");
assert.equal(toolShape("write"), "note");
assert.equal(toolShape("edit"), "note");
assert.equal(toolShape("read"), "note");
assert.equal(toolShape("grep"), "search");
assert.equal(toolShape("web_search"), "search");
assert.equal(toolShape("delegate"), "plain", "unknown tool keeps the plain chip");
assert.equal(toolShape(undefined), "plain");

// 2. Read-only tint
assert.equal(isReadOnly("read"), true);
assert.equal(isReadOnly("write"), false);

// 3. Arg extraction — values may contain spaces, keys terminate them
assert.equal(argValue("command=ls -la /tmp", "command"), "ls -la /tmp");
assert.equal(argValue("command=git log --oneline path=/x/y", "command"), "git log --oneline");
assert.equal(argValue("command=git log path=/x/y", "path"), "/x/y");
assert.equal(argValue("path=/a/b", "command"), "");
assert.equal(argValue(undefined, "path"), "");

// 4. Headline picks the most specific key present
assert.equal(headline("bash", "command=pwd"), "pwd");
assert.equal(headline("grep", "pattern=TODO path=src"), "TODO");
assert.equal(headline("odd", "foo=bar  baz=1"), "foo=bar baz=1", "no known key → whole args");
// the brain rewrites an edit's `patch` into `path=` (registry.summarize_args),
// so a note chip always has a path to title itself with
assert.equal(headline("edit", "path=/a/b.ts"), "/a/b.ts");

// 5. File tab split, including line selectors and bare names
assert.deepEqual(fileTab("/home/v/src/lib/store.ts"), { name: "store.ts", dir: "/home/v/src/lib" });
assert.deepEqual(fileTab("README.md"), { name: "README.md", dir: "" });
assert.deepEqual(fileTab("src/a.ts:10-20"), { name: "a.ts", dir: "src" });

// 5b. Closed-chip target: short, one line, never an arg dump
assert.equal(target("read", "path=/home/v/src/lib/store.ts"), "lib/store.ts", "note keeps one parent dir");
assert.equal(target("write", "path=README.md"), "README.md", "bare name has no parent");
assert.equal(target("edit", "path=/a/b.ts:10-20"), "a/b.ts", "line selector stripped");
assert.equal(target("bash", "command=npm run build"), "npm run build", "a command is its own target");
assert.equal(target("grep", "pattern=TODO path=src"), "TODO");
assert.equal(target("odd", "foo=bar baz=1"), "", "arg dump is not a target");
assert.equal(target("bash", undefined), "");
// 5c. delegate: a plain chip, named by its squad member
assert.equal(target("delegate", "label=researcher prompt=go find it"), "researcher");
assert.equal(target("delegate", "child=writer prompt=draft it"), "writer",
  "`child` is the tool's alias for `label`");
assert.equal(target("delegate", "prompt=just do it"), "", "unnamed delegate has no target");

// 6. Exit-status footer split off the terminal body
{
  const { body, exit } = splitExit("hello\nworld\n[exit 0 · 0.10s]");
  assert.equal(body, "hello\nworld");
  assert.equal(exit, "exit 0 · 0.10s");
}
assert.deepEqual(splitExit("no footer"), { body: "no footer", exit: "" });
assert.deepEqual(splitExit(null), { body: "", exit: "" });

// 7. Line count ignores blank lines
assert.equal(lineCount("a\n\nb\n"), 2);
assert.equal(lineCount(null), 0);

console.log("toolshape: all checks passed");
