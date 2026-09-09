"""System-prompt / message assembly for the model.

A mixin aspect of :class:`~xu_brain.features.agent.loop.Agent`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..session import Session
from .attachments import dereference as _dereference_images
from .checkpoint import _CHECKPOINT_MARKER


class PromptMixin:
    def _model_chain(self, model: str | None, provider_pin: str | None, node: Any = None) -> list[tuple[Any, str]]:
        """Resolved ``[(provider, model), …]`` to try for one turn, in order.

        Primary (honoring the session's pinned provider) first, then the preset
        node's own ``fallbacks``, then the global ``model_fallbacks`` config.
        Fallbacks resolve across *any* enabled provider — the pin disambiguates
        the primary model id, it must not gate the backups. Unresolvable
        entries are dropped, so an empty result means nothing is configured.
        """
        wanted: list[tuple[str | None, str | None]] = [(model, provider_pin)]
        for m in list(getattr(node, "fallbacks", None) or []):
            wanted.append((m, None))
        cfg_fallbacks = getattr(self.config, "model_fallbacks", None)
        for m in (cfg_fallbacks() if callable(cfg_fallbacks) else []):
            wanted.append((m, None))
        chain: list[tuple[Any, str]] = []
        seen: set[tuple[str, str]] = set()
        for want, pin in wanted:
            resolved = self.providers.resolve(want, pin)
            if resolved is None:
                continue
            provider, resolved_model = resolved
            key = (str(getattr(provider, "id", provider)), resolved_model)
            if key in seen:
                continue
            seen.add(key)
            chain.append((provider, resolved_model))
        return chain

    def _root_node(self, session_id: str):
        """Root ``AgentNode`` of the active orchestration preset, or None."""
        import xu_brain.features.presets as _presets
        pid = self.config.session_preset(session_id) if session_id else None
        if not pid:
            return None
        preset = _presets.presets.get(pid) if _presets.presets else None
        return preset.root if preset else None

    def _skill_block(self, sid: str, prefix: str, body: str) -> str:
        """Model-facing skill section: header + (dir line) + body.

        The ``# skill dir:`` line tells the model where the skill's own
        support files (references/, scripts/, assets/) live, so file-backed
        skills can resolve relative paths in their body. Loose single-file
        skills get no dir line (nothing to reach).
        """
        header = f"{prefix}: {sid}"
        try:
            d = self.skills.dir_of(sid)
        except Exception:  # noqa: BLE001 — header must never break a turn
            d = None
        if d is not None:
            header += (f"\n# skill dir: {d} — relative paths in this skill "
                       f"(references/, scripts/, assets/) resolve against this dir")
        return header + "\n" + body

    def _build_messages(self, session: Session, node: Any = None) -> list[dict[str, Any]]:
        # Node persona (an orchestration-preset agent) overrides the global /
        # per-session persona when provided; otherwise fall through to normal
        # resolution. ``job`` appends a role line to the persona.
        persona = node.persona if (node is not None and node.persona) else None
        if persona is None:
            persona = self.config.resolve_persona_text(session.id)
        if persona is None:
            soul = self.data_home / "SOUL.md"
            persona = soul.read_text("utf-8") if soul.exists() else "You are Xu, a concise personal AI."
        if node is not None and node.job:
            persona = f"{persona}\n\nRole: {node.job}"
        # Hardcoded harness identity — always present, appended after the persona.
        system = persona + "\n\nYou are using Xu Harness."
        # Node skills/tools/memory: ``None`` = inherit global, a list = scope.
        allow = set(node.skills) if (node is not None and node.skills is not None) else None
        # Global memory is the *user's* standing context: it belongs to the
        # agent that talks to the user. A leaf sub-agent gets its whole brief
        # from the delegating prompt, so inheriting global memory only feeds it
        # rules it cannot act on (e.g. "ask the user for approval" — a leaf has
        # no channel to the user). Orchestrators still inherit; leaves default
        # to no memory unless the node pins an explicit scope.
        mem_scope = node.memory if node is not None else None
        if node is not None and node.memory is None and node.role != "orchestrator":
            mem_scope = []
        # Context priority: auto-matched skills first (most likely relevant to
        # the active turn), then already-loaded skills, then memory. Under a
        # tight context, the auto-matched intent wins over ambient accumulation.
        # Sections are (kind, sid, text): a 'skill' section can degrade to an
        # overview tier, 'memory' to a per-entry-capped reflect. sid is the
        # skill id (None for memory); text is the full tier-1 block.
        priority: list[tuple[str, str | None, str]] = []
        ambient: list[tuple[str, str | None, str]] = []
        ambient_after: list[tuple[str, str | None, str]] = []
        # Session skill overrides (Agent State → SKILLS) layer over the global
        # ambient set: absent override = follow the global default, so a new
        # session injects exactly what Config enables.
        skill_ov = self.config.session_skill_overrides(session.id)
        session_disabled = {sid for sid, on in skill_ov.items() if not on}
        enabled = (
            {s["id"] for s in self.skills.list() if s.get("ambient")}
            | {sid for sid, on in skill_ov.items() if on}
        ) - session_disabled
        enabled = {sid for sid in enabled if allow is None or sid in allow}
        # Drop override ids that no longer exist in the catalog (a removed
        # skill leaves its override behind) so a stale entry can't break a turn.
        enabled &= {s["id"] for s in self.skills.list(all=True)}
        # Enabled (hard-reserved) skills are ALWAYS injected, exempt from the
        # context budget (Option A). Build them first, independent of state.
        # Session cwd: the harness's own resolution root for file/glob/eval and
        # the bash tool's spawn dir. Hard-injected (budget-exempt) because a
        # model that guesses this reads and writes the wrong tree.
        hard: list[str] = [f"# cwd (session)\n{session.cwd}"]
        for sid in sorted(enabled):
            # Session-only enables read the body without marking the skill
            # LOADED globally — that would leak it into other sessions' prompts.
            body = self.skills.read_body(sid) if sid in skill_ov else self.skills.load(sid)
            if body:
                hard.append(self._skill_block(sid, "# skill (enabled)", body))
        # keyword auto-match across the recent context (last few user turns +
        # trailing tool results), weighted by recency so an earlier hint still
        # scores but a fresh one wins. Enabled skills are exempt (already in).
        auto = [mid for mid in self._match_recent(session)
                if (allow is None or mid in allow) and mid not in session_disabled]
        for mid in auto:
            if mid in enabled:
                continue
            body = self.skills.load(mid)
            if body:
                priority.append(("skill", mid, self._skill_block(mid, "# skill (auto)", body)))
        for s in self.skills.list():
            if s["id"] in enabled or s["id"] in auto:
                continue  # already injected above
            if allow is not None and s["id"] not in allow:
                continue  # scoped out by the node
            if s.get("state") != "LOADED" or s["id"] in session_disabled:
                continue
            body = self.skills.load(s["id"])
            if body:
                ambient.append(("skill", s["id"], self._skill_block(s["id"], "# skill", body)))
        # memory reflect (node-scoped when the node pins a memory allowlist)
        if mem_scope is not None:
            mem = self.memory.reflect(only=mem_scope)
        else:
            mem = self.memory.reflect()
        if mem:
            ambient_after.append(("memory", None, f"# memory\n{mem}"))
        # Progressive-disclosure budget: the standing prompt carries
        # at most ``context_skill_budget`` chars of *non-enabled* skill/memory
        # body. Enabled skills are hard-reserved and skip the cap; auto-matched
        # (priority) content wins the remaining window over ambient loaded
        # skills and memory, which trim last.
        budget = int(self.config.get("context_skill_budget", 6000))
        if budget and budget > 0:
            def _overview(kind: str, sid: str | None) -> str | None:
                if kind == "skill" and sid:
                    try:
                        block = self._skill_block(sid, "# skill (overview)",
                                                   self.skills.overview(sid))
                        return block
                    except Exception:  # noqa: BLE001 - overview must never break a turn
                        return None
                if kind == "memory":
                    only = mem_scope if isinstance(mem_scope, list) else None
                    capped = self.memory.reflect(only=only, entry_cap=200)
                    return f"# memory\n{capped}" if capped else None
                return None

            chosen: list[str] = []
            used = 0
            for kind, sid, text in priority + ambient + ambient_after:
                cost = len(text)
                if cost <= budget - used:
                    chosen.append(text)
                    used += cost
                    continue
                # Tier 2: degrade to an overview before dropping entirely.
                short = _overview(kind, sid)
                if short and len(short) <= budget - used:
                    chosen.append(short)
                    used += len(short)
            sections = hard + chosen
        else:
            sections = hard + [t for _k, _s, t in priority] \
                + [t for _k, _s, t in ambient] + [t for _k, _s, t in ambient_after]
        if sections:
            system += "\n\n" + "\n\n".join(sections)
        # Session rules: one copy in the system prompt so they govern every
        # turn without being repeated on each user message.
        rules = (node.rules if (node is not None and node.rules) else None) or self.config.session_rules(session.id)
        if rules:
            system += "\n\n[SESSION RULES]\n" + "\n".join(f"- {rule}" for rule in rules)
        # Orchestrator roster: the Main sees its squad and is told to delegate.
        if node is not None and node.role == "orchestrator" and node.children:
            roster = [f"- {c.name} — {c.job or c.role}" for c in node.children]
            system += "\n\n[SQUAD ROSTER]\n" + "\n".join(roster)
            system += (
                "\n\nYou are the orchestrator (Main). Delegate sub-tasks to a "
                "squad member with the `delegate` tool and use their result — "
                "do not do their specialist work yourself."
            )
        # Current local time — hardcoded into the system prompt instead of the
        # old `clock` plugin hook, so the model always knows the date/time.
        now = datetime.now().astimezone()
        system += f"\n\n# time\nNow: {now.strftime('%a %d %b %Y %H:%M')} ({now.strftime('%z')})"
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in session.messages:
            # Pass through persisted compression summaries — the one system block
            # that carries conversation content; everything else is rebuilt fresh
            # (persona/skills) or skipped. Without this, a persisted summary is
            # silently dropped and the history effectively never compacts.
            # Compaction *failure* notes are display-only and never replayed.
            if m.role == "system" and not (
                isinstance(m.content, str) and m.content.startswith(_CHECKPOINT_MARKER)
            ):
                continue
            # Attached images never enter the API input: each image part is
            # stored under data_home and replaced by its path, which the agent
            # reads with inspect_image (its own isolated vision call). Replaying
            # base64 cost tokens on every later turn and hard-failed a text-only
            # model. The display transcript keeps the data URL for the thumbnail.
            content = _dereference_images(m.content, getattr(self, "data_home", None), session.id)
            msg: dict[str, Any] = {"role": m.role, "content": content}
            # Persisted tool rounds: assistant rows declare their tool_calls,
            # tool rows answer them by id — so the next turn's history is a
            # valid tool-call sequence, not orphan tool messages.
            if m.role == "assistant":
                if m.tool_calls:
                    msg["tool_calls"] = m.tool_calls
            elif m.role == "tool" and m.tool_call_id:
                msg["tool_call_id"] = m.tool_call_id
            # thinking-mode models (deepseek-reasoner et al.) require the prior
            # reasoning_content echoed back on assistant turns
            if m.role == "assistant" and m.reasoning:
                msg["reasoning_content"] = m.reasoning
            if m.role == "assistant" and m.reasoning_signature:
                msg["reasoning_signature"] = m.reasoning_signature
            messages.append(msg)
        return messages

    def _match_recent(self, session: Session) -> list[str]:
        """Keyword-match skills against the recent conversation, not just the
        last user message. Scores the trailing user turns and tool results,
        weighted by recency, then merges into one deduped candidate list."""
        scored: dict[str, int] = {}
        # Most recent first; cap how far back we look.
        for idx, m in enumerate(reversed(session.messages[-40:])):
            if m.role not in ("user", "tool"):
                continue
            weight = max(1, 8 - idx)  # fresh messages score higher
            text = str(m.content)
            if isinstance(m.content, list):
                text = " ".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in m.content)
            for mid in self.skills.match(text):
                scored[mid] = scored.get(mid, 0) + weight
        return sorted(scored, key=scored.get, reverse=True)
