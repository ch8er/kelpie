"""Path-valued config keys, in one place: ``~`` is expanded in every one of them as the
config file is read, before any section is loaded, so ``[storage] path``, ``[report] dir``,
the boards' ``db_path`` and the rest all behave the same (issue 334).

Only ``~`` and ``~user`` at the start of a value are expanded (as the shell does), never
``$VARS``: a value is data, not a command line. ``$KELPIE_DB``, which replaces ``[storage]
path``, is expanded the same way. A value that is not a string (``false`` to turn a path
off) is left as it is, for its section's loader to accept or refuse.

``PATH_KEYS`` lists every such key. A key is a dotted path into the file's tables; a part
ending in ``[]`` is a list (an array of tables, or of strings for the last part). A new
path-valued key goes here; ``tests/test_config_paths.py`` fails until it does.
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class Kind(Enum):
    """What ``kelpie doctor`` expects of a configured path."""

    EXISTS = "exists"  # read by Kelpie: must exist (a database, a directory, a file)
    PARENT = "parent"  # created by Kelpie in an existing directory (the ledger)
    CREATED = "created"  # created by Kelpie or a hook with its directories, when first used
    PATTERN = "pattern"  # glob patterns: matching nothing is not an error
    COMMAND = "command"  # an executable: a path when it has a "/", else looked up on PATH


PATH_KEYS: Mapping[str, Kind] = {
    "storage.path": Kind.PARENT,
    "report.dir": Kind.EXISTS,
    "traces.token_file": Kind.EXISTS,
    "traces.tokens[].token_file": Kind.EXISTS,
    "agent.ca_file": Kind.EXISTS,
    "hub.tls_cert": Kind.EXISTS,
    "hub.tls_key": Kind.EXISTS,
    "instructions.files[].paths[]": Kind.PATTERN,
    "adapters.hermes.db_path": Kind.EXISTS,
    "adapters.hermes.spool": Kind.CREATED,
    "adapters.hermes.cli": Kind.COMMAND,
    "adapters.hermes.wait_key": Kind.CREATED,
    "adapters.hermes.state_home": Kind.EXISTS,
    "adapters.hermes.boards[].db_path": Kind.EXISTS,
    "adapters.hermes.boards[].wait_key": Kind.CREATED,
    "adapters.hermes.tools.slots[].paths[]": Kind.PATTERN,
    "adapters.claude_code.tasks_dir": Kind.EXISTS,
    "adapters.claude_code.spool": Kind.CREATED,
    "adapters.claude_code.stop_dir": Kind.CREATED,
    "adapters.openclaw.state_dir": Kind.EXISTS,
    "adapters.openclaw.workboard": Kind.EXISTS,
    "adapters.openclaw.spool": Kind.CREATED,
    "adapters.openclaw.stop_dir": Kind.CREATED,
    "adapters.openclaw.cli": Kind.COMMAND,
}


@dataclass(frozen=True, slots=True)
class ConfiguredPath:
    """One path value set in the config file, after expansion."""

    key: str  # where it is, with list positions: adapters.hermes.boards[2].db_path
    value: str
    kind: Kind


def expand(value: str) -> str:
    """``~`` or ``~user`` at the start (after leading blanks, which loaders strip) expanded;
    nothing else (no ``$VARS``)."""
    bare = value.lstrip()
    return os.path.expanduser(bare) if bare.startswith("~") else value


def read_config(path: Path) -> dict[str, Any]:
    """The config file's tables with ``~`` expanded in every key of ``PATH_KEYS``. Raises
    ``OSError`` or ``tomllib.TOMLDecodeError``."""
    with path.open("rb") as f:
        data = tomllib.load(f)
    for key in PATH_KEYS:
        _expand_at(data, key.split("."))
    return data


def configured_paths(data: Mapping[str, Any]) -> list[ConfiguredPath]:
    """Every path value the config sets (strings only), in ``PATH_KEYS`` order; none of an
    adapter whose section says ``enabled = false``."""
    found: list[ConfiguredPath] = []
    adapters = data.get("adapters")
    off = {
        f"adapters.{name}."
        for name, section in (adapters.items() if isinstance(adapters, Mapping) else ())
        if isinstance(section, Mapping) and section.get("enabled") is False
    }
    for key, kind in PATH_KEYS.items():
        if any(key.startswith(prefix) for prefix in off):
            continue
        for where, value in _values(data, key.split("."), ""):
            if isinstance(value, str):
                found.append(ConfiguredPath(where, value, kind))
    return found


def _expand_at(node: object, parts: list[str]) -> None:
    head, rest = parts[0], parts[1:]
    is_list = head.endswith("[]")
    name = head.removesuffix("[]")
    if not isinstance(node, dict) or name not in node:
        return
    value = node[name]
    if is_list:
        if isinstance(value, str) and not rest:  # one pattern for a list of them
            node[name] = expand(value)
            return
        if not isinstance(value, list):
            return
        if rest:
            for item in value:
                _expand_at(item, rest)
        else:
            node[name] = [expand(v) if isinstance(v, str) else v for v in value]
    elif rest:
        _expand_at(value, rest)
    elif isinstance(value, str):
        node[name] = expand(value)


def _values(node: object, parts: list[str], at: str) -> Iterator[tuple[str, object]]:
    head, rest = parts[0], parts[1:]
    is_list = head.endswith("[]")
    name = head.removesuffix("[]")
    if not isinstance(node, Mapping) or name not in node:
        return
    here = f"{at}.{name}" if at else name
    value = node[name]
    if not is_list:
        if rest:
            yield from _values(value, rest, here)
        else:
            yield here, value
        return
    if isinstance(value, str) and not rest:
        yield f"{here}[1]", value
        return
    if not isinstance(value, list):
        return
    for i, item in enumerate(value, 1):
        if rest:
            yield from _values(item, rest, f"{here}[{i}]")
        else:
            yield f"{here}[{i}]", item
