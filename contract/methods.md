# Xu contract

Shared surface between the web shell and the Python brain: WebSocket transport
on `ws://127.0.0.1:9876` (override `XU_BRAIN_URL`), JSON-RPC 2.0 message framing.

## Transport

- One WebSocket connection from the shell to the brain daemon.
- Client → server: JSON-RPC 2.0 **requests** (with `id`) and **notifications** (no `id`).
- Server → client: request **results/errors** (`id` match), plus server-initiated
  **events** (notifications, no `id`), plus **streaming chunks** of an active turn
  (events carrying a `turn_id`).

## Framing

```jsonc
// request
{ "jsonrpc": "2.0", "id": 1, "method": "session.send", "params": { "session_id": "u7f2a1c", "text": "hi" } }
// result
{ "jsonrpc": "2.0", "id": 1, "result": { ... } }
// error  (JSON-RPC error object)
{ "jsonrpc": "2.0", "id": 1, "error": { "code": -32001, "message": "provider down", "data": { ... } } }
// server event (notification)
{ "jsonrpc": "2.0", "method": "event", "params": { "event": "turn.delta", "turn_id": "...", "delta": { ... } } }
```

## Client → server methods

| Method | Params | Result | Notes |
|---|---|---|---|
| `app.info` | — | `{ version, data_home, brain_pid }` | handshake |
| `app.status` | — | `{ brain: "ok", version, providers: [...], rss_mb }` | status squares |
| `app.doctor` | — | `{ checks: [{ name, ok, detail }] }` | diagnostics: data_home/providers |
| `logs.list` | `{ limit?, source?, level? }` | `{ logs: [{ ts, level, source, message, detail? }] }` | activity ring buffer, newest first; capped at 1000 entries in the brain |
| `provider.list` | — | `[{ id, type, base_url, models, key_set }]` | key never returned |
| `provider.upsert` | `{ id?, type, base_url, api_key?, models? }` | `{ id }` | keychain in shell/Rust |
| `provider.delete` | `{ id }` | `{}` | |
| `provider.set_enabled` | `{ id, enabled }` | `{ id, enabled }` | keep the provider configured but out of the model list |
| `provider.test` | `{ id }` | `{ ok, latency_ms, models?, error? }` | auto-fetch + connectivity |
| `keychain.get` | `{ id }` | `{ key }` | shell-resolved keychain (brain → shell) |
| `session.list` | — | `[{ id, title, cwd, updated_at, message_count }]` | newest first |
| `session.create` | `{ cwd?, model?, persona? }` | `{ session }` | |
| `session.delete` | `{ id }` | `{}` | pruning-friendly |
| `session.get` | `{ id }` | `{ session, messages, live? }` | `messages` = the **display transcript**: append-only, never shrunk by compaction, capped at the newest 1000 rows. `live` = in-flight turn snapshot `{ steps, reasoning }` when a turn is streaming |
| `session.title` | `{ id }` | `{ title }` | auto or explicit |
| `session.send` | `{ id, text, images? }` | `{ turn_id }` | starts agent turn; `images` = list of data: or http(s): URLs attached to the user message. Bytes are stored under `data_home/attachments/<session>/`; the pixels are sent in that turn's request only (routed to config `vision_model` when set) while persisted history keeps `[image attached: <path>]`, so nothing replays base64. A model that rejects the image gets one retry without it plus a `turn.notice`. The display transcript keeps the data URL for the thumbnail |
| `session.stop` | `{ id }` | `{}` | cooperative abort |
| `session.compress` | `{ id }` | `{ ok, async: true }` | compact the transcript now. Returns *before* the work finishes — the summarizer is a long streaming call, so it runs off the request path and reports via the `compaction.done` event |
| `session.queue.cancel` | `{ id, queued_id }` | `{ cancelled }` | drop a message queued mid-turn; emits `queue.cancelled` when it was still pending |
| `session.queue.steer` | `{ id, queued_id }` | `{ steered }` | interrupt the running turn and run a queued message now: promotes it to the front of the queue, then stops the turn so its chaining tail starts it as a fresh turn. `steered: false` when the id was already spliced into the live turn |
| `workspace.set_cwd` | `{ session_id, cwd }` | `{ cwd }` | CWD row control |
| `fs.list` | `{ path? }` | `{ path, dirs }` | child directory *names* for the CWD picker. Not confined to the session cwd — it is how the user reaches a new project — and never returns file contents |
| `state.get` | `{ session_id }` | `{ model, rules, context, cwd, preset }` | agent state panel; rules are per-session; `preset` = active orchestration tree or null |
| `state.set_model` | `{ session_id, model }` | `{ model }` | |
| `state.set_persona` | `{ session_id, persona? }` | `{ persona }` | per-session persona override (`null` = fall back to the global default) |
| `state.set_rules` | `{ session_id, rules }` | `{ rules }` | replace the ordered per-session rules list |
| `session.set_preset` | `{ session_id, preset_id? }` | `{ preset_id }` | bind a session to an orchestration preset (`null` = none) |
| `preset.list` | — | `{ presets: [{ id, name, node_count, created, updated }] }` | orchestration presets, newest first |
| `preset.get` | `{ id }` | `{ preset }` | full tree of an orchestration preset |
| `preset.upsert` | `{ id?, name, tree }` | `{ preset }` | `tree` = root `AgentNode`; create (no id) or overwrite. Each node may carry `fallbacks: [model id]` — ordered backups tried when its `model` fails |
| `preset.delete` | `{ id }` | `{}` | remove a preset |
| `persona.list` | — | `{ personas, active }` | name + id per persona; `active` = global default |
| `persona.get` | `{ id }` | `{ id, text }` | full system-prompt text |
| `persona.upsert` | `{ id, text }` | `{ id, personas }` | create or overwrite |
| `persona.delete` | `{ id }` | `{ personas }` | deleting the active persona clears the default |
| `persona.set_active` | `{ id }` | `{ active }` | set global default persona (`null` = SOUL.md fallback) |
| `memory.list` | — | `[{ id, text, badge }]` | `from-session` / `user-edited` |
| `memory.update` | `{ id, text }` | `{}` | approval-gated write; badge flips to `user-edited` |
| `memory.add` | `{ text }` | `{ entries }` | new `user-edited` entry from the panel |
| `memory.delete` | `{ id }` | `{}` | remove entry; emits `memory.updated` |
| `memory.replace_all` | `{ entries: string[] }` | `{ entries }` | whole-file editor — paragraphs map positionally; changed→update, removed→delete, new→append |
| `memory.mnemo` | — | `{ available, data_dir }` | built-in Mnemosyne status for the Config → Memory pane |
| `skill.list` | `{ session_id? }` | `[{ id, name, desc, state, ambient, keywords }]` | catalog; `session_id` returns that session's effective state (per-session overrides applied), omit for the global defaults |
| `skill.set` | `{ id, enabled, session_id? }` | `{ skills }` | toggle a skill and return the catalog; with `session_id` the toggle is a **per-session override** (other sessions keep following the global default), `-32602` unknown skill, `-32002` unknown session |
| `skill.load` | `{ id }` | `{ id, body }` | load a skill *into the agent* (progressive disclosure); use `skill.body` to read one for editing |
| `skill.body` | `{ id }` | `{ id, body }` | read the current skill body for editing without changing skill state |
| `skill.add` | `{ id, name, desc, body, keywords? }` | `{ skills }` | create a custom skill and return the catalog |
| `skill.update` | `{ id, name?, desc?, body?, keywords? }` | `{ skills }` | update a custom skill and return the catalog |
| `skill.remove` | `{ id }` | `{ skills }` | remove a custom skill and return the catalog |
| `tool.list` | `{ session_id? }` | `{ toolsets: [{ toolset, enabled, tools: [...] }], dropins: [{ name, toolset, approval, enabled }] }` | state panel toggles; `session_id` returns that session's effective enable state |
| `tool.set_enabled` | `{ toolset, enabled, session_id? }` | `{}` | with `session_id` a **per-session override** for the toolset; `-32002` unknown session |
| `tool.set_dropin_enabled` | `{ name, enabled, session_id? }` | `{}` | toggle one user-authored drop-in tool; with `session_id` a **per-session override**; `-32002` if the name is not an active drop-in (or unknown session)
| `todo.get` | `{ session_id }` | `{ phases: [...] }` | current plan for the session; `{ phases: [] }` when the agent has not written one |
| `config.get` | — | `{ context_length, compress_threshold, approval_mode, approval_modes, job_timeout, vision_model, model_fallbacks }` | `approval_modes` = custom modes `{ name: { auto: [tool pat], prompt: [tool pat] } }`; `model_fallbacks` = global ordered backup models |
| `config.set` | `{ key, value }` | `{}` | `key: "approval_modes"` replaces the custom-mode map; `key: "approval_mode"` accepts `manual`, `yolo`, or a custom mode name; `key: "model_fallbacks"` takes an ordered array of model ids (deduped, blanks dropped) |
| `approval.resolve` | `{ request_id, approved, remember? }` | `{ resolved }` | settle inline approval card; `remember: true` with `approved: true` = stop prompting that tool for the rest of the session (RISKY only — the ALWAYS-gate still re-prompts) |
| `reply.resolve` | `{ request_id, answer }` | `{ resolved }` | settle `ask` clarification |
| `git.detail` | `{ session_id? }` | `{ repo, cwd, branch?, files: [{ state, path }], commits: [{ sha, subject, ts, author }] }` | git panel. Best-effort: a non-repo or missing `git` yields `{ repo: false }`. Files capped at 50, commits at 10 |
| `subagent.send` | `{ prompt, session_id? }` | `{ id }` | spawn a managed background subagent |
| `subagent.list` | — | `{ subagents: [...] }` | managed subagents + status |
| `subagent.message` | `{ id, message }` | `{ accepted }` | follow-up prompt to a subagent |
| `subagent.interrupt` | `{ id }` | `{ ok }` | stop a running subagent |
| `subagent.activity` | `{ run_id? , session_id? }` | `{ runs: [{ id, child, prompt, status, started, finished, result, steps, reasoning }] }` | what a delegated sub-agent did/is doing — one run by `run_id` (from the `delegate` tool chip) or all runs of a session |
| `slots.list` | — | `{ slots: { <slot>: [names] } }` | which plugins fill which slot; introspection only |
| `layouts.list` | — | `{ layouts, panels, custom_layouts, custom_themes, active }` | plugin-registered layouts plus agent-authored definitions from `data_home/layouts/*/layout.json` |
| `plugin.list` | — | `{ plugins: [{ name, version, description, provides, requires, unmet, enabled, settings, themes, ui? }] }` | Config → Plugins. `settings` is the manifest's declared schema with each key's live `value`; `themes` are the colour schemes it contributes (`{id, name, layout?, colors{}, css?}`), offered in Config → Themes while enabled; `ui` is the frontend contribution `{module, element, mount, label?, icon?, assets?}` — built-in mounts are `statusbar` (chip), `config` (Config pane, tab from `label`/`icon`), `view` (nav entry + full view, entry from `label`/`icon`), `layout` (replaces the shell); any other lowercase-dashed mount loads with a warning and renders only in a layout that offers it; `assets` are extra files served under `/plugins/<name>/<file>` while enabled; `unmet` names required slots nothing currently fills |
| `plugin.enable` | `{ name }` | `{ ok, name }` | loads the plugin now; `-32002` if unknown |
| `plugin.disable` | `{ name }` | `{ ok, name }` | disposes every registration it made |
| `plugin.set_setting` | `{ name, key, value }` | `{ ok, name, key, value, plugin }` | writes one **declared** setting, coerced to its declared type. `-32002` unknown plugin, `-32602` undeclared key or unfittable value |
| `plugin.reload` | `{}` | `{ loaded, plugins }` | re-scan `data_home/plugins`. Fail-soft: a bad plugin is logged and skipped, the brain keeps running |

### Plugin-provided methods

Not Core: served by a plugin filling the `rpc` slot, so they are absent when the
plugin is disabled. The shell must tolerate `-32601 method not found`.

The `mcp` plugin backs Config → MCP. Every method is a thin skin over one hub,
so the panel and the agent's `mcp` tool always see the same state.

| Method | Params | Result | Notes |
|---|---|---|---|
| `mcp.list` | `{}` | `{ servers: [Server], toolset, config, config_error }` | `mcp`: re-reads `data_home/mcp.json` on every call, so a hand-edit needs no reload |
| `mcp.add` | `{ name, command, args?, env?, cwd?, timeout?, enabled?, connect? }` | `Server` | writes `mcp.json` and connects. `args` may be one shell-ish line. `-32602` bad name, empty command, duplicate, or non-stdio transport |
| `mcp.remove` | `{ name }` | `{ ok }` | disconnects, retires its tools, rewrites `mcp.json` |
| `mcp.connect` | `{ name }` | `Server` | starts the child process; its tools appear for the next turn. A failed connect resolves with `error` set, it does not raise |
| `mcp.disconnect` | `{ name }` | `Server` | stops the child process and retires its tools |
| `mcp.set` | `{ name, enabled? \| trusted? }` | `Server` | `enabled` = autostart (off also stops it now); `trusted` = its tools skip approval, and re-registers a live server |

`Server` = `{ name, command, args, cwd, env_keys, transport, enabled, configured,
state: on|off|error, trusted, error, tools, server }`. `env_keys` is keys only —
a value is either a literal token or a `${VAR}` the brain expands, and neither
belongs in a UI payload.


## Server → client events

| Event | Payload | Meaning |
|---|---|---|
| `turn.started` | `{ turn_id, session_id, model }` | thinking row begins |
| `turn.delta` | `{ turn_id, session_id, delta }` | streamed text chunk |
| `turn.reasoning` | `{ turn_id, session_id, delta }` | streamed thinking |
| `turn.tool` | `{ turn_id, session_id, tool, args, status, elapsed?, output?, image?, image_alt? }` | tool chip update (`image` = data URL from `show_image`, rendered under the chip) |
| `turn.notice` | `{ turn_id, session_id, text, detail? }` | transient status (e.g. retry) |
| `turn.queue` | `{ turn_id, session_id, queued_id, text }` | user message queued above the composer mid-turn (rendered as a "queued" bubble) |
| `turn.dequeue` | `{ turn_id, session_id, messages: [{ queued_id, text, images? }] }` | queued messages spliced into the live turn at a tool round (shell moves them from the queued tray into the timeline) |
| `queue.cancelled` | `{ session_id, queued_id }` | a queued message was dropped before it could be consumed (e.g. cancelled in another tab) |
| `turn.approval` | `{ turn_id, session_id?, request_id, tool, args, reason, level }` | inline approval card; `level` = `risky` \| `always` — the shell hides "Always" on `always` |
| `turn.approval_resolved` | `{ request_id, approved }` | card settles |
| `turn.finished` | `{ turn_id, session_id, stop_reason, usage? }` | thinking row ends |
| `memory.captured` | `{ session_id, turn_id, content, importance, kind }` | accepted memory candidate captured from a completed root turn |
| `turn.failed` | `{ turn_id, session_id, error }` | visible error |
| `context.updated` | `{ session_id, usage_pct, compress: bool, tokens, calibrated: bool, pressure? }` | CONTEXT meter — `usage_pct` is anchored to the provider's real `prompt_tokens` once sampled, heuristic until then |
| `state.updated` | `{ session_id, model?, cwd? }` | agent state panel |
| `memory.updated` | `{ entries }` | panel refresh |
| `status.updated` | `{ brain, providers }` | status squares |
| `session.updated` | `{ session_id }` | title/count drift |
| `compaction.started` | `{ session_id }` | transcript compaction began |
| `compaction.done` | `{ session_id, ok, before, after, dropped, error? }` | compaction settled; token counts before/after and how many messages were folded away |
| `subagent.activity` | `{ session_id, run }` | one full sub-agent run record (`{ id, child, prompt, status, started, finished, result, steps, reasoning }`); `session_id` is the *parent* session |
| `subagent.delta` | `{ session_id, run_id, kind, delta, status }` | token-level append for a streaming sub-agent. `kind` is `text` or `reasoning`; the delta is appended to the run's **last existing step**, so no step count changes |
| `todo.updated` | `{ session_id }` | the agent rewrote its plan; re-fetch with `todo.get` |

## Errors

JSON-RPC codes: `-32700` parse, `-32600` invalid request, `-32601` method
unknown, `-32602` invalid params, `-32603` internal. App codes `-32000..-32099`:
`-32001` provider error, `-32002` session not found, `-32003` approval
required, `-32004` turn in progress, `-32005` config invalid.

## Versioning

This file is the source of truth. The shell mirrors method names across
`web/src/lib/store.svelte.ts` and its concern mixins in
`web/src/lib/store/`; the brain registers them per concern in
`brain/xu_brain/features/*/rpc.py` and `brain/xu_brain/plugins/rpc.py`, with
`brain/xu_brain/core/runtime.py` (`_register_methods`) keeping the rest. All
of them move together.

`brain/tests/test_contract_doc.py` fails when a registered method has no row
here, so the drift cannot come back silently. Run it with
`python -m pytest tests/test_contract_doc.py`.
