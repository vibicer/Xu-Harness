"""Preset store — user-authored agent compositions ("orchestration presets").

A preset is a tree of agents: a ``role:"orchestrator"`` root (the Main) plus
``role:"agent"`` sub-agents, each with its own inline persona, job, model,
rules, and allowances over skills/tools/memory. This is a static *definition*;
the subagent runtime in :mod:`xu_brain.features.agent.orchestrator` is what executes it.

Each preset is stored as one JSON file under ``data_home/presets/{id}.json`` so
it survives a brain restart and each file edits independently.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _pid() -> str:
    return "p" + uuid.uuid4().hex[:10]


# Optional node allowances use ``None`` = inherit the global config.
# A list (including empty ``[]``) = restrict to exactly these refs.
# ``skills``/``tools`` reference the global tool/skill catalogs by id;
# ``memory`` references memory namespaces (or entry ids).
@dataclass
class AgentNode:
    id: str
    name: str
    role: str = "agent"            # "orchestrator" | "agent"
    persona: str = ""              # inline system-prompt text; independent of global persona store
    job: str = ""                  # one-line role description (system-prompt seed)
    model: str | None = None
    # Ordered backup model ids tried when ``model`` can't be resolved or its
    # provider keeps failing during a turn (see Agent._model_chain).
    fallbacks: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    skills: list[str] | None = None
    tools: list[str] | None = None
    memory: list[str] | None = None
    children: list["AgentNode"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "persona": self.persona,
            "job": self.job,
            "model": self.model,
            "fallbacks": list(self.fallbacks),
            "rules": list(self.rules),
            "skills": None if self.skills is None else list(self.skills),
            "tools": None if self.tools is None else list(self.tools),
            "memory": None if self.memory is None else list(self.memory),
            "children": [c.to_dict() for c in self.children],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "AgentNode":
        return cls(
            id=str(d.get("id") or _pid()),
            name=str(d.get("name") or "agent"),
            role=str(d.get("role") or "agent"),
            persona=str(d.get("persona") or ""),
            job=str(d.get("job") or ""),
            model=d.get("model"),
            fallbacks=[m for m in (d.get("fallbacks") or []) if isinstance(m, str) and m],
            rules=[r for r in (d.get("rules") or []) if isinstance(r, str)],
            skills=_opt_list(d.get("skills")),
            tools=_opt_list(d.get("tools")),
            memory=_opt_list(d.get("memory")),
            children=[cls.from_dict(c) for c in (d.get("children") or [])],
        )


def _opt_list(v: Any) -> list[str] | None:
    """None stays None; a list is coerced to string ids (``[]`` allowed)."""
    if v is None:
        return None
    return [str(x) for x in v]


@dataclass
class Preset:
    id: str
    name: str
    root: AgentNode
    created: float = 0.0
    updated: float = 0.0

    def to_dict(self, summary: bool = False) -> dict[str, Any]:
        d = {
            "id": self.id,
            "name": self.name,
            "root": self.root.to_dict(),
            "created": self.created,
            "updated": self.updated,
        }
        if summary:
            d["root"] = {"id": self.root.id, "name": self.root.name,
                          "role": self.root.role, "children": len(self.root.children)}
        return d


def count_nodes(node: AgentNode) -> int:
    return 1 + sum(count_nodes(c) for c in node.children)


class PresetStore:
    """One JSON file per preset under ``data_home/presets/``."""

    def __init__(self, data_home: Path) -> None:
        self.dir = Path(data_home) / "presets"
        self._cache: dict[str, Preset] = {}

    def _path(self, preset_id: str) -> Path:
        return self.dir / f"{preset_id}.json"

    def _load_all(self) -> None:
        if self.dir.exists():
            for path in self.dir.glob("*.json"):
                try:
                    d = json.loads(path.read_text("utf-8"))
                    preset = self._decode(d)
                    if preset:
                        self._cache[preset.id] = preset
                except (json.JSONDecodeError, OSError):
                    continue

    def _decode(self, d: dict[str, Any]) -> Preset | None:
        if "root" not in d:
            return None
        return Preset(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or "untitled"),
            root=AgentNode.from_dict(d["root"]),
            created=float(d.get("created") or time.time()),
            updated=float(d.get("updated") or time.time()),
        )

    def _write(self, preset: Preset) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(preset.id).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(preset.to_dict(), indent=2), "utf-8")
        tmp.replace(self._path(preset.id))
        self._cache[preset.id] = preset

    def get(self, preset_id: str) -> Preset | None:
        if preset_id in self._cache:
            return self._cache[preset_id]
        # Re-read from disk in case another brain instance wrote it.
        path = self._path(preset_id)
        if not path.exists():
            self._load_all()
            return self._cache.get(preset_id)
        try:
            preset = self._decode(json.loads(path.read_text("utf-8")))
        except (json.JSONDecodeError, OSError):
            return None
        if preset:
            self._cache[preset.id] = preset
        return preset

    def list(self) -> list[dict[str, Any]]:
        if not self.dir.exists():
            return []
        self._load_all()
        presets = sorted(self._cache.values(), key=lambda p: p.updated, reverse=True)
        return [
            {"id": p.id, "name": p.name, "node_count": count_nodes(p.root),
             "created": p.created, "updated": p.updated}
            for p in presets
        ]

    def upsert(self, preset_id: str | None, name: str, root: dict[str, Any] | AgentNode) -> Preset:
        """Create (new id) or overwrite (existing id) a preset."""
        now = time.time()
        node = root if isinstance(root, AgentNode) else AgentNode.from_dict(root)
        if not node.id:
            node.id = _pid()
        existing = self.get(preset_id) if preset_id else None
        preset = Preset(
            id=preset_id or _pid(),
            name=name.strip() or "untitled",
            root=node,
            created=existing.created if existing else now,
            updated=now,
        )
        self._write(preset)
        return preset

    def delete(self, preset_id: str) -> bool:
        # Guard against empty/traversal ids; preset ids are "p" + hex.
        if not (len(preset_id) >= 2 and preset_id.startswith("p") and preset_id.isalnum()):
            return False
        path = self._path(preset_id)
        self._cache.pop(preset_id, None)
        if not path.exists():
            return False
        path.unlink(missing_ok=True)
        return True


# Shared module-level store (the server injects the real one).
presets: PresetStore | None = None
