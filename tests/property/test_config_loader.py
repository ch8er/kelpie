"""The config loader: whatever the file holds, ``load_settings`` (and the rules it
configures) either loads or raises ``ConfigError``, which ``kelpie serve`` prints as one
line. Any other exception is a traceback where a message naming the key belongs.

Every TOML block of the docs and ``kelpie.example.toml`` is a starting point, then a few of
its values are swapped for values of the wrong type or range, keys are dropped or added."""

from __future__ import annotations

import copy
import re
import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kelpie import app
from kelpie.app import ConfigError, load_rules, load_settings
from tests.property.strategies import paths as _paths

ROOT = Path(__file__).parents[2]


def _bases() -> list[dict[str, Any]]:
    found = [tomllib.loads((ROOT / "kelpie.example.toml").read_text())]
    for doc in sorted((ROOT / "docs").rglob("*.md")):
        for block in re.findall(r"```toml\n(.*?)```", doc.read_text(), re.S):
            found.append(tomllib.loads(block))
    return found


BASES = _bases()

DURATIONS = st.sampled_from(["0s", "1s", "-1s", "1h", "30m", "7d", "1h30m", "99999999999d", "x"])
LEAVES = (
    st.booleans()
    | st.integers(min_value=-(2**63), max_value=2**63 - 1)
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.text(max_size=20)
    | DURATIONS
    | st.just("~/x")
    | st.just("")
    | st.datetimes(min_value=datetime(1, 1, 2), timezones=st.just(UTC))
    | st.dates(min_value=date(1, 1, 1))
    | st.times()
)
VALUES = st.recursive(
    LEAVES,
    lambda inner: (
        st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=3)
    ),
    max_leaves=6,
)


def _set(data: Any, path: tuple[Any, ...], value: Any) -> None:
    for key in path[:-1]:
        data = data[key]
    if value is _DROP:
        if isinstance(data, dict):
            data.pop(path[-1], None)
        else:
            data[path[-1]] = None if not data else data[0]
    else:
        data[path[-1]] = value


_DROP = object()


@pytest.fixture(autouse=True)
def _no_files(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)


@given(st.sampled_from(range(len(BASES))), st.data())
def test_any_config_loads_or_is_a_config_error(i: int, data: st.DataObject) -> None:
    config = copy.deepcopy(BASES[i])
    paths = _paths(config)
    for _ in range(data.draw(st.integers(min_value=0, max_value=4))):
        if not paths:
            break
        _set(config, data.draw(st.sampled_from(paths)), data.draw(VALUES | st.just(_DROP)))
        paths = _paths(config)
    if data.draw(st.booleans()):
        config[data.draw(st.sampled_from(sorted({k for b in BASES for k in b})))] = data.draw(
            VALUES
        )
    _load(config)


def _load(config: dict[str, Any]) -> bool:
    """Whether ``config`` loaded; a config error is False, anything else raises."""
    original = app.read_config
    app.read_config = lambda path: config  # type: ignore[assignment]
    try:
        load_rules(load_settings(Path("kelpie.toml")).engine)
        return True
    except ConfigError:
        return False
    finally:
        app.read_config = original  # type: ignore[assignment]


def test_every_base_loads() -> None:
    """The starting points load as they are: changes to them are what the property tests."""
    assert all(_load(copy.deepcopy(base)) for base in BASES)
