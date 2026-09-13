"""Tests for repo_map toolset and project map prompt injection."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from xu_brain.features.agent.loop import Agent
from xu_brain.features.session import SessionStore
from xu_brain.features.tools import all_tools
from xu_brain.features.tools.repo_map import RepoMapTool


@pytest.fixture
def sample_project(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    src.mkdir()

    py_file = src / "auth.py"
    py_file.write_text(
        "class AuthService:\n"
        "    def login(self, username, password):\n"
        "        pass\n"
        "    async def logout(self):\n"
        "        pass\n\n"
        "def hash_pass(pwd):\n"
        "    pass\n",
        encoding="utf-8",
    )

    ts_file = src / "client.ts"
    ts_file.write_text(
        "export interface UserProfile {\n"
        "    id: string;\n"
        "    name: string;\n"
        "}\n\n"
        "export async function fetchUser(id: string): Promise<UserProfile> {\n"
        "    return { id, name: 'test' };\n"
        "}\n\n"
        "export const API_URL = 'http://localhost';\n",
        encoding="utf-8",
    )

    rs_file = src / "engine.rs"
    rs_file.write_text(
        "pub struct StateMachine;\n\n"
        "pub fn process_event(state: &mut StateMachine) {\n"
        "}\n",
        encoding="utf-8",
    )

    return tmp_path


def _make_ctx(cwd: Path) -> SimpleNamespace:
    return SimpleNamespace(cwd=str(cwd), config={})


def test_repo_map_in_all_tools():
    tools = {t.name: t for t in all_tools()}
    assert "repo_map" in tools
    assert tools["repo_map"].toolset == "search"


def test_repo_map_overview(sample_project: Path):
    tool = RepoMapTool()
    ctx = _make_ctx(sample_project)
    res = asyncio.run(tool.run({"action": "overview"}, ctx))
    assert not res.error
    assert "Codebase Overview" in res.output
    assert ".py" in res.output
    assert ".ts" in res.output
    assert ".rs" in res.output


def test_repo_map_tree(sample_project: Path):
    tool = RepoMapTool()
    ctx = _make_ctx(sample_project)
    res = asyncio.run(tool.run({"action": "tree"}, ctx))
    assert not res.error
    assert "auth.py" in res.output
    assert "client.ts" in res.output
    assert "engine.rs" in res.output


def test_repo_map_symbols_search(sample_project: Path):
    tool = RepoMapTool()
    ctx = _make_ctx(sample_project)

    # Search class
    res = asyncio.run(tool.run({"action": "symbols", "query": "AuthService"}, ctx))
    assert not res.error
    assert "auth.py" in res.output
    assert "[class] AuthService" in res.output

    # Search interface
    res2 = asyncio.run(tool.run({"action": "symbols", "query": "UserProfile"}, ctx))
    assert not res2.error
    assert "client.ts" in res2.output
    assert "[interface] UserProfile" in res2.output

    # Search function
    res3 = asyncio.run(tool.run({"action": "symbols", "query": "fetchUser"}, ctx))
    assert not res3.error
    assert "[func] fetchUser(id)" in res3.output

    # Search rust struct
    res4 = asyncio.run(tool.run({"action": "symbols", "query": "StateMachine"}, ctx))
    assert not res4.error
    assert "[type] StateMachine" in res4.output


def test_repo_map_structural_map(sample_project: Path):
    tool = RepoMapTool()
    ctx = _make_ctx(sample_project)
    res = asyncio.run(tool.run({"action": "map"}, ctx))
    assert not res.error
    assert "Repository Map" in res.output
    assert "class AuthService" in res.output
    assert "login" in res.output
    assert "interface UserProfile" in res.output


def test_prompt_auto_injects_agents_md(tmp_path: Path):
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text("# Project Architecture\n- Core in src/\n", encoding="utf-8")

    from xu_brain.core.config import Config
    from xu_brain.core.governance import ApprovalManager
    from xu_brain.features.memory import MemoryStore
    from xu_brain.features.skills import SkillsEngine
    from xu_brain.features.tools.registry import ToolRegistry
    from xu_brain.plugins import PluginBus

    store = SessionStore(tmp_path)
    config = Config(tmp_path)
    approvals = ApprovalManager(config)
    registry = ToolRegistry(approvals)

    agent = Agent(
        store,
        tmp_path,
        None,
        registry,
        approvals,
        MemoryStore(tmp_path),
        SkillsEngine(tmp_path),
        PluginBus(tmp_path),
        config,
    )

    session = store.create(cwd=str(tmp_path))
    msgs = agent._build_messages(session)
    sys_prompt = msgs[0]["content"]

    assert "# AGENTS.md (project map)" in sys_prompt
    assert "Project Architecture" in sys_prompt
    assert "- Core in src/" in sys_prompt
