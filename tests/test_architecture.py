"""Enforce CLAUDE.md invariants statically (import-linter covers the same boundary in CI)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "kelpie"
FORBIDDEN_IN_CORE = (
    "kelpie.alerts",
    "kelpie.adapters",
    "kelpie.connectors",
    "kelpie.app",
    "kelpie.server",
    "kelpie.cli",
    "kelpie.metrics",
    "kelpie.traces",
)
FORBIDDEN_IN_TRACES = (
    "kelpie.adapters",
    "kelpie.connectors",
    "kelpie.rules",
    "kelpie.app",
    "kelpie.server",
    "kelpie.cli",
    "kelpie.metrics",
)
FORBIDDEN_IN_RESOURCES = (*FORBIDDEN_IN_TRACES, "kelpie.traces")
FORBIDDEN_IN_ALERTS = (*FORBIDDEN_IN_RESOURCES, "kelpie.resources")
# No LLM clients in the control loop. Only report/ may use one.
LLM_ROOTS = {"anthropic", "openai", "litellm", "langchain", "ollama", "mistralai", "vertexai"}
# google.protobuf (OTLP decoding) is fine; Google's model SDKs are not.
LLM_PREFIXES = ("google.generativeai", "google.genai", "google.ai", "google.cloud.aiplatform")


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


@pytest.mark.parametrize("path", _files("traces"), ids=_ids)
def test_traces_build_on_core_only(path: Path) -> None:
    bad = {m for m in _imports(path) if m.startswith(FORBIDDEN_IN_TRACES)}
    assert not bad, f"{path.name} imports {bad}"


@pytest.mark.parametrize("path", _files("resources"), ids=_ids)
def test_resources_build_on_core_only(path: Path) -> None:
    bad = {m for m in _imports(path) if m.startswith(FORBIDDEN_IN_RESOURCES)}
    assert not bad, f"{path.name} imports {bad}"


@pytest.mark.parametrize("path", _files("alerts"), ids=_ids)
def test_alerts_build_on_core_only(path: Path) -> None:
    bad = {m for m in _imports(path) if m.startswith(FORBIDDEN_IN_ALERTS)}
    assert not bad, f"{path.name} imports {bad}"


@pytest.mark.parametrize(
    "path",
    _files("core", "rules", "traces", "resources", "alerts", "server.py", "app.py"),
    ids=_ids,
)
def test_control_loop_has_no_llm(path: Path) -> None:
    bad = {
        m
        for m in _imports(path)
        if m.split(".")[0] in LLM_ROOTS
        or m.startswith(LLM_PREFIXES)
        or m.startswith("kelpie.report.root_cause")  # Kelpie's own LLM client (raw HTTP)
    }
    assert not bad, f"{path.name} imports LLM client {bad}"


SIMULATOR_FREE = (
    "core",
    "rules",
    "adapters",
    "connectors",
    "alerts",
    "traces",
    "resources",
    "report",
    "app.py",
    "server.py",
    "metrics.py",
    "net.py",
    "remote",
    "doctor.py",
    "why.py",
    "snooze.py",
    "cli.py",  # the demo command imports it lazily, inside the command
)


@pytest.mark.parametrize("path", _files(*SIMULATOR_FREE), ids=_ids)
def test_the_simulator_stays_out_of_kelpie_itself(path: Path) -> None:
    """kelpie.sim (fake GitHub, fake hermes CLI) is for the demo and tests only."""
    tree = ast.parse(path.read_text())
    top = {
        m
        for node in tree.body
        if isinstance(node, ast.Import | ast.ImportFrom)
        for m in _imports_in(ast.unparse(node), _module_name(path).rpartition(".")[0])
    }
    found = top if path.name == "cli.py" else _imports(path)
    bad = {m for m in found if m.startswith(("kelpie.sim", "kelpie.demo"))}
    assert not bad, f"{path.name} imports {bad}"


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
