"""Spend modes per provider (core/spend.py): config, provider of a model, auto mode."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from kelpie.core.budgets import load_budgets
from kelpie.core.prices import DEFAULT_PRICES, PriceTable
from kelpie.core.spend import ProviderMode, SpendConfig, SpendMode, load_spend

WEEK = timedelta(days=7)
DEFAULTS = PriceTable((), DEFAULT_PRICES)


def config(spend: Any = None, budgets: Any = None, cost_reports: frozenset[str] = frozenset()):  # type: ignore[no-untyped-def]
    loaded = load_budgets(budgets, None, WEEK)
    return SpendConfig(load_spend(spend), loaded.prices, loaded.budgets, cost_reports)


def budget(**kw: Any) -> dict[str, Any]:
    return {"name": "b", "scope": "day", "unit": "usd", "hard": 10, **kw}


def test_defaults() -> None:
    settings = load_spend(None)
    assert settings.default_prices and settings.providers == ()
    assert load_spend({"providers": {}}).providers == ()


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ([], "[spend] must be a table"),
        ({"x": 1}, "unknown [spend] keys: ['x']"),
        ({"default_prices": "yes"}, "default_prices must be true or false"),
        ({"providers": []}, "[spend.providers] must be a table"),
        ({"providers": {" a": {}}}, "names must be 1-63"),
        ({"providers": {"my gw": {}}}, "names must be 1-63"),
        ({"providers": {"a.b": {}}}, "names must be 1-63"),
        ({"providers": {"a": {"quotas": "claude-max"}}}, "quotas must be a list"),
        ({"providers": {"a": {"quotas": ["a b"]}}}, "quotas must be a list"),
        ({"providers": {"a": 1}}, "[spend.providers.a] must be a table"),
        ({"providers": {"a": {"plan": 1}}}, "unknown [spend.providers.a] keys: ['plan']"),
        ({"providers": {"a": {"mode": "free"}}}, 'mode must be "auto", "api" or "subscription"'),
        ({"providers": {"a": {"mode": ["api"]}}}, "mode must be"),
        ({"providers": {"a": {"models": "claude-*"}}}, "models must be a list"),
        ({"providers": {"a": {"models": [""]}}}, "models must be a list"),
        ({"providers": {"a": {"limit_tokens": 0, "limit_window": "5h"}}}, "positive whole"),
        ({"providers": {"a": {"limit_tokens": True, "limit_window": "5h"}}}, "positive whole"),
        ({"providers": {"a": {"limit_tokens": 5, "limit_window": "soon"}}}, "limit_window"),
        ({"providers": {"a": {"limit_tokens": 5, "limit_window": 60}}}, "limit_window"),
        ({"providers": {"a": {"limit_tokens": 5}}}, "go together"),
        ({"providers": {"a": {"limit_window": "5h"}}}, "go together"),
        (
            {"providers": {"a": {"mode": "api", "limit_tokens": 5, "limit_window": "5h"}}},
            "limit_tokens is for subscriptions",
        ),
    ],
)
def test_bad_spend_config_is_refused(data: Any, message: str) -> None:
    with pytest.raises(ValueError) as exc:
        load_spend(data)
    assert message in str(exc.value)


def test_provider_of_a_model() -> None:
    spend = config({"providers": {"router": {"models": ["llama-*", "Claude-Opus-5-5"]}}})
    assert spend.provider_for("llama-3") == "router"
    assert spend.provider_for("claude-opus-5-5-20260401") == "router"  # canonical id
    assert spend.provider_for("claude-haiku-4-5") == "anthropic"  # default table
    assert spend.provider_for("claude-opus-9") == "anthropic"  # by its name
    assert spend.provider_for("us.anthropic.claude-sonnet-9-v1:0") == "anthropic"
    assert spend.provider_for("gpt-6.1-sol") == "openai"
    assert spend.provider_for("o4-mini") == "openai"
    assert spend.provider_for("models/gemini-9-pro") == "google"
    assert spend.provider_for("mistral-large") is None
    assert spend.provider_for(None) is None


def mode(spend: SpendConfig, provider: str | None = "anthropic") -> tuple[SpendMode, str]:
    m = spend.mode_for(provider)
    return m.mode, m.reason


def test_explicit_mode_wins() -> None:
    spend = config(
        {"providers": {"anthropic": {"mode": "subscription"}}},
        [budget()],
        frozenset({"anthropic"}),
    )
    assert mode(spend) == (SpendMode.SUBSCRIPTION, "mode set in [spend.providers.anthropic]")


def test_a_cost_report_means_api() -> None:
    spend = config(None, [budget(mode="subscription")], frozenset({"anthropic"}))
    assert mode(spend) == (SpendMode.API, "its cost report is configured")


def test_a_subscription_budget_means_subscription() -> None:
    named = config(None, [budget(), budget(name="s", mode="subscription", providers=["openai"])])
    assert mode(named, "openai")[0] is SpendMode.SUBSCRIPTION
    assert "budget 's' has mode = subscription and names it" in mode(named, "openai")[1]
    assert mode(named)[0] is SpendMode.API  # the usd api day budget, for the others


def test_quota_names_link_a_subscription_budget_to_its_provider() -> None:
    """Budget providers are [connectors.llm_quotas] names: claude-max is not anthropic
    unless [spend.providers.anthropic] quotas says so."""
    budgets = [
        budget(name="max", mode="subscription", providers=["claude-max"]),
        budget(name="proj", scope="project", match="WEB-*"),  # usd, mode api by default
    ]
    unlinked = config(None, budgets)
    assert mode(unlinked)[0] is SpendMode.UNKNOWN  # neither the quota nor the project tells
    assert unlinked.unlinked() == (("max", "claude-max"),)
    linked = config({"providers": {"anthropic": {"quotas": ["claude-max"]}}}, budgets)
    assert mode(linked) == (
        SpendMode.SUBSCRIPTION,
        "budget 'max' has mode = subscription and names it",
    )
    assert linked.unlinked() == ()


def test_only_a_farm_wide_usd_budget_hints_at_api() -> None:
    scoped = config(None, [budget(scope="agent", match="dev-*")])
    assert mode(scoped)[0] is SpendMode.UNKNOWN
    (reason,) = [mode(config(None, [budget()]))[1]]
    assert reason == "day budget 'b' counts the farm's spend in usd, budget mode api"


def test_without_the_default_table_names_still_tell_the_provider() -> None:
    loaded = load_budgets(None, None, WEEK, default_prices=False)
    spend = SpendConfig(load_spend(None), loaded.prices, loaded.budgets)
    assert spend.provider_for("claude-haiku-4-5") == "anthropic"
    assert spend.provider_for("gpt-5") == "openai"
    every = config(None, [budget(mode="subscription")])
    assert mode(every) == (
        SpendMode.SUBSCRIPTION,
        "budget 'b' has mode = subscription and covers every provider",
    )


def test_a_limit_means_subscription() -> None:
    spend = config(
        {"providers": {"anthropic": {"limit_tokens": 5_000_000, "limit_window": "5h"}}}, [budget()]
    )
    assert mode(spend) == (
        SpendMode.SUBSCRIPTION,
        "limit_tokens set in [spend.providers.anthropic]",
    )
    assert spend.limit_for("anthropic") == (5_000_000, timedelta(hours=5))
    assert spend.limit_for("openai") is None


def test_a_usd_api_budget_means_api() -> None:
    assert mode(config(None, [budget()]))[0] is SpendMode.API
    tokens_only = config(None, [budget(unit="tokens")])
    assert mode(tokens_only)[0] is SpendMode.UNKNOWN


def test_nothing_tells() -> None:
    m, reason = mode(config())
    assert m is SpendMode.UNKNOWN and '[spend.providers.anthropic] mode = "api"' in reason
    assert config().mode_for(None) == ProviderMode(
        None, SpendMode.UNKNOWN, "the model's provider is not known"
    )


def test_modes_lists_configured_providers_first() -> None:
    spend = config({"providers": {"openai": {"mode": "api"}}})
    assert [m.provider for m in spend.modes(["anthropic", "openai", None, "anthropic"])] == [
        "openai",
        "anthropic",
        None,
    ]
    assert spend.mode_for_model("gpt-5").mode is SpendMode.API
