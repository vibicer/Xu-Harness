"""Tests for filesystem backup, rollback, and undo/redo capabilities."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from xu_brain.features.backup import BackupManager
from xu_brain.features.tools.file import FileEditTool, FileWriteTool, UndoTool
from xu_brain.features.agent.scheduler import _tool_io


def test_backup_manager_create_and_undo(tmp_path: Path) -> None:
    data_home = tmp_path / "data_home"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    mgr = BackupManager(data_home)

    target_file = cwd / "hello.txt"
    tok = mgr.record_pre_state("s1", "t1", target_file, "write", str(cwd))
    assert tok is not None
    assert not tok.existed

    target_file.write_text("hello world", encoding="utf-8")
    snap = mgr.commit_post_state(tok)
    assert snap is not None
    assert snap.status == "active"
    assert target_file.exists()

    # Undo should delete the created file
    res = mgr.undo("s1", cwd=str(cwd))
    assert res.success
    assert "hello.txt" in res.deleted
    assert not target_file.exists()

    # Redo should bring it back
    redo_res = mgr.redo("s1", cwd=str(cwd))
    assert redo_res.success
    assert "hello.txt" in redo_res.restored
    assert target_file.exists()
    assert target_file.read_text(encoding="utf-8") == "hello world"


def test_backup_manager_modify_and_undo(tmp_path: Path) -> None:
    data_home = tmp_path / "data_home"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    mgr = BackupManager(data_home)

    target_file = cwd / "code.py"
    target_file.write_text("version 1\n", encoding="utf-8")

    tok = mgr.record_pre_state("s1", "t1", target_file, "edit", str(cwd))
    assert tok is not None
    assert tok.existed

    target_file.write_text("version 2\n", encoding="utf-8")
    snap = mgr.commit_post_state(tok)
    assert snap is not None

    diff_text = mgr.diff("s1", turn_id="t1")
    assert "-version 1" in diff_text
    assert "+version 2" in diff_text

    # Undo should restore version 1
    res = mgr.undo("s1", cwd=str(cwd))
    assert res.success
    assert "code.py" in res.restored
    assert target_file.read_text(encoding="utf-8") == "version 1\n"

    # Redo should restore version 2
    redo_res = mgr.redo("s1", cwd=str(cwd))
    assert redo_res.success
    assert "code.py" in redo_res.restored
    assert target_file.read_text(encoding="utf-8") == "version 2\n"


def test_backup_manager_noop_not_committed(tmp_path: Path) -> None:
    data_home = tmp_path / "data_home"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    mgr = BackupManager(data_home)

    target_file = cwd / "same.txt"
    target_file.write_text("unchanged", encoding="utf-8")

    tok = mgr.record_pre_state("s1", "t1", target_file, "edit", str(cwd))
    # Do not change file
    snap = mgr.commit_post_state(tok)
    assert snap is None

    history = mgr.history("s1")
    assert len(history) == 0


def test_backup_manager_path_targeted_undo(tmp_path: Path) -> None:
    data_home = tmp_path / "data_home"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    mgr = BackupManager(data_home)

    f1 = cwd / "f1.txt"
    f2 = cwd / "f2.txt"
    f1.write_text("orig1\n", encoding="utf-8")
    f2.write_text("orig2\n", encoding="utf-8")

    # Turn 1 modifies f1 and f2
    tok1 = mgr.record_pre_state("s1", "t1", f1, "edit", str(cwd))
    f1.write_text("new1\n", encoding="utf-8")
    mgr.commit_post_state(tok1)

    tok2 = mgr.record_pre_state("s1", "t1", f2, "edit", str(cwd))
    f2.write_text("new2\n", encoding="utf-8")
    mgr.commit_post_state(tok2)

    # Targeted undo only reverts f1
    res = mgr.undo("s1", path="f1.txt", cwd=str(cwd))
    assert res.success
    assert f1.read_text(encoding="utf-8") == "orig1\n"
    assert f2.read_text(encoding="utf-8") == "new2\n"


def test_write_and_edit_tools_with_undo_tool(tmp_path: Path) -> None:
    data_home = tmp_path / "data_home"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    mgr = BackupManager(data_home)

    ctx = SimpleNamespace(
        data_home=data_home,
        cwd=str(cwd),
        session_id="session-test",
        turn_id="turn-1",
        backups=mgr,
        config={},
    )

    write_tool = FileWriteTool()
    edit_tool = FileEditTool()
    undo_tool = UndoTool()

    # 1. Write new file
    res = asyncio.run(
        write_tool.run({"path": "sample.py", "content": "print('v1')\n"}, ctx)
    )
    assert not res.error
    assert (cwd / "sample.py").exists()

    # 2. Edit the file in turn 2
    ctx.turn_id = "turn-2"
    patch = "[sample.py]\nPUT 1:\n+print('v2')"
    res = asyncio.run(edit_tool.run({"patch": patch}, ctx))
    assert not res.error
    assert (cwd / "sample.py").read_text(encoding="utf-8") == "print('v2')\n"

    # 3. Undo turn 2 via UndoTool
    res = asyncio.run(undo_tool.run({}, ctx))
    assert not res.error
    assert (cwd / "sample.py").read_text(encoding="utf-8") == "print('v1')\n"

    # 4. Undo turn 1 via UndoTool (should delete newly created sample.py)
    res = asyncio.run(undo_tool.run({}, ctx))
    assert not res.error
    assert not (cwd / "sample.py").exists()


def test_undo_scheduler_io() -> None:
    # With path: normal write conflict on that path
    r, w, is_barrier = _tool_io("undo", {"path": "src/app.py"}, cwd="/root")
    assert not is_barrier
    assert len(w) == 1
    assert "/root/src/app.py" in list(w)[0]

    # Without path: acts as barrier
    r, w, is_barrier = _tool_io("undo", {}, cwd="/root")
    assert is_barrier
    assert len(w) == 0



def test_rpc_methods(tmp_path: Path) -> None:
    from xu_brain.core.runtime import build_app
    from xu_brain.core.contract import RpcError

    data_home = tmp_path / "data_home"
    app = build_app(data_home)
    session = app.sessions.create(cwd=str(tmp_path))

    # Test initial history is empty
    hist = asyncio.run(app.dispatch("fs.history", {"session_id": session.id}))
    assert hist == {"history": []}

    # Test undo on empty session
    res = asyncio.run(app.dispatch("fs.undo", {"session_id": session.id}))
    assert res["ok"]
    assert res["restored"] == []

    # Test session not found error
    with pytest.raises(RpcError) as exc_info:
        asyncio.run(app.dispatch("fs.undo", {"session_id": "nonexistent"}))
    assert exc_info.value.code == -32002


def test_slash_undo_in_session_send(tmp_path: Path) -> None:
    from xu_brain.core.runtime import build_app

    data_home = tmp_path / "data_home"
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    app = build_app(data_home)
    session = app.sessions.create(cwd=str(cwd))

    target = cwd / "created.txt"
    # Take a backup and write file
    tok = app.backups.record_pre_state(session.id, "t1", target, "write", str(cwd))
    target.write_text("content", encoding="utf-8")
    app.backups.commit_post_state(tok)
    assert target.exists()

    # User sends /undo
    send_res = asyncio.run(
        app.dispatch("session.send", {"id": session.id, "text": "/undo"})
    )
    assert send_res["turn_id"].startswith("undo-")
    assert not target.exists()

    # Verify transcript messages
    display = app.sessions.display(session.id)
    assert len(display) >= 2
    assert display[-2]["content"] == "/undo"
    assert "Undo File Changes" in display[-1]["content"]

    # User sends /redo
    redo_res = asyncio.run(
        app.dispatch("session.send", {"id": session.id, "text": "/redo"})
    )
    assert redo_res["turn_id"].startswith("redo-")
    assert target.exists()
    assert target.read_text(encoding="utf-8") == "content"
