"""Plugin host capabilities beyond loading: settings, policy wiring, requires.

Three gaps this pins, each of which used to fail *silently* — the shape of bug a
plugin author cannot debug from the outside:

1. ``manifest.settings`` was validated, parsed, then dropped on the floor. The
   host never surfaced it, so a plugin had to ship its own RPC just to read its
   own settings, and Config could not render a control for any of them.
2. ``ctx.register_policy`` filed the policy into the slot store and stopped.
   ``ApprovalManager`` — the thing that actually consults policies — never saw
   it. A ``policy`` plugin loaded clean, claimed to gate tools, and did nothing.
   On a safety surface, quiet is the worst failure mode there is.
3. ``manifest.requires`` was validated and ignored, so a plugin declaring a
   dependency loaded into a host with nothing providing it and failed later,
   somewhere else.

Run: ``python -m pytest tests/test_plugin_capabilities.py -q`` from ``brain/``.
"""
import json
from pathlib import Path

from xu_brain.api.manifest import validate_manifest
from xu_brain.core.governance import ApprovalLevel, ApprovalManager
from xu_brain.core.bus import HookBus
from xu_brain.core.host import PluginHost


class _SlotStore:
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


class _Config:
    """Stand-in for AppConfig: the two methods the host's settings path uses."""

    def __init__(self, values=None):
        self._values = dict(values or {})
        self.saves = 0

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        self._values[key] = value
        self.saves += 1

    def all(self):
        return dict(self._values)


class _App:
    def __init__(self, *, config=None, approvals=None):
        self.slots = _SlotStore()
        self.methods = {}
        self.config = config if config is not None else _Config()
        # Only present when a test wants the real gate; absent otherwise, which
        # is itself a case worth covering (the host must warn, not crash).
        if approvals is not None:
            self.approvals = approvals

    def register_handler(self, name, handler):
        self.methods[name] = handler
        return lambda: self.methods.pop(name, None)


def _write_plugin(root: Path, name: str, body: str, manifest: dict | None = None) -> Path:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    manifest = manifest if manifest is not None else {
        "name": name, "version": "1.0.0", "provides": ["rpc"]
    }
    (pkg / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    (pkg / "activate.py").write_text(body, "utf-8")
    return pkg


def _record(host, name):
    return next(r for r in host.list() if r["name"] == name)


# --------------------------------------------------------------------------- #
# 1. manifest settings reach the shell
# --------------------------------------------------------------------------- #

def test_declared_settings_are_listed_with_their_defaults(tmp_path):
    host = PluginHost(HookBus())
    app = _App()
    _write_plugin(
        tmp_path, "conf", "def activate(ctx): pass\n",
        manifest={
            "name": "conf", "version": "1.0.0", "provides": ["hook"],
            "settings": [
                {"key": "measure", "type": "integer", "default": 68, "label": "Measure"},
                {"key": "show", "type": "boolean", "default": True},
            ],
        },
    )
    host.load_dir(tmp_path, app)

    settings = {s["key"]: s for s in _record(host, "conf")["settings"]}
    assert set(settings) == {"measure", "show"}
    # `value` is what is in EFFECT, so an unset key reports its default rather
    # than None — otherwise the shell renders an empty control for a live value.
    assert settings["measure"]["value"] == 68
    assert settings["show"]["value"] is True
    assert settings["measure"]["label"] == "Measure"
    assert settings["measure"]["type"] == "integer"


def test_stored_value_wins_over_the_declared_default(tmp_path):
    host = PluginHost(HookBus())
    app = _App(config=_Config({"plugin.conf.measure": 92}))
    _write_plugin(
        tmp_path, "conf", "def activate(ctx): pass\n",
        manifest={
            "name": "conf", "version": "1.0.0",
            "settings": [{"key": "measure", "type": "integer", "default": 68}],
        },
    )
    host.load_dir(tmp_path, app)
    setting = _record(host, "conf")["settings"][0]
    assert setting["value"] == 92
    assert setting["default"] == 68  # the declaration is preserved alongside


def test_a_plugin_reads_back_what_the_host_stored(tmp_path):
    """ctx.settings() and the listed record must agree — they are the same value
    seen from the two sides, and a mismatch means the shell edits one thing while
    the plugin reads another."""
    host = PluginHost(HookBus())
    app = _App(config=_Config({"plugin.conf.measure": 40}))
    seen = {}
    _write_plugin(
        tmp_path, "conf",
        "def activate(ctx):\n    ctx.log('x')\n    globals()['SEEN'] = ctx.settings()\n",
        manifest={
            "name": "conf", "version": "1.0.0",
            "settings": [{"key": "measure", "type": "integer", "default": 68}],
        },
    )
    host.load_dir(tmp_path, app)
    ctx, module = host._live["conf"]  # noqa: SLF001 — asserting the two views agree
    seen = module.SEEN
    assert seen["measure"] == 40
    assert _record(host, "conf")["settings"][0]["value"] == 40


def test_no_declared_settings_is_an_empty_list_not_absent(tmp_path):
    """An empty list lets the shell skip the block without a null check; `None`
    would make every consumer guard the same field."""
    host = PluginHost(HookBus())
    _write_plugin(tmp_path, "plain", "def activate(ctx): pass\n")
    host.load_dir(tmp_path, _App())
    assert _record(host, "plain")["settings"] == []


def test_a_keyless_setting_rejects_the_whole_manifest(tmp_path):
    """Validation runs before any of the plugin's code, so a malformed settings
    block skips the plugin entirely rather than half-loading it."""
    host = PluginHost(HookBus())
    _write_plugin(
        tmp_path, "half", "def activate(ctx): pass\n",
        manifest={
            "name": "half", "version": "1.0.0",
            "settings": [{"type": "string"}, {"key": "real", "type": "string"}],
        },
    )
    assert host.load_dir(tmp_path, _App()) == 0
    assert host.list() == []


def test_record_builder_skips_a_keyless_entry(tmp_path):
    """The builder does not lean on validation having run. Called directly here
    because a manifest that reaches it always passed validation — this is the
    belt to that braces, and it is cheap."""
    from xu_brain.api.manifest import Manifest

    host = PluginHost(HookBus())
    host.load_dir(tmp_path, _App())  # gives the host an app + root
    manifest = Manifest(
        name="synth",
        settings=[{"type": "string"}, {"key": "real", "type": "string", "default": "v"}],
    )
    schema = host._settings_schema(manifest)  # noqa: SLF001
    assert [s["key"] for s in schema] == ["real"]
    assert schema[0]["value"] == "v"


def test_settings_are_listed_for_a_disabled_plugin_too(tmp_path):
    """You configure a plugin before turning it on. If the schema only appeared
    once enabled, the settings would pop into existence after the fact."""
    host = PluginHost(HookBus())
    app = _App()
    _write_plugin(
        tmp_path, "off", "def activate(ctx): pass\n",
        manifest={
            "name": "off", "version": "1.0.0",
            "settings": [{"key": "k", "type": "string", "default": "v"}],
        },
    )
    host.load_dir(tmp_path, app)
    host.disable("off")
    record = _record(host, "off")
    assert record["enabled"] is False
    assert record["settings"][0]["value"] == "v"


# --------------------------------------------------------------------------- #
# 2. the policy slot actually reaches the gate
# --------------------------------------------------------------------------- #

_POLICY_PLUGIN = """
class Strict:
    def is_always_gated(self, tool, args):
        return tool == "danger"

    def level_for(self, tool, args, declared):
        return None


def activate(ctx):
    ctx.register_policy(Strict())
"""


def test_registered_policy_gates_the_real_manager(tmp_path):
    gate = ApprovalManager("manual")
    host = PluginHost(HookBus())
    app = _App(approvals=gate)
    _write_plugin(
        tmp_path, "strict", _POLICY_PLUGIN,
        manifest={"name": "strict", "version": "1.0.0", "provides": ["policy"]},
    )
    host.load_dir(tmp_path, app)

    # The slot record still happens (introspection via slots.list)...
    assert [e["plugin"] for e in app.slots.list()["policy"]] == ["strict"]
    # ...but the point is the gate consulting it.
    assert gate.level_for("danger", {}, ApprovalLevel.NEVER) is ApprovalLevel.ALWAYS
    assert gate.level_for("harmless", {}, ApprovalLevel.NEVER) is ApprovalLevel.NEVER


def test_disabling_a_policy_plugin_stops_it_gating(tmp_path):
    """A disabled plugin's criteria must stop applying. Otherwise "off" still
    blocks your tools and nothing in the UI explains why."""
    gate = ApprovalManager("manual")
    host = PluginHost(HookBus())
    app = _App(approvals=gate)
    _write_plugin(
        tmp_path, "strict", _POLICY_PLUGIN,
        manifest={"name": "strict", "version": "1.0.0", "provides": ["policy"]},
    )
    host.load_dir(tmp_path, app)
    assert gate.level_for("danger", {}, ApprovalLevel.NEVER) is ApprovalLevel.ALWAYS

    host.disable("strict")
    assert gate.level_for("danger", {}, ApprovalLevel.NEVER) is ApprovalLevel.NEVER
    assert gate._policies == []  # noqa: SLF001 — the leak this test exists for


def test_reload_does_not_stack_a_second_copy_of_a_policy(tmp_path):
    """Every other registration kind is disposed on reload; a policy that was not
    would accumulate one copy per reload — invisible until something is gated
    twice, or a stale rule outlives its plugin."""
    gate = ApprovalManager("manual")
    host = PluginHost(HookBus())
    app = _App(approvals=gate)
    _write_plugin(
        tmp_path, "strict", _POLICY_PLUGIN,
        manifest={"name": "strict", "version": "1.0.0", "provides": ["policy"]},
    )
    host.load_dir(tmp_path, app)
    host.reload()
    host.reload()
    assert len(gate._policies) == 1  # noqa: SLF001


def test_policy_without_a_gate_warns_and_keeps_loading(tmp_path, caplog):
    """No gate is a host-integration bug, not a plugin bug — so the plugin must
    still load, and the log must say the policy is inert rather than implying it
    took effect."""
    host = PluginHost(HookBus())
    app = _App()  # deliberately no `approvals`
    _write_plugin(
        tmp_path, "strict", _POLICY_PLUGIN,
        manifest={"name": "strict", "version": "1.0.0", "provides": ["policy"]},
    )
    with caplog.at_level("WARNING"):
        assert host.load_dir(tmp_path, app) == 1
    assert "will NOT be consulted" in caplog.text


def test_a_policy_cannot_lower_the_frozen_gate(tmp_path):
    """Regression guard on the security boundary: a plugin returning NEVER for a
    destructive shell command must not be able to auto-run it."""
    gate = ApprovalManager("yolo")
    host = PluginHost(HookBus())
    app = _App(approvals=gate)
    _write_plugin(
        tmp_path, "loose",
        "class Loose:\n"
        "    def is_always_gated(self, tool, args):\n"
        "        return False\n"
        "    def level_for(self, tool, args, declared):\n"
        "        from xu_brain.core.governance import ApprovalLevel\n"
        "        return ApprovalLevel.NEVER\n"
        "\n"
        "def activate(ctx):\n"
        "    ctx.register_policy(Loose())\n",
        manifest={"name": "loose", "version": "1.0.0", "provides": ["policy"]},
    )
    host.load_dir(tmp_path, app)
    # mkfs is in the frozen destructive-shell set; `rm -rf` deliberately is not
    # (see _ALWAYS_DELETE_TOOLS), so testing with it would prove nothing.
    level = gate.level_for("bash", {"command": "mkfs.ext4 /dev/sda1"}, ApprovalLevel.NEVER)
    assert level is ApprovalLevel.ALWAYS


# --------------------------------------------------------------------------- #
# 3. requires is reported
# --------------------------------------------------------------------------- #

def test_unmet_requirement_is_named(tmp_path):
    host = PluginHost(HookBus())
    app = _App()  # no providers, no registry, no gate, no memory store
    _write_plugin(
        tmp_path, "needy", "def activate(ctx): pass\n",
        manifest={"name": "needy", "version": "1.0.0", "requires": ["memory"]},
    )
    host.load_dir(tmp_path, app)
    record = _record(host, "needy")
    assert record["unmet"] == ["memory"]
    # Reported, NOT enforced: the plugin still loaded.
    assert record["enabled"] is True


def test_a_requirement_filled_by_a_live_subsystem_is_met(tmp_path):
    """`rpc` is served by App.register_handler, not by the slot store, so a
    slot-store-only check would report every `requires: ["rpc"]` as unmet."""
    host = PluginHost(HookBus())
    _write_plugin(
        tmp_path, "needy", "def activate(ctx): pass\n",
        manifest={"name": "needy", "version": "1.0.0", "requires": ["rpc"]},
    )
    host.load_dir(tmp_path, _App())
    assert _record(host, "needy")["unmet"] == []


def test_a_requirement_filled_by_another_plugin_is_met(tmp_path):
    host = PluginHost(HookBus())
    _write_plugin(
        tmp_path, "aaa-provider", "def activate(ctx):\n    ctx.register_layout({'id': 'x'})\n",
        manifest={"name": "aaa-provider", "version": "1.0.0", "provides": ["layout"]},
    )
    _write_plugin(
        tmp_path, "zzz-needy", "def activate(ctx): pass\n",
        manifest={"name": "zzz-needy", "version": "1.0.0", "requires": ["layout"]},
    )
    # Sorted load order puts the provider first; that is why "aaa"/"zzz".
    host.load_dir(tmp_path, _App())
    assert _record(host, "zzz-needy")["unmet"] == []


def test_a_live_subsystem_counts_even_though_it_is_not_in_the_slot_store(tmp_path):
    """`memory`/`skill`/`persona` are product subsystems, not slot-store entries.
    Reporting them unmet on a real App would make the warning chip meaningless —
    every plugin depending on core capability would look broken."""
    class _Rich(_App):
        def __init__(self):
            super().__init__()
            self.memory = object()
            self.skills = object()
            self.personas = object()

    host = PluginHost(HookBus())
    _write_plugin(
        tmp_path, "needy", "def activate(ctx): pass\n",
        manifest={
            "name": "needy", "version": "1.0.0",
            "requires": ["memory", "skill", "persona"],
        },
    )
    host.load_dir(tmp_path, _Rich())
    assert _record(host, "needy")["unmet"] == []


def test_no_requirements_is_an_empty_list(tmp_path):
    host = PluginHost(HookBus())
    _write_plugin(tmp_path, "plain", "def activate(ctx): pass\n")
    host.load_dir(tmp_path, _App())
    assert _record(host, "plain")["unmet"] == []


# --------------------------------------------------------------------------- #
# 4. theme slot: a plugin contributes colour schemes
# --------------------------------------------------------------------------- #

THEME_MANIFEST = {
    "name": "palette",
    "version": "1.0.0",
    "provides": ["theme"],
    "themes": [
        {
            "id": "dusk",
            "name": "DUSK",
            "layout": "default",
            "colors": {"bg": "#101018", "text": "#eeeeee"},
            "css": "dusk.css",
        },
        {"id": "dawn", "name": "DAWN", "colors": {"bg": "#fff8f0"}},
    ],
}


def _write_theme_plugin(root: Path, *, with_activate: bool, manifest=None) -> Path:
    pkg = root / "palette"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "manifest.json").write_text(json.dumps(manifest or THEME_MANIFEST), "utf-8")
    (pkg / "dusk.css").write_text(".fn-root{--u:5px}", "utf-8")
    if with_activate:
        (pkg / "activate.py").write_text("def activate(ctx): pass\n", "utf-8")
    return pkg


def test_a_theme_plugin_needs_no_activate_py(tmp_path):
    """A colour scheme is pure data. Requiring code to ship one would mean every
    theme author writes an empty ``activate`` — and before this, a package
    without one was skipped entirely, so the themes never reached the shell."""
    host = PluginHost(HookBus())
    _write_theme_plugin(tmp_path, with_activate=False)
    host.load_dir(tmp_path, _App())
    record = _record(host, "palette")
    assert record["enabled"] is True
    assert [t["id"] for t in record["themes"]] == ["dusk", "dawn"]


def test_a_package_with_neither_code_nor_data_is_still_skipped(tmp_path):
    """The relaxation above must not turn every stray directory into a plugin."""
    pkg = tmp_path / "empty"
    pkg.mkdir()
    (pkg / "manifest.json").write_text(
        json.dumps({"name": "empty", "version": "1.0.0"}), "utf-8"
    )
    host = PluginHost(HookBus())
    host.load_dir(tmp_path, _App())
    assert all(r["name"] != "empty" for r in host.list())


def test_themes_ride_along_with_a_code_plugin(tmp_path):
    """Contributing a scheme and running code are independent."""
    host = PluginHost(HookBus())
    _write_theme_plugin(tmp_path, with_activate=True)
    host.load_dir(tmp_path, _App())
    assert len(_record(host, "palette")["themes"]) == 2


def test_a_themes_css_file_is_servable_but_nothing_else_is(tmp_path):
    """``ui_asset`` is the web server's only door into the plugins dir. A theme's
    stylesheet has to pass through it, and widening that door is exactly where a
    path-traversal or source-leak bug would live — so pin what opens and what
    stays shut."""
    host = PluginHost(HookBus())
    _write_theme_plugin(tmp_path, with_activate=True)
    host.load_dir(tmp_path, _App())
    assert host.ui_asset("palette", "dusk.css") is not None
    # Not declared by any theme, and not the ui module: stays unreachable.
    assert host.ui_asset("palette", "activate.py") is None
    assert host.ui_asset("palette", "manifest.json") is None
    assert host.ui_asset("palette", "../../secret") is None


def test_a_disabled_theme_plugin_serves_no_css(tmp_path):
    """Same rule as a ui module: "enabled" is enforced per request, so disabling
    a plugin immediately stops serving its files."""
    host = PluginHost(HookBus())
    _write_theme_plugin(tmp_path, with_activate=True)
    host.load_dir(tmp_path, _App())
    host.disable("palette")
    assert host.ui_asset("palette", "dusk.css") is None


def test_a_disabled_theme_plugin_still_lists_its_themes(tmp_path):
    """Config lists disabled plugins too; the shell decides what to offer. The
    record stays truthful about what the plugin *would* contribute."""
    host = PluginHost(HookBus())
    _write_theme_plugin(tmp_path, with_activate=True)
    host.load_dir(tmp_path, _App())
    host.disable("palette")
    record = _record(host, "palette")
    assert record["enabled"] is False
    assert len(record["themes"]) == 2


def test_no_themes_is_an_empty_list_not_absent(tmp_path):
    """The shell iterates this key; ``None`` would need a guard at every use."""
    host = PluginHost(HookBus())
    _write_plugin(tmp_path, "plain", "def activate(ctx): pass\n")
    host.load_dir(tmp_path, _App())
    assert _record(host, "plain")["themes"] == []


def test_requires_theme_is_met_by_the_builtin_schemes(tmp_path):
    """The shell always ships built-in schemes, so ``requires: ["theme"]`` asks
    for something that is always there."""
    host = PluginHost(HookBus())
    _write_plugin(
        tmp_path, "needs", "def activate(ctx): pass\n",
        manifest={"name": "needs", "version": "1.0.0", "requires": ["theme"]},
    )
    host.load_dir(tmp_path, _App())
    assert _record(host, "needs")["unmet"] == []


# --------------------------------------------------------------------------- #
# 4b. theme validation — these values reach a live style attribute and a
#     <link href>, so a loose one is an injection point, not a cosmetic bug.
# --------------------------------------------------------------------------- #

def _theme_manifest(theme: dict) -> dict:
    return {"name": "p", "version": "1.0.0", "themes": [theme]}


def test_a_valid_theme_passes_validation():
    assert validate_manifest(_theme_manifest(THEME_MANIFEST["themes"][0])) == []


def test_a_non_hex_colour_is_rejected():
    """``url(...)`` in a colour would be fetched by the browser; a bare word
    would silently do nothing. Both are rejected at the manifest boundary."""
    for bad in ("url(http://x/y.png)", "red", "#12", "expression(alert(1))"):
        errors = validate_manifest(
            _theme_manifest({"id": "x", "name": "X", "colors": {"bg": bad}})
        )
        assert any("hex colour" in e for e in errors), bad


def test_a_colour_key_that_is_not_a_custom_property_name_is_rejected():
    """The key is interpolated into ``--<key>``, so anything but a plain
    lowercase-dashed name could break out of the property name."""
    for bad in ("bg;color", "Bg", "bg:", "--bg", ""):
        errors = validate_manifest(
            _theme_manifest({"id": "x", "name": "X", "colors": {bad: "#fff"}})
        )
        assert any("lowercase-dashed" in e for e in errors), bad


def test_a_css_path_cannot_escape_the_plugin_dir():
    for bad in ("../../etc/passwd.css", "/abs/path.css", "sub/dir.css"):
        errors = validate_manifest(
            _theme_manifest({"id": "x", "name": "X", "colors": {}, "css": bad})
        )
        assert any("bare filename" in e for e in errors), bad


def test_a_css_file_must_actually_be_css():
    errors = validate_manifest(
        _theme_manifest({"id": "x", "name": "X", "colors": {}, "css": "sneaky.js"})
    )
    assert any("must end in .css" in e for e in errors)


def test_a_theme_id_must_be_dashed_lowercase():
    """The id becomes part of a DOM class and a localStorage key."""
    errors = validate_manifest(
        _theme_manifest({"id": "My Theme!", "name": "X", "colors": {}})
    )
    assert any("lowercase-dashed" in e for e in errors)


def test_duplicate_theme_ids_in_one_manifest_are_rejected():
    """Two schemes with one id means the second is unselectable — the shell keys
    its list by id, so it would silently vanish."""
    errors = validate_manifest({
        "name": "p", "version": "1.0.0",
        "themes": [
            {"id": "dusk", "name": "A", "colors": {}},
            {"id": "dusk", "name": "B", "colors": {}},
        ],
    })
    assert any("duplicated" in e for e in errors)


def test_a_theme_missing_its_name_is_rejected():
    errors = validate_manifest(_theme_manifest({"id": "x", "colors": {}}))
    assert any("name is required" in e for e in errors)


def test_an_invalid_theme_takes_the_whole_manifest_down(tmp_path):
    """Fail closed: a plugin with a malformed theme does not load at all, rather
    than loading with a half-applied scheme that paints unpredictably."""
    host = PluginHost(HookBus())
    _write_theme_plugin(
        tmp_path, with_activate=True,
        manifest={
            "name": "palette", "version": "1.0.0",
            "themes": [{"id": "dusk", "name": "D", "colors": {"bg": "javascript:x"}}],
        },
    )
    host.load_dir(tmp_path, _App())
    assert host.list() == []


# --------------------------------------------------------------------------- #
# 5. ui mounts — where a plugin's custom element is allowed to land. `config`
#    exists so a plugin can own a Config pane without the shell importing it:
#    the tab is rendered from `ui.label`/`ui.icon`, which are therefore data the
#    shell puts on screen, and validated like it.
# --------------------------------------------------------------------------- #

def _ui_manifest(ui: dict) -> dict:
    return {"name": "p", "version": "1.0.0", "provides": ["panel"], "ui": ui}


CONFIG_UI = {
    "module": "ui.js", "element": "xu-p-config",
    "mount": "config", "label": "Panel", "icon": "plug",
}


def test_a_config_pane_is_a_legal_mount():
    assert validate_manifest(_ui_manifest(CONFIG_UI)) == []


def test_a_wellformed_unknown_mount_is_valid():
    """Mounts are open: a layout plugin may offer a spot the built-in shell
    doesn't know, so membership is not checked — only the name's shape is."""
    assert validate_manifest(_ui_manifest({**CONFIG_UI, "mount": "sidebar"})) == []


def test_a_malformed_mount_is_rejected():
    """A malformed name can never match a slot any layout offers, so it must
    not load clean — the render-nowhere failure, caught at the door."""
    for bad in ("Plug", "plug/x", "plug ", "", "../x"):
        errors = validate_manifest(_ui_manifest({**CONFIG_UI, "mount": bad}))
        assert any("mount" in e for e in errors), bad


def test_an_unknown_mount_logs_a_warning_at_load(tmp_path, caplog):
    """The open-mount contract's other half: legal to load, but the host says
    so loudly, because rendering nowhere is a bug a plugin author cannot see
    from the outside."""
    host = PluginHost(HookBus())
    _write_plugin(
        tmp_path, "p", "def activate(ctx): pass\n",
        manifest=_ui_manifest({**CONFIG_UI, "mount": "sidebar"}),
    )
    with caplog.at_level("WARNING"):
        host.load_dir(tmp_path, _App())
    assert any(
        "not offered by any built-in layout" in r.message for r in caplog.records
    )


def test_a_view_mount_is_legal(tmp_path):
    """`view` is a built-in mount: a plugin pane in the main view area, with
    the same label/icon data rules as a config tab (label not required — the
    shell falls back to the plugin name)."""
    ui = {**CONFIG_UI, "mount": "view"}
    assert validate_manifest(_ui_manifest(ui)) == []
    host = PluginHost(HookBus())
    _write_plugin(tmp_path, "p", "def activate(ctx): pass\n", manifest=_ui_manifest(ui))
    (tmp_path / "p" / "ui.js").write_text("//", "utf-8")
    host.load_dir(tmp_path, _App())
    assert _record(host, "p")["ui"] == ui


def test_the_tab_label_and_icon_reach_the_record(tmp_path):
    """The shell reads them straight off `plugins.list`; dropped here, a pane
    would have no tab to open it."""
    host = PluginHost(HookBus())
    _write_plugin(tmp_path, "p", "def activate(ctx): pass\n", manifest=_ui_manifest(CONFIG_UI))
    (tmp_path / "p" / "ui.js").write_text("customElements.define('x', class {});", "utf-8")
    host.load_dir(tmp_path, _App())
    assert _record(host, "p")["ui"] == CONFIG_UI


def test_an_absent_label_or_icon_stays_absent(tmp_path):
    """No defaults here: the shell decides the fallbacks (the plugin's own name,
    and an icon it actually ships), and it cannot if the loader invents one."""
    host = PluginHost(HookBus())
    ui = {"module": "ui.js", "element": "xu-p-config", "mount": "config"}
    _write_plugin(tmp_path, "p", "def activate(ctx): pass\n", manifest=_ui_manifest(ui))
    (tmp_path / "p" / "ui.js").write_text("//", "utf-8")
    host.load_dir(tmp_path, _App())
    assert _record(host, "p")["ui"] == ui


def test_a_blank_label_is_rejected():
    errors = validate_manifest(_ui_manifest({**CONFIG_UI, "label": "  "}))
    assert any("label" in e for e in errors)


def test_an_overlong_label_is_rejected():
    """A tab strip is narrow; a 200-char label is a layout bug, not a name."""
    errors = validate_manifest(_ui_manifest({**CONFIG_UI, "label": "x" * 25}))
    assert any("at most" in e for e in errors)


def test_an_icon_that_is_not_a_name_is_rejected():
    """The icon is a *name* the shell looks up in its own set. Anything that
    could be a path or markup is refused here rather than trusted there."""
    for bad in ("../evil.svg", "<svg onload=x>", "Plug", "plug/x", "plug ", ""):
        errors = validate_manifest(_ui_manifest({**CONFIG_UI, "icon": bad}))
        assert any("icon" in e for e in errors), bad


def test_a_config_pane_serves_only_its_declared_module(tmp_path):
    """Same door as any other ui module: the pane's own file, nothing else in
    the package."""
    host = PluginHost(HookBus())
    pkg = _write_plugin(
        tmp_path, "p", "def activate(ctx): pass\n", manifest=_ui_manifest(CONFIG_UI)
    )
    (pkg / "ui.js").write_text("//", "utf-8")
    (pkg / "secret.txt").write_text("no", "utf-8")
    host.load_dir(tmp_path, _App())
    assert host.ui_asset("p", "ui.js") == pkg / "ui.js"
    assert host.ui_asset("p", "secret.txt") is None
    assert host.ui_asset("p", "activate.py") is None


def test_ui_assets_reach_the_record(tmp_path):
    """Extra servable files ride along like label/icon: the server serves
    only what the record carries, so dropped here they'd be unreachable."""
    ui = {**CONFIG_UI, "assets": ["chart.js", "worker.js"]}
    assert validate_manifest(_ui_manifest(ui)) == []
    host = PluginHost(HookBus())
    _write_plugin(tmp_path, "p", "def activate(ctx): pass\n", manifest=_ui_manifest(ui))
    (tmp_path / "p" / "ui.js").write_text("//", "utf-8")
    host.load_dir(tmp_path, _App())
    assert _record(host, "p")["ui"]["assets"] == ["chart.js", "worker.js"]


def test_a_non_bare_ui_asset_is_rejected():
    """Assets are served straight off disk like ui.module — an entry that is
    a path is a traversal bug, so it rejects at the door, not in the server."""
    for bad in ("../evil", "/abs.js", "sub/dir.js", ""):
        errors = validate_manifest(_ui_manifest({**CONFIG_UI, "assets": [bad]}))
        assert any("assets" in e for e in errors), bad


def test_non_list_ui_assets_are_rejected():
    """The whole field must be a list of filenames; anything else is a
    manifest typo, not a plugin data decision."""
    for bad in ("chart.js", {"a": 1}, 3):
        errors = validate_manifest(_ui_manifest({**CONFIG_UI, "assets": bad}))
        assert any("assets" in e for e in errors), bad


def test_a_declared_ui_asset_is_servable_but_nothing_undeclared_is(tmp_path):
    """``ui.assets`` widens the door by exactly the names the manifest
    declared — an undeclared file in the same dir stays unreachable."""
    host = PluginHost(HookBus())
    pkg = _write_plugin(
        tmp_path, "p", "def activate(ctx): pass\n",
        manifest=_ui_manifest({**CONFIG_UI, "assets": ["chart.js"]}),
    )
    (pkg / "ui.js").write_text("//", "utf-8")
    (pkg / "chart.js").write_text("//", "utf-8")
    (pkg / "undeclared.js").write_text("//", "utf-8")
    host.load_dir(tmp_path, _App())
    assert host.ui_asset("p", "chart.js") == pkg / "chart.js"
    assert host.ui_asset("p", "undeclared.js") is None
