"""AST-only architecture guards for the xu_brain package layout."""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "xu_brain"

# Explicit composition-root exceptions.  These are deliberately file-specific:
# runtime.py may wire features, while config.py may defer only its preset import.
RUNTIME_EXCEPTION = "xu_brain.core.runtime"
CONFIG_MODULES = ("xu_brain.core.config", "xu_brain.config")
CONFIG_PRESET_EXCEPTION = "xu_brain.core.config"


@dataclass(frozen=True)
class Module:
    name: str
    path: Path
    tree: ast.Module
    future_annotations: bool


def _modules() -> dict[str, Module]:
    result = {}
    for path in sorted(ROOT.rglob("*.py")):
        rel = path.relative_to(ROOT.parent).with_suffix("")
        name = ".".join(rel.parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        future = any(
            isinstance(n, ast.ImportFrom)
            and n.module == "__future__"
            and any(a.name == "annotations" for a in n.names)
            for n in tree.body
        )
        result[name] = Module(name, path, tree, future)
    assert result, f"no Python modules found below {ROOT}"
    return result


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parents = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    return parents


def _inside_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = parents.get(node)
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return True
        current = parents.get(current)
    return False


def _type_checking_guarded(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    current = node
    while current in parents:
        parent = parents[current]
        if isinstance(parent, ast.If) and parent.test.__class__ is ast.Name:
            if parent.test.id == "TYPE_CHECKING":
                return True
        current = parent
    return False


def _absolute_import(source: str, node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.Import):
        names = [a.name for a in node.names]
        if any(not n or any(part == "" for part in n.split(".")) for n in names):
            raise AssertionError(f"unparseable import in {source}: {ast.unparse(node)}")
        return names
    if node.level < 0 or (node.level == 0 and node.module is None):
        raise AssertionError(f"unparseable import in {source}: {ast.unparse(node)}")
    if node.level == 0:
        assert node.module is not None
        return [node.module]
    package = source.split(".")[:-1]
    if node.level > len(package) + 1:
        raise AssertionError(f"relative import escapes package in {source}: {ast.unparse(node)}")
    base = package[: len(package) - node.level + 1]
    prefix = ".".join(base + ([node.module] if node.module else []))
    if not prefix:
        raise AssertionError(f"unparseable import in {source}: {ast.unparse(node)}")
    # ``from . import x`` names modules below the resolved package.
    return [prefix + ("." + a.name if node.module is None else "") for a in node.names]


def _imports(mod: Module, *, runtime_only: bool = False):
    """Yield ``(node, local, absolute_names)``.  With ``runtime_only`` only the
    imports that actually execute at import time are yielded."""
    parents = _parents(mod.tree)
    for node in ast.walk(mod.tree):
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        local = _inside_function(node, parents)
        if runtime_only and local:
            continue
        # TYPE_CHECKING imports are runtime edges only without postponed annotations.
        if runtime_only and _type_checking_guarded(node, parents) and mod.future_annotations:
            continue
        yield node, local, _absolute_import(mod.name, node)


def _existing(modules: dict[str, Module], name: str) -> str | None:
    if name in modules:
        return name
    parts = name.split(".")
    while parts and ".".join(parts) not in modules:
        parts.pop()
    return ".".join(parts) if parts else None


def test_core_does_not_import_features_or_plugins_at_module_level():
    modules = _modules()
    violations = []
    for mod in modules.values():
        if not (mod.name == "xu_brain.core" or mod.name.startswith("xu_brain.core.")) or mod.name == RUNTIME_EXCEPTION:
            continue  # runtime.py is the explicit composition-root allowlist entry.
        for node, local, names in _imports(mod):
            if not local and any(n.startswith(("xu_brain.features", "xu_brain.plugins")) for n in names):
                violations.append(f"{mod.name}: {ast.unparse(node)}")
    assert not violations, "core module-level imports from features/plugins:\n" + "\n".join(violations)


def test_config_preset_import_is_deferred():
    modules = _modules()
    mod = next((modules[name] for name in CONFIG_MODULES if name in modules), None)
    if mod is None:
        return
    matches = [(node, local) for node, local, names in _imports(mod) if any(n.endswith("presets") for n in names)]
    assert matches, f"{mod.path} must contain its deferred presets import"
    assert all(local for _, local in matches), f"{mod.path} presets import must remain inside session_preset()"


def test_api_is_a_leaf_contract():
    modules = _modules()
    bad = []
    for mod in modules.values():
        if not (mod.name == "xu_brain.api" or mod.name.startswith("xu_brain.api.")):
            continue
        for node, local, names in _imports(mod):
            if not local and any(n.startswith(("xu_brain.core", "xu_brain.features", "xu_brain.plugins")) for n in names):
                bad.append(f"{mod.name}: {ast.unparse(node)}")
    assert not bad, "api module-level imports from forbidden layers:\n" + "\n".join(bad)


def test_governance_imports_stdlib_only():
    modules = _modules()
    mod = modules.get("xu_brain.core.governance")
    if mod is None:
        return
    bad = [f"{ast.unparse(node)}" for node, _, names in _imports(mod) if any(n.startswith("xu_brain.") for n in names)]
    assert not bad, "core/governance.py has intra-package imports: " + "; ".join(bad)


def test_features_do_not_import_plugins():
    # The invariant is that no feature *executes* an import of plugins: plugins
    # depend on features (a hook transforms a ToolResult), so a runtime edge back
    # would close the cycle.  An import under ``if TYPE_CHECKING`` in a module
    # with postponed annotations never runs and its annotations stay strings, so
    # it is a type-only edge, not a layering violation — same exemption the cycle
    # test applies.  A plain module-level ``from ...plugins import X`` still fails.
    bad = []
    for mod in _modules().values():
        if not (mod.name == "xu_brain.features" or mod.name.startswith("xu_brain.features.")):
            continue
        for node, _, names in _imports(mod, runtime_only=True):
            if any(n.startswith("xu_brain.plugins") for n in names):
                bad.append(f"{mod.name}: {ast.unparse(node)}")
    assert not bad, "feature module-level imports from plugins:\n" + "\n".join(bad)


def test_module_dependency_graph_has_no_cycles():
    modules = _modules()
    graph = {name: set() for name in modules}
    for mod in modules.values():
        for _, _, names in _imports(mod, runtime_only=True):
            for name in names:
                target = _existing(modules, name)
                if target:
                    graph[mod.name].add(target)
    visiting, done = set(), set()

    def visit(name: str, path: list[str]):
        if name in visiting:
            cycle = path[path.index(name):] + [name]
            raise AssertionError("import cycle: " + " -> ".join(cycle))
        if name in done:
            return
        visiting.add(name)
        for target in sorted(graph[name]):
            visit(target, path + [name])
        visiting.remove(name)
        done.add(name)

    for name in sorted(graph):
        visit(name, [])


def test_every_package_directory_has_init():
    # Shipped plugin packages (plugins/builtin/<name>/) are deliberately not
    # importable: the host loads them by path from data_home after seeding, so
    # an __init__.py there would claim an import surface that never exists.
    def _is_shipped_plugin(p):
        return "builtin" in p.relative_to(ROOT).parts and (p / "manifest.json").is_file()

    missing = [
        str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
        if p.is_dir() and p.name != "__pycache__" and any(p.glob("*.py"))
        and not (p / "__init__.py").exists() and not _is_shipped_plugin(p)
    ]
    assert not missing, "package directories missing __init__.py: " + ", ".join(sorted(missing))


def test_import_resolver_rejects_unparseable_relative_import():
    tree = ast.parse("from . import x")
    node = tree.body[0]
    assert _absolute_import("xu_brain.api.context", node) == ["xu_brain.api.x"]
    try:
        _absolute_import("xu_brain", ast.ImportFrom(module=None, names=[ast.alias(name="x")], level=2))
    except AssertionError:
        pass
    else:
        raise AssertionError("resolver accepted a relative import escaping the package")
