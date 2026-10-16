"""A minimal TOML writer for generated configs (the standard library only reads TOML).

Supports what a Kelpie config uses: nested tables, arrays of tables, strings, booleans,
numbers and lists of scalars. The output is checked by parsing it back with ``tomllib``.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from typing import Any


def _scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)  # a JSON string is a valid TOML basic string
    if isinstance(value, Sequence):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    if isinstance(value, Mapping):
        return "{" + ", ".join(f"{_key(k)} = {_scalar(v)}" for k, v in value.items()) + "}"
    raise TypeError(f"cannot write {value!r} as TOML")


def _key(key: str) -> str:
    return key if key.replace("_", "").replace("-", "").isalnum() else json.dumps(key)


def _is_table_array(value: Any) -> bool:
    return (
        isinstance(value, Sequence)
        and not isinstance(value, str)
        and bool(value)
        and all(isinstance(v, Mapping) for v in value)
    )


def _table(path: list[str], table: Mapping[str, Any], out: list[str]) -> None:
    scalars = [
        (k, v) for k, v in table.items() if not isinstance(v, Mapping) and not _is_table_array(v)
    ]
    if path and (scalars or not table):
        out.append(f"\n[{'.'.join(_key(p) for p in path)}]")
    out.extend(f"{_key(k)} = {_scalar(v)}" for k, v in scalars)
    for k, v in table.items():
        if isinstance(v, Mapping):
            _table([*path, k], v, out)
        elif _is_table_array(v):
            for item in v:
                out.append(f"\n[[{'.'.join(_key(p) for p in [*path, k])}]]")
                nested = {ik: iv for ik, iv in item.items() if isinstance(iv, Mapping)}
                out.extend(
                    f"{_key(ik)} = {_scalar(iv)}"
                    for ik, iv in item.items()
                    if not isinstance(iv, Mapping)
                )
                for ik, iv in nested.items():  # e.g. [connectors.llm_quotas.providers.fields]
                    _table([*path, k, ik], iv, out)


def dumps(data: Mapping[str, Any]) -> str:
    out: list[str] = []
    _table([], data, out)
    text = "\n".join(out).lstrip("\n") + "\n"
    if tomllib.loads(text) != _plain(data):
        raise ValueError("generated TOML does not round-trip")
    return text


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_plain(v) for v in value]
    return value
