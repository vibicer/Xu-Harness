"""Skills engine — progressive disclosure catalog.

A skill is a directory under ``data_home/skills/<pack>/<skill_id>/`` containing
a ``SKILL.md`` with YAML front matter (``name``, ``description``, ``keywords``,
optional ``match_limit``) followed by a markdown body. A *loose* skill is a
single ``.md`` file directly under a pack dir.

Progressive disclosure: the catalog (name + description + state) is always
available via :meth:`list`; the full body is only fetched on :meth:`load`, which
also flips the skill's state to ``LOADED``. Keyword :meth:`match` scores the
catalog case-insensitively and respects each skill's ``match_limit``.

A built-in ``general`` pack ships under ``brain/xu_brain/features/skills/general/`` and
is seeded into a fresh ``data_home/skills`` on first init.
"""
from __future__ import annotations

import json
import time
import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:
    import yaml  # PyYAML — optional but commonly present
except ImportError:  # pragma: no cover — fall back to a tiny parser
    yaml = None  # type: ignore[assignment]

def _digest(body: str, max_chars: int = 600) -> str:
    """Extract a small structural summary without interpreting markdown."""
    if not body:
        return ""
    lines = body.splitlines()
    selected: list[str] = []
    start = 0
    if lines and lines[0].startswith("#"):
        selected.append(lines[0])
        start = 1
    while start < len(lines) and not lines[start].strip():
        start += 1
    paragraph: list[str] = []
    index = start
    while index < len(lines) and lines[index].strip():
        if lines[index].lstrip().startswith(("- ", "* ")):
            break
        paragraph.append(lines[index])
        index += 1
    selected.extend(paragraph)
    cutoff = len(body) / 4
    position = 0
    for line in lines:
        if position >= cutoff:
            break
        if line.lstrip().startswith(("- ", "* ")):
            selected.append(line)
        position += len(line) + 1
    if not selected:
        return body[:max_chars]
    result: list[str] = []
    used = 0
    dropped = False
    for line in selected:
        cost = len(line) if not result else len(line) + 1
        if used + cost > max_chars:
            dropped = True
            break
        result.append(line)
        used += cost
    if dropped and result:
        suffix = "\n…"
        while result and len("\n".join(result)) + len(suffix) > max_chars:
            result.pop()
        if result:
            return "\n".join(result) + suffix
        return suffix[1:max_chars]
    return "\n".join(result)


__all__ = ["SkillsEngine", "SkillState", "Skill", "BUILTIN_PACK_DIR", "BUILTIN_PACK_DIRS"]

# Where the shipped packs live (relative to this file). BUILTIN_PACK_DIR stays
# as the `general` alias so existing importers keep working.
_PACKS_ROOT = Path(__file__).parent
BUILTIN_PACK_DIR = _PACKS_ROOT / "general"
BUILTIN_PACK_DIRS: tuple[Path, ...] = (BUILTIN_PACK_DIR, _PACKS_ROOT / "xu")

_DEFAULT_MATCH_LIMIT = 5


class SkillState:
    """Skill lifecycle states (LOADED / DEMAND / OFF)."""

    LOADED = "LOADED"    # body has been fetched into context this session
    DEMAND = "DEMAND"   # catalog-known, not yet loaded (default for most)
    OFF = "OFF"         # disabled by the user


@dataclass
class Skill:
    """A resolved skill record."""

    id: str
    name: str
    desc: str
    state: str = SkillState.DEMAND
    keywords: list[str] = field(default_factory=list)
    match_limit: int = _DEFAULT_MATCH_LIMIT
    pack: str = ""
    path: Path | None = None          # SKILL.md path (dir skills) or the .md file
    body: str | None = None           # cached full body once loaded
    ambient: bool = False             # enabled = hard-reserved, always injected

    def catalog(self) -> dict[str, Any]:
        """Compact catalog row — what :meth:`list` returns."""
        return {"id": self.id, "name": self.name, "desc": self.desc, "state": self.state,
                "ambient": self.ambient, "keywords": self.keywords}


# ---------------------------------------------------------------------------
# Front-matter parsing
#

_FRONT_RE = re.compile(r"^\s*---\s*\n(.*?)\n\s*---\s*\n?(.*)\Z", re.DOTALL)


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """Split a ``SKILL.md`` into (front-matter dict, body markdown)."""
    m = _FRONT_RE.match(text)
    if not m:
        return {}, text
    raw_fm, body = m.group(1), m.group(2)
    if yaml is not None:
        try:
            meta = yaml.safe_load(raw_fm) or {}
            if not isinstance(meta, dict):
                meta = {}
        except yaml.YAMLError:
            meta = _parse_yaml_fallback(raw_fm)
    else:
        meta = _parse_yaml_fallback(raw_fm)
    return meta, body.lstrip("\n")


def _parse_yaml_fallback(raw: str) -> dict[str, Any]:
    """Minimal YAML subset parser for ``key: value`` and ``key: [a, b]``.

    Only what a SKILL.md front-matter needs; not a general YAML parser.
    """
    out: dict[str, Any] = {}
    for line in raw.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if not key:
            continue
        try:
            out[key] = json.loads(val)
            continue
        except (json.JSONDecodeError, TypeError):
            pass
        if val.startswith("[") and val.endswith("]"):
            inner = val[1:-1]
            items = [v.strip().strip("\"'") for v in inner.split(",") if v.strip()]
            out[key] = items
        elif val:
            out[key] = val.strip("\"'")
        else:
            out[key] = ""
    return out


# ---------------------------------------------------------------------------
# Engine

def _render_frontmatter(meta: dict[str, Any]) -> str:
    """Render editable skill metadata as a YAML front-matter block."""
    data: dict[str, Any] = {
        "name": str(meta.get("name") or ""),
        "description": str(meta.get("description") or ""),
        "keywords": [str(keyword) for keyword in (meta.get("keywords") or [])],
    }
    if meta.get("match_limit") is not None:
        data["match_limit"] = meta["match_limit"]
    for key, value in meta.items():
        if key not in data:
            data[key] = value
    if yaml is not None:
        rendered = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).rstrip()
    else:
        rendered = "\n".join(
            f"{key}: {json.dumps(value, ensure_ascii=False)}"
            for key, value in data.items()
        )
    return f"---\n{rendered}\n---"

class SkillsEngine:
    """Progressive-disclosure skill catalog backed by ``data_home/skills``."""

    def __init__(self, data_home: Path | str) -> None:
        self.data_home = Path(data_home).expanduser()
        self.skills_dir = self.data_home / "skills"
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._skills: dict[str, Skill] = {}
        self._last_use: dict[str, float] = {}
        # Context budget for LOADED skill bodies: total char cap (0 = no cap).
        # See _evict_over_budget: prevents the system prompt from bloating
        # unboundedly across a session as skills accumulate.
        self.max_loaded_chars = 0  # set by the agent, 0 = unlimited
        # Per-session override source: ``(session_id) -> {skill_id: enabled}``,
        # wired to Config by the App. Absent key = follow the global default.
        self.session_overrides: Callable[[str], dict[str, bool]] = lambda _sid: {}
        self._state_file = self.skills_dir / ".state.json"
        self._scan()
        self._apply_saved_state()
        # Seed unconditionally: a pack shipped by a later version must also
        # reach installs that already have a non-empty skills dir.
        self._seed_builtin()

    # ---- scanning ----

    def _scan(self) -> None:
        """Walk ``skills_dir`` and index every skill (dir or loose .md).

        Pack layout::

            skills/<pack>/<skill_id>/SKILL.md     # dir skill
            skills/<pack>/<skill_id>.md           # loose skill
            skills/<pack>.md                      # loose skill, pack=root
        """
        self._skills.clear()
        if not self.skills_dir.is_dir():
            return
        for pack_dir in sorted(self.skills_dir.iterdir()):
            if pack_dir.name.startswith(".") or not pack_dir.is_dir():
                # A loose .md directly under skills_dir is a root-pack skill.
                if pack_dir.suffix == ".md" and pack_dir.is_file():
                    self._index_loose(pack_dir, pack="root")
                continue
            pack = pack_dir.name
            for entry in sorted(pack_dir.iterdir()):
                if entry.name.startswith("."):
                    continue
                if entry.is_dir():
                    skill_md = entry / "SKILL.md"
                    if skill_md.is_file():
                        self._index_dir(skill_md, pack=pack, skill_id=entry.name)
                elif entry.suffix == ".md":
                    self._index_loose(entry, pack=pack)

    # ---- persisted enable/disable state ----

    def _apply_saved_state(self) -> None:
        """Re-apply persisted OFF + ambient (always-loaded) states after a (re)scan."""
        saved = self._read_state()
        for sid in saved.get("disabled", []):
            skill = self._skills.get(sid)
            if skill is not None:
                skill.state = SkillState.OFF
                skill.body = None
                skill.ambient = False
        for sid in saved.get("ambient", []):
            skill = self._skills.get(sid)
            if skill is not None and skill.state != SkillState.OFF:
                skill.ambient = True
                if skill.path is not None and skill.path.is_file():
                    _, skill.body = self._read(skill.path)
                    skill.state = SkillState.LOADED

    def _read_state(self) -> dict[str, Any]:
        try:
            return json.loads(self._state_file.read_text("utf-8"))
        except (OSError, ValueError):
            return {}

    def _write_state(self) -> None:
        disabled = sorted(sid for sid, s in self._skills.items() if s.state == SkillState.OFF)
        ambient = sorted(sid for sid, s in self._skills.items() if s.ambient)
        try:
            self._state_file.write_text(
                json.dumps({"disabled": disabled, "ambient": ambient}, indent=2), "utf-8"
            )
        except OSError:
            pass
    def _index_dir(self, skill_md: Path, *, pack: str, skill_id: str) -> None:
        meta, _body = self._read(skill_md)
        sid = f"{pack}/{skill_id}"
        self._skills[sid] = Skill(
            id=sid,
            name=str(meta.get("name") or skill_id),
            desc=str(meta.get("description") or ""),
            keywords=list(meta.get("keywords") or meta.get("tags") or []),
            match_limit=_int_or(meta.get("match_limit"), _DEFAULT_MATCH_LIMIT),
            pack=pack,
            path=skill_md,
        )

    def _index_loose(self, md_path: Path, *, pack: str) -> None:
        if md_path.name == "DESCRIPTION.md":
            # pack-level blurb, not a skill
            return
        meta, _body = self._read(md_path)
        stem = md_path.stem
        sid = f"{pack}/{stem}" if pack != "root" else stem
        self._skills[sid] = Skill(
            id=sid,
            name=str(meta.get("name") or stem),
            desc=str(meta.get("description") or ""),
            keywords=list(meta.get("keywords") or []),
            match_limit=_int_or(meta.get("match_limit"), _DEFAULT_MATCH_LIMIT),
            pack=pack,
            path=md_path,
        )

    @staticmethod
    def _read(md_path: Path) -> tuple[dict[str, Any], str]:
        try:
            text = md_path.read_text("utf-8")
        except OSError:
            return {}, ""
        return _parse_frontmatter(text)

    # ---- seeding ----

    def _seed_builtin(self) -> None:
        """Copy shipped packs/skills missing from the skills dir.

        Runs on every boot, not just a fresh one: a pack or an individual
        skill added by a later version has to reach installs that already
        have a skills dir. Entries that are already present are left alone,
        so user edits and per-skill enable/disable state survive.
        """
        seeded = False
        for pack in sorted(BUILTIN_PACK_DIRS):
            if not pack.is_dir():
                continue
            dest = self.skills_dir / pack.name
            if not dest.exists():
                try:
                    shutil.copytree(pack, dest)
                except OSError:
                    continue
                seeded = True
                continue
            # Pack already present — pick up skills (or loose .md files) added
            # by later versions, without touching anything already there.
            for item in sorted(pack.iterdir()):
                if item.name == "__pycache__" or (dest / item.name).exists():
                    continue
                try:
                    if item.is_dir():
                        shutil.copytree(item, dest / item.name)
                    else:
                        shutil.copy2(item, dest / item.name)
                    seeded = True
                except OSError:
                    continue
        if seeded:
            self._scan()
            self._apply_saved_state()

    # ---- public API ----

    def list(self, all: bool = False, session_id: str | None = None) -> list[dict[str, Any]]:
        """Catalog rows: ``[{id, name, desc, state, ambient, keywords}]``.

        By default OFF skills are excluded (the model-facing catalog). Pass
        ``all=True`` for management UIs so disabled skills stay visible.

        ``session_id`` overlays that session's overrides onto the global
        ``ambient``/``state`` so each session's catalog reads as its own
        effective set; without it the rows are the global defaults.
        """
        ov = self.session_overrides(session_id) if session_id else {}
        rows: list[dict[str, Any]] = []
        for s in sorted(self._skills.values(), key=lambda s: s.id):
            eff_state, eff_ambient = s.state, s.ambient
            if s.id in ov:
                on = ov[s.id]
                eff_ambient = on
                if not on:
                    eff_state = SkillState.OFF
                elif s.state == SkillState.OFF:
                    eff_state = SkillState.DEMAND
            if all or eff_state != SkillState.OFF:
                row = s.catalog()
                row["state"] = eff_state
                row["ambient"] = eff_ambient
                rows.append(row)
        return rows

    @staticmethod
    def _validate_id(id: str) -> str:
        if (
            not isinstance(id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", id)
            or id in {".", ".."}
        ):
            raise ValueError("skill id must be a non-empty safe slug")
        return id

    def create(
        self, id: str, name: str, desc: str, body: str,
        keywords: list[str] | None = None, pack: str = "custom",
    ) -> str:
        sid = self._validate_id(id)
        if not isinstance(pack, str) or not pack or pack in {".", ".."} or any(
            char.isspace() or char in {"/", "\\"} for char in pack
        ):
            raise ValueError("skill pack must be a safe name")
        catalog_id = f"{pack}/{sid}"
        if catalog_id in self._skills:
            raise ValueError(f"skill already exists: {catalog_id}")
        skill_md = self.skills_dir / pack / sid / "SKILL.md"
        if skill_md.exists():
            raise ValueError(f"skill already exists: {catalog_id}")
        skill_md.parent.mkdir(parents=True, exist_ok=False)
        skill_md.write_text(
            _render_frontmatter({"name": name, "description": desc, "keywords": keywords or []})
            + "\n" + body,
            "utf-8",
        )
        self._scan()
        self._apply_saved_state()
        return catalog_id

    def save(
        self, id: str, *, name: str | None = None, desc: str | None = None,
        body: str | None = None, keywords: list[str] | None = None,
    ) -> str:
        skill = self._skills.get(id)
        if skill is None:
            raise KeyError(f"unknown skill: {id}")
        if skill.path is None:
            raise FileNotFoundError(f"skill file missing: {id}")
        meta, current_body = self._read(skill.path)
        if name is not None:
            meta["name"] = name
        if desc is not None:
            meta["description"] = desc
        if keywords is not None:
            meta["keywords"] = keywords
        if body is not None:
            current_body = body
        skill.path.write_text(_render_frontmatter(meta) + "\n" + current_body, "utf-8")
        self._scan()
        self._apply_saved_state()
        return id

    def read_body(self, id: str) -> str:
        """Return the current skill body without changing lifecycle state."""
        skill = self._skills.get(id)
        if skill is None:
            raise KeyError(f"unknown skill: {id}")
        if skill.path is None or not skill.path.is_file():
            raise FileNotFoundError(f"skill body missing: {id}")
        _, body = self._read(skill.path)
        return body

    def overview(self, id: str) -> str:
        """Return a compact skill overview without changing its load state."""
        skill = self._skills.get(id)
        if skill is None:
            raise KeyError(f"unknown skill: {id}")
        if skill.state == SkillState.OFF:
            raise ValueError(f"skill disabled: {id}")
        if skill.body is not None:
            body = skill.body
        else:
            if skill.path is None or not skill.path.is_file():
                raise FileNotFoundError(f"skill body missing: {id}")
            _, body = self._read(skill.path)
        return f"{skill.name} — {skill.desc.splitlines()[0] if skill.desc else ''}\n{_digest(body)}"

    def load(self, id: str) -> str:
        """Return the full SKILL.md body and mark the skill LOADED.

        Progressive disclosure: catalog stays cheap until this is called.
        """
        skill = self._skills.get(id)
        if skill is None:
            raise KeyError(f"unknown skill: {id}")
        if skill.state == SkillState.OFF:
            raise ValueError(f"skill disabled: {id}")
        if skill.body is None:
            if skill.path is None or not skill.path.is_file():
                raise FileNotFoundError(f"skill body missing: {id}")
            _, body = self._read(skill.path)
            skill.body = body
        skill.state = SkillState.LOADED
        self._last_use[id] = time.monotonic()
        self._evict_over_budget()
        return skill.body

    def dir_of(self, id: str) -> Path | None:
        """Directory of a dir-skill (holds references/, scripts/, assets/).

        Lets file-backed skills reach their own support files at runtime:
        the model-facing section header carries this absolute path, and
        relative paths in the body resolve against it. ``None`` for loose
        single-file skills (no sibling files to reach).
        """
        skill = self._skills.get(id)
        if skill is None or skill.path is None:
            return None
        return skill.path.parent

    def _evict_over_budget(self) -> None:
        """If LOADED skill bodies exceed ``max_loaded_chars``, unload the
        least-recently-used skills until back under budget. No-op when the
        budget is unset (0 = unlimited), so this is off by default."""
        limit = self.max_loaded_chars
        if limit <= 0:
            return
        loaded = [(sid, s) for sid, s in self._skills.items()
                  if s.state == SkillState.LOADED and s.body]
        total = sum(len(s.body or "") for _sid, s in loaded)
        if total <= limit:
            return
        # Coldest first, so eviction never touches the just-loaded skill.
        for sid, s in sorted(loaded, key=lambda pair: self._last_use.get(pair[0], 0.0)):
            if total <= limit:
                break
            total -= len(s.body or "")

    def unload(self, id: str) -> None:
        """Drop the cached body; state returns to DEMAND."""
        skill = self._skills.get(id)
        if skill is None:
            raise KeyError(f"unknown skill: {id}")
        skill.body = None
        if skill.state == SkillState.LOADED:
            skill.state = SkillState.DEMAND

    def enable(self, id: str) -> None:
        """Enable a skill = hard-reserved: always loaded and injected every turn."""
        skill = self._skills.get(id)
        if skill is None:
            raise KeyError(f"unknown skill: {id}")
        if skill.state == SkillState.OFF:
            skill.state = SkillState.DEMAND  # promote so load() accepts it
        self.load(id)  # marks LOADED + caches body
        skill.ambient = True
        self._write_state()

    def disable(self, id: str) -> None:
        skill = self._skills.get(id)
        if skill is None:
            raise KeyError(f"unknown skill: {id}")
        skill.state = SkillState.OFF
        skill.body = None
        skill.ambient = False
        self._write_state()
        self._write_state()

    def match(self, text: str) -> list[str]:
        """Case-insensitive keyword match across the catalog.

        Each skill contributes its own ``match_limit`` (default global cap 5);
        the overall result is capped at the smallest limit among matched
        skills, falling back to ``_DEFAULT_MATCH_LIMIT``.
        """
        if not text:
            return []
        haystack = text.lower()
        matches: list[tuple[int, str]] = []  # (score, id)
        cap = _DEFAULT_MATCH_LIMIT
        for skill in self._skills.values():
            if skill.state == SkillState.OFF:
                continue
            score = 0
            for kw in skill.keywords:
                if not kw:
                    continue
                if kw.lower() in haystack:
                    score += 1
            # Fall back to name/desc token overlap if no explicit keywords hit.
            if score == 0:
                for tok in _tokenize(skill.name + " " + skill.desc):
                    if tok in haystack:
                        score += 1
            if score > 0:
                matches.append((score, skill.id))
                if skill.match_limit < cap:
                    cap = skill.match_limit
        matches.sort(key=lambda pair: (-pair[0], pair[1]))
        return [sid for _score, sid in matches[: max(1, cap)]]

    # ---- pack management ----

    def _validate_pack(self, root: Path) -> None:
        """Require a pack to contain at least one valid dir or loose skill."""
        skill_files = list(root.glob("*/SKILL.md")) + list(root.glob("*.md"))
        skill_files = [p for p in skill_files if p.name not in {"DESCRIPTION.md", "README.md"}]
        if not skill_files:
            raise ValueError("skill pack must contain at least one SKILL.md or .md skill")
        for skill_md in skill_files:
            metadata, _body = self._read(skill_md)
            if not metadata.get("name") or not metadata.get("description"):
                raise ValueError(f"skill metadata requires name and description: {skill_md.name}")

    @staticmethod
    def _safe_zip_member(name: str) -> Path:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"unsafe skill archive path: {name}")
        return path

    def install(self, pack_path: Path | str) -> str:
        """Install and validate a directory or zip pack under ``skills_dir``."""
        src = Path(pack_path).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(f"pack not found: {src}")
        name = src.stem if src.suffix.lower() == ".zip" else src.name
        self._validate_id(name)
        dest = self.skills_dir / name
        if dest.exists():
            raise FileExistsError(f"pack already installed: {name}")
        if src.is_dir():
            self._validate_pack(src)
            shutil.copytree(src, dest)
        elif src.suffix.lower() == ".zip":
            with zipfile.ZipFile(src) as zf:
                for member in zf.infolist():
                    rel = self._safe_zip_member(member.filename)
                    target = dest / rel
                    if member.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(member) as source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
            try:
                self._validate_pack(dest)
            except Exception:
                shutil.rmtree(dest, ignore_errors=True)
                raise
        else:
            raise ValueError(f"unsupported pack format: {src}")
        self._scan()
        return name

    def remove(self, id: str) -> None:
        """Delete a skill (loose file) or its containing pack dir."""
        skill = self._skills.get(id)
        if skill is None:
            raise KeyError(f"unknown skill: {id}")
        if skill.path is None:
            return
        target: Path
        if skill.pack and skill.pack != "root":
            # A dir skill: remove just that skill's dir; a loose skill: remove
            # the .md file. If the pack dir becomes empty, prune it too.
            if skill.path.name == "SKILL.md":
                target = skill.path.parent
            else:
                target = skill.path
        else:
            target = skill.path
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            # rmtree on a loose file raises NotADirectoryError, which
            # ignore_errors swallows — the file would survive and the skill
            # resurrect on the next scan. Unlink files explicitly.
            try:
                target.unlink()
            except OSError:
                pass
        # Prune an empty pack dir.
        if not target.is_dir() and target.parent != self.skills_dir:
            try:
                if not any(target.parent.iterdir()):
                    target.parent.rmdir()
            except OSError:
                pass
        del self._skills[id]

    def update(self, id: str, source: Path | str) -> str:
        """Re-install a skill/pack from ``source`` over the existing one."""
        self.remove(id)
        return self.install(source)

    def manage(
        self,
        action: str,
        id: str | None = None,
        source: str | None = None,
    ) -> Any:
        """Dispatch ``install``/``update``/``remove``/``enable``/``disable``."""
        action = action.lower().strip()
        if action == "install":
            if not source:
                raise ValueError("install requires source")
            return self.install(source)
        if action == "update":
            if not id or not source:
                raise ValueError("update requires id and source")
            return self.update(id, source)
        if action == "remove":
            if not id:
                raise ValueError("remove requires id")
            self.remove(id)
            return {"removed": id}
        if action == "enable":
            if not id:
                raise ValueError("enable requires id")
            self.enable(id)
            return {"enabled": id}
        if action == "disable":
            if not id:
                raise ValueError("disable requires id")
            self.disable(id)
            return {"disabled": id}
        raise ValueError(f"unknown action: {action}")


# ---------------------------------------------------------------------------
# helpers
#

def _int_or(v: Any, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


_STOP = {"a", "an", "the", "and", "or", "of", "to", "for", "in", "on", "with", "this", "that"}


def _tokenize(s: str) -> list[str]:
    """Lowercase word tokens for fallback matching (stopwords dropped)."""
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _STOP and len(w) > 2]
