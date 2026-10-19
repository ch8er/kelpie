"""Spend follows each provider's spend mode (live review 2, item 4; track E's
kelpie.core.spend): on a subscription tokens and the share of the plan's limit, with an API
key dollars, never summed; a provider whose mode is not set shows dollars as an estimate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.budgets import Price, PriceTable
from kelpie.core.model import Session, Status, Task
from kelpie.core.spend import ProviderSpend, SpendConfig, SpendSettings
from kelpie.report.daily import ReportInputs, build_report, render_markdown, render_summary
from kelpie.report.sections.throughput import throughput_section
from tests.report.test_daily import DAY, llm

START = datetime(2026, 3, 2, 8, tzinfo=UTC)
EVERY = timedelta(minutes=10)
PRICES = PriceTable(
    (
        ("claude-sonnet-x", Price(input=3.0, output=15.0, cached_input=0.3)),
        ("gpt-x", Price(input=2.0, output=8.0, cached_input=0.2)),
        ("model-house", Price(input=1.0, output=1.0, cached_input=0.1)),
    )
)


def spend(*providers: ProviderSpend) -> SpendConfig:
    return SpendConfig(settings=SpendSettings(providers=providers), prices=PRICES)


PLAN = ProviderSpend("anthropic", mode="subscription", limit_tokens=10_000_000,
                     limit_window=timedelta(days=7))  # fmt: skip
API = ProviderSpend("openai", mode="api")
SPANS = [
    *llm("S-1", "T-1", START, 10, EVERY, (90_000, 10_000, 0), model="claude-sonnet-x"),
    *llm("S-2", "T-2", START, 10, EVERY, (90_000, 10_000, 0), model="gpt-x"),
]
DONE = (
    Task("T-1", "t", Status.DONE, START + timedelta(hours=3)),
    Task("T-2", "t", Status.DONE, START + timedelta(hours=3)),
)


def report(config: SpendConfig | None, spans: list = SPANS, **kw: object):  # type: ignore[no-untyped-def]
    inputs = ReportInputs(day=DAY, tz=UTC, spans=spans, prices=PRICES, spend=config,
                          tasks=DONE, **kw)  # type: ignore[arg-type]  # fmt: skip
    return build_report(inputs, sections={"throughput": throughput_section})


def test_a_subscription_is_tokens_and_limit_share_an_api_key_is_dollars() -> None:
    r = report(spend(PLAN, API))
    assert r.spend.plan_tokens == 1_000_000 and r.spend.plans == (("anthropic", 1_000_000),)
    assert r.spend.usd == pytest.approx(10 * (0.09 * 2 + 0.01 * 8))  # gpt-x only
    assert r.limits == {"anthropic": (10_000_000, timedelta(days=7))}
    markdown = render_markdown(r)
    assert "1,000,000 billed tokens, $2.60" in markdown
    assert "1,000,000 tokens on subscriptions (anthropic: 10% of its 7-day limit)" in markdown
    assert "$1.30 per completed task" in markdown  # dollars over completed tasks, API only


def test_a_subscription_only_farm_shows_no_dollars() -> None:
    plan_only = [s for s in SPANS if s.model == "claude-sonnet-x"]
    r = report(spend(PLAN), plan_only)
    head = render_summary(r)
    assert "$" not in head.split("tasks completed")[1]  # no dollars per task
    assert "500,000 plan tokens per completed task" in head
    markdown = render_markdown(r)
    assert "| 500,000 plan tokens |" in markdown  # throughput: cost per completed task


def test_no_limit_configured_says_so() -> None:
    r = report(spend(ProviderSpend("anthropic", mode="subscription"), API))
    assert "anthropic: limit not configured" in render_markdown(r)


def test_a_provider_without_a_mode_is_an_estimate() -> None:
    r = report(spend())  # nothing tells: unknown, priced as an estimate
    markdown = render_markdown(r)
    assert r.spend.unknown_usd == pytest.approx(r.spend.usd) and r.spend.plan_tokens == 0
    assert "(estimate, mode not set)" in markdown


def test_without_spend_config_the_report_is_unchanged() -> None:
    legacy = render_markdown(report(None))
    assert "2,000,000 tokens, $" in legacy and "subscriptions" not in legacy
    assert "mode not set" not in legacy


def test_chaos_on_a_subscription_is_burned_tokens_not_dollars() -> None:
    from kelpie.core.actions import Severity
    from kelpie.core.states import OpenState
    from kelpie.report.store import StateRow

    sid = "no_progress:S-1"
    row = StateRow(OpenState(sid, "no_progress", START, START, START + timedelta(hours=2), 0,
                             Severity.WARNING, "s", "T-1"))  # fmt: skip
    sessions = (Session("S-1", "T-1", "dev-1", START, START + timedelta(hours=2)),)
    r = report(spend(PLAN, API), states=(row,), sessions=sessions)
    head = render_summary(r)
    assert "tokens on subscriptions (anthropic: " in head and "burned" in head
    assert r.chaos_spend.usd == 0 and r.chaos_spend.plan_tokens > 0
