"""The default price table and model-id matching (core/prices.py)."""

from __future__ import annotations

from datetime import date

import pytest

from kelpie.core.prices import (
    CHECKED,
    DEFAULT_PRICES,
    SOURCES,
    Price,
    PriceTable,
    canonical_model,
    model_id,
)
from kelpie.core.spans import TokenUsage
from kelpie.traces.fit import cheaper_model

DEFAULTS = PriceTable((), DEFAULT_PRICES)


@pytest.mark.parametrize(
    ("model", "canonical"),
    [
        ("claude-haiku-4-5", "claude-haiku-4-5"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4-5"),
        ("Claude-Haiku-4-5", "claude-haiku-4-5"),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", "claude-haiku-4-5"),
        ("anthropic.claude-sonnet-4-5-20250929-v1:0", "claude-sonnet-4-5"),
        ("claude-haiku-4-5@20251001", "claude-haiku-4-5"),
        ("claude-opus-4-6[1m]", "claude-opus-4-6"),
        ("anthropic/claude-sonnet-4-5", "claude-sonnet-4-5"),
        ("models/gemini-3.8-flash", "gemini-3.8-flash"),
        ("gpt-5.4-mini-2026-03-17", "gpt-5.4-mini"),
        ("gpt-5-latest", "gpt-5"),
        ("  gpt-5 ", "gpt-5"),
        ("x" * 10_000, "x" * 200),
    ],
)
def test_canonical_model(model: str, canonical: str) -> None:
    assert canonical_model(model) == canonical


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-5-5", Price(4, 20, 0.2, 5)),
        ("claude-sonnet-4-20250514", Price(3, 15, 0.3, 3.75)),  # alias of claude-sonnet-4-0
        ("claude-opus-4-20250514", Price(15, 75, 1.5, 18.75)),
        ("claude-3-5-haiku-20241022", Price(0.8, 4, 0.08, 1)),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", Price(1, 5, 0.1, 1.25)),
        ("gpt-5.4-mini-2026-03-17", Price(0.75, 4.5, 0.075)),
        ("models/gemini-3.8-flash", Price(0.75, 3.75, 0.075)),
    ],
)
def test_defaults_price_common_ids(model: str, expected: Price) -> None:
    assert DEFAULTS.price_for(model) == expected


def test_unknown_models_have_no_price() -> None:
    for model in ("gpt-4o", "claude-opus-9", "llama-3", "", None):
        assert DEFAULTS.price_for(model) is None
    assert DEFAULTS.unpriced(["gpt-4o", "claude-opus-5-5", None, "gpt-4o", "llama"]) == (
        "gpt-4o",
        "llama",
    )


def test_announced_changes_apply_from_their_day() -> None:
    assert DEFAULTS.price_for("gemini-4-argon", date(2026, 11, 30)) == Price(2, 10, 0.1)
    assert DEFAULTS.price_for("gemini-4-argon", date(2026, 12, 1)) == Price(4, 20, 0.2)
    assert DEFAULTS.price_for("gemini-4-argon") == Price(2, 10, 0.1)  # as checked
    assert DEFAULTS.price_for("gemini-3.8-flash", date(2027, 1, 1)) == Price(1.5, 7.5, 0.15)


def test_config_overrides_the_defaults() -> None:
    mine = Price(1, 2, 0.5)
    table = PriceTable((("claude-opus-5-5", mine),), DEFAULT_PRICES)
    assert table.price_for("claude-opus-5-5") == mine
    assert table.price_for("claude-opus-5-5-20260401") == mine  # its family
    assert table.price_for("us.anthropic.claude-opus-5-5-v1:0") == mine
    assert table.price_for("gpt-5") == Price(1.25, 10, 0.125)  # untouched default
    assert table.configured() == PriceTable(table.entries)
    assert table.configured().price_for("gpt-5") is None


A, B, C = Price(1, 2, 0.1), Price(3, 4, 0.3), Price(5, 6, 0.5)


def test_what_matched_before_still_matches_the_same_entry() -> None:
    """Exact name, then a glob on the id as written (as before normalization existed);
    only then the normalized matches."""
    table = PriceTable((("claude-sonnet-4-5", A), ("us.anthropic.*", B)))
    assert table.price_for("claude-sonnet-4-5") == A
    assert table.price_for("us.anthropic.claude-sonnet-4-5-20250929-v1:0") == B  # the glob
    assert table.price_for("anthropic.claude-sonnet-4-5-20250929-v1:0") == A  # normalized
    ordered = PriceTable((("claude-*", B), ("claude-opus-5-5", A)))
    assert ordered.price_for("claude-opus-5-5") == A  # an exact name beats an earlier glob
    assert ordered.price_for("claude-opus-5-5-20260401") == B  # the glob matches as written


def test_a_dated_entry_prices_only_its_own_snapshot() -> None:
    table = PriceTable((("gpt-4o-2024-08-06", A),))
    assert table.price_for("gpt-4o-2024-08-06") == A
    assert table.price_for("openai/gpt-4o-2024-08-06") == A  # same snapshot, wrapped
    assert table.price_for("gpt-4o-2024-05-13") is None  # another snapshot
    assert table.price_for("gpt-4o") is None
    family = PriceTable((("claude-haiku-4-5-20251001", A), ("claude-haiku-4-5", B)))
    assert family.price_for("us.anthropic.claude-haiku-4-5-20251001-v1:0") == A
    assert family.price_for("claude-haiku-4-5-20260101") == B  # the undated family entry


def test_globs_on_normalized_ids_ignore_case() -> None:
    table = PriceTable((("Claude-Haiku-*", A), ("gemini-*", C)))
    assert table.price_for("us.anthropic.claude-haiku-4-5-20251001-v1:0") == A
    assert table.price_for("models/gemini-3.8-flash") == C
    assert table.price_for("Claude-Haiku-4-5") == A  # as written


def test_ids_that_only_look_wrapped_are_left_alone() -> None:
    assert model_id("qwen3:32") == "qwen3:32"  # a Bedrock suffix only after anthropic.
    assert model_id("us-gov.anthropic.claude-haiku-4-5") != "claude-haiku-4-5"
    assert canonical_model("anthropic/claude-sonnet-4.5") == "claude-sonnet-4-5"
    assert canonical_model("anthropic/claude-3.5-haiku") == "claude-3-5-haiku"
    assert canonical_model("gpt-5.4-mini") == "gpt-5.4-mini"  # dots kept outside Claude


def test_provider_for() -> None:
    assert DEFAULTS.provider_for("us.anthropic.claude-opus-4-1-20250805-v1:0") == "anthropic"
    assert DEFAULTS.provider_for("gpt-5.5") == "openai"
    assert DEFAULTS.provider_for("models/gemini-4-argon") == "google"
    assert DEFAULTS.provider_for("llama") is None
    assert DEFAULTS.provider_for(None) is None


def test_the_table_is_consistent() -> None:
    """Every row: a known provider with a source page, sane prices, one row per model and
    day, canonical ids (so the lookup can find them)."""
    seen: set[tuple[str, date | None]] = set()
    for row in DEFAULT_PRICES:
        assert row.provider in SOURCES
        assert canonical_model(row.model) == row.model
        assert all(canonical_model(a) == a for a in row.aliases)
        p = row.price
        assert 0 < p.cached_input <= p.input < p.output
        assert p.cache_write is None or p.cache_write >= p.input
        assert (row.model, row.since) not in seen
        seen.add((row.model, row.since))
        assert row.since is None or row.since > CHECKED  # only announced changes ahead
    providers = {r.provider for r in DEFAULT_PRICES}
    assert providers == {"anthropic", "openai", "google"}


def test_cost_prices_cached_input_apart() -> None:
    usage = TokenUsage("s", "t", "claude-opus-5-5", 1, 1_000_000, 100_000, 400_000)
    price = DEFAULTS.price_for("claude-opus-5-5")
    assert price is not None
    assert price.cost(usage) == pytest.approx(0.6 * 4 + 0.4 * 0.2 + 0.1 * 20)


def test_cheaper_model_finds_dated_candidates() -> None:
    seen = {"claude-opus-5-5", "claude-haiku-4-5-20251001", "llama"}
    assert cheaper_model(DEFAULTS, "claude-opus-5-5", seen) == "claude-haiku-4-5-20251001"
    assert cheaper_model(DEFAULTS, "claude-haiku-4-5-20251001", seen) is None
    assert cheaper_model(DEFAULTS, "llama", seen) is None
