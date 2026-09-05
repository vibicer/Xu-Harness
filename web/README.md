# Xu webui

Svelte 5 shell for Xu. Talks to the brain daemon over WebSocket JSON-RPC;
the brain serves the built `dist/`. Owns: chat surfaces, config panes,
layouts, color schemes.

## Run

```bash
npm install
npm run dev                  # vite dev server on 127.0.0.1:1421, brain on :9876
npm run build                # production bundle into dist/
npm test                     # structure checks via node --test
npx svelte-check --tsconfig ./tsconfig.json
```

From the repo root, `bash xu test` runs brain pytest + svelte-check + these
checks together.

## Package

```
src/
  main.ts               # svelte mount
  App.svelte            # resolves brain.layout; lazy-loads that one layout
  lib/
    store.svelte.ts     # composition root; mixin chain; exports `brain`
    store/              # one mixin per concern over StoreCoreBase:
                        # events, appearance, providers, tools, skills,
                        # plugins, presets, memory, personas, subagents,
                        # approvals — plus two non-mixin files:
      core.svelte.ts    # typed seams: rpc client + stubs the root overrides
      shared.ts         # plain-.ts leaf types; keeps the import graph acyclic
    rpc.ts              # WebSocket JSON-RPC client
    types.ts            # wire shapes mirrored from contract/methods.md
    components/         # views plus the chat and config parts
      ConfigView.svelte # config shell: tile grid, message banner, refresh()
      config/           # nine panes, one file each, + shared helpers
      chat/             # transcript, composer, approvals, sub-run modal
    layouts/            # self-registering layouts + shared CSS-var palette
      registry.ts       # globs ./*/layout.ts (eager) — the registration
      palette.ts        # editable CSS vars every scheme may set
      default/          # a layout is a folder: layout.ts + Layout.svelte
      simple/           # ditto, plus its own stylesheet
    theme.css           # global stylesheet
checks/                 # 13 node --test files; no browser, no component mounting
```

The chain reads innermost-first: memory → skills → tools → providers →
personas → subagents → presets → approvals → plugins → appearance → events,
with appearance wrapping plugins so `plugins` exists before anything derives
off it. Pane registration is a recipe, not a registry: new file under `config/`,
import + mount it in `ConfigView.svelte`, add its id to the `CfgModule` union
and a tile to `MODULES`. There is no `<style>` block in any config panel —
styling is global, keep it that way. A layout registers by dropping its
folder in; each folder owns its color schemes, flat-mapped with the folder's
id into `BUILTIN_THEMES`. The checks pin structure by reading source text
(store surface, config shell/panel split, layout registry, plugin layout and
theme wiring) and by calling the pure transcript/tab helpers directly.

## Contract

Methods and event payloads: `contract/methods.md` at the repo root. This
shell's client is `src/lib/rpc.ts`; `src/lib/types.ts` mirrors the wire
shapes. Keep both in sync with that file.
