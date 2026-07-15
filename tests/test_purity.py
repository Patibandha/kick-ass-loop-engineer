"""Import-purity guard: the core package depends on stdlib + lazy pyyaml only.

The DoD invariant is that ``kickass_loop_engineer`` imports nothing third-party
except ``pyyaml`` — and that ``yaml`` is imported LAZILY (inside functions), never
eagerly at module scope, so merely importing the package pulls in zero third-party
code. This test walks every module in the package, classifies every import, and
fails on any disallowed dependency. ``scripts/`` (the demo-GIF generator, which
imports Pillow) is deliberately OUT of scope — it lives outside the package and is
never imported by it.
"""
import ast
import os
import sys

PACKAGE = "kickass_loop_engineer"
#: The single third-party dependency the core is allowed to import (lazily).
ALLOWED_THIRD_PARTY = {"yaml"}

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG_DIR = os.path.join(_ROOT, "src", PACKAGE)


def _package_modules() -> list:
    """Return absolute paths of every ``.py`` file in the package tree."""
    modules = []
    for dirpath, _dirs, files in os.walk(_PKG_DIR):
        for name in files:
            if name.endswith(".py"):
                modules.append(os.path.join(dirpath, name))
    assert modules, "no package modules found — wrong path?"
    return modules


def _top_module(name: str) -> str:
    """Return the top-level package of a dotted import name."""
    return name.split(".", 1)[0]


def _imports(tree: ast.AST):
    """Yield ``(top_module, is_eager)`` for every absolute import in ``tree``.

    ``is_eager`` is True when the import runs at module load (module/class scope),
    False when it is nested inside a function (a lazy import). Relative imports and
    ``__future__`` are skipped — they are first-party / compiler directives.
    """
    def walk(node, in_function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Import):
                for alias in child.names:
                    yield _top_module(alias.name), not in_function
            elif isinstance(child, ast.ImportFrom):
                if child.level == 0 and child.module and child.module != "__future__":
                    yield _top_module(child.module), not in_function
            nested = in_function or isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef))
            yield from walk(child, nested)

    yield from walk(tree, False)


def _classify(module: str) -> str:
    """Return 'stdlib', 'first_party', 'allowed_third_party', or 'forbidden'."""
    if module == PACKAGE:
        return "first_party"
    if module in sys.stdlib_module_names:
        return "stdlib"
    if module in ALLOWED_THIRD_PARTY:
        return "allowed_third_party"
    return "forbidden"


def test_core_imports_stdlib_and_lazy_pyyaml_only():
    forbidden = []
    eager_yaml = []
    for path in _package_modules():
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        rel = os.path.relpath(path, _ROOT)
        for module, is_eager in _imports(tree):
            kind = _classify(module)
            if kind == "forbidden":
                forbidden.append(f"{rel}: {module}")
            elif kind == "allowed_third_party" and is_eager:
                eager_yaml.append(f"{rel}: {module}")

    assert not forbidden, (
        "core package imports third-party modules other than lazy pyyaml: "
        + ", ".join(sorted(forbidden)))
    assert not eager_yaml, (
        "pyyaml must be imported LAZILY (inside functions), not at module scope: "
        + ", ".join(sorted(eager_yaml)))
