# Xu brain

Python daemon for Xu. Serves the Svelte webui (HTTP) and the RPC contract
(WebSocket). Owns: agent loop, tools, memory, skills, approvals, plugins.

## Run

```bash
uv sync
uv run xu-brain                  # default 127.0.0.1:9876
XU_BRAIN_URL=ws://127.0.0.1:9999 uv run xu-brain
```

## Package

```
xu_brain/
  __init__.py           # package identity
  __main__.py           # entrypoint
  core/
    runtime.py          # boot, WebSocket serving, and JSON-RPC runtime
    contract.py         # frozen JSON-RPC wire contract
    governance.py       # approval gate and governance enforcement
    host.py              # plugin loader and context factory
    bus.py               # in-process event and transform bus
    notify.py            # server-to-shell notifications
    config.py            # data home and app settings
    activity.py          # bounded process-wide activity log
    web.py               # static SPA and plugin UI serving
  api/
    context.py           # plugin activation context
    events.py            # plugin hook event and transform names
    manifest.py          # plugin manifest schema and validation
  features/
    agent/               # turn loop split by concern: loop, prompt, context,
                         # compaction, delegation, live, checkpoint, plus
                         # providers, scheduler, and orchestrator
    session/             # session registry and SQLite store
    tools/               # toolsets, shared protocol, registry, and schema
    memory/              # single-file memory store
    skills/              # progressive-disclosure skills engine
    presets/             # user-authored orchestration presets
  plugins/               # user plugin loading and event-bus wiring
```

## Contract

Methods and event payloads: `contract/methods.md` at the repo root. The webui
(`web/`) and this package share that file; keep them in sync.
