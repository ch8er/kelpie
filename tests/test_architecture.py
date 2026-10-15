"""Enforce CLAUDE.md invariants statically (import-linter covers the same boundary in CI)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kelpie"
FORBIDDEN_IN_CORE = ("kelpie.adapters", "kelpie.connectors")
# No LLM clients in the control loop. Only report/ may use one.
LLM_ROOTS = {"anthropic", "openai", "litellm", "langchain", "google", "ollama", "mistralai"}


def _module_name(path: Path) -> str:
    parts = path.relative_to(SRC.parent).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _imports(path: Path) -> set[str]:
    module = _module_name(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    return _imports_in(path.read_text(), package)


def _imports_in(source: str, package: str) -> set[str]:
    """Absolute, relative and ``importlib.import_module("...")`` imports in ``source``."""
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)]
                found.add(".".join([*base, node.module] if node.module else base))
            elif node.module:
                found.add(node.module)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute | ast.Name)
            and getattr(node.func, "attr", getattr(node.func, "id", None)) == "import_module"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            found.add(node.args[0].value)
    return found


def _files(*names: str) -> list[Path]:
    paths: list[Path] = []
    for name in names:
        target = SRC / name
        paths.extend(sorted(target.rglob("*.py")) if target.is_dir() else [target])
    return paths


def _ids(path: Path) -> str:
    return str(path.relative_to(SRC))


@pytest.mark.parametrize("path", _files("core", "rules"), ids=_ids)
def test_core_and_rules_do_not_import_integrations(path: Path) -> None:
    bad = {m for m in _imports(path) if m.startswith(FORBIDDEN_IN_CORE)}
    assert not bad, f"{path.name} imports {bad}"


@pytest.mark.parametrize("path", _files("core", "rules", "server.py", "app.py"), ids=_ids)
def test_control_loop_has_no_llm(path: Path) -> None:
    bad = {m for m in _imports(path) if m.split(".")[0] in LLM_ROOTS}
    assert not bad, f"{path.name} imports LLM client {bad}"


def test_detector_sees_relative_and_dynamic_imports() -> None:
    source = (
        "from ..adapters import hermes\n"
        "from . import model\n"
        "import importlib\n"
        "importlib.import_module('kelpie.connectors.github')\n"
    )
    assert _imports_in(source, "kelpie.core") == {
        "kelpie.adapters",
        "kelpie.core",
        "importlib",
        "kelpie.connectors.github",
    }
