"""Section settings from ``[report.sections.<name>]``: declared keys, checked values, and
the token sections reading them."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from kelpie.report.sections import SECTION_KEYS, load_sections
from kelpie.report.sections._config import keys_of, settings_from
from kelpie.traces.fit import FitSettings
from kelpie.traces.tokens import TokenSettings


def test_token_sections_declare_their_settings() -> None:
    load_sections()
    assert SECTION_KEYS["tokens"] == frozenset(keys_of(TokenSettings()))
    assert SECTION_KEYS["tokens_fit"] == frozenset(keys_of(FitSettings()))
    assert {"cache_ttl", "runaway_tokens", "min_saving_tokens"} <= SECTION_KEYS["tokens"]
    assert {"routine_roles", "allowed_models"} <= SECTION_KEYS["tokens_fit"]


def test_values_are_read_and_converted() -> None:
    found = settings_from(
        TokenSettings(),
        {"cache_ttl": "1h", "break_ratio": 0.4, "runaway_tokens": 150_000, "runaway_factor": 3},
    )
    assert found.cache_ttl == timedelta(hours=1) and found.break_ratio == 0.4
    assert found.runaway_tokens == 150_000 and found.runaway_factor == 3.0
    fit = settings_from(FitSettings(), {"routine_roles": [" triage "], "min_after": "6h"})
    assert fit.routine_roles == ("triage",) and fit.min_after == timedelta(hours=6)
    assert settings_from(TokenSettings(), {}) == TokenSettings()


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("cache_ttl", "soon", "cache_ttl"),
        ("cache_ttl", "0s", "> 0"),
        ("runaway_tokens", 1.5, "whole number"),
        ("runaway_tokens", -1, "whole number"),
        ("runaway_tokens", True, "number"),
        ("break_ratio", 1.5, "between 0 and 1"),
        ("runaway_factor", float("inf"), ">= 0"),
        ("runaway_factor", "4", ">= 0"),
    ],
)
def test_bad_token_values_are_refused(key: str, value: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        settings_from(TokenSettings(), {key: value})


@pytest.mark.parametrize("value", ["triage", ["triage", 3], [""], None])
def test_bad_role_lists_are_refused(value: Any) -> None:
    with pytest.raises(ValueError, match="routine_roles"):
        settings_from(FitSettings(), {"routine_roles": value})
