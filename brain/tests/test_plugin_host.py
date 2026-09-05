"""Plugin host + unified event bus tests.

Covers: a manifest-based plugin activates and fills a slot (introspectable via
the slot store); a malformed plugin fails soft (logged + skipped, boot keeps
going); and the shared bus serves both ``on`` (fire-and-forget) and ``transform``
(return-replacing) kinds.
"""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from xu_brain.core.bus import HookBus
from xu_brain.core.host import PluginHost


class _SlotStore:
    """Minimal stand-in for App.slots so activation can be tested in isolation."""

    def __init__(self):
        self._slots = {}

    def register(self, kind, plugin, value):
        entries = self._slots.setdefault(kind, [])
        entry = {"kind": kind, "plugin": plugin, "value": value}
        entries.append(entry)

        def dispose():
            if entry in entries:
                entries.remove(entry)

        return dispose

    def list(self):
        return self._slots


class _App:
    """Stand-in App exposing the imperative rpc registration plugins use."""

    def __init__(self):
        self.slots = _SlotStore()
        self.methods = {}

    def register(self, name):  # decorator form, as the real App has
        def wrap(fn):
            self.methods[name] = fn
            return fn

        return wrap

    def register_handler(self, name, handler):
        previous = self.methods.get(name)
        self.methods[name] = handler

        def dispose():
            if self.methods.get(name) is not handler:
                return
            if previous is None:
                self.methods.pop(name, None)
            else:
                self.methods[name] = previous

        return dispose


def _write_plugin(root: Path, name: str, body: str, manifest: dict | None = None) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    manifest = manifest if manifest is not None else {
        "name": name, "version": "1.0.0", "provides": ["rpc"]
    }
    (pkg / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    (pkg / "activate.py").write_text(body, "utf-8")
    return pkg


def test_plugin_activates_and_fills_slot(tmp_path):
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()

    _write_plugin(
        tmp_path,
        "hello",
        'def activate(ctx):\n    ctx.register_rpc("hello.ping", lambda p: {"ok": True})\n',
    )
    n = host.load_dir(tmp_path, app)
    assert n == 1
    assert [a["name"] for a in host.list()] == ["hello"]
    # The rpc slot is live: the handler actually lands in the method table.
    assert "hello.ping" in app.methods
    assert app.methods["hello.ping"]({}) == {"ok": True}


def test_malformed_plugin_fails_soft(tmp_path):
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()

    # No manifest.json
    (tmp_path / "bare").mkdir()
    (tmp_path / "bare" / "activate.py").write_text("def activate(ctx): pass\n", "utf-8")
    # manifest but activate() raises
    _write_plugin(tmp_path, "boom", "def activate(ctx):\n    raise RuntimeError('nope')\n")
    # a valid one still loads
    _write_plugin(tmp_path, "good", "def activate(ctx): pass\n")

    n = host.load_dir(tmp_path, app)
    assert n == 1  # only "good" survived
    assert [a["name"] for a in host.list()] == ["good"]


def test_shared_bus_serves_on_and_transform():
    bus = HookBus()
    seen = []
    h = bus.on("turn.start", lambda p: seen.append("on") or None)
    t = bus.transform("before_llm", lambda msgs: [*msgs, "x"])

    async def run():
        await bus.emit("turn.start", {"id": 1})
        out = await bus.apply("before_llm", ["a"])
        return seen, out

    seen, out = asyncio.run(run())
    assert seen == ["on"]
    assert out == ["a", "x"]
    h.dispose()
    t.dispose()
    assert bus.subscribed("before_llm") == 0

def test_manifest_description_reaches_the_listing(tmp_path):
    """The description is what Config → Plugins renders, so it has to survive
    manifest → Manifest → host.list(). A missing one degrades to ""."""
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()

    _write_plugin(
        tmp_path, "described", "def activate(ctx):\n    pass\n",
        {"name": "described", "version": "1.0.0", "description": "  does a thing  "},
    )
    _write_plugin(
        tmp_path, "bare", "def activate(ctx):\n    pass\n",
        {"name": "bare", "version": "1.0.0"},
    )
    assert host.load_dir(tmp_path, app) == 2
    got = {a["name"]: a["description"] for a in host.list()}
    assert got == {"described": "does a thing", "bare": ""}

    # A non-string description is a validation error, so the plugin is skipped.
    _write_plugin(
        tmp_path, "broken", "def activate(ctx):\n    pass\n",
        {"name": "broken", "version": "1.0.0", "description": 42},
    )
    assert host.load_dir(tmp_path, app) == 2


def test_context_exposes_the_app_for_rpc_plugins(tmp_path):
    """An ``rpc``-slot plugin has to reach real subsystems (e.g. ``app.sessions``
    to resolve a session cwd), so ``ctx.app`` is part of the context surface."""
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()
    app.sessions = SimpleNamespace(get=lambda sid: SimpleNamespace(cwd="/tmp/x"))
    _write_plugin(
        tmp_path,
        "peek",
        "def activate(ctx):\n"
        "    async def cwd(params):\n"
        "        return {'cwd': ctx.app.sessions.get(params['id']).cwd}\n"
        "    ctx.register_rpc('peek.cwd', cwd)\n",
    )
    assert host.load_dir(tmp_path, app) == 1
    got = asyncio.run(app.methods["peek.cwd"]({"id": "s1"}))
    assert got == {"cwd": "/tmp/x"}



def test_enable_disable_round_trip(tmp_path):
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()

    _write_plugin(tmp_path, "mymod", "def activate(ctx):\n    return None\n")

    assert host.load_dir(tmp_path, app) == 1
    assert host.disable("mymod")
    assert [a["enabled"] for a in host.list()] == [False]
    assert host.enable("mymod")
    assert [a["enabled"] for a in host.list()] == [True]

    # disabled plugins are listed but not activated on reload
    host.disable("mymod")
    assert host.load_dir(tmp_path, app) == 1
    assert [a["enabled"] for a in host.list()] == [False]


def test_plugin_contributes_layout_and_panel_slots(tmp_path):
    """Phase 4 gate: an plugin registers a layout/panel and it shows up in
    slots.list — a new layout without a web rebuild."""
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()
    store = app.slots

    _write_plugin(
        tmp_path,
        "layoutful",
        (
            "def activate(ctx):\n"
            "    ctx.register_layout({'name': 'fancy', 'builtin': False})\n"
            "    ctx.register_panel({'name': 'analytics', 'builtin': False})\n"
        ),
    )
    host.load_dir(tmp_path, app)
    listed = store.list()
    layouts = [e["value"] for e in listed.get("layout", [])]
    panels = [e["value"] for e in listed.get("panel", [])]
    assert any("fancy" in str(v) for v in layouts)
    assert any("analytics" in str(v) for v in panels)


def test_plugin_state_file_does_not_collide_with_pluginbus(tmp_path):
    """Regression: PluginHost and the legacy flat-file PluginBus both scan
    ``data_home/plugins``. They must not share one ``enabled.json`` — the
    plugin bus keys it by module *stem*, the host by plugin *name*, so a
    shared file made each loader silently disable the other's entries."""
    from xu_brain.plugins import PluginBus

    bus = HookBus()
    host = PluginHost(bus)
    app = _App()
    _write_plugin(tmp_path, "myaug", "def activate(ctx):\n    return None\n")

    host.load_dir(tmp_path, app)
    host.disable("myaug")

    # The host owns its own file, and leaves enabled.json to the plugin bus.
    assert (tmp_path / "plugins.enabled.json").is_file()
    assert json.loads((tmp_path / "plugins.enabled.json").read_text()) == []
    assert not (tmp_path / "enabled.json").is_file()

    # A flat plugin toggled off writes enabled.json without clobbering plugins.
    (tmp_path / "flatplug.py").write_text("def before_llm(m):\n    return m\n", "utf-8")
    pb = PluginBus(tmp_path, bus=HookBus())
    pb.load_dir(tmp_path)
    pb.set_enabled([])  # disable every flat plugin

    assert json.loads((tmp_path / "plugins.enabled.json").read_text()) == []
    assert "myaug" not in json.loads((tmp_path / "enabled.json").read_text())


# ---- lifecycle: reload must replace, not stack ------------------------ #


_COUNTER = (
    "calls = []\n"
    "\n"
    "def activate(ctx):\n"
    "    ctx.transform('on_message_out', lambda text: text + '!')\n"
    "    ctx.register_rpc('count.ping', lambda p: {'ok': True})\n"
    "    ctx.register_layout({'name': 'c'})\n"
)


def test_reload_replaces_registrations_instead_of_stacking(tmp_path):
    """Regression: load_dir used to clear its record without disposing the
    handles, so every reload added a second copy of every hook."""
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()
    _write_plugin(tmp_path, "counter", _COUNTER)

    host.load_dir(tmp_path, app)
    assert asyncio.run(bus.apply("on_message_out", "hi")) == "hi!"

    for _ in range(3):
        host.load_dir(tmp_path, app)

    # One transform, one slot entry, one rpc handler — not four.
    assert bus.subscribed("on_message_out") == 1
    assert asyncio.run(bus.apply("on_message_out", "hi")) == "hi!"
    assert len(app.slots.list()["layout"]) == 1
    assert list(app.methods) == ["count.ping"]


def test_disable_takes_effect_immediately(tmp_path):
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()
    _write_plugin(tmp_path, "counter", _COUNTER)
    host.load_dir(tmp_path, app)

    host.disable("counter")

    assert bus.subscribed("on_message_out") == 0
    assert asyncio.run(bus.apply("on_message_out", "hi")) == "hi"
    assert app.methods == {}


def test_rpc_dispose_restores_the_shadowed_core_method(tmp_path):
    """A plugin may override a Core method, but unloading must put it back."""
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()
    app.methods["app.doctor"] = lambda p: {"core": True}
    _write_plugin(
        tmp_path,
        "shadow",
        "def activate(ctx):\n    ctx.register_rpc('app.doctor', lambda p: {'core': False})\n",
    )

    host.load_dir(tmp_path, app)
    assert app.methods["app.doctor"]({}) == {"core": False}

    host.deactivate_all()
    assert app.methods["app.doctor"]({}) == {"core": True}


def test_async_activate_and_deactivate_hook(tmp_path):
    """``activate`` may be ``async def`` (I/O setup), and ``deactivate`` is
    called so a plugin can release what the host can't see."""
    marker = tmp_path / "trace.txt"
    _write_plugin(
        tmp_path,
        "asyncplug",
        (
            "import pathlib\n"
            f"TRACE = pathlib.Path({str(marker)!r})\n"
            "\n"
            "async def activate(ctx):\n"
            "    TRACE.write_text('activated')\n"
            "\n"
            "def deactivate(ctx):\n"
            "    TRACE.write_text('deactivated')\n"
        ),
    )

    async def scenario():
        bus = HookBus()
        host = PluginHost(bus)
        app = _App()
        host.load_dir(tmp_path, app)
        await asyncio.sleep(0)  # let the scheduled coroutine run
        assert marker.read_text() == "activated"
        await host.shutdown()
        return marker.read_text()

    assert asyncio.run(scenario()) == "deactivated"


def test_activate_raising_midway_undoes_partial_registrations(tmp_path):
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()
    _write_plugin(
        tmp_path,
        "halfdead",
        (
            "def activate(ctx):\n"
            "    ctx.transform('on_message_out', lambda t: t + '?')\n"
            "    raise RuntimeError('boom')\n"
        ),
    )

    assert host.load_dir(tmp_path, app) == 0
    assert bus.subscribed("on_message_out") == 0


def test_reload_uses_the_app_it_booted_with(tmp_path):
    """The reload path passes nothing, so the host must remember the App it
    booted with — a substitute would drop every slot registration."""
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()
    _write_plugin(tmp_path, "counter", _COUNTER)

    host.load_dir(tmp_path, app)
    assert host.reload() == 1
    assert list(app.methods) == ["count.ping"]
    assert len(app.slots.list()["layout"]) == 1
    assert bus.subscribed("on_message_out") == 1


def test_reload_rereads_source_edited_within_the_same_second(tmp_path):
    """A plugin rewritten to the *same length* in the same second must reload.

    ``spec.loader.exec_module`` trusts __pycache__, whose staleness check is
    (mtime, size) at one-second resolution — so an agent iterating on a plugin
    got the previous bytecode and the reload reported success anyway.
    """
    bus = HookBus()
    host = PluginHost(bus)
    app = _App()
    _write_plugin(
        tmp_path, "swap",
        'def activate(ctx):\n    ctx.register_rpc("swap.aaa", lambda p: 1)\n',
    )
    host.load_dir(tmp_path, app)
    assert list(app.methods) == ["swap.aaa"]

    # Same byte count, no sleep: only re-reading the source can catch this.
    (tmp_path / "swap" / "activate.py").write_text(
        'def activate(ctx):\n    ctx.register_rpc("swap.bbb", lambda p: 1)\n', "utf-8"
    )
    assert host.reload() == 1
    assert list(app.methods) == ["swap.bbb"]
