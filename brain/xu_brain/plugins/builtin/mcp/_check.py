"""Runnable check for the MCP plugin — no brain, no shell, no network.

    cd <xu>/brain && PYTHONPATH=. python xu_brain/plugins/builtin/mcp/_check.py

Exercises the client against ``_echo_server.py``: handshake, catalog, a call, a
failing call, a request timeout, the env allowlist, and name mangling. Then runs
the live hub — add / connect / agent tool / disconnect / remove — against a temp
data_home. Prints a line per check and exits non-zero on the first failure.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


client_mod = _load("mcp_client_check", "client.py")
plugin = _load("mcp_activate_check", "activate.py")


class FakeRegistry:
    """Just enough of ToolRegistry for the hub: add, drop, look up."""

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def register(self, tool):
        self.tools[tool.name] = tool
        return lambda: self.tools.pop(tool.name, None)

    def unregister(self, name):
        return self.tools.pop(name, None)

    def get(self, name):
        return self.tools.get(name)


class FakeCtx:
    """The slice of the plugin context the hub touches."""

    def __init__(self, data_home: Path) -> None:
        self.registry = FakeRegistry()
        self.app = type("App", (), {"data_home": str(data_home), "registry": self.registry})()
        self.rpcs: dict[str, object] = {}
        self._settings: dict[str, object] = {}

    def settings(self):
        return dict(self._settings)

    def set_setting(self, key, value):
        self._settings[key] = value

    def register_tool(self, tool):
        self.registry.register(tool)

    def register_rpc(self, name, handler):
        self.rpcs[name] = handler

    def log(self, *_a, **_kw):
        pass


async def hub_checks(server: list[str]) -> None:
    """The management surface, end to end, in a throwaway data_home."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        ctx = FakeCtx(home)
        await plugin.activate(ctx)
        assert sorted(ctx.rpcs) == [
            "mcp.add", "mcp.connect", "mcp.disconnect", "mcp.list", "mcp.remove", "mcp.set",
        ], sorted(ctx.rpcs)
        assert ctx.registry.get("mcp") is not None, "the admin tool must always be there"
        ok("activate registers 6 RPCs + the mcp tool with no config present")

        entry = await ctx.rpcs["mcp.add"]({
            "name": "echo", "command": server[0], "args": server[1:],
            "env": {"TOKEN": "${MCP_CHECK_SECRET}"},
        })
        assert entry["state"] == "on" and len(entry["tools"]) == 3, entry
        assert ctx.registry.get("mcp__echo_echo") is not None
        saved = json.loads((home / "mcp.json").read_text())["mcpServers"]["echo"]
        assert saved["command"] == server[0], saved
        ok(f"mcp.add persists to mcp.json and connects ({len(entry['tools'])} tools)")

        assert entry["env_keys"] == ["TOKEN"] and "env" not in entry, entry
        ok("the payload carries env key names, never their values")

        listed = await ctx.rpcs["mcp.list"]({})
        assert [s["name"] for s in listed["servers"]] == ["echo"], listed
        result = await ctx.registry.get("mcp").run({"action": "list"}, None)
        assert "echo: on" in result.output and not result.error, result.output
        tools = await ctx.registry.get("mcp").run({"action": "tools", "server": "echo"}, None)
        assert "mcp__echo_echo" in tools.output, tools.output
        ok("mcp.list and the agent tool report the same server")

        off = await ctx.rpcs["mcp.disconnect"]({"name": "echo"})
        assert off["state"] == "off" and off["tools"] == [], off
        assert ctx.registry.get("mcp__echo_echo") is None, "disconnect must retire its tools"
        back = await ctx.rpcs["mcp.connect"]({"name": "echo"})
        assert back["state"] == "on" and len(back["tools"]) == 3, back
        ok("disconnect retires the tools, connect brings them back")

        trusted = await ctx.rpcs["mcp.set"]({"name": "echo", "trusted": True})
        assert trusted["trusted"] and trusted["state"] == "on", trusted
        level = ctx.registry.get("mcp__echo_echo").approval
        assert level.value == "never", level
        ok("trusting a live server re-registers its tools at NEVER")

        for bad, why in (
            ({"name": "b ad", "command": "x"}, "a space is not a name"),
            ({"name": "ok", "command": ""}, "no command"),
            ({"name": "echo", "command": "x"}, "duplicate"),
            ({"name": "http1", "command": "x", "type": "sse"}, "unsupported transport"),
        ):
            try:
                await ctx.rpcs["mcp.add"](bad)
            except Exception as exc:  # RpcError
                assert getattr(exc, "code", None) == -32602, (bad, exc)
            else:
                raise AssertionError(f"mcp.add accepted {why}: {bad}")
        ok("mcp.add refuses bad names, empty commands, dupes and non-stdio")

        await ctx.rpcs["mcp.remove"]({"name": "echo"})
        assert json.loads((home / "mcp.json").read_text())["mcpServers"] == {}
        assert ctx.registry.get("mcp__echo_echo") is None
        assert plugin.live_clients() == {}, "remove must reap the child"
        ok("mcp.remove disconnects, retires tools and rewrites the config")

        await plugin.deactivate(ctx)


def ok(label: str) -> None:
    print(f"ok   {label}")


async def main() -> int:
    server = [sys.executable, str(HERE / "_echo_server.py")]
    client = client_mod.StdioClient("echo", server[0], server[1:], timeout=5.0)

    info = await client.start()
    assert info["serverInfo"]["name"] == "echo", info
    ok("handshake returns serverInfo")

    tools = await client.list_tools()
    names = sorted(t["name"] for t in tools)
    assert names == ["boom", "echo", "sleep"], names
    ok(f"tools/list → {names}")

    result = await client.call_tool("echo", {"text": "hello mcp"})
    text, is_error = plugin.render(result)
    assert (text, is_error) == ("hello mcp", False), (text, is_error)
    ok("tools/call round-trips text")

    text, is_error = plugin.render(await client.call_tool("boom", {}))
    assert is_error and text == "exploded", (text, is_error)
    ok("isError result surfaces as an error")

    slow = client_mod.StdioClient("slow", server[0], server[1:], timeout=0.3)
    await slow.start()
    try:
        await slow.call_tool("sleep", {"seconds": 5})
    except client_mod.MCPError as exc:
        assert "slow" in str(exc) and "timed out" in str(exc), exc
        ok(f"timeout names the server and method: {exc}")
    else:  # pragma: no cover — a hung request must not pass silently
        raise AssertionError("a 5s call under a 0.3s timeout should have raised")
    await slow.close()

    await client.close()
    assert client._proc is None
    ok("close is idempotent and reaps the child")
    await client.close()

    os.environ["MCP_CHECK_SECRET"] = "s3cret"
    env = client_mod.child_env({"TOKEN": "${MCP_CHECK_SECRET}"})
    assert env["TOKEN"] == "s3cret", env
    assert "MCP_CHECK_SECRET" not in env, "the parent env must not leak wholesale"
    assert "PATH" in env, "a server still needs PATH"
    ok("env is an allowlist plus explicit ${VAR} forwarding")

    long_tool = "x" * 90
    name = plugin.tool_name("srv", long_tool)
    assert len(name) == 64 and name.startswith("mcp__srv_"), name
    assert plugin.tool_name("a-b", "Do.Thing") == "mcp__a_b_do_thing"
    ok(f"names sanitized and capped at 64 ({name[-9:]})")

    await hub_checks(server)

    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
