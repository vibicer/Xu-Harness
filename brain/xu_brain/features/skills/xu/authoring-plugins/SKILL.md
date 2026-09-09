---
name: Authoring Xu Plugins
description: Write an Xu plugin package — manifest fields, slots, settings, and an optional frontend.
keywords: [plugin, manifest, activate, slot, hook, rpc, hot-reload, ui.js, custom element]
match_limit: 5
---

# Authoring Xu Plugins
A plugin is a directory under `data_home/plugins/<name>/`. Write its files with
the ordinary `write`/`edit` tools. There is no authoring or reload tool: the
brain re-scans on every boot, and the Config → Plugins panel calls
`plugin.reload` / `plugin.enable` from the shell. After writing the files, tell
the user to hit **Reload** in Config → Plugins (or restart the brain), then
enable the plugin there.

## Minimum package

Two files are required.

`manifest.json`:
```json
{
  "name": "my-plugin",
  "version": "1.0.0",
  "description": "One line, shown in Config → Plugins.",
  "provides": ["hook"]
}
```

`activate.py`:
```python
def activate(ctx):
    ctx.on("session.start", lambda **kw: ctx.log("hello"))
```

`activate(ctx)` is called once per load. Register everything there; the host
tracks each registration and disposes it on unload, so a reload never stacks a
second copy of a hook.

## Manifest fields

| Field | Rule |
|---|---|
| `name` | Required. Must match the directory name and be a safe package name (`-`/`.` allowed). |
| `version` | String, e.g. `"1.0.0"`. |
| `description` | One line; surfaced in the frontend plugin list. |
| `provides` | Slots this plugin fills. Unknown values warn but do not fail. |
| `requires` | Slots that must exist first. |
| `settings` | List of `{key, type, default, label?}`. |
| `ui` | Optional frontend: `{module, element, mount, label?, icon?, assets?}`. |

Slots: `provider, tool, rpc, hook, memory, layout, panel, policy, skill,
persona, theme`.

Setting `type` must be one of `string, boolean, integer, number, list` — not
`int`, not `bool`. A bad type rejects the whole manifest and the plugin is
skipped with a log line, so check the brain log if a plugin fails to appear.

## The ctx API

- `ctx.register_rpc(name, handler)` — a JSON-RPC method. Namespace it
  (`fs.read`, not `read`). Document it under "Plugin-provided methods" in
  `contract/methods.md`, and note that the shell must tolerate `-32601`.
- `ctx.on(event, handler)` — subscribe to a bus event.
- `ctx.transform(name, fn)` — a return-replacing hook.
- `ctx.settings()` — this plugin's settings dict.
- `ctx.app` — the live `App`: `.sessions`, `.registry`, and so on.
- `ctx.log(msg)` — namespaced logging.

Handlers may be sync or async. Raise `RpcError` from
`xu_brain.core.contract` to return a proper JSON-RPC error.

## Frontend (optional)

Add a `ui` block and ship the module beside the manifest:

```json
"ui": { "module": "ui.js", "element": "xu-my-plugin", "mount": "statusbar" }
```

- `module` must be a bare filename in the plugin dir. The brain serves it at
  `/plugins/<name>/<module>`, for **enabled** plugins only.
- `element` must contain a dash (custom-element rule) and be defined via
  `customElements.define`.
- `mount` is a known spot: `statusbar` (a chip in the layout's status strip),
- `mount` is open: any lowercase-dashed name loads. Built-in spots: `statusbar`
  (a chip in the layout's status strip), `config` (a pane in Config, opened by
  a tab the shell renders for you), `view` (a nav entry + full view, entry
  rendered from `ui.label`/`ui.icon`), `layout` (the whole shell). Any other
  mount loads with a warning and renders only in a layout that offers a slot
  with that name — a layout plugin renders other plugins' elements itself,
  which is how it defines its own regions.
  to the plugin name, and the icon must name one the shell ships (`icons.ts`) or
  it falls back. The shell owns the `.cfg-pane` wrapper and shows/hides it per
  tab, so a config pane renders bare content and never asks which tab is open —
  reuse `.cfg-sec` / `.cfg-row` / `.k-btn` / `.chip-tag` / `.toggle` and it
  matches every theme for free.

Plain JS only — no Svelte, no build step. The shell sets `node.brain = brain`
on the element, so read state and call RPCs from there. Prefer **light DOM**:
shadow DOM would wall the element off from the shell's global classes, and that
inheritance is what themes it per layout. Prefix your own classes.

## Security

Plugin Python runs unsandboxed and plugin JS runs in the shell's origin. The
brain's WebSocket is unauthenticated. If a plugin touches the filesystem,
resolve every path and require it inside the session `cwd` — that check is the
only barrier against absolute paths, `..`, and escaping symlinks. Cap read and
write sizes. Never make a delete recursive.

## Getting it right

If the plugin does not appear, it was skipped, not crashed: the host is
fail-soft and logs the reason. Read the brain log before touching the code.

Leave a runnable `_check.py` in the plugin dir that exercises the RPCs and
handlers directly, and say how to run it. A plugin that only works inside the
shell is a plugin nobody can debug.

