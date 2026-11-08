"""Config validators and duration texts that several rules share word for word.

Only helpers whose copies were identical live here, so moving them changed no message or
check. Rules with their own variant (a zero allowed, a different message) keep it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from kelpie.core.serde import parse_duration


def positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def positive_duration(value: Any, name: str) -> timedelta:
    try:
        result = parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if result <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return result


def strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return tuple(value)


def human(d: timedelta) -> str:
    """``d`` as "1h 05m", "2h" or "40m" (whole minutes, never negative)."""
    minutes = max(0, int(d.total_seconds() // 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours}h {minutes:02d}m"
    return f"{hours}h" if hours else f"{minutes}m"


def hours(d: timedelta) -> str:
    """``d`` in hours with one decimal: "1.5h"."""
    return f"{d.total_seconds() / 3600:.1f}h"
