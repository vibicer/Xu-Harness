"""Data home and app settings."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_CONTEXT_LENGTH = 128_000
DEFAULT_COMPRESS_THRESHOLD = 0.60
DEFAULT_JOB_TIMEOUT = None
DEFAULT_MAX_PARALLEL_SUBAGENTS = 100

# Settings keys with defaults — mirrors the Config view's hot-apply surface.
_DEFAULTS: dict[str, Any] = {
    "context_length": DEFAULT_CONTEXT_LENGTH,
    "compress_threshold": DEFAULT_COMPRESS_THRESHOLD,
    "retain_ratio": 0.16,  # fraction of context_length kept verbatim on compaction
    "compaction_retries": 1,  # extra compaction attempts after a non-shrinking summary
    "compaction_max_tokens": 8192,  # cap on the summarizer call; a `length` finish is a rejected checkpoint
    "context_skill_budget": 6000,  # max chars of skill/memory body injected into the system prompt
    "approval_mode": "manual",  # "manual" | "yolo"
    "job_timeout": DEFAULT_JOB_TIMEOUT,
    "max_parallel_subagents": DEFAULT_MAX_PARALLEL_SUBAGENTS,
    "vision_model": None,  # model for image-bearing turns + inspect_image (None = session model)
    # Global ordered backup models for every turn: tried when the active model
    # can't be resolved or its provider keeps failing (Config → Orchestration).
    "model_fallbacks": [],
    "session_model": {},  # {session_id: model_id} — per-session active model
    "session_provider": {},  # {session_id: provider_id} — pinned provider for a session model
    "session_persona": {},  # {session_id: persona_id} — per-session persona override
    "session_skills": {},  # {session_id: {skill_id: enabled}} — per-session overrides on the global skill defaults
    "session_tools": {},  # {session_id: {toolset_or_tool: enabled}} — per-session overrides on the global toolset/drop-in defaults
    "active_persona": None,  # global default persona id (None → SOUL.md fallback)
    "prune_keep": 30,  # cap on saved sessions — oldest is pruned when exceeded (count, not age)
    "retry_max": 10,  # max provider-retry attempts per turn on transient error
    "retry_interval": 3,  # base backoff seconds; grows +2 each attempt (3,5,7,9…)
    "max_tokens": None,  # global default max_tokens (None = provider default)
    "session_max_tokens": {},  # {session_id: int|None} — per-session override
    # Secrets belong in config, not env — Config is the source of truth
    # (agent env is only a fallback). Firecrawl key may be a single str or a
    # comma-separated / list pool rotated round-robin (see tools/web.py).
    "firecrawl_key": None,
    "firecrawl_enabled": False,  # master switch — off until a key is added & flipped on
    "approval_modes": {},  # custom approval modes: name → {auto: [tool pat], prompt: [...]}
}


class Config:
    """Settings persisted as JSON in data home (``settings.json``)."""

    def __init__(self, data_home: Path) -> None:
        self.data_home = data_home
        self.file = data_home / "settings.json"
        self._values: dict[str, Any] = {}
        self.load()

    def load(self) -> None:
        if self.file.exists():
            try:
                self._values = json.loads(self.file.read_text("utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                # A corrupt file used to be discarded in silence, which reads as
                # "all settings reset" -- including provider keys. Keep the
                # bytes aside and say so, so it can be recovered by hand.
                self._values = {}
                self._quarantine(exc)
        for key, default in _DEFAULTS.items():
            self._values.setdefault(key, _deepcopy_default(default))

    def _quarantine(self, exc: Exception) -> None:
        backup = self.file.with_suffix(".json.corrupt")
        try:
            self.file.replace(backup)
        except OSError:
            backup = self.file
        print(f"xu: settings.json unreadable ({exc}); kept as {backup}", file=sys.stderr)

    def save(self) -> None:
        self.data_home.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: `set()` saves on every call, so a crash mid-write
        # would otherwise leave a half-file and lose every setting on next boot.
        tmp = self.file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._values, indent=2, ensure_ascii=False), "utf-8")
        tmp.replace(self.file)

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._values[key] = value
        self.save()

    def all(self) -> dict[str, Any]:
        return dict(self._values)

    # ---- session-scoped helpers ----

    def session_model(self, session_id: str) -> str | None:
        return self._values.get("session_model", {}).get(session_id)

    def session_provider(self, session_id: str) -> str | None:
        """Explicit provider pinned for a session's model (None = auto)."""
        return self._values.get("session_provider", {}).get(session_id)

    def set_session_provider(self, session_id: str, provider: str | None) -> None:
        sp = dict(self._values.get("session_provider", {}))
        if provider is None:
            sp.pop(session_id, None)
        else:
            sp[session_id] = provider
        self._values["session_provider"] = sp
        self.save()

    def set_session_model(self, session_id: str, model: str | None) -> None:
        sm = dict(self._values.get("session_model", {}))
        if model is None:
            sm.pop(session_id, None)
        else:
            sm[session_id] = model
        self._values["session_model"] = sm
        self.save()

    def session_max_tokens(self, session_id: str) -> int | None:
        """Per-session override → global default → None (provider decides)."""
        smt = self._values.get("session_max_tokens", {})
        if session_id in smt:
            return smt[session_id]
        return self._values.get("max_tokens")

    def toolset_enabled(self, toolset: str, default: bool = True) -> bool:
        return self._values.get("toolsets", {}).get(toolset, default)

    def set_toolset(self, toolset: str, enabled: bool) -> None:
        ts = dict(self._values.get("toolsets", {}))
        ts[toolset] = enabled
        self._values["toolsets"] = ts
        self.save()

    def dropin_enabled(self, name: str, default: bool = True) -> bool:
        return self._values.get("dropin_tools", {}).get(name, default)

    def set_dropin(self, name: str, enabled: bool) -> None:
        d = dict(self._values.get("dropin_tools", {}))
        d[name] = enabled
        self._values["dropin_tools"] = d
        self.save()

    # ---- per-session skill/tool overrides ----
    # Delta over the global defaults, mirroring session_max_tokens: an absent
    # key means "follow the global default". A new session has no overrides,
    # so it follows Config — nothing needs to be snapshotted at creation.

    def session_skill_overrides(self, session_id: str) -> dict[str, bool]:
        """Raw per-session skill overrides ``{skill_id: enabled}``; absent = default."""
        ov = self._values.get("session_skills", {}).get(session_id, {})
        return {k: bool(v) for k, v in ov.items() if isinstance(v, bool)}

    def set_session_skill(self, session_id: str, skill_id: str, enabled: bool | None) -> None:
        """Set (or clear, with None) one session's override for a skill."""
        self._set_session_override("session_skills", session_id, skill_id, enabled)

    def session_tool_overrides(self, session_id: str) -> dict[str, bool]:
        """Raw per-session tool overrides keyed like the UI toggles them:
        toolset names for built-ins, tool names for drop-ins; absent = default."""
        ov = self._values.get("session_tools", {}).get(session_id, {})
        return {k: bool(v) for k, v in ov.items() if isinstance(v, bool)}

    def set_session_tool(self, session_id: str, name: str, enabled: bool | None) -> None:
        """Set (or clear, with None) one session's override for a toolset or drop-in tool."""
        self._set_session_override("session_tools", session_id, name, enabled)

    def _set_session_override(self, key: str, session_id: str, item: str, enabled: bool | None) -> None:
        all_ov = dict(self._values.get(key, {}))
        ov = dict(all_ov.get(session_id, {}))
        old = ov.get(item)
        if enabled is None:
            ov.pop(item, None)
        else:
            ov[item] = bool(enabled)
        if ov:
            all_ov[session_id] = ov
        else:
            all_ov.pop(session_id, None)
        if old != enabled:
            self._values[key] = all_ov
            self.save()

    def retry_max(self) -> int:
        return int(self._values.get("retry_max", 10))

    def retry_interval(self) -> int:
        return int(self._values.get("retry_interval", 3))

    def vision_model(self) -> str | None:
        """Model an image-bearing turn (and ``inspect_image``) routes to; None →
        the session's active model, which is often multimodal anyway.

        The pixels ride in that one request only: history keeps the path
        (``agent/attachments.py``), so a model that cannot see fails once, is
        retried without the image, and never poisons the following turns."""
        v = self._values.get("vision_model")
        return str(v) if v else None

    def model_fallbacks(self) -> list[str]:
        """Global ordered backup models, tried after the turn's own model (and
        after a preset node's own ``fallbacks``) is exhausted."""
        raw = self._values.get("model_fallbacks") or []
        return [str(m) for m in raw if isinstance(m, str) and m.strip()]

    # ---- personas (preset system prompts) ----

    @property
    def personas_dir(self) -> Path:
        d = self.data_home / "personas"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def list_personas(self) -> list[dict[str, Any]]:
        """``[{id, name}]`` for every saved preset, sorted by id."""
        out = []
        for f in sorted(self.personas_dir.glob("*.md")):
            out.append({"id": f.stem, "name": self._persona_name(f)})
        return out

    @staticmethod
    def _persona_name(path: Path) -> str:
        try:
            for line in path.read_text("utf-8").splitlines():
                if line.strip().startswith("#"):
                    return line.strip().lstrip("#").strip() or path.stem
        except OSError:
            pass
        return path.stem

    def get_persona(self, persona_id: str) -> str | None:
        f = self.personas_dir / f"{persona_id}.md"
        if not f.is_file():
            return None
        try:
            return f.read_text("utf-8")
        except OSError:
            return None

    def save_persona(self, persona_id: str, text: str) -> str:
        persona_id = _slug(persona_id)
        if not persona_id:
            raise ValueError("persona id required")
        (self.personas_dir / f"{persona_id}.md").write_text(text, "utf-8")
        return persona_id

    def delete_persona(self, persona_id: str) -> None:
        f = self.personas_dir / f"{persona_id}.md"
        if f.is_file():
            f.unlink()
        # clear references
        if self._values.get("active_persona") == persona_id:
            self._values["active_persona"] = None
        sp = dict(self._values.get("session_persona", {}))
        for sid, pid in list(sp.items()):
            if pid == persona_id:
                sp.pop(sid, None)
        self._values["session_persona"] = sp
        self.save()

    def active_persona(self) -> str | None:
        return self._values.get("active_persona")

    def set_active_persona(self, persona_id: str | None) -> None:
        self._values["active_persona"] = persona_id
        self.save()

    def session_persona(self, session_id: str) -> str | None:
        return self._values.get("session_persona", {}).get(session_id)

    def set_session_persona(self, session_id: str, persona_id: str | None) -> None:
        sp = dict(self._values.get("session_persona", {}))
        if persona_id is None:
            sp.pop(session_id, None)
        else:
            sp[session_id] = persona_id
        self._values["session_persona"] = sp
        self.save()

    def session_rules(self, session_id: str) -> list[str]:
        """Return the ordered rules configured for one session."""
        rules = self._values.get("session_rules", {}).get(session_id, [])
        return [rule for rule in rules if isinstance(rule, str) and rule.strip()]

    def set_session_rules(self, session_id: str, rules: list[str]) -> None:
        """Persist normalized, non-empty rules for one session."""
        clean = [rule.strip() for rule in rules if isinstance(rule, str) and rule.strip()]
        all_rules = dict(self._values.get("session_rules", {}))
        if clean:
            all_rules[session_id] = clean
        else:
            all_rules.pop(session_id, None)
        self._values["session_rules"] = all_rules
        self.save()
    def session_preset(self, session_id: str) -> str | None:
        """The id of the orchestration preset bound to one session, if any."""
        # Only return ids that point at an existing preset (the store prunes
        # on delete, but a config may lag a deleted preset).
        import xu_brain.features.presets as _presets
        store = _presets.presets
        pid = self._values.get("session_preset", {}).get(session_id)
        if not pid:
            return None
        if store is not None and store.get(pid) is None:
            return None
        return pid

    def set_session_preset(self, session_id: str, preset_id: str | None) -> None:
        sp = dict(self._values.get("session_preset", {}))
        if preset_id is None:
            sp.pop(session_id, None)
        else:
            sp[session_id] = preset_id
        self._values["session_preset"] = sp
        self.save()

    def delete_session(self, session_id: str) -> None:
        """Remove persisted settings associated with a deleted session."""
        changed = False
        for key in ("session_model", "session_provider", "session_persona", "session_max_tokens", "session_rules", "session_preset", "session_skills", "session_tools"):
            values = dict(self._values.get(key, {}))
            if session_id in values:
                values.pop(session_id)
                self._values[key] = values
                changed = True
        if changed:
            self.save()

    def resolve_persona_text(self, session_id: str | None) -> str | None:
        """Per-session override → global active → None (caller falls back to SOUL.md)."""
        pid = self.session_persona(session_id) if session_id else None
        pid = pid or self.active_persona()
        if not pid:
            return None
        return self.get_persona(pid)


def _slug(text: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s


def _deepcopy_default(v: Any) -> Any:
    return json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v

def default_data_home() -> Path:
    """``~/.xu`` — matches the onboarding default."""
    return Path(os.environ.get("XU_DATA_HOME", Path.home() / ".xu"))


def ensure_data_home(data_home: Path) -> Path:
    data_home = Path(data_home).expanduser().resolve()
    for sub in ("sessions", "skills", "plugins", "logs"):
        (data_home / sub).mkdir(parents=True, exist_ok=True)
    # Persona + memory files — created empty on first run if absent.
    soul = data_home / "SOUL.md"
    if not soul.exists():
        soul.write_text("# Xu persona\n\nYou are Xu, a concise personal AI.\n", "utf-8")
    mem = data_home / "MEMORY.md"
    if not mem.exists():
        mem.write_text("", "utf-8")
    return data_home
