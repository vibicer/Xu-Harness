"""Unit and integration tests for the git toolset."""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from xu_brain.core.governance import ApprovalLevel, ApprovalManager
from xu_brain.features.tools import all_tools
from xu_brain.features.tools.git_tool import GitTool, git


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create a minimal clean Git repository."""
    subprocess.run(["git", "init", "-b", "main", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test User"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"], check=True)

    readme = tmp_path / "README.md"
    readme.write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "Initial commit"], check=True)
    return tmp_path


def _make_ctx(cwd: Path) -> SimpleNamespace:
    return SimpleNamespace(cwd=str(cwd), config={})


def test_git_tool_in_all_tools():
    tools = {t.name: t for t in all_tools()}
    assert "git" in tools
    assert tools["git"].toolset == "git"
    assert tools["git"].approval is ApprovalLevel.RISKY


def test_git_status_clean(repo: Path):
    tool = GitTool()
    ctx = _make_ctx(repo)
    res = asyncio.run(tool.run({"action": "status"}, ctx))
    assert not res.error
    assert "working tree clean" in res.output or "main" in res.output


def test_git_status_dirty(repo: Path):
    tool = GitTool()
    ctx = _make_ctx(repo)
    (repo / "newfile.txt").write_text("hello", encoding="utf-8")
    res = asyncio.run(tool.run({"action": "status"}, ctx))
    assert not res.error
    assert "newfile.txt" in res.output


def test_git_diff_working_tree(repo: Path):
    tool = GitTool()
    ctx = _make_ctx(repo)
    (repo / "README.md").write_text("# Test Repo Modified\n", encoding="utf-8")
    res = asyncio.run(tool.run({"action": "diff"}, ctx))
    assert not res.error
    assert "+# Test Repo Modified" in res.output


def test_git_add_and_diff_staged(repo: Path):
    tool = GitTool()
    ctx = _make_ctx(repo)
    new_f = repo / "hello.py"
    new_f.write_text("print('hello')\n", encoding="utf-8")

    # Add
    add_res = asyncio.run(tool.run({"action": "add", "paths": ["hello.py"]}, ctx))
    assert not add_res.error
    assert "hello.py" in add_res.output

    # Diff staged
    diff_res = asyncio.run(tool.run({"action": "diff", "staged": True}, ctx))
    assert not diff_res.error
    assert "hello.py" in diff_res.output
    assert "+print('hello')" in diff_res.output


def test_git_restore_staged(repo: Path):
    tool = GitTool()
    ctx = _make_ctx(repo)
    new_f = repo / "to_unstage.txt"
    new_f.write_text("temp", encoding="utf-8")

    asyncio.run(tool.run({"action": "add", "paths": ["to_unstage.txt"]}, ctx))
    # Unstage via restore
    rest_res = asyncio.run(tool.run({"action": "restore", "staged": True, "paths": ["to_unstage.txt"]}, ctx))
    assert not rest_res.error
    assert "Restored 1 path(s) from staging" in rest_res.output

    # Verify no longer in staged diff
    diff_res = asyncio.run(tool.run({"action": "diff", "staged": True}, ctx))
    assert "to_unstage.txt" not in diff_res.output


def test_git_commit_flow(repo: Path):
    tool = GitTool()
    ctx = _make_ctx(repo)
    f = repo / "feature.txt"
    f.write_text("awesome", encoding="utf-8")

    asyncio.run(tool.run({"action": "add", "paths": ["feature.txt"]}, ctx))
    commit_res = asyncio.run(tool.run({"action": "commit", "message": "feat: add feature"}, ctx))
    assert not commit_res.error

    log_res = asyncio.run(tool.run({"action": "log", "limit": 2}, ctx))
    assert not log_res.error
    assert "feat: add feature" in log_res.output


def test_git_branch_operations(repo: Path):
    tool = GitTool()
    ctx = _make_ctx(repo)

    # create branch
    b_res = asyncio.run(tool.run({"action": "branch", "subaction": "create", "branch_name": "feat/xyz"}, ctx))
    assert not b_res.error

    # list branches
    l_res = asyncio.run(tool.run({"action": "branch", "subaction": "list"}, ctx))
    assert not l_res.error
    assert "feat/xyz" in l_res.output

    # switch to it
    s_res = asyncio.run(tool.run({"action": "branch", "subaction": "switch", "branch_name": "feat/xyz"}, ctx))
    assert not s_res.error

    # verify current branch
    st_res = asyncio.run(tool.run({"action": "status"}, ctx))
    assert "feat/xyz" in st_res.output


def test_git_governance_levels():
    gov = ApprovalManager("manual")

    # Read actions should be NEVER
    assert gov.level_for("git", {"action": "status"}, ApprovalLevel.RISKY) is ApprovalLevel.NEVER
    assert gov.level_for("git", {"action": "diff"}, ApprovalLevel.RISKY) is ApprovalLevel.NEVER
    assert gov.level_for("git", {"action": "log"}, ApprovalLevel.RISKY) is ApprovalLevel.NEVER
    assert gov.level_for("git", {"action": "show"}, ApprovalLevel.RISKY) is ApprovalLevel.NEVER
    assert gov.level_for("git", {"action": "blame"}, ApprovalLevel.RISKY) is ApprovalLevel.NEVER
    assert gov.level_for("git", {"action": "branch", "subaction": "list"}, ApprovalLevel.RISKY) is ApprovalLevel.NEVER
    assert gov.level_for("git", {"action": "stash", "subaction": "list"}, ApprovalLevel.RISKY) is ApprovalLevel.NEVER

    # Modifying actions should remain RISKY (or prompt)
    assert gov.level_for("git", {"action": "add"}, ApprovalLevel.RISKY) is ApprovalLevel.RISKY
    assert gov.level_for("git", {"action": "commit"}, ApprovalLevel.RISKY) is ApprovalLevel.RISKY
    assert gov.level_for("git", {"action": "restore"}, ApprovalLevel.RISKY) is ApprovalLevel.RISKY
    assert gov.level_for("git", {"action": "branch", "subaction": "create"}, ApprovalLevel.RISKY) is ApprovalLevel.RISKY

    # Destructive actions escalate to ALWAYS
    assert gov.level_for("git", {"action": "clean"}, ApprovalLevel.RISKY) is ApprovalLevel.ALWAYS
    assert gov.level_for("git", {"action": "reset", "hard": True}, ApprovalLevel.RISKY) is ApprovalLevel.ALWAYS
    assert gov.level_for("git", {"action": "branch", "subaction": "delete", "force": True}, ApprovalLevel.RISKY) is ApprovalLevel.ALWAYS
