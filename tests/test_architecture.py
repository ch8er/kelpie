"""Enforce CLAUDE.md invariants statically."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kelpie"
FORBIDDEN_IN_CORE = ("kelpie.adapters", "kelpie.connectors")
# No LLM clients in the control loop (core + rules). The report may use one.
LLM_MODULES = ("anthropic", "openai", "litellm", "langchain", "google.generativeai", "ollama")


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return found


def _files(*packages: str) -> list[Path]:
    return sorted(p for pkg in packages for p in (SRC / pkg).rglob("*.py"))


@pytest.mark.parametrize("path", _files("core", "rules"), ids=lambda p: str(p.relative_to(SRC)))
def test_core_and_rules_do_not_import_integrations(path: Path) -> None:
    bad = {m for m in _imports(path) if m.startswith(FORBIDDEN_IN_CORE)}
    assert not bad, f"{path.name} imports {bad}"


@pytest.mark.parametrize("path", _files("core", "rules"), ids=lambda p: str(p.relative_to(SRC)))
def test_control_loop_has_no_llm(path: Path) -> None:
    bad = {m for m in _imports(path) if m.split(".")[0] in {x.split(".")[0] for x in LLM_MODULES}}
    assert not bad, f"{path.name} imports LLM client {bad}"
