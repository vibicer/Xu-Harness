"""Frozen governance enforcer.

The **gate** — ApprovalManager, the ALWAYS-gate (deletes / destructive shell /
durable memory writes), approval modes, and the per-tool approval levels — is
frozen. The **criteria** the gate consults are pluggable via the ``policy`` slot: an :class:`ApprovalPolicy`
plugin may escalate or relax *its own* rules but can never remove the gate.

Modes: ``manual`` (prompt for risky tools) and ``yolo`` (auto-approve risky).
**Always gated regardless of mode**: deletes and destructive shell patterns and
durable memory writes (tools declare ``ApprovalLevel.ALWAYS``).

The agent loop calls :meth:`ApprovalManager.request` before a risky/always tool.
If user input is required it emits a ``turn.approval`` event and awaits the
matching ``resolve`` RPC — the shell renders an inline dialogue card.
"""
from __future__ import annotations

import asyncio
import re
import secrets
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class ApprovalLevel(Enum):
    """When a tool must ask the user before running."""

    NEVER = "never"      # safe reads — bash read-only? no, bash is RISKY
    RISKY = "risky"      # prompt in manual mode; auto-approved in yolo
    ALWAYS = "always"    # gated regardless of mode: deletes, destructive shell, durable memory writes


# Shell patterns that are always destructive regardless of a tool's declared
# level. Truncating redirects (`> file`) are NOT gated -- they're commonly
# used for dev logs (`> /tmp/x.log`). `rm`/`rmdir`-style deletes are also
# NOT gated (user preference): only genuinely destructive primitives and
# permission-wipes remain.
_DESTRUCTIVE_SHELL = re.compile(
    r"\b(unlink|mv\b.*--no-target-directory|mkfs|dd\b.*of=|"
    r"shutdown|reboot|halt|:()\{\:|\:&\};:|chmod\s+-R\s+0|chmod\b.*\b777\b|"
    r"truncate\s+-s\s*0|find\b.*\s-delete\b|git\s+clean\b[^|]*\s-f)"
)
_ALWAYS_DELETE_TOOLS: set[str] = set()  # empty by user preference; gate kept

# Reason string returned by ApprovalManager.request when the user did not
# answer an approval card before the timeout: the caller should pause the turn.
TIMEOUT_REASON = "approval timed out"


@runtime_checkable
class ApprovalPolicy(Protocol):
    """A pluggable ``policy`` slot feeding the frozen gate.

    A policy may override the always-gate decision and the effective level for
    its own tools, but the gate (request/resolve/timeout machinery) is untouched.
    """

    def is_always_gated(self, tool_name: str, args: dict[str, Any]) -> bool: ...

    def level_for(
        self, tool_name: str, args: dict[str, Any], declared: ApprovalLevel
    ) -> ApprovalLevel: ...


class ApprovalManager:
    def __init__(self, mode: str = "manual", event_emitter=None) -> None:
        self.mode = mode  # "manual" | "yolo"
        self._event_emitter = event_emitter
        self._pending: dict[str, asyncio.Future[bool]] = {}
        # Owning session per pending approval, so a per-session stop only
        # denies that session's cards.
        self._pending_sessions: dict[str, str | None] = {}
        # Pluggable criteria (policy slot). None → frozen defaults.
        self._policies: list[ApprovalPolicy] = []
        # User-defined modes (config key ``approval_modes``): name → criteria.
        self._modes: dict[str, dict[str, list[str]]] = {}
        # Tool name per pending approval, so ``resolve(remember=True)`` knows
        # what to whitelist without the shell having to send it back.
        self._pending_tools: dict[str, str] = {}
        # "Always approve, this session": session_id → tool names the user
        # cleared for the rest of the session. Consulted for RISKY only -- the
        # frozen ALWAYS-gate is never satisfied from here.
        self._session_auto: dict[str | None, set[str]] = {}

    def add_policy(self, policy: ApprovalPolicy) -> None:
        """Register a ``policy`` plugin's criteria.

        A policy refines the *risky* band -- it may escalate anything to ALWAYS
        and may re-level tools the frozen gate does not claim. It cannot lower
        the frozen ALWAYS-gate; see :meth:`level_for`.
        """
        self._policies.append(policy)

    def remove_policy(self, policy: ApprovalPolicy) -> None:
        """Un-register a policy. Needed because a plugin can be disabled or
        reloaded mid-session: without this the criteria of an unloaded plugin
        would keep gating tools, and a reload would stack a second copy.

        Silent when absent -- teardown runs on paths that may have already
        removed it, and raising there would abort the rest of the disposal.
        """
        try:
            self._policies.remove(policy)
        except ValueError:
            pass

    def set_emitter(self, emitter) -> None:
        self._event_emitter = emitter

    def set_mode(self, mode: str) -> None:
        if mode not in ("manual", "yolo") and mode not in self._modes:
            raise ValueError(f"invalid approval mode: {mode}")
        self.mode = mode

    def set_modes(self, modes: Any) -> None:
        """Load user-defined approval modes (name → {auto: [...], prompt: [...]}).

        Custom criteria only re-resolve *risky* tools: ``auto`` downgrades to
        NEVER, ``prompt`` escalates to ALWAYS. The frozen ALWAYS-gate
        (destructive shell, deletes, durable memory writes) is checked first
        and can never be bypassed by a custom mode."""
        validated: dict[str, dict[str, list[str]]] = {}
        for name, spec in (modes or {}).items():
            if not isinstance(name, str) or not name or name in ("manual", "yolo"):
                continue
            if not isinstance(spec, dict):
                continue
            validated[name] = {
                "auto": [t for t in spec.get("auto", []) if isinstance(t, str) and t],
                "prompt": [t for t in spec.get("prompt", []) if isinstance(t, str) and t],
            }
        self._modes = validated
        # Deactivate the active mode if its definition was removed.
        if self.mode not in ("manual", "yolo") and self.mode not in validated:
            self.mode = "manual"

    @staticmethod
    def _mode_matches(patterns: Any, tool_name: str) -> bool:
        """Tool name match: exact, or trailing-``*`` prefix wildcard."""
        return any(
            tool_name == pat or (pat.endswith("*") and tool_name.startswith(pat[:-1]))
            for pat in (patterns or [])
        )

    @staticmethod
    def is_always_gated(tool_name: str, args: dict[str, Any]) -> bool:
        """Risky-level tools whose *specific* invocation is always gated."""
        if tool_name in _ALWAYS_DELETE_TOOLS:
            return True
        if tool_name == "bash":
            cmd = str(args.get("command", ""))
            if _DESTRUCTIVE_SHELL.search(cmd):
                return True
        return False

    def _policy_gated(self, tool_name: str, args: dict[str, Any]) -> bool:
        """True if any policy *escalates* this invocation into the always-gate.

        Escalation only -- a policy answering ``False`` cannot clear the frozen
        gate, it just declines to add one.
        """
        for policy in self._policies:
            try:
                if policy.is_always_gated(tool_name, args):
                    return True
            except Exception:  # a broken policy must not open the gate
                return True
        return False

    def level_for(
        self, tool_name: str, args: dict[str, Any], declared: ApprovalLevel
    ) -> ApprovalLevel:
        """Resolve the effective level: declared level, escalated to ALWAYS
        for destructive invocations (or by a policy plugin).

        Order matters. The frozen ALWAYS-gate is decided *before* any policy is
        consulted: policies are criteria, not an override switch, so a plugin
        returning NEVER for `bash` must not be able to auto-run `mkfs`.
        """
        if self.is_always_gated(tool_name, args) or self._policy_gated(tool_name, args):
            return ApprovalLevel.ALWAYS
        for policy in self._policies:
            try:
                level = policy.level_for(tool_name, args, declared)
            except Exception:  # a broken policy falls through to the defaults
                continue
            if level is not None:
                return level
        spec = self._modes.get(self.mode)
        if spec is not None and declared is ApprovalLevel.RISKY:
            if self._mode_matches(spec.get("auto"), tool_name):
                return ApprovalLevel.NEVER
            if self._mode_matches(spec.get("prompt"), tool_name):
                return ApprovalLevel.ALWAYS
        return declared

    async def request(
        self,
        tool_name: str,
        args: dict[str, Any],
        declared: ApprovalLevel,
        reason: str = "",
        *,
        session_id: str | None = None,
    ) -> tuple[bool, str | None]:
        """Returns (approved, reason_if_denied). Never raises.

        - NEVER  → approved immediately.
        - RISKY  → yolo auto-approves; manual prompts.
        - ALWAYS → prompts regardless of mode.
        """
        level = self.level_for(tool_name, args, declared)
        if level is ApprovalLevel.NEVER:
            return True, None
        if level is ApprovalLevel.RISKY and self.mode == "yolo":
            return True, None
        # "Always approve, this session" from a previous card. RISKY only: the
        # frozen ALWAYS-gate re-prompts every time by design.
        if level is ApprovalLevel.RISKY and tool_name in self._session_auto.get(
            session_id, ()
        ):
            return True, None

        # Prompt the user via an inline dialogue card.
        request_id = "a" + secrets.token_hex(4)
        fut: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = fut
        self._pending_sessions[request_id] = session_id
        self._pending_tools[request_id] = tool_name
        if self._event_emitter is not None:
            payload: dict[str, Any] = {
                "request_id": request_id,
                "tool": tool_name,
                "args": args,
                "reason": reason or _default_reason(tool_name, args, level),
                # The shell hides "always approve" for ALWAYS -- remembering it
                # would be a lie, since the frozen gate re-prompts anyway.
                "level": level.value,
            }
            if session_id is not None:
                payload["session_id"] = session_id
            await self._event_emitter("turn.approval", **payload)
        try:
            approved = await asyncio.wait_for(fut, timeout=300.0)
            return approved, None if approved else "denied by user"
        except TimeoutError:
            # Tell the shell so the card doesn't linger forever unanswered.
            if self._event_emitter is not None:
                await self._event_emitter(
                    "turn.approval_resolved", request_id=request_id, approved=False
                )
            return False, TIMEOUT_REASON
        finally:
            self._pending.pop(request_id, None)
            self._pending_sessions.pop(request_id, None)
            self._pending_tools.pop(request_id, None)

    def resolve(self, request_id: str, approved: bool, *, remember: bool = False) -> bool:
        """Shell RPC: settle an inline approval card. Returns False if unknown.

        ``remember`` = the card's "always approve, this session" button: the
        tool stops prompting for the rest of this session. Recorded regardless
        of the card's level, but only ever *consulted* for RISKY in
        :meth:`request`, so an ALWAYS-gated action can never be waved through.
        """
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            return False
        if approved and remember:
            tool = self._pending_tools.get(request_id)
            if tool:
                sid = self._pending_sessions.get(request_id)
                self._session_auto.setdefault(sid, set()).add(tool)
        fut.set_result(approved)
        return True

    def forget_session(self, session_id: str | None) -> None:
        """Drop a session's "always approve" whitelist (session deleted)."""
        self._session_auto.pop(session_id, None)

    def session_for(self, request_id: str) -> str | None:
        """Session that owns this pending approval (for event tagging)."""
        return self._pending_sessions.get(request_id)

    async def cancel_for_session(self, session_id: str | None) -> None:
        """On turn stop: settle this session's pending approvals as denied and
        resolve their cards. Other sessions' prompts stay live."""
        for request_id, fut in list(self._pending.items()):
            if self._pending_sessions.get(request_id) != session_id:
                continue
            if not fut.done():
                fut.set_result(False)
            self._pending.pop(request_id, None)
            self._pending_sessions.pop(request_id, None)
            self._pending_tools.pop(request_id, None)
            if self._event_emitter is not None:
                await self._event_emitter(
                    "turn.approval_resolved", request_id=request_id, approved=False
                )

    async def cancel_all(self) -> None:
        """On shutdown: settle any pending approvals as denied and tell the
        shell so every open card resolves instead of sticking forever."""
        for request_id, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_result(False)
            if self._event_emitter is not None:
                await self._event_emitter(
                    "turn.approval_resolved", request_id=request_id, approved=False
                )
        self._pending.clear()
        self._pending_sessions.clear()
        self._pending_tools.clear()


def _default_reason(tool_name: str, args: dict[str, Any], level: ApprovalLevel) -> str:
    if level is ApprovalLevel.ALWAYS:
        if tool_name == "bash":
            return f"Destructive shell command: {args.get('command', '')[:120]}"
        if tool_name in _ALWAYS_DELETE_TOOLS:
            return f"Durable/irreversible write: {tool_name}"
        return f"Always-gated action: {tool_name}"
    return f"Risky action requested: {tool_name}"


__all__ = [
    "_ALWAYS_DELETE_TOOLS",
    "_DESTRUCTIVE_SHELL",
    "ApprovalLevel",
    "ApprovalManager",
    "ApprovalPolicy",
    "TIMEOUT_REASON",
]
