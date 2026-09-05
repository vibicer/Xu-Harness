"""Built-in plugin seeding + the shipped MCP client plugin.

Two things are pinned here. First, that a shipped plugin package reaches
``data_home/plugins`` on boot (a fresh install must not need a manual copy) and
that re-seeding neither clobbers a user's own files nor keeps a stale copy past
a version bump. Second, that the MCP plugin actually speaks the protocol: it
connects to a real child process (``_echo_server.py``), registers the server's
tools into the live registry under the mangled names, runs one, and reaps the
process on deactivate.
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from xu_brain.core.bus import HookBus
from xu_brain.core.contract import RpcError
from xu_brain.core.governance import ApprovalLevel, ApprovalManager
from xu_brain.core.host import PluginHost
from xu_brain.features.tools.registry import ToolRegistry
from xu_brain.plugins.seed import BUILTIN_PLUGIN_DIR, seed_builtin_plugins

MCP_DIR = BUILTIN_PLUGIN_DIR / "mcp"


def _module(name: str, path: Path):
    """Load a plugin file by path — the host execs them, so they aren't imports."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Config:
    """Stand-in for App.config: plugin settings live under `plugin.<name>.<key>`."""

    def __init__(self, values=None):
        self._values = dict(values or {})

    def all(self):
        return dict(self._values)

    def set(self, key, value):
        self._values[key] = value


class _Slots:
    def register(self, kind, plugin, value):
        return lambda: None


class _App:
    def __init__(self, data_home: Path, config=None):
        self.data_home = data_home
        self.config = config or _Config()
        self.slots = _Slots()
        self.registry = ToolRegistry(ApprovalManager())

        # `mcp.*` land here, so a test can drive the panel's RPCs for real.
        self.rpcs: dict = {}
    def register_handler(self, name, handler):
        self.rpcs[name] = handler
        return lambda: self.rpcs.pop(name, None)


def _write_config(data_home: Path, *, server: str = "echo", timeout: float = 5.0) -> None:
    (data_home / "mcp.json").write_text(json.dumps({
        "mcpServers": {
            server: {
                "command": sys.executable,
                "args": [str(MCP_DIR / "_echo_server.py")],
                "timeout": timeout,
            }
        }
    }), "utf-8")


# ---- seeding ---------------------------------------------------------- #


def test_shipped_plugins_are_seeded_into_data_home(tmp_path: Path) -> None:
    written = seed_builtin_plugins(tmp_path)

    assert "mcp" in written
    manifest = json.loads((tmp_path / "plugins" / "mcp" / "manifest.json").read_text())
    assert manifest["name"] == "mcp"
    assert (tmp_path / "plugins" / "mcp" / "activate.py").is_file()
    # Nothing changed on the second boot: re-copying every time would fight a
    # user who edited a shipped file.
    assert seed_builtin_plugins(tmp_path) == []


def test_reseed_refreshes_on_version_change_and_keeps_user_files(tmp_path: Path) -> None:
    seed_builtin_plugins(tmp_path)
    installed = tmp_path / "plugins" / "mcp"
    shipped_version = json.loads((MCP_DIR / "manifest.json").read_text())["version"]

    manifest = json.loads((installed / "manifest.json").read_text())
    manifest["version"] = "0.0.1"
    (installed / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    (installed / "notes.md").write_text("mine", "utf-8")

    assert seed_builtin_plugins(tmp_path) == ["mcp"]
    refreshed = json.loads((installed / "manifest.json").read_text())
    assert refreshed["version"] == shipped_version, "an outdated copy must be refreshed"
    assert (installed / "notes.md").read_text() == "mine", "a user's own file must survive"


def test_seeding_ignores_a_source_dir_without_a_manifest(tmp_path: Path) -> None:
    source = tmp_path / "shipped"
    (source / "not-a-plugin").mkdir(parents=True)
    (source / "not-a-plugin" / "readme.txt").write_text("x", "utf-8")

    assert seed_builtin_plugins(tmp_path, source=source) == []
    assert not (tmp_path / "plugins" / "not-a-plugin").exists()


# ---- the client ------------------------------------------------------- #


async def test_client_handshake_lists_and_calls_tools() -> None:
    client_mod = _module("mcp_client_t", MCP_DIR / "client.py")
    client = client_mod.StdioClient(
        "echo", sys.executable, [str(MCP_DIR / "_echo_server.py")], timeout=5.0
    )
    try:
        info = await client.start()
        assert info["serverInfo"]["name"] == "echo"
        assert sorted(t["name"] for t in await client.list_tools()) == ["boom", "echo", "sleep"]
        result = await client.call_tool("echo", {"text": "hi"})
        assert result["content"][0]["text"] == "hi"
    finally:
        await client.close()


async def test_request_timeout_names_the_server_and_method() -> None:
    client_mod = _module("mcp_client_t2", MCP_DIR / "client.py")
    client = client_mod.StdioClient(
        "slowpoke", sys.executable, [str(MCP_DIR / "_echo_server.py")], timeout=0.3
    )
    await client.start()
    try:
        raised = None
        try:
            await client.call_tool("sleep", {"seconds": 5})
        except client_mod.MCPError as exc:
            raised = exc
        assert raised is not None, "a blocked server must not hang the caller"
        assert "slowpoke" in str(raised) and "tools/call" in str(raised)
    finally:
        await client.close()


def test_child_env_is_an_allowlist_plus_declared_vars(monkeypatch) -> None:
    client_mod = _module("mcp_client_t3", MCP_DIR / "client.py")
    monkeypatch.setenv("XU_TEST_SECRET", "s3cret")

    env = client_mod.child_env({"TOKEN": "${XU_TEST_SECRET}", "PLAIN": "v"})

    assert env["TOKEN"] == "s3cret" and env["PLAIN"] == "v"
    assert "XU_TEST_SECRET" not in env, "third-party code must not inherit the whole env"
    assert "PATH" in env


def test_tool_names_are_sanitized_and_capped() -> None:
    plugin = _module("mcp_activate_t", MCP_DIR / "activate.py")

    assert plugin.tool_name("files", "read_file") == "mcp__files_read_file"
    assert plugin.tool_name("a-b", "Do.Thing") == "mcp__a_b_do_thing"
    # A server that already prefixes its own tools must not stutter.
    assert plugin.tool_name("caido", "caido_list_projects") == "mcp__caido_list_projects"
    long_name = plugin.tool_name("srv", "x" * 90)
    assert len(long_name) == 64
    # Two long names on one server must stay distinct, not collapse.
    assert long_name != plugin.tool_name("srv", "y" * 90)


def test_render_folds_structured_content_and_flags_errors() -> None:
    plugin = _module("mcp_activate_t2", MCP_DIR / "activate.py")

    text, is_error = plugin.render({"content": [{"type": "text", "text": "ok"}]})
    assert (text, is_error) == ("ok", False)

    text, _ = plugin.render({
        "content": [{"type": "text", "text": "summary"}],
        "structuredContent": {"n": 1},
    })
    assert "summary" in text and '"n": 1' in text

    # The common real-world shape: the same payload as compact JSON text *and*
    # structuredContent. Appending a pretty copy would bill the model twice.
    payload = {"projects": [{"id": "9194", "name": "bb"}]}
    text, _ = plugin.render({
        "content": [{"type": "text", "text": json.dumps(payload)}],
        "structuredContent": payload,
    })
    assert text.count('"name"') == 1 and "```json" not in text

    text, is_error = plugin.render({"content": [{"type": "text", "text": "nope"}], "isError": True})
    assert is_error and text == "nope"

    # An image cannot ride in tool output; say so rather than dropping it.
    text, _ = plugin.render({"content": [{"type": "image", "data": "..."}]})
    assert text == "[image content omitted]"


# ---- the plugin, end to end ------------------------------------------ #


async def test_activate_registers_server_tools_and_deactivate_reaps(tmp_path: Path) -> None:
    seed_builtin_plugins(tmp_path)
    _write_config(tmp_path)
    app = _App(tmp_path)
    host = PluginHost(HookBus())

    assert host.load_dir(tmp_path / "plugins", app) >= 1
    await host.ready()

    tool = app.registry.get("mcp__echo_echo")
    assert tool is not None, sorted(app.registry.names())
    assert tool.toolset == "mcp"
    assert tool.approval is ApprovalLevel.RISKY, "third-party code prompts by default"
    assert tool.description.startswith("[echo] ")
    assert tool.schema["properties"]["text"]["type"] == "string"

    result = await tool.run({"text": "hello mcp"}, None)
    assert result.output == "hello mcp" and not result.error

    failed = await app.registry.get("mcp__echo_boom").run({}, None)
    assert failed.error and failed.output == "exploded"

    module = host._live["mcp"][1]
    host.deactivate_all()
    assert app.registry.get("mcp__echo_echo") is None, "disabling must retire its tools"
    for _ in range(50):  # deactivate() is async: the host schedules it
        if not module.live_clients():
            break
        await asyncio.sleep(0.02)
    assert module.live_clients() == {}, "every child process must be reaped"


async def test_trusted_server_tools_skip_approval(tmp_path: Path) -> None:
    seed_builtin_plugins(tmp_path)
    _write_config(tmp_path)
    app = _App(tmp_path, config=_Config({"plugin.mcp.trusted_servers": ["echo"]}))
    host = PluginHost(HookBus())

    host.load_dir(tmp_path / "plugins", app)
    await host.ready()
    try:
        assert app.registry.get("mcp__echo_echo").approval is ApprovalLevel.NEVER
    finally:
        host.deactivate_all()


async def test_unsupported_transport_and_missing_config_are_reported(tmp_path: Path) -> None:
    seed_builtin_plugins(tmp_path)
    (tmp_path / "mcp.json").write_text(json.dumps({
        "mcpServers": {"remote": {"type": "http", "url": "https://example.com/mcp"}}
    }), "utf-8")
    app = _App(tmp_path)
    host = PluginHost(HookBus())

    host.load_dir(tmp_path / "plugins", app)
    await host.ready()
    try:
        # Loaded, no tools: an http entry is skipped with a warning, and the
        # plugin still comes up for the stdio servers beside it.
        assert [p["name"] for p in host.list() if p["name"] == "mcp"] == ["mcp"]
        assert [n for n in app.registry.names() if n.startswith("mcp__")] == []
    finally:
        host.deactivate_all()


# ---- the management surface (Config → MCP) ---------------------------- #


async def test_rpcs_add_connect_disconnect_and_remove_a_server(tmp_path: Path) -> None:
    """The panel's round trip, through the real host and the real registry.

    A server added at runtime must reach ``mcp.json`` *and* the registry, and
    removing it must retire its tools and reap the child — otherwise the panel
    would be showing state the agent doesn't have.
    """
    seed_builtin_plugins(tmp_path)
    app = _App(tmp_path)
    host = PluginHost(HookBus())
    host.load_dir(tmp_path / "plugins", app)
    await host.ready()
    module = host._live["mcp"][1]
    try:
        # Subset, not equality: seeding brings in whatever else ships in
        # builtin/, and this test is only about the mcp surface.
        assert {"mcp.add", "mcp.connect", "mcp.disconnect", "mcp.list", "mcp.remove", "mcp.set"} <= set(app.rpcs)
        # Registered with no mcp.json present: a fresh install still gets a panel.
        assert app.registry.get("mcp") is not None

        row = await app.rpcs["mcp.add"]({
            "name": "echo",
            "command": sys.executable,
            # A one-line args string is what the panel sends.
            "args": f"{MCP_DIR / '_echo_server.py'}",
            "env": {"TOKEN": "literal-secret"},
            "timeout": 5,
        })
        assert row["state"] == "on" and len(row["tools"]) == 3, row
        assert app.registry.get("mcp__echo_echo") is not None
        saved = json.loads((tmp_path / "mcp.json").read_text())["mcpServers"]["echo"]
        assert saved["command"] == sys.executable and saved["args"], saved

        # The payload names env keys but never their values — it goes to a UI.
        assert row["env_keys"] == ["TOKEN"]
        assert "literal-secret" not in json.dumps(row)

        off = await app.rpcs["mcp.disconnect"]({"name": "echo"})
        assert off["state"] == "off" and off["tools"] == []
        assert app.registry.get("mcp__echo_echo") is None, "disconnect must retire tools"
        assert module.live_clients() == {}

        back = await app.rpcs["mcp.connect"]({"name": "echo"})
        assert back["state"] == "on" and len(back["tools"]) == 3
        assert app.registry.get("mcp__echo_echo") is not None

        # Trust is a plugin setting, and the level is baked in at registration —
        # so flipping it has to re-register, not just remember.
        trusted = await app.rpcs["mcp.set"]({"name": "echo", "trusted": True})
        assert trusted["trusted"] and trusted["state"] == "on"
        assert app.registry.get("mcp__echo_echo").approval is ApprovalLevel.NEVER
        assert app.config.all()["plugin.mcp.trusted_servers"] == ["echo"]

        await app.rpcs["mcp.remove"]({"name": "echo"})
        assert json.loads((tmp_path / "mcp.json").read_text())["mcpServers"] == {}
        assert app.registry.get("mcp__echo_echo") is None
        assert module.live_clients() == {}, "remove must reap the child"
    finally:
        host.deactivate_all()


async def test_mcp_add_refuses_what_it_cannot_run(tmp_path: Path) -> None:
    """Bad input is refused before it reaches mcp.json.

    ``mcp.add`` is reachable from an unauthenticated local socket and it writes a
    command that later gets executed, so every rejection here is load-bearing.
    """
    seed_builtin_plugins(tmp_path)
    app = _App(tmp_path)
    host = PluginHost(HookBus())
    host.load_dir(tmp_path / "plugins", app)
    await host.ready()
    try:
        bad = [
            {"name": "has space", "command": "x"},
            {"name": "ok", "command": "   "},
            {"name": "sse-one", "command": "x", "type": "sse"},
            {"name": "../escape", "command": "x"},
        ]
        for spec in bad:
            with pytest.raises(RpcError) as caught:
                await app.rpcs["mcp.add"](spec)
            assert caught.value.code == -32602, spec
        assert not (tmp_path / "mcp.json").exists(), "a refused add must not write"
    finally:
        host.deactivate_all()


async def test_hand_edited_config_shows_up_without_a_reload(tmp_path: Path) -> None:
    """mcp.json is a user-editable file, so `mcp.list` re-reads it."""
    seed_builtin_plugins(tmp_path)
    app = _App(tmp_path)
    host = PluginHost(HookBus())
    host.load_dir(tmp_path / "plugins", app)
    await host.ready()
    try:
        assert (await app.rpcs["mcp.list"]({}))["servers"] == []
        _write_config(tmp_path, server="typed-in")
        snap = await app.rpcs["mcp.list"]({})
        assert [s["name"] for s in snap["servers"]] == ["typed-in"]
        # Present but not started: listing must not spawn anything.
        assert snap["servers"][0]["state"] == "off"
        assert snap["config"].endswith("mcp.json")
    finally:
        host.deactivate_all()
