"""git toolset — repository inspection and version control operations.

Provides structured, governed Git operations for coding agents:
- Inspect: status, diff, log, show, blame (read-only; approval NEVER in governance)
- Stage/Restore: add, restore (approval RISKY)
- Commit: commit (approval RISKY)
- Branch: branch, checkout (list is NEVER; create/switch/delete is RISKY)
- Stash: stash (list/show is NEVER; push/pop/drop is RISKY)
"""
from __future__ import annotations

import asyncio
import shutil
from typing import Any

from ...core.governance import ApprovalLevel
from .base import Tool, ToolContext, ToolResult


async def _run_git(
    cmd_args: list[str],
    cwd: str,
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    """Run a git subcommand in `cwd` and return (returncode, stdout, stderr)."""
    git_bin = shutil.which("git") or "git"
    try:
        proc = await asyncio.create_subprocess_exec(
            git_bin,
            *cmd_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        code = proc.returncode if proc.returncode is not None else -1
        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")
        return code, stdout, stderr
    except FileNotFoundError:
        return -1, "", "git executable not found on PATH"
    except asyncio.TimeoutError:
        return -1, "", f"git timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001
        return -1, "", str(exc)


class GitTool(Tool):
    name = "git"
    toolset = "git"
    description = (
        "Run governed Git version-control operations. Actions: status, diff, log, show, "
        "add, restore, commit, branch, checkout, stash, blame. Returns clean structured text."
    )
    approval = ApprovalLevel.RISKY
    schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "status",
                    "diff",
                    "log",
                    "show",
                    "add",
                    "restore",
                    "commit",
                    "branch",
                    "checkout",
                    "stash",
                    "blame",
                ],
                "description": "Git operation to perform.",
            },
            "path": {
                "type": "string",
                "description": "Target file or directory path (for diff, log, show, blame).",
            },
            "paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "List of file paths (for add, restore, diff, checkout).",
            },
            "message": {
                "type": "string",
                "description": "Commit message or stash description.",
            },
            "staged": {
                "type": "boolean",
                "description": "If true, operate on staged changes (diff --staged, restore --staged).",
                "default": False,
            },
            "revision": {
                "type": "string",
                "description": "Commit SHA, tag, branch, or range (e.g. 'HEAD~1', 'main..HEAD').",
            },
            "limit": {
                "type": "integer",
                "description": "Max commits to display in log (default: 10).",
                "default": 10,
            },
            "stat": {
                "type": "boolean",
                "description": "If true, include diffstat / summary numbers.",
                "default": False,
            },
            "subaction": {
                "type": "string",
                "description": "Sub-action for branch (list, create, switch, delete) or stash (push, pop, list, show, drop).",
            },
            "branch_name": {
                "type": "string",
                "description": "Target branch name for branch operations.",
            },
            "lines": {
                "type": "string",
                "description": "Line range for blame (e.g. '1,50').",
            },
            "amend": {
                "type": "boolean",
                "description": "If true, amend previous commit with current staged changes.",
                "default": False,
            },
            "verbose": {
                "type": "boolean",
                "description": "If true, show verbose status output.",
                "default": False,
            },
            "force": {
                "type": "boolean",
                "description": "Force flag (e.g. force delete branch -D).",
                "default": False,
            },
        },
        "required": ["action"],
    }
    output_schema = None

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        action = str(args.get("action", "")).strip().lower()
        if not action:
            return ToolResult.err("action required (e.g. status, diff, log, add, commit)")

        cwd = ctx.cwd

        if action == "status":
            return await self._action_status(args, cwd)
        if action == "diff":
            return await self._action_diff(args, cwd)
        if action == "log":
            return await self._action_log(args, cwd)
        if action == "show":
            return await self._action_show(args, cwd)
        if action == "add":
            return await self._action_add(args, cwd)
        if action == "restore":
            return await self._action_restore(args, cwd)
        if action == "commit":
            return await self._action_commit(args, cwd)
        if action == "branch":
            return await self._action_branch(args, cwd)
        if action == "checkout":
            return await self._action_checkout(args, cwd)
        if action == "stash":
            return await self._action_stash(args, cwd)
        if action == "blame":
            return await self._action_blame(args, cwd)

        return ToolResult.err(f"unrecognized git action: {action}")

    async def _action_status(self, args: dict[str, Any], cwd: str) -> ToolResult:
        verbose = bool(args.get("verbose", False))
        cmd = ["status"] if verbose else ["status", "--short", "--branch"]
        code, stdout, stderr = await _run_git(cmd, cwd)
        if code != 0:
            return ToolResult.err(stderr.strip() or stdout.strip() or "git status failed")
        out = stdout.strip()
        lines = out.splitlines()
        # In short mode, if there's only the branch header, append a clear clean note
        if not verbose and len(lines) <= 1:
            out += "\nnothing to commit, working tree clean"
        return ToolResult.ok(out, raw={"branch_lines": len(lines)})

    async def _action_diff(self, args: dict[str, Any], cwd: str) -> ToolResult:
        staged = bool(args.get("staged", False))
        stat = bool(args.get("stat", False))
        revision = args.get("revision")
        path = args.get("path")
        paths: list[str] = args.get("paths") or ([path] if path else [])

        cmd = ["diff"]
        if staged:
            cmd.append("--cached")
        if stat:
            cmd.append("--stat")
        if revision:
            cmd.append(str(revision))
        if paths:
            cmd.append("--")
            cmd.extend(paths)

        code, stdout, stderr = await _run_git(cmd, cwd)
        if code != 0:
            return ToolResult.err(stderr.strip() or stdout.strip() or "git diff failed")
        out = stdout.strip()
        if not out:
            scope = "staged" if staged else "working tree"
            target_desc = f" for {', '.join(paths)}" if paths else ""
            return ToolResult.ok(f"No {scope} changes{target_desc}.", raw={"empty": True})
        return ToolResult.ok(out, raw={"staged": staged, "stat": stat})

    async def _action_log(self, args: dict[str, Any], cwd: str) -> ToolResult:
        limit = max(1, min(100, int(args.get("limit", 10) or 10)))
        oneline = bool(args.get("oneline", True))
        stat = bool(args.get("stat", False))
        revision = args.get("revision")
        path = args.get("path")

        cmd = ["log", f"-n{limit}"]
        if stat:
            cmd.extend(["--stat", "--oneline"])
        elif oneline:
            cmd.append("--oneline")
        else:
            cmd.append("--format=medium")

        if revision:
            cmd.append(str(revision))
        if path:
            cmd.extend(["--", str(path)])

        code, stdout, stderr = await _run_git(cmd, cwd)
        if code != 0:
            return ToolResult.err(stderr.strip() or stdout.strip() or "git log failed")
        out = stdout.strip()
        if not out:
            return ToolResult.ok("No commits found.", raw={"count": 0})
        return ToolResult.ok(out, raw={"commits": len(out.splitlines())})

    async def _action_show(self, args: dict[str, Any], cwd: str) -> ToolResult:
        revision = str(args.get("revision") or "HEAD")
        stat = bool(args.get("stat", False))
        path = args.get("path")

        cmd = ["show"]
        if stat:
            cmd.append("--stat")
        cmd.append(revision)
        if path:
            cmd.extend(["--", str(path)])

        code, stdout, stderr = await _run_git(cmd, cwd)
        if code != 0:
            return ToolResult.err(stderr.strip() or stdout.strip() or f"git show {revision} failed")
        return ToolResult.ok(stdout.strip(), raw={"revision": revision})

    async def _action_add(self, args: dict[str, Any], cwd: str) -> ToolResult:
        path = args.get("path")
        paths: list[str] = args.get("paths") or ([path] if path else [])
        if not paths:
            return ToolResult.err("paths required for git add (provide array of paths to stage)")

        cmd = ["add", "--", *paths]
        code, stdout, stderr = await _run_git(cmd, cwd)
        if code != 0:
            return ToolResult.err(stderr.strip() or stdout.strip() or "git add failed")

        # Include summary of what is currently staged
        _, stat_out, _ = await _run_git(["diff", "--cached", "--stat"], cwd)
        msg = f"Staged {len(paths)} path(s): {', '.join(paths)}"
        if stat_out.strip():
            msg += f"\n\nStaged changes:\n{stat_out.strip()}"
        return ToolResult.ok(msg, raw={"paths": paths})

    async def _action_restore(self, args: dict[str, Any], cwd: str) -> ToolResult:
        path = args.get("path")
        paths: list[str] = args.get("paths") or ([path] if path else [])
        staged = bool(args.get("staged", False))
        if not paths:
            return ToolResult.err("paths required for git restore (provide array of paths to restore)")

        cmd = ["restore"]
        if staged:
            cmd.append("--staged")
        cmd.extend(["--", *paths])

        code, stdout, stderr = await _run_git(cmd, cwd)
        if code != 0:
            return ToolResult.err(stderr.strip() or stdout.strip() or "git restore failed")
        target_desc = "from staging" if staged else "in working tree"
        return ToolResult.ok(f"Restored {len(paths)} path(s) {target_desc}: {', '.join(paths)}", raw={"paths": paths, "staged": staged})

    async def _action_commit(self, args: dict[str, Any], cwd: str) -> ToolResult:
        message = str(args.get("message", "")).strip()
        amend = bool(args.get("amend", False))
        if not message:
            return ToolResult.err("message required for git commit")

        cmd = ["commit"]
        if amend:
            cmd.append("--amend")
        cmd.extend(["-m", message])

        code, stdout, stderr = await _run_git(cmd, cwd)
        if code != 0:
            return ToolResult.err(stderr.strip() or stdout.strip() or "git commit failed")
        return ToolResult.ok(stdout.strip(), raw={"message": message, "amend": amend})

    async def _action_branch(self, args: dict[str, Any], cwd: str) -> ToolResult:
        subaction = str(args.get("subaction") or args.get("branch_action") or "list").lower()
        branch_name = str(args.get("branch_name") or args.get("name") or "").strip()
        force = bool(args.get("force", False))

        if subaction == "list":
            cmd = ["branch", "-a", "--sort=-committerdate"]
        elif subaction == "create":
            if not branch_name:
                return ToolResult.err("branch_name required to create branch")
            cmd = ["branch", branch_name]
        elif subaction in ("switch", "checkout"):
            if not branch_name:
                return ToolResult.err("branch_name required to switch branch")
            cmd = ["switch", branch_name]
        elif subaction == "delete":
            if not branch_name:
                return ToolResult.err("branch_name required to delete branch")
            cmd = ["branch", "-D" if force else "-d", branch_name]
        else:
            return ToolResult.err(f"unrecognized branch subaction: {subaction} (expected list, create, switch, delete)")

        code, stdout, stderr = await _run_git(cmd, cwd)
        if code != 0:
            return ToolResult.err(stderr.strip() or stdout.strip() or f"git branch {subaction} failed")
        out = stdout.strip() or f"Branch {subaction} '{branch_name}' succeeded."
        return ToolResult.ok(out, raw={"subaction": subaction, "branch_name": branch_name})

    async def _action_checkout(self, args: dict[str, Any], cwd: str) -> ToolResult:
        branch = str(args.get("branch_name") or args.get("name") or args.get("branch") or "").strip()
        path = args.get("path")
        paths: list[str] = args.get("paths") or ([path] if path else [])
        create = bool(args.get("create", False))

        if branch:
            cmd = ["checkout", "-b", branch] if create else ["checkout", branch]
        elif paths:
            cmd = ["checkout", "--", *paths]
        else:
            return ToolResult.err("checkout requires branch_name (to switch branches) or paths (to check out files)")

        code, stdout, stderr = await _run_git(cmd, cwd)
        if code != 0:
            return ToolResult.err(stderr.strip() or stdout.strip() or "git checkout failed")
        out = stdout.strip() or f"Checked out {'branch ' + branch if branch else ', '.join(paths)}."
        return ToolResult.ok(out, raw={"branch": branch, "paths": paths})

    async def _action_stash(self, args: dict[str, Any], cwd: str) -> ToolResult:
        subaction = str(args.get("subaction") or "list").lower()
        message = str(args.get("message", "")).strip()

        if subaction == "push":
            cmd = ["stash", "push", "-m", message] if message else ["stash", "push"]
        elif subaction == "pop":
            cmd = ["stash", "pop"]
        elif subaction == "list":
            cmd = ["stash", "list"]
        elif subaction == "show":
            cmd = ["stash", "show", "-p"]
        elif subaction == "drop":
            cmd = ["stash", "drop"]
        else:
            return ToolResult.err(f"unrecognized stash subaction: {subaction} (expected push, pop, list, show, drop)")

        code, stdout, stderr = await _run_git(cmd, cwd)
        if code != 0:
            return ToolResult.err(stderr.strip() or stdout.strip() or f"git stash {subaction} failed")
        out = stdout.strip() or f"Stash {subaction} succeeded."
        return ToolResult.ok(out, raw={"subaction": subaction})

    async def _action_blame(self, args: dict[str, Any], cwd: str) -> ToolResult:
        path = args.get("path") or (args.get("paths")[0] if args.get("paths") else None)
        if not path:
            return ToolResult.err("path required for git blame")
        lines = args.get("lines")

        cmd = ["blame"]
        if lines:
            cmd.append(f"-L{lines}")
        cmd.extend(["--", str(path)])

        code, stdout, stderr = await _run_git(cmd, cwd)
        if code != 0:
            return ToolResult.err(stderr.strip() or stdout.strip() or "git blame failed")
        return ToolResult.ok(stdout.strip(), raw={"path": str(path)})


git = GitTool()
