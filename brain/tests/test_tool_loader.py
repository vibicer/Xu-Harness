"""Drop-in tool loader: discovery, contracts, and bad-file isolation."""
from __future__ import annotations

import asyncio
import importlib
import re
from pathlib import Path

from xu_brain.core.governance import ApprovalLevel
from xu_brain.features.tools.loader import load_dropin_tools
from xu_brain.features.tools.toolbuilder import _TOOL_HDR, _TRAILER_TMPL


def _write(data_home: Path, filename: str, src: str) -> None:
    (data_home / "tools").mkdir(parents=True, exist_ok=True)
    (data_home / "tools" / filename).write_text(src, encoding="utf-8")


_MODULE = (
    "from xu_brain.features.tools.loader import tool\n"
    "from xu_brain.features.tools.base import ToolResult\n\n"
    "@tool({name!r}, {desc!r})\n"
    "async def _impl(args, ctx):\n"
    "    return ToolResult.ok('pong')\n"
    "tools = [_impl]\n"
)


def test_decorated_tool_discovered_with_defaults(tmp_path: Path) -> None:
    _write(tmp_path, "ping.py", _MODULE.format(name="ping", desc="ping"))
    tools = load_dropin_tools(tmp_path)
    assert len(tools) == 1
    assert tools[0].name == "ping"
    assert tools[0].toolset == "dropin"
    assert tools[0].approval is ApprovalLevel.RISKY


def test_loaded_tool_runs(tmp_path: Path) -> None:
    _write(tmp_path, "ping.py", _MODULE.format(name="ping", desc="ping"))
    result = asyncio.run(load_dropin_tools(tmp_path)[0].run({}, None))  # type: ignore[arg-type]
    assert result.output == "pong"


def test_bad_module_is_skipped_others_load(tmp_path: Path) -> None:
    _write(tmp_path, "broken.py", "def ( =\n")
    _write(tmp_path, "good.py", _MODULE.format(name="ok", desc="ok"))
    tools = load_dropin_tools(tmp_path)
    assert [t.name for t in tools] == ["ok"]


def test_missing_tools_dir_returns_empty(tmp_path: Path) -> None:
    assert load_dropin_tools(tmp_path) == []


# The verbatim public import surface a drop-in author (or `computer_info.py`)
# uses. If these paths ever move again the loader must fail loudly, not return
# [] the way the load_dropin_tools→xu_brain.tools path move did.
_PUBLIC_SURFACE = (
    "from xu_brain.features.tools.loader import tool, ApprovalLevel\n"
    "from xu_brain.features.tools.base import ToolResult\n\n"
    "@tool('pinned', 'pinned', approval=ApprovalLevel.RISKY)\n"
    "async def _impl(args, ctx):\n"
    "    return ToolResult.ok('pinned')\n"
    "tools = [_impl]\n"
)


def test_public_import_surface_still_loads(tmp_path: Path) -> None:
    _write(tmp_path, "pinned.py", _PUBLIC_SURFACE)
    tools = load_dropin_tools(tmp_path)
    assert [t.name for t in tools] == ["pinned"]
    # The re-exported ApprovalLevel is the very governance enum, not a copy.
    assert tools[0].approval is ApprovalLevel.RISKY


def test_toolbuilder_template_imports_resolve() -> None:
    """Every `from X import Y` the codegen template emits must actually
    import — pins the generator (`_TOOL_HDR`/`_TRAILER_TMPL`) and the
    drop-in loader to the same public paths."""
    src = _TOOL_HDR + _TRAILER_TMPL
    lines = [ln for ln in src.splitlines() if ln.startswith("from ")]
    assert lines, "codegen template should pin its own import lines"
    for line in lines:
        m = re.match(r"from\s+(\S+)\s+import\s+(.+)$", line.strip())
        assert m, f"unparseable import line in template: {line!r}"
        mod = importlib.import_module(m.group(1))
        for name in m.group(2).split(","):
            name = name.split(" as ")[0].strip()
            # AttributeError here means the template drifted from the
            # public surface the loader relies on.
            getattr(mod, name)
