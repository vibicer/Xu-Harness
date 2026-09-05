"""Tests for the platform-dependent logic in the repo-root launcher xu.py.

xu.py lives outside the xu_brain package, so it is loaded via importlib from
a path derived relative to this file.  These tests never start the brain,
never bind :9876 and never touch ~/.xu.
"""

import importlib.util
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # brain/tests -> repo root

_spec = importlib.util.spec_from_file_location("xu_launcher", REPO / "xu.py")
xu = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(xu)


# ---------------------------------------------------------------------------
# 1. venv interpreter path
# ---------------------------------------------------------------------------

def test_venv_python_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert xu.venv_python().parts[-2:] == ("bin", "python")


def test_venv_python_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    assert xu.venv_python().parts[-2:] == ("Scripts", "python.exe")


# ---------------------------------------------------------------------------
# 2. Windows liveness never touches os.kill (there it would terminate the
#    target); it goes through OpenProcess/GetExitCodeProcess/CloseHandle.
# ---------------------------------------------------------------------------

class FakeKernel32:
    def __init__(self, handle=0x100, exit_code=259):
        self.handle = handle
        self.exit_code = exit_code
        self.opened = []
        self.closed = []

    def OpenProcess(self, access, inherit, pid):
        self.opened.append((access, inherit, pid))
        return self.handle

    def GetExitCodeProcess(self, handle, out):
        assert handle == self.handle
        out[0] = self.exit_code
        return 1

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return 1


def test_windows_alive_never_calls_os_kill(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")

    def boom(*a, **k):
        raise AssertionError("os.kill must never be used for liveness on Windows")

    monkeypatch.setattr(os, "kill", boom)
    fake = FakeKernel32(handle=0x100, exit_code=259)  # STILL_ACTIVE
    monkeypatch.setattr(xu, "_kernel32", lambda: fake)

    assert xu.process_alive(4242) is True
    assert fake.opened == [(0x1000, False, 4242)]
    assert fake.closed == [0x100]  # handle always released


def test_windows_alive_dead_process(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    fake = FakeKernel32(handle=0x100, exit_code=0)
    monkeypatch.setattr(xu, "_kernel32", lambda: fake)

    assert xu.process_alive(4242) is False
    assert fake.closed == [0x100]


def test_windows_alive_unopenable_handle(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    fake = FakeKernel32(handle=0)  # OpenProcess denied
    monkeypatch.setattr(xu, "_kernel32", lambda: fake)

    assert xu.process_alive(4242) is False
    assert fake.closed == []  # nothing to close


# ---------------------------------------------------------------------------
# 3. detached background launch
# ---------------------------------------------------------------------------

def test_detach_kwargs_posix(monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert xu.detach_kwargs() == {"start_new_session": True}


def test_detach_kwargs_windows(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    kw = xu.detach_kwargs()
    assert set(kw) == {"creationflags"}
    assert kw["creationflags"] == 0x00000008 | 0x00000200


# ---------------------------------------------------------------------------
# 4. readiness poll: TCP connect, early abort on dead child
# ---------------------------------------------------------------------------

def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_poll_ready_true_on_listening_socket():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        start = time.monotonic()
        assert xu.poll_ready("127.0.0.1", s.getsockname()[1]) is True
        assert time.monotonic() - start < 2.0


def test_poll_ready_false_when_port_closed():
    assert xu.poll_ready("127.0.0.1", _closed_port(), timeout=0.3) is False


def test_poll_ready_false_promptly_when_child_dead():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()  # reaped -> proc.poll() is not None
    start = time.monotonic()
    assert xu.poll_ready("127.0.0.1", _closed_port(), child) is False
    assert time.monotonic() - start < 2.0  # early abort, not the 5s budget


# ---------------------------------------------------------------------------
# 5. usage text + dispatch
# ---------------------------------------------------------------------------

def test_usage_lists_all_commands(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XU_DATA_HOME", str(tmp_path))  # keep mkdir off ~/.xu
    rc = xu.main([])
    out = capsys.readouterr().out
    assert rc == 1
    for cmd in ("start", "stop", "restart", "status", "logs", "test"):
        assert f"xu {cmd}" in out
    assert "XU_BRAIN_PORT" in out and "XU_DATA_HOME" in out


def test_unknown_command_prints_usage_and_fails(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XU_DATA_HOME", str(tmp_path))
    rc = xu.main(["frobnicate"])
    assert rc == 1
    assert "xu — personal AI harness" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# extras: tool resolution via shutil.which, Windows taskkill argv.
# The PATHEXT/.cmd-shim branch of shutil.which needs sys.platform=="win32"
# and _winapi, so it cannot be exercised on Linux; we cover the POSIX side.
# ---------------------------------------------------------------------------

def test_find_tool_resolves_to_absolute_path(monkeypatch, tmp_path):
    tool = tmp_path / "bun"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    found = xu.find_tool("bun")
    assert found is not None
    assert Path(found) == tool.resolve()
    assert xu.find_tool("definitely-not-a-tool") is None


def test_terminate_windows_uses_taskkill(monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    seen = []

    def fake_run(argv, **kw):
        seen.append((list(argv), kw))

        class R:
            returncode = 0

        return R()

    monkeypatch.setattr(xu.subprocess, "run", fake_run)
    xu.terminate(777)
    xu.terminate(777, force=True)
    assert seen[0][0] == ["taskkill", "/PID", "777", "/T"]
    assert seen[1][0] == ["taskkill", "/PID", "777", "/T", "/F"]
    for _argv, kw in seen:
        assert not kw.get("shell")  # never shell=True
