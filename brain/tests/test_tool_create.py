"""tool_create / tool_list / tool_remove: author, list, and delete drop-in tools."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from xu_brain.features.tools.base import ToolResult
from xu_brain.features.tools.loader import active_dropins
from xu_brain.features.tools.registry import ToolRegistry
from xu_brain.features.tools.toolbuilder import tool_create, tool_list, tool_remove

_BODY = "return ToolResult.ok('pong')"


def _ctx(tmp_path: Path, registry: ToolRegistry) -> SimpleNamespace:
    return SimpleNamespace(data_home=str(tmp_path), agent=SimpleNamespace(registry=registry))


@pytest.fixture(autouse=True)
def _iso() -> None:
    # active_dropins is module-global; reset per test so ordering never leaks.
    active_dropins().clear()


def _add_builtin(registry: ToolRegistry, name: str) -> None:
    class _Fake:
        toolset = "core"
        description = ""
        approval = None
        schema = {}

        async def run(self, args, ctx) -> ToolResult:
            return ToolResult.ok("x")

    _Fake.name = name  # set after class body so the arg isn't shadowed
    registry.register(_Fake())


def test_creates_and_registers_callable_tool(tmp_path: Path) -> None:
    registry = ToolRegistry(None)
    result = asyncio.run(
        tool_create.run(
            {"name": "echo", "description": "echo", "schema": {}, "body": _BODY},
            _ctx(tmp_path, registry),
        )
    )
    assert result.error is False
    assert "echo" in result.output and "registered" in result.output

    written = tmp_path / "tools" / "echo.py"
    assert written.exists()

    assert "echo" in registry.names()
    assert "echo" in active_dropins()
    out = asyncio.run(registry._tools["echo"].run({}, SimpleNamespace()))  # noqa: SLF001
    assert out.output == "pong"


def test_refuses_builtin_name(tmp_path: Path) -> None:
    registry = ToolRegistry(None)
    _add_builtin(registry, "read")

    result = asyncio.run(
        tool_create.run({"name": "read", "body": _BODY}, _ctx(tmp_path, registry))
    )
    assert result.error is True
    assert "cannot shadow" in result.output
    assert not (tmp_path / "tools").exists()  # nothing written


def test_rejects_invalid_name_without_writing(tmp_path: Path) -> None:
    registry = ToolRegistry(None)
    for bad in ("not-a-name", "class", ""):
        result = asyncio.run(
            tool_create.run({"name": bad, "body": _BODY}, _ctx(tmp_path, registry))
        )
        assert result.error is True, bad
    assert not (tmp_path / "tools").exists()


def test_rejects_bad_body_without_writing(tmp_path: Path) -> None:
    registry = ToolRegistry(None)
    result = asyncio.run(
        tool_create.run({"name": "x", "body": "def ( =\n"}, _ctx(tmp_path, registry))
    )
    assert result.error is True
    assert "compile" in result.output
    assert not (tmp_path / "tools").exists()


# ---- tool_list / tool_remove ----


def test_tool_list_shows_created_tool(tmp_path: Path) -> None:
    registry = ToolRegistry(None)
    ctx = _ctx(tmp_path, registry)
    asyncio.run(
        tool_create.run({"name": "echo", "body": _BODY}, ctx)
    )
    result = asyncio.run(tool_list.run({}, ctx))
    assert result.error is False
    assert "echo" in result.output
    assert result.raw["tools"][0]["name"] == "echo"
    assert result.raw["tools"][0]["registered"] is True


def test_tool_list_empty(tmp_path: Path) -> None:
    result = asyncio.run(tool_list.run({}, _ctx(tmp_path, ToolRegistry(None))))
    assert result.error is False
    assert "no drop-in" in result.output


def test_tool_remove_deletes_file_and_unregisters(tmp_path: Path) -> None:
    registry = ToolRegistry(None)
    ctx = _ctx(tmp_path, registry)
    asyncio.run(tool_create.run({"name": "echo", "body": _BODY}, ctx))
    assert (tmp_path / "tools" / "echo.py").exists()

    result = asyncio.run(tool_remove.run({"name": "echo"}, ctx))
    assert result.error is False
    assert "echo" in result.output
    assert "echo" not in registry.names()
    assert "echo" not in active_dropins()
    assert not (tmp_path / "tools" / "echo.py").exists()


def test_tool_remove_refuses_builtin(tmp_path: Path) -> None:
    registry = ToolRegistry(None)
    _add_builtin(registry, "read")
    result = asyncio.run(tool_remove.run({"name": "read"}, _ctx(tmp_path, registry)))
    assert result.error is True
    assert "cannot remove" in result.output
    assert "read" in registry.names()  # still present


def test_tool_remove_unknown(tmp_path: Path) -> None:
    result = asyncio.run(tool_remove.run({"name": "nope"}, _ctx(tmp_path, ToolRegistry(None))))
    assert result.error is True
    assert "not an active drop-in" in result.output


def test_dropin_toggle_isolated_from_toolset(tmp_path: Path) -> None:
    """A disabled drop-in disappears from the model toolset while built-ins and
    sibling tools in the same toolset stay available."""
    registry = ToolRegistry(None)
    registry.register(
        type(
            "_Shell",
            (),
            {
                "name": "bash",
                "toolset": "terminal",
                "description": "",
                "approval": None,
                "schema": {},
                "run": lambda self, args, ctx: ToolResult.ok("shell"),
            },
        )()
    )
    # custom tool parked in the same toolset
    asyncio.run(
        tool_create.run(
            {"name": "mine", "toolset": "terminal", "body": _BODY}, _ctx(tmp_path, registry)
        )
    )
    assert registry.tool_enabled("mine") is True
    assert registry.tool_enabled("bash") is True

    registry.enable_tool("mine", False)
    names = [s["function"]["name"] for s in registry.schemas_for_model()]
    assert "mine" not in names
    assert "bash" in names
    assert registry.tool_enabled("mine") is False
    assert registry.tool_enabled("bash") is True  # sibling unaffected
