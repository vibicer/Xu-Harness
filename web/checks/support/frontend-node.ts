/** In-memory frontend imports for Node checks: no Vite, browser, or build output. */
import { existsSync, readFileSync } from "node:fs";
import { registerHooks, stripTypeScriptTypes } from "node:module";
import { fileURLToPath } from "node:url";
import { compile, compileModule } from "svelte/compiler";

export function registerFrontendImports(): { deregister: () => void } {
  return registerHooks({
    resolve(specifier, context, nextResolve) {
      if (specifier.startsWith(".") && context.parentURL?.startsWith("file:")) {
        const url = new URL(specifier, context.parentURL);
        if (!existsSync(fileURLToPath(url))) {
          const typed = new URL(`${url.href}.ts`);
          if (existsSync(fileURLToPath(typed))) {
            return nextResolve(typed.href, context);
          }
        }
      }
      return nextResolve(specifier, context);
    },
    load(url, context, nextLoad) {
      if (url.endsWith(".svelte") || url.endsWith(".svelte.ts")) {
        const filename = fileURLToPath(url);
        const source = readFileSync(filename, "utf8");
        const result = url.endsWith(".svelte")
          ? compile(source, { filename, generate: "server", dev: false })
          : compileModule(stripTypeScriptTypes(source), {
              filename,
              generate: "server",
              dev: false,
            });
        return { format: "module", source: result.js.code, shortCircuit: true };
      }
      return nextLoad(url, context);
    },
  });
}
