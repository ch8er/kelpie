"""Build domain objects from plain dicts (YAML/JSON fixtures, API payloads)."""

from __future__ import annotations

import dataclasses
import re
import types
from datetime import datetime, timedelta
from enum import Enum
from functools import cache
from typing import Any, Union, get_args, get_origin, get_type_hints

from kelpie.core import model
from kelpie.core.model import Snapshot

_DURATION = re.compile(r"^(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$")


def parse_duration(value: str | int | float | timedelta) -> timedelta:
    """Parse ``"1d2h30m"``-style strings; bare numbers are seconds."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ValueError(f"invalid duration: {value!r}")
    if isinstance(value, int | float):
        return timedelta(seconds=value)
    m = _DURATION.match(value.strip())
    if not m or not any(m.groupdict().values()):
        raise ValueError(f"invalid duration: {value!r}")
    parts = {k: int(v) for k, v in m.groupdict().items() if v}
    return timedelta(
        days=parts.get("d", 0),
        hours=parts.get("h", 0),
        minutes=parts.get("m", 0),
        seconds=parts.get("s", 0),
    )


def parse_datetime(value: str | datetime) -> datetime:
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError(f"datetime must be timezone-aware: {value!r}")
    return dt


@cache
def _hints(cls: type) -> dict[str, Any]:
    return get_type_hints(cls, vars(model))


def _convert(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        inner = [a for a in get_args(tp) if a is not type(None)]
        return _convert(inner[0], value)
    if origin is tuple:
        (item_tp, _) = get_args(tp)
        return tuple(_convert(item_tp, v) for v in value)
    if origin is dict or tp is Any:
        return value
    if tp is datetime:
        return parse_datetime(value)
    if tp is timedelta:
        return parse_duration(value)
    if isinstance(tp, type) and issubclass(tp, Enum):
        return tp(value)
    if dataclasses.is_dataclass(tp):
        return from_dict(tp, value)
    return value


def from_dict[T](cls: type[T], data: dict[str, Any]) -> T:
    """Construct a model dataclass, rejecting unknown keys to catch fixture typos."""
    hints = _hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"{cls.__name__}: unknown fields {sorted(unknown)}")
    return cls(**{k: _convert(hints[k], v) for k, v in data.items()})


def snapshot_from_dict(data: dict[str, Any]) -> Snapshot:
    return from_dict(Snapshot, data)
