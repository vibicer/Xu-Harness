"""Session-scoped JSON-RPC handlers: one session's state, cwd, git, todo."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...core.contract import RpcError
from ...core.notify import notify
from . import DEFAULT_CWD

if TYPE_CHECKING:
    from ...core.runtime import App


_AHEAD_BEHIND_RE = re.compile(r"\b(ahead|behind) (\d+)")


def _parse_branch_header(head: str) -> tuple[str, str | None, int, int]:
    """Parse a `git status --branch` header body into (branch, upstream, ahead, behind).

    Git writes divergence as ``main...origin/main [ahead 1, behind 2]`` — words
    and spaces, no ``=``. A branch with no upstream has no ``...`` section.
    """
    branch, sep, rest = head.partition("...")
    if not sep:
        return head.strip(), None, 0, 0
    upstream, _, tracking = rest.partition("[")
    counts = dict.fromkeys(("ahead", "behind"), 0)
    for word, num in _AHEAD_BEHIND_RE.findall(tracking):
        counts[word] = int(num)
    return branch.strip(), upstream.strip() or None, counts["ahead"], counts["behind"]


def _git_info(cwd: str) -> dict[str, Any]:
    """Best-effort git status snapshot for a directory (never raises).

    One `git status --porcelain=v1 --branch` call yields branch, tracking
    ahead/behind, and staged/unstaged/untracked counts; one `log -1` adds
    the last commit anchor. Kept cheap: called on every state.get/state.set_cwd."""
    try:
        import subprocess

        def _run(*args: str) -> str:
            r = subprocess.run(
                ["git", "-C", cwd, *args],
                capture_output=True, text=True, timeout=1,
            )
            return r.stdout.strip()

        if _run("rev-parse", "--is-inside-work-tree") != "true":
            return {"repo": False}
        st = _run("status", "--porcelain=v1", "--branch")
        branch, upstream, ahead, behind = "?", None, 0, 0
        staged = unstaged = untracked = 0
        for line in st.splitlines():
            if line.startswith("##"):
                branch, upstream, ahead, behind = _parse_branch_header(line[2:].strip())
                continue
            if line.startswith("??"):
                untracked += 1
                continue
            if len(line) >= 2:
                x, y = line[0], line[1]
                if x not in (" ", "?"):
                    staged += 1
                if y not in (" ", "?"):
                    unstaged += 1
        # last commit anchor: short sha + subject + epoch seconds
        last = _run("log", "-1", "--format=%h%x1f%s%x1f%ct")
        sha, subject, ts = (last.split("\x1f") + ["", "", ""])[:3]
        return {
            "repo": True,
            "branch": branch,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "staged": staged,
            "unstaged": unstaged,
            "untracked": untracked,
            "dirty": bool(staged or unstaged or untracked),
            "last": {"sha": sha, "subject": subject, "ts": int(ts) if ts.isdigit() else None},
        }
    except Exception:
        return {"repo": False}


def register(app: App) -> None:
    @app.register("git.detail")
    async def git_detail(params: dict[str, Any]) -> dict[str, Any]:
        """Full git snapshot for the git panel: status summary + changed files
        + recent commits. Best-effort: a non-repo or missing git yields
        repo=False and the shell renders an empty state."""
        sid = params.get("session_id")
        session = app.sessions.get(sid) if sid else None
        cwd = session.cwd if session else DEFAULT_CWD
        info = _git_info(cwd)
        if not info.get("repo"):
            return {"repo": False, "cwd": cwd}
        import subprocess

        def _run(*args: str) -> str:
            r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=2)
            return r.stdout.strip()

        files = []
        try:
            for line in _run("status", "--porcelain=v1").splitlines():
                if len(line) < 4:
                    continue
                xy, path = line[:2], line[3:]
                state = "untracked" if xy == "??" else "added" if xy[0] not in (" ", "?") else "modified" if xy[1] not in (" ", "?") else "staged"
                files.append({"state": state, "path": path.strip('"')})
        except Exception:  # noqa: BLE001 — listing is best-effort
            pass
        commits: list[dict[str, Any]] = []
        try:
            raw = _run("log", "-10", "--format=%h%x1f%s%x1f%ct%x1f%an")
            for line in raw.splitlines():
                sha, subject, ts, author = (line.split("\x1f") + ["", "", "", ""])[:4]
                commits.append({"sha": sha, "subject": subject, "ts": int(ts) if ts.isdigit() else None, "author": author})
        except Exception:  # noqa: BLE001
            pass
        return {**info, "cwd": cwd, "files": files[:50], "commits": commits}

    @app.register("session.list")
    async def session_list(params: dict[str, Any]) -> dict[str, Any]:
        sessions = app.sessions.list()
        for s in sessions:
            s["model"] = app.config.session_model(s["id"])
        return {"sessions": sessions}

    @app.register("session.create")
    async def session_create(params: dict[str, Any]) -> dict[str, Any]:
        session = app.sessions.create(params.get("cwd"))
        # Enforce the saved-session cap: oldest is replaced when the limit is exceeded.
        try:
            app.sessions.prune(keep=int(app.config.get("prune_keep", 30)))
        except Exception:  # noqa: BLE001 — pruning is best-effort
            pass
        model = params.get("model")
        if model:
            app.config.set_session_model(session.id, str(model))
        # Carry the pre-session persona pick onto the new session; without this
        # the first turn resolves to SOUL.md/default (persona set with no
        # session_id is dropped by state.set_persona).
        persona = params.get("persona")
        if persona:
            app.config.set_session_persona(session.id, str(persona))
        await notify.emit("session.updated", session_id=session.id)
        return {"session": session.to_dict()}

    @app.register("session.get")
    async def session_get(params: dict[str, Any]) -> dict[str, Any]:
        session = app.sessions.get(params["id"])
        if session is None:
            raise RpcError(-32002, "session not found", {"session_id": params["id"]})
        return {
            "session": {
                **session.to_dict(),
                "model": app.config.session_model(session.id),
                "persona": app.config.session_persona(session.id),
            },
            # Frontend transcript comes from the append-only display table, not
            # the model-visible context: compaction rewrites the latter, so
            # reading it here is what used to make old turns vanish.
            "messages": app.sessions.display(session.id) or [m.to_dict() for m in session.messages],
            # in-flight turn snapshot so a (re)opened tab resumes mid-stream
            "live": app.agent.live_draft(session.id),
        }

    @app.register("session.delete")
    async def session_delete(params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("id") or "")
        if not session_id or app.sessions.get(session_id) is None:
            raise RpcError(-32002, "session not found", {"session_id": session_id})
        if session_id in app.agent._turns and not app.agent._turns[session_id].done():
            raise RpcError(-32004, "turn in progress", {"session_id": session_id})
        app.sessions.delete(session_id)
        app.config.delete_session(session_id)
        app.approvals.forget_session(session_id)
        return {}

    @app.register("session.compress")
    async def session_compress(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("id") or "")
        if not sid or app.sessions.get(sid) is None:
            raise RpcError(-32002, "session not found", {"session_id": sid})
        # Run off the request path: the summarizer is a long streaming call, and
        # awaiting it inline would block this websocket's read loop — freezing
        # navigation and every other session until it finishes. The result is
        # pushed back via the `compaction.done` event instead.
        async def _run() -> None:
            await app.agent.compress_session(sid)
        asyncio.create_task(_run())
        return {"ok": True, "async": True}

    @app.register("session.title")
    async def session_title(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("id") or "")
        if not sid or app.sessions.get(sid) is None:
            raise RpcError(-32002, "session not found", {"session_id": sid})
        return {"title": app.sessions.set_title(sid, params.get("title", "session"))}

    @app.register("session.send")
    async def session_send(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("id") or "")
        text = params.get("text")
        if not sid or app.sessions.get(sid) is None:
            raise RpcError(-32002, "session not found", {"session_id": sid})
        if not isinstance(text, str):
            raise RpcError(-32602, "text required and must be a string")
        # Auto-title a fresh session from the first user message.
        session = app.sessions.get(sid)
        if session is not None and session.title == "new session":
            first = text.strip().splitlines()[0] if text.strip() else ""
            sentence = re.split(r"(?<=[.!?])\s", first)[0] if first else ""
            title = (sentence or first or "session")[:60].strip()
            if title:
                app.sessions.set_title(session.id, title)
                await notify.emit("session.updated", session_id=session.id)
        turn_id, queued_id = await app.agent.send(sid, text, images=params.get("images"))
        return {"turn_id": turn_id, "queued_id": queued_id}

    @app.register("session.queue.cancel")
    async def session_queue_cancel(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("id") or "")
        queued_id = str(params.get("queued_id") or "")
        if not sid or app.sessions.get(sid) is None:
            raise RpcError(-32002, "session not found", {"session_id": sid})
        cancelled = app.agent.cancel_queued(sid, queued_id)
        if cancelled:
            await notify.emit(
                "queue.cancelled", session_id=sid, queued_id=queued_id,
            )
        return {"cancelled": cancelled}

    @app.register("session.stop")
    async def session_stop(params: dict[str, Any]) -> dict[str, Any]:
        await app.agent.stop(params["id"])
        return {}

    @app.register("workspace.set_cwd")
    async def set_cwd(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "")
        cwd = params.get("cwd")
        if not sid or app.sessions.get(sid) is None:
            raise RpcError(-32002, "session not found", {"session_id": sid})
        if not isinstance(cwd, str) or not cwd:
            raise RpcError(-32602, "cwd required and must be a string")
        p = Path(cwd).expanduser()
        if not p.is_dir():
            raise RpcError(-32602, "cwd must be an existing directory", {"cwd": cwd})
        cwd = app.sessions.set_cwd(sid, str(p))
        git = _git_info(cwd)
        await notify.emit("state.updated", session_id=sid, cwd=cwd, git=git)
        return {"cwd": cwd, "git": git}

    @app.register("fs.list")
    async def fs_list(params: dict[str, Any]) -> dict[str, Any]:
        """List child directories of a path for the CWD explorer modal."""
        raw = params.get("path") or DEFAULT_CWD
        try:
            p = Path(raw).expanduser().resolve()
        except (OSError, ValueError):
            p = Path(DEFAULT_CWD)
        if not p.is_dir():
            p = Path(DEFAULT_CWD)
        try:
            dirs = sorted(
                (e.name for e in p.iterdir() if e.is_dir()),
                key=str.casefold,
            )
        except OSError:
            dirs = []
        return {"path": str(p), "dirs": dirs}

    @app.register("state.get")
    async def state_get(params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("session_id")
        session = app.sessions.get(sid) if sid else None
        cwd = session.cwd if session else DEFAULT_CWD
        return {
            "model": app.config.session_model(sid) if sid else None,
            "persona": app.config.session_persona(sid) if sid else None,
            "rules": app.config.session_rules(sid) if sid else [],
            "preset": app.presets.get(app.config.session_preset(sid)).to_dict() if (sid and app.config.session_preset(sid)) else None,
            # Real per-session estimate (same math as context.updated pushes)
            **(app.agent.context_usage(sid) if sid else {"context": 0, "tokens": None}),
            "cwd": cwd,
            "git": _git_info(cwd),
            "approval_mode": app.config.get("approval_mode", "manual"),
        }

    @app.register("state.set_model")
    async def state_set_model(params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("session_id")
        model = params.get("model")
        provider = params.get("provider")
        if sid:
            app.config.set_session_model(sid, model)
            # The explicit provider (when the UI qualifies a model pick) pins
            # routing so same-id models across providers resolve correctly.
            if provider is not None or model is None:
                app.config.set_session_provider(sid, provider or None)
        await notify.emit("state.updated", session_id=sid, model=model)
        return {"model": model, "provider": provider}

    @app.register("state.set_persona")
    async def state_set_persona(params: dict[str, Any]) -> dict[str, Any]:
        sid = params.get("session_id")
        persona = params.get("persona") or None
        if sid:
            app.config.set_session_persona(sid, persona)
        await notify.emit("state.updated", session_id=sid, persona=persona)
        return {"persona": persona}

    @app.register("state.set_rules")
    async def state_set_rules(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "")
        rules = params.get("rules")
        if not sid:
            raise RpcError(-32602, "session_id is required")
        if not isinstance(rules, list) or not all(isinstance(rule, str) for rule in rules):
            raise RpcError(-32602, "rules must be a list of strings")
        if app.sessions.get(sid) is None:
            raise RpcError(-32002, "session not found", {"session_id": sid})
        app.config.set_session_rules(sid, rules)
        clean = app.config.session_rules(sid)
        await notify.emit("state.updated", session_id=sid, rules=clean)
        return {"rules": clean}

    @app.register("session.set_preset")
    async def session_set_preset(params: dict[str, Any]) -> dict[str, Any]:
        sid = str(params.get("session_id") or "")
        pid = params.get("preset_id") or None
        if not sid:
            raise RpcError(-32602, "session_id is required")
        if app.sessions.get(sid) is None:
            raise RpcError(-32002, "session not found", {"session_id": sid})
        if pid is not None and app.presets.get(pid) is None:
            raise RpcError(-32002, "preset not found", {"preset_id": pid})
        app.config.set_session_preset(sid, pid)
        await notify.emit("state.updated", session_id=sid, preset_id=pid)
        return {"preset_id": pid}

    @app.register("todo.get")
    async def todo_get(params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "")
        if not session_id or app.sessions.get(session_id) is None:
            raise RpcError(-32002, "session not found", {"session_id": session_id})
        f = app.data_home / "sessions" / f"{session_id}.todo.json"
        if not f.exists():
            return {"phases": []}
        try:
            return json.loads(f.read_text("utf-8"))
        except (json.JSONDecodeError, OSError):
            return {"phases": []}
