"""Section settings from ``[report.sections.<name>]``: a frozen dataclass of defaults,
overridden by the table's keys (the dataclass's field names), each value checked against
the type of its default. A bad value raises :class:`ValueError`, which the report notes
for that section only."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import fields, replace
from datetime import timedelta
from typing import Any

from kelpie.core.serde import parse_duration

# Fields that are shares of something: between 0 and 1.
SHARES = ("_ratio", "_share", "_tolerance")


def keys_of(defaults: Any) -> tuple[str, ...]:
    """The keys a section with these settings accepts."""
    return tuple(f.name for f in fields(defaults))


def settings_from[T](defaults: T, config: Mapping[str, Any]) -> T:
    """``defaults`` with the values ``config`` sets, checked (see the module docs)."""
    changes = {}
    for name in keys_of(defaults):
        if name in config:
            changes[name] = _value(name, getattr(defaults, name), config[name])
    return replace(defaults, **changes) if changes else defaults


def _value(name: str, default: Any, value: Any) -> Any:
    if isinstance(default, timedelta):
        try:
            found = parse_duration(value)
        except ValueError as err:
            raise ValueError(f"{name}: {err}") from None
        if found <= timedelta(0):
            raise ValueError(f"{name}: must be a duration > 0, got {value!r}")
        return found
    if isinstance(default, tuple):
        if not isinstance(value, list | tuple) or not all(
            isinstance(v, str) and v.strip() for v in value
        ):
            raise ValueError(f"{name}: must be a list of non-empty strings, got {value!r}")
        return tuple(v.strip() for v in value)
    if isinstance(default, bool) or isinstance(value, bool):
        raise ValueError(f"{name}: must be a number, got {value!r}")
    if isinstance(default, int):
        if not isinstance(value, int) or value < 0:
            raise ValueError(f"{name}: must be a whole number >= 0, got {value!r}")
        return value
    if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name}: must be a number >= 0, got {value!r}")
    if name.endswith(SHARES) and value > 1:
        raise ValueError(f"{name}: must be between 0 and 1, got {value!r}")
    return float(value)
