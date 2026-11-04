"""``~`` in every path-valued config key (issue 334), from one list: ``PATH_KEYS``.

The settings types are walked for every field that holds a path, so a new one fails here
until it is mapped to its key and the key is in ``PATH_KEYS``; then configs that set every
key to a ``~`` value are loaded and each field is checked expanded.
"""

from __future__ import annotations

import dataclasses
import re
import typing
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from kelpie.app import Settings, load_settings
from kelpie.configpaths import PATH_KEYS, configured_paths, expand, read_config

# Every settings field that holds a path -> the config key it is loaded from. The fields
# are found by type (``Path`` anywhere in the annotation) and by name for the paths kept as
# strings (an executable, glob patterns).
FIELDS: dict[str, str] = {
    "Settings.db_path": "storage.path",
    "ReportSettings.dir": "report.dir",
    "TraceSettings.token_file": "traces.token_file",
    "AgentSettings.ca_file": "agent.ca_file",
    "HubSettings.tls_cert": "hub.tls_cert",
    "HubSettings.tls_key": "hub.tls_key",
    "Source.paths": "instructions.files[].paths[]",
    "HermesSettings.db_path": "adapters.hermes.db_path",
    "HermesSettings.spool": "adapters.hermes.spool",
    "HermesSettings.cli": "adapters.hermes.cli",
    "HermesSettings.wait_key": "adapters.hermes.wait_key",
    "HermesSettings.state_home": "adapters.hermes.state_home",
    "HermesBoardSettings.db_path": "adapters.hermes.boards[].db_path",
    "HermesBoardSettings.wait_key": "adapters.hermes.boards[].wait_key",
    "SlotPool.paths": "adapters.hermes.tools.slots[].paths[]",
    "ClaudeCodeSettings.tasks_dir": "adapters.claude_code.tasks_dir",
    "ClaudeCodeSettings.spool": "adapters.claude_code.spool",
    "ClaudeCodeSettings.stop_dir": "adapters.claude_code.stop_dir",
    "OpenClawSettings.state_dir": "adapters.openclaw.state_dir",
    "OpenClawSettings.workboard": "adapters.openclaw.workboard",
    "OpenClawSettings.spool": "adapters.openclaw.spool",
    "OpenClawSettings.stop_dir": "adapters.openclaw.stop_dir",
    "OpenClawSettings.cli": "adapters.openclaw.cli",
}
NOT_CONFIG = {"Settings.source"}  # the config file itself
PATH_NAMES = {"cli", "paths"}  # path values kept as strings


def path_fields() -> set[str]:
    found: set[str] = set()
    seen: set[type] = set()

    def walk(cls: type) -> None:
        if cls in seen:
            return
        seen.add(cls)
        hints = typing.get_type_hints(cls)
        for f in dataclasses.fields(cls):
            hint = hints[f.name]
            if _mentions(hint, Path) or f.name in PATH_NAMES:
                found.add(f"{cls.__name__}.{f.name}")
            for arg in _flat(hint):
                if isinstance(arg, type) and dataclasses.is_dataclass(arg):
                    walk(arg)

    walk(Settings)
    return found - NOT_CONFIG


def _flat(hint: object) -> Iterator[object]:
    yield hint
    for arg in typing.get_args(hint):
        yield from _flat(arg)


def _mentions(hint: object, kind: type) -> bool:
    return any(arg is kind for arg in _flat(hint))


def test_every_path_field_is_in_path_keys() -> None:
    assert path_fields() == set(FIELDS), (
        "a settings field holds a path: add its key to kelpie.configpaths.PATH_KEYS "
        "and the field to FIELDS here"
    )
    assert set(FIELDS.values()) == set(PATH_KEYS)


def _single(home: Path) -> str:
    return """
[storage]
path = "~/kelpie/kelpie.db"
[report]
dir = "~/kelpie/reports"
[traces]
token_file = "~/kelpie/traces.env"
[agent]
name = "host-a"
server = "https://hub.example.org"
ca_file = "~/kelpie/ca.crt"
[instructions]
[[instructions.files]]
paths = ["~/agents/*/AGENTS.md", "/srv/agents/*.md"]
[adapters.hermes]
db_path = "~/.hermes/kanban.db"
spool = "~/.hermes/kelpie/spool.jsonl"
cli = "~/bin/hermes"
wait_key = "~/kelpie/hermes-wait.key"
state_home = "~/.hermes"
[adapters.hermes.tools]
names = ["xcodebuild"]
[[adapters.hermes.tools.slots]]
name = "sim"
paths = ["~/locks/sim-*.lock"]
[adapters.claude_code]
tasks_dir = "~/.claude/tasks"
spool = "~/.claude/kelpie/spool.jsonl"
stop_dir = "~/.claude/kelpie/stop"
[adapters.openclaw]
state_dir = "~/.openclaw"
workboard = "~/.openclaw/workboard.json"
spool = "~/.openclaw/kelpie/spool.jsonl"
stop_dir = "~/.openclaw/kelpie/stop"
cli = "~/bin/openclaw"
"""


def _boards(home: Path) -> str:
    return """
[storage]
path = "~/kelpie/kelpie.db"
[hub]
tls_cert = "~/kelpie/tls.crt"
tls_key = "~/kelpie/tls.key"
[[hub.agents]]
name = "host-a"
token_env = "KELPIE_AGENT_HOST_A"
[adapters.hermes]
[[adapters.hermes.boards]]
name = "qr"
db_path = "~/.hermes/kanban/boards/qr/kanban.db"
wait_key = "~/kelpie/qr.key"
[[adapters.hermes.boards]]
name = "sign"
db_path = "~/.hermes/kanban/boards/sign/kanban.db"
"""


def _get(obj: object, attr: str) -> Iterator[object]:
    """Every value of ``attr`` on instances of its class reachable from ``obj``."""
    cls, name = attr.split(".")
    seen: set[int] = set()

    def walk(node: object) -> Iterator[object]:
        if id(node) in seen or node is None or isinstance(node, str | Path | int | float):
            return
        seen.add(id(node))
        if type(node).__name__ == cls:
            yield getattr(node, name)
        if dataclasses.is_dataclass(node) and not isinstance(node, type):
            for f in dataclasses.fields(node):
                yield from walk(getattr(node, f.name))
        elif isinstance(node, tuple | list):
            for item in node:
                yield from walk(item)
        elif isinstance(node, dict):
            for item in node.values():
                yield from walk(item)

    return walk(obj)


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str | Path):
        yield str(value)
    elif isinstance(value, tuple | list):
        for v in value:
            yield from _strings(v)


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("KELPIE_DB", raising=False)
    return home


@pytest.mark.parametrize("make", [_single, _boards])
def test_tilde_is_expanded_in_every_path_key(
    make: Any, home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KELPIE_AGENT_HOST_A", "x" * 32)
    config = tmp_path / "kelpie.toml"
    config.write_text(make(home))
    settings = load_settings(config)
    configured = {_key(found.key) for found in configured_paths(read_config(config))}
    checked = 0
    for attr, key in FIELDS.items():
        if key not in configured:  # a default (e.g. db_path beside boards): not read
            continue
        for value in _get(settings, attr):
            for text in _strings(value):
                assert not text.startswith("~"), f"{key} ({attr}) kept ~: {text}"
                checked += 1
    assert checked
    assert settings.db_path == home / "kelpie" / "kelpie.db"


def test_the_configs_above_set_every_path_key(home: Path, tmp_path: Path) -> None:
    keys: set[str] = set()
    for make in (_single, _boards):
        config = tmp_path / "kelpie.toml"
        config.write_text(make(home))
        for found in configured_paths(read_config(config)):
            assert not found.value.startswith("~")
            keys.add(_key(found.key))
    assert keys == set(PATH_KEYS)


def _key(where: str) -> str:
    """``adapters.hermes.boards[2].db_path`` -> its ``PATH_KEYS`` key."""
    return re.sub(r"\[\d+\]", "[]", where)


def test_only_a_leading_tilde_and_no_variables(home: Path) -> None:
    assert expand("~/x") == f"{home}/x"
    assert expand("~") == str(home)
    assert expand("$HOME/x") == "$HOME/x"
    assert expand("/a/~/b") == "/a/~/b"
    assert expand("x~") == "x~"


def test_non_path_keys_and_false_are_left_alone(home: Path, tmp_path: Path) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text(
        '[adapters.hermes]\nspool = false\nauthor = "~kelpie"\n[storage]\npath = ":memory:"\n'
    )
    data = read_config(config)
    assert data["adapters"]["hermes"] == {"spool": False, "author": "~kelpie"}
    assert data["storage"]["path"] == ":memory:"


def test_one_pattern_for_a_list_and_leading_blanks(home: Path, tmp_path: Path) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text(
        '[instructions]\n[[instructions.files]]\npaths = "~/AGENTS.md"\n'
        '[report]\ndir = "  ~/reports"\n'
    )
    data = read_config(config)
    assert data["instructions"]["files"][0]["paths"] == f"{home}/AGENTS.md"
    assert data["report"]["dir"] == f"{home}/reports"
    keys = [p.key for p in configured_paths(data)]
    assert keys == ["report.dir", "instructions.files[1].paths[1]"]


def test_a_disabled_adapters_paths_are_not_listed(home: Path, tmp_path: Path) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text(
        '[adapters.claude_code]\nenabled = false\ntasks_dir = "~/gone"\n'
        '[adapters.openclaw]\nstate_dir = "~/.openclaw"\n'
    )
    assert [p.key for p in configured_paths(read_config(config))] == ["adapters.openclaw.state_dir"]


def test_kelpie_db_is_expanded_too(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KELPIE_DB", "~/kelpie/env.db")
    assert load_settings(None).db_path == home / "kelpie" / "env.db"
