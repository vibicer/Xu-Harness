"""Windows/POSIX seam for the long-lived children: `bash` and `eval`.

`bash` used to hardcode `/bin/bash`, `start_new_session=True` and `os.killpg`,
all three POSIX-only, so it could not start on Windows at all; `eval` shared
the last two and blew up with AttributeError on its timeout path. The platform
moves now live in `_process`. These tests flip `_process.IS_WINDOWS` so the
Windows branches are covered from a POSIX host.

Run: brain/.venv/bin/python -m pytest tests/test_terminal_platform.py -q
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from xu_brain.core.governance import ApprovalManager
from xu_brain.features.tools import _process
from xu_brain.features.tools import eval_tool as E
from xu_brain.features.tools import terminal as T
from xu_brain.features.tools.base import ToolContext
from xu_brain.features.tools.registry import ToolRegistry

# A Windows environment as Python reports it, with no bash anywhere.
WIN_ENV = {
    "ProgramFiles": r"C:\Program Files",
    "ProgramFiles(x86)": r"C:\Program Files (x86)",
    "LocalAppData": r"C:\Users\dev\AppData\Local",
    "SystemRoot": r"C:\Windows",
    "PATH": r"C:\Windows\System32",
}


@pytest.fixture
def as_windows(monkeypatch):
    monkeypatch.setattr(_process, "IS_WINDOWS", True)


@pytest.fixture
def as_posix(monkeypatch):
    monkeypatch.setattr(_process, "IS_WINDOWS", False)


def _ctx(tmp_path: Path) -> ToolContext:
    registry = ToolRegistry(ApprovalManager("yolo"), output_cap=10_000)
    return ToolContext(
        data_home=tmp_path,
        session_id="term-test",
        turn_id="t0",
        cwd=str(tmp_path),
        agent=None,  # type: ignore[arg-type]
        events=None,  # type: ignore[arg-type]
        approvals=registry.approvals,
        providers=None,  # type: ignore[arg-type]
        memory=None,  # type: ignore[arg-type]
        skills=None,  # type: ignore[arg-type]
        flat_plugins=None,  # type: ignore[arg-type]
        config={},
    )


# --- which bash ------------------------------------------------------------


def test_git_bash_beats_the_wsl_shim(as_windows, monkeypatch):
    """`System32\\bash.exe` is the WSL launcher: it runs, but resolves /mnt/c
    paths while every other tool uses C:\\, so a session would silently
    disagree with itself. It must never be picked implicitly."""
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: r"C:\Windows\System32\bash.exe")

    candidates = T._windows_bash_candidates(WIN_ENV)

    assert r"C:\Program Files\Git\bin\bash.exe" in candidates
    assert not any("System32" in c for c in candidates), candidates


def test_a_non_system32_bash_on_path_is_still_a_candidate(as_windows, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: r"C:\msys64\usr\bin\bash.exe")

    assert r"C:\msys64\usr\bin\bash.exe" in T._windows_bash_candidates(WIN_ENV)


def test_xu_bash_wins_and_loses_its_quotes(as_windows, monkeypatch):
    """A `setx`-style value arrives with literal quotes attached."""
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    env = {**WIN_ENV, "XU_BASH": '"D:\\tools\\bash.exe"'}

    assert T._windows_bash_candidates(env)[0] == r"D:\tools\bash.exe"


def test_a_quoted_program_files_still_builds_a_usable_path(as_windows, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    env = {**WIN_ENV, "ProgramFiles": '"C:\\Program Files"'}

    assert r"C:\Program Files\Git\bin\bash.exe" in T._windows_bash_candidates(env)


def test_no_bash_resolves_to_none_not_an_exception(as_windows, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(T, "_runnable", lambda _p: False)

    assert T._bash_path(WIN_ENV) is None


def test_runnable_accepts_a_dangling_symlink(tmp_path):
    """A Windows app-execution alias stats like a link to nowhere; accepting it
    makes a broken install fail loudly at spawn rather than silently falling
    through to a different shell."""
    link = tmp_path / "bash.exe"
    link.symlink_to(tmp_path / "missing.exe")

    assert T._runnable(str(link))
    assert not T._runnable(str(tmp_path / "absent.exe"))
    assert not T._runnable("")


def test_windows_sentinel_asks_for_a_native_cwd(as_windows):
    """`$PWD` under Git Bash is `/c/Users/...`, which cannot be handed back as
    a spawn cwd."""
    assert "pwd -W" in T._pwd_expr()


def test_posix_sentinel_uses_pwd_directly(as_posix):
    assert T._pwd_expr() == '"$PWD"'


def test_the_windows_hint_names_the_fix(as_windows):
    assert "Git for Windows" in T._no_bash_message()


# --- spawn isolation -------------------------------------------------------


def test_isolation_uses_creationflags_on_windows(as_windows):
    """`start_new_session` is silently ignored on Windows, so passing it would
    leave no group to kill."""
    kwargs = _process.spawn_isolation()

    assert "start_new_session" not in kwargs
    assert kwargs["creationflags"] == _process.CREATE_NEW_PROCESS_GROUP


def test_isolation_uses_a_new_session_on_posix(as_posix):
    assert _process.spawn_isolation() == {"start_new_session": True}


# --- teardown --------------------------------------------------------------


def test_windows_teardown_shells_taskkill_with_the_tree_flag(as_windows, monkeypatch):
    seen: list[tuple[str, ...]] = []

    async def fake_exec(*cmd, **_kw):
        seen.append(cmd)

        class _Done:
            async def wait(self):
                return 0

        return _Done()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    # force=False must still use /F: Windows maps every signal to
    # TerminateProcess, so a graceful ladder does not exist there.
    asyncio.run(_process.kill_tree(4321, force=False))

    assert seen == [("taskkill", "/PID", "4321", "/T", "/F")]


def test_teardown_survives_a_missing_taskkill(as_windows, monkeypatch):
    async def boom(*_a, **_k):
        raise OSError("taskkill not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)

    asyncio.run(_process.kill_tree(1, force=True))  # must not raise


def test_posix_teardown_signals_the_process_group(as_posix, monkeypatch):
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(os, "killpg", lambda pgid, sig: killed.append((pgid, sig)))

    asyncio.run(_process.kill_tree(77, force=True))
    asyncio.run(_process.kill_tree(77, force=False))

    assert [pgid for pgid, _ in killed] == [77, 77]
    assert killed[0][1] != killed[1][1]      # SIGKILL vs SIGTERM


def test_posix_teardown_ignores_an_already_dead_group(as_posix, monkeypatch):
    monkeypatch.setattr(os, "getpgid", lambda _pid: 1)

    def gone(*_a):
        raise ProcessLookupError

    monkeypatch.setattr(os, "killpg", gone)

    asyncio.run(_process.kill_tree(9, force=True))  # must not raise


def test_eval_kills_through_the_shared_helper(as_windows, monkeypatch):
    """The `eval` kernel shared bash's POSIX-only teardown: on Windows its
    timeout path raised AttributeError on `os.killpg`."""
    calls: list[int] = []

    async def fake_kill(pid, *, force):
        calls.append(pid)

    monkeypatch.setattr(_process, "kill_tree", fake_kill)

    class _Live:
        pid = 555
        returncode = None

        async def wait(self):
            return 0

    kernel = E.EvalKernel("/tmp")
    kernel._proc = _Live()  # type: ignore[assignment]

    asyncio.run(kernel.kill())

    assert calls == [555]
    assert kernel._proc is None


# --- the POSIX path must be untouched --------------------------------------


def test_missing_bash_is_reported_not_raised(monkeypatch, tmp_path):
    """The agent loop must see a message, not a FileNotFoundError."""
    monkeypatch.setattr(T, "_bash_path", lambda *_a, **_k: None)
    T._SHELLS.pop("term-test", None)

    result = asyncio.run(T.bash.run({"command": "echo hi"}, _ctx(tmp_path)))

    assert result.error
    assert "no bash binary found" in result.output
    T._SHELLS.pop("term-test", None)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell behaviour")
def test_the_real_shell_still_runs_and_keeps_its_cwd(tmp_path):
    ctx = _ctx(tmp_path)
    T._SHELLS.pop("term-test", None)
    (tmp_path / "sub").mkdir()

    async def scenario():
        first = await T.bash.run({"command": "cd sub && pwd"}, ctx)
        assert not first.error, first.output
        assert "sub" in first.output
        # cwd persists into the next call
        second = await T.bash.run({"command": 'basename "$PWD"'}, ctx)
        assert "sub" in second.output, second.output
        await T._SHELLS["term-test"].close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    T._SHELLS.pop("term-test", None)


@pytest.mark.skipif(os.name == "nt", reason="POSIX process groups")
def test_a_timeout_still_kills_the_shell_and_recovers(tmp_path):
    """The teardown refactor must keep rc=124 and a usable next call."""
    ctx = _ctx(tmp_path)
    T._SHELLS.pop("term-test", None)

    async def scenario():
        timed_out = await T.bash.run({"command": "sleep 30", "timeout": 1}, ctx)
        assert "exit 124" in timed_out.output, timed_out.output
        recovered = await T.bash.run({"command": "echo alive"}, ctx)
        assert "alive" in recovered.output, recovered.output
        await T._SHELLS["term-test"].close()

    asyncio.run(asyncio.wait_for(scenario(), timeout=90))
    T._SHELLS.pop("term-test", None)
