"""``[spend]``: how Kelpie turns tokens into spend.

Pure config. ``default_prices`` (true by default) puts Kelpie's dated default price table
(:mod:`kelpie.core.prices`) under the ``[prices]`` entries; false leaves only ``[prices]``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["SpendSettings", "load_spend"]

SPEND_KEYS = frozenset({"default_prices"})


@dataclass(frozen=True, slots=True)
class SpendSettings:
    default_prices: bool = True


def load_spend(data: Any) -> SpendSettings:
    """Parse ``[spend]``; raises ValueError on anything unexpected."""
    if data is None:
        return SpendSettings()
    if not isinstance(data, Mapping):
        raise ValueError("[spend] must be a table")
    if extra := set(data) - SPEND_KEYS:
        raise ValueError(f"unknown [spend] keys: {sorted(extra)}")
    default_prices = data.get("default_prices", True)
    if not isinstance(default_prices, bool):
        raise ValueError("[spend] default_prices must be true or false")
    return SpendSettings(default_prices=default_prices)
