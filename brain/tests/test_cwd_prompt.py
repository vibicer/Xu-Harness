"""session cwd in the system prompt — runnable check.

    python -m pytest tests/test_cwd_prompt.py -q

The harness resolves file/glob/eval against ``session.cwd`` and spawns the bash
tool there, but nothing used to tell the model what that path was. Without the
stamp a model falls back to whatever static directory its persona claims and
reads the wrong tree.

- the stamp carries the session's real cwd, not the brain's process cwd
- two sessions with different cwds get different stamps (not hardcoded)
- the stamp survives a full context budget squeeze (hard-reserved)
"""
import asyncio

import pytest


@pytest.fixture()
def data_home(tmp_path):
    return tmp_path / "xu"


def _prompt(app, cwd):
    from xu_brain.features.session import Message

    async def exercise():
        session = app.sessions.create(cwd=str(cwd))
        app.sessions.append(session.id, Message(role="user", content="hi"))
        return app.agent._build_messages(app.sessions.get(session.id))[0]["content"]

    return asyncio.run(exercise())


def test_stamp_carries_session_cwd(data_home, tmp_path):
    from xu_brain.core.runtime import build_app

    app = build_app(data_home)
    project = tmp_path / "Project AI" / "Kata"  # a space, as in the real path
    project.mkdir(parents=True)

    content = _prompt(app, project)
    assert f"# cwd (session)\n{project}" in content


def test_stamp_is_per_session_not_hardcoded(data_home, tmp_path):
    from xu_brain.core.runtime import build_app

    app = build_app(data_home)
    a, b = tmp_path / "alpha", tmp_path / "beta"
    a.mkdir()
    b.mkdir()

    assert str(a) in _prompt(app, a)
    assert str(b) in _prompt(app, b)


def test_stamp_survives_zero_context_budget(data_home, tmp_path):
    """Budget-exempt: a tight window trims skills and memory, never the cwd."""
    from xu_brain.core.runtime import build_app

    app = build_app(data_home)
    app.config.set("context_skill_budget", 1)
    project = tmp_path / "squeezed"
    project.mkdir()

    assert str(project) in _prompt(app, project)
