"""Every ``self._helper(...)`` in the Agent actually exists on the Agent.

Deleting a helper while a call site survives is invisible to the import and to
any test that never reaches that line: the turn dies at runtime with
``AttributeError: 'Agent' object has no attribute '_session_model'`` — chat
simply stops working. This walks the AST instead of trusting coverage.

The Agent is assembled from concern mixins (``compaction``, ``context``,
``delegation``, ``live``, ``prompt``), so a call in one module routinely lands
on a method defined in another. The check therefore pools every definition
across the whole composition and resolves each call against the pool — a split
that drops a method is caught wherever the caller lives.

Run: python -m pytest tests/test_agent_self_refs.py
"""
from __future__ import annotations

import ast
from pathlib import Path

AGENT_DIR = Path(__file__).resolve().parents[1] / "xu_brain" / "features" / "agent"
# loop.py owns Agent; the rest contribute mixin classes it inherits.
PARTS = ("loop", "compaction", "context", "delegation", "live", "prompt")


def _classes() -> list[ast.ClassDef]:
    found = []
    for part in PARTS:
        path = AGENT_DIR / f"{part}.py"
        assert path.exists(), f"missing agent module: {path}"
        tree = ast.parse(path.read_text("utf-8"), filename=str(path))
        found += [n for n in tree.body if isinstance(n, ast.ClassDef)]
    assert found, "no classes found across the agent composition"
    return found


def test_agent_calls_only_defined_attributes() -> None:
    classes = _classes()
    defined: set[str] = set()
    called: set[str] = set()
    for cls in classes:
        defined |= {n.name for n in cls.body
                    if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
        # Attributes bound anywhere in a class body (``self.x = ...``) are fine.
        defined |= {t.attr for n in ast.walk(cls) if isinstance(n, ast.Assign | ast.AnnAssign)
                    for t in ([n.target] if isinstance(n, ast.AnnAssign) else n.targets)
                    if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                    and t.value.id == "self"}
        called |= {n.func.attr for n in ast.walk(cls)
                   if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and isinstance(n.func.value, ast.Name) and n.func.value.id == "self"}
    from xu_brain.features.agent.loop import Agent
    missing = sorted(c for c in called
                     if c not in defined and not hasattr(Agent, c))
    assert not missing, f"Agent calls undefined self attributes: {missing}"


def test_every_mixin_is_actually_inherited() -> None:
    """A mixin nobody inherits is dead code that still passes every test above.

    Each non-``loop`` module contributes exactly one ``*Mixin``; if one stops
    being a base of Agent its methods vanish from the live object while the
    file (and this suite's AST pool) still looks healthy.
    """
    from xu_brain.features.agent.loop import Agent
    bases = {c.__name__ for c in Agent.__mro__}
    for part in PARTS:
        if part == "loop":
            continue
        tree = ast.parse((AGENT_DIR / f"{part}.py").read_text("utf-8"))
        mixins = [n.name for n in tree.body
                  if isinstance(n, ast.ClassDef) and n.name.endswith("Mixin")]
        assert len(mixins) == 1, f"{part}.py should define exactly one mixin, got {mixins}"
        assert mixins[0] in bases, f"{mixins[0]} is not a base of Agent (dead module)"


def test_mixins_do_not_import_the_loop() -> None:
    """The composition is a DAG: mixins may share leaves, never the assembler.

    Importing ``loop`` from a mixin would make the package import order
    load-bearing and turn a clean split back into a knot.
    """
    offenders = []
    for part in PARTS:
        if part == "loop":
            continue
        tree = ast.parse((AGENT_DIR / f"{part}.py").read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("loop"):
                offenders.append(f"{part}.py:{node.lineno}")
            if isinstance(node, ast.Import):
                offenders += [f"{part}.py:{node.lineno}" for a in node.names
                              if a.name.endswith("agent.loop")]
    assert not offenders, "mixins import loop.py (cycle): " + ", ".join(offenders)
