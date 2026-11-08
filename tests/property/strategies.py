"""Hypothesis strategies shared by the property tests."""

from __future__ import annotations

import json
from typing import Any

from hypothesis import strategies as st

# JSON scalars, the awkward ones included: huge ints, non-finite floats, odd strings.
SCALARS = (
    st.none()
    | st.booleans()
    | st.integers(min_value=-(2**70), max_value=2**70)
    | st.floats(allow_nan=True, allow_infinity=True)
    | st.text(max_size=40)
)

JSON = st.recursive(
    SCALARS,
    lambda inner: (
        st.lists(inner, max_size=4) | st.dictionaries(st.text(max_size=12), inner, max_size=4)
    ),
    max_leaves=20,
)


def records(keys: dict[str, st.SearchStrategy[Any]]) -> st.SearchStrategy[dict[str, Any]]:
    """Objects that use the parser's own keys (each with a plausible value or any JSON
    value) plus a few unknown ones: closer to real lines than arbitrary JSON."""
    known = st.fixed_dictionaries({}, optional={k: v | JSON for k, v in keys.items()})
    extra = st.dictionaries(st.text(max_size=8), JSON, max_size=2)
    return st.builds(lambda a, b: {**b, **a}, known, extra)


def lines(objects: st.SearchStrategy[Any]) -> st.SearchStrategy[str]:
    """Spool lines: mostly JSON objects, some text that is not JSON at all."""
    encoded = objects.map(lambda o: json.dumps(o, allow_nan=True))
    nested = st.integers(min_value=1, max_value=20_000).map(lambda n: "[" * n + "]" * (n % 3))
    return encoded | st.text(max_size=200) | nested


def paths(data: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Every key path into nested dicts and lists, so a test can change one value."""
    out = [prefix] if prefix else []
    if isinstance(data, dict):
        for key, value in data.items():
            out += paths(value, (*prefix, key))
    elif isinstance(data, list):
        for i, value in enumerate(data):
            out += paths(value, (*prefix, i))
    return out
