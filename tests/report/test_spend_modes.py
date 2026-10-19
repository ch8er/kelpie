"""Spend follows each provider's spend mode (live review 2, item 4; track E's
kelpie.core.spend): on a subscription tokens and the share of the plan's limit, with an API
key dollars, never summed; a provider whose mode is not set shows dollars as an estimate."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Severity
from kelpie.core.budgets import Price, PriceTable
from kelpie.core.model import Session, Span, SpanKind, Status, Task
from kelpie.core.spans import TokenUsage
from kelpie.core.spend import ProviderSpend, SpendConfig, SpendSettings
from kelpie.core.states import OpenState
from kelpie.report.daily import (
    DailyReport,
    ReportInputs,
    _Pricer,
    build_report,
    render_markdown,
    render_summary,
)
from kelpie.report.sections import Spend
from kelpie.report.sections.throughput import throughput_section
from kelpie.report.sections.tokens_fit import _Prices, _saving
from kelpie.report.store import StateRow

DAY = date(2026, 3, 2)
START = datetime(2026, 3, 2, 8, tzinfo=UTC)
PRICES = PriceTable(
    (
        ("claude-sonnet-x", Price(input=3.0, output=15.0, cached_input=0.3)),
        ("gpt-x", Price(input=2.0, output=8.0, cached_input=0.2)),
        ("gpt-mini", Price(input=0.5, output=2.0, cached_input=0.05)),
    )
)
PLAN = ProviderSpend(
    "anthropic", mode="subscription", limit_tokens=10_000_000, limit_window=timedelta(days=7)
)
API = ProviderSpend("openai", mode="api")
DONE = (
    Task("T-1", "t", Status.DONE, START + timedelta(hours=3)),
    Task("T-2", "t", Status.DONE, START + timedelta(hours=3)),
)


def spend(*providers: ProviderSpend) -> SpendConfig:
    return SpendConfig(settings=SpendSettings(providers=providers), prices=PRICES)


def calls(session: str, task: str, model: str, n: int = 10) -> list[Span]:
    """``n`` model calls of 90k input and 10k output tokens, ten minutes apart."""
    return [
        Span(
            trace_id=f"tr-{session}",
            span_id=f"{session}-{i}",
            name="chat",
            kind=SpanKind.LLM,
            start=START + i * timedelta(minutes=10),
            end=START + i * timedelta(minutes=10) + timedelta(seconds=4),
            task_id=task,
            session_id=session,
            model=model,
            input_tokens=90_000,
            output_tokens=10_000,
        )
        for i in range(n)
    ]


MIXED = [*calls("S-1", "T-1", "claude-sonnet-x"), *calls("S-2", "T-2", "gpt-x")]
PLAN_ONLY = calls("S-1", "T-1", "claude-sonnet-x")


def report(config: SpendConfig | None, spans: list[Span] = MIXED, **kw: Any) -> DailyReport:
    inputs = ReportInputs(
        day=DAY, tz=UTC, spans=spans, prices=PRICES, spend=config, tasks=DONE, **kw
    )
    return build_report(inputs, sections={"throughput": throughput_section})


def test_a_subscription_is_tokens_and_limit_share_an_api_key_is_dollars() -> None:
    r = report(spend(PLAN, API))
    assert r.spend.plan_tokens == 1_000_000 and r.spend.plans == (("anthropic", 1_000_000),)
    assert r.spend.usd == pytest.approx(10 * (0.09 * 2 + 0.01 * 8))  # gpt-x only
    assert r.limits == {"anthropic": (10_000_000, timedelta(days=7))}
    markdown = render_markdown(r)
    assert "1,000,000 billed tokens, $2.60" in markdown
    assert "1,000,000 tokens on subscriptions (anthropic: 10% of one 7-day limit)" in markdown
    # per completed task: dollars over the tasks, and plan tokens beside them, never summed
    assert "$1.30 and 500,000 plan tokens per completed task" in markdown
    assert "$1.30 and 500,000 plan tokens" in markdown.split("## Farm throughput")[1]


def test_a_subscription_only_farm_shows_no_dollars() -> None:
    r = report(spend(PLAN), PLAN_ONLY)
    head = render_summary(r)
    assert "$" not in head
    assert "0 tokens burned" in head and "500,000 plan tokens per completed task" in head
    markdown = render_markdown(r)
    assert "## Spend\n\n1,000,000 tokens on subscriptions (anthropic:" in markdown
    assert "0 billed tokens" not in markdown
    assert "| 500,000 plan tokens |" in markdown  # throughput: cost per completed task


def test_short_windows_compare_the_average_window() -> None:
    hourly = ProviderSpend("anthropic", mode="subscription", limit_tokens=200_000,
                           limit_window=timedelta(hours=5))  # fmt: skip
    # 1M tokens in a day of 4.8 five-hour windows: about 208k per window, 104% of 200k
    markdown = render_markdown(report(spend(hourly, API)))
    assert "anthropic: on average 104% of its 5-hour limit per window" in markdown


def test_no_limit_configured_says_so() -> None:
    r = report(spend(ProviderSpend("anthropic", mode="subscription"), API))
    assert "anthropic: limit not configured" in render_markdown(r)


def test_a_provider_without_a_mode_is_marked_everywhere() -> None:
    r = report(spend(PLAN))  # gpt-x: its provider's mode is not set
    markdown = render_markdown(r)
    assert r.spend.usd > 0 and r.spend.unknown_usd == pytest.approx(r.spend.usd)
    assert markdown.count("(estimate, mode not set)") >= 3  # headline, spend line, throughput


def test_without_spend_config_the_report_is_unchanged() -> None:
    legacy = render_markdown(report(None))
    assert "2,000,000 tokens, $" in legacy and "subscriptions" not in legacy
    assert "mode not set" not in legacy


def test_chaos_on_a_subscription_is_tokens_never_dollars() -> None:
    sid = "no_progress:S-1"
    row = StateRow(OpenState(sid, "no_progress", START, START, START + timedelta(hours=2), 0,
                             Severity.WARNING, "s", "T-1"))  # fmt: skip
    sessions = (Session("S-1", "T-1", "dev-1", START, START + timedelta(hours=2)),)
    r = report(spend(PLAN, API), states=(row,), sessions=sessions)
    [line] = r.chaos
    assert line.spend.usd == 0 and line.spend.plan_tokens > 0 and r.chaos_usd == 0
    head = render_summary(r)
    assert "; 1,000,000 tokens on subscriptions (anthropic: 10% of one 7-day limit) burned" in head
    markdown = render_markdown(r)
    assert "| 0 + 1,000,000 plan | - |" in markdown  # the table never prices plan tokens
    assert all(a.usd == 0 for a in r.recommendations)


def test_unpriced_billed_tokens_are_not_priced() -> None:
    r = report(spend(API), calls("S-9", "T-1", "gpt-unknown-model"))
    assert r.spend.unpriced_tokens == r.spend.billed_tokens and not r.priced


def test_the_pricer_never_prices_a_plan() -> None:
    pricer = _Pricer(PRICES, spend(PLAN, API), DAY)
    assert pricer.input_usd(1_000_000, "claude-sonnet-x") == 0.0
    assert pricer.input_usd(1_000_000, "gpt-x") == pytest.approx(2.0)
    rows = [TokenUsage("S", "T", "claude-sonnet-x", calls=1, input_tokens=1_000, output_tokens=10)]
    assert pricer.price(rows * 2).plans == (("anthropic", 2_020),)


def test_spend_adds_plans_per_provider() -> None:
    a = Spend(10, 1.0, 0, 5, (("anthropic", 5),), 0.5)
    b = Spend(7, 0.0, 0, 7, (("anthropic", 3), ("google", 4)))
    total = a + b
    assert total.plans == (("anthropic", 8), ("google", 4))
    assert (total.tokens, total.plan_tokens, total.billed_tokens) == (17, 12, 5)
    assert (total.usd, total.unknown_usd) == (1.0, 0.5)


def test_no_model_fit_saving_across_a_subscription() -> None:
    class Ctx:
        def __init__(self) -> None:
            self.pricer = _Pricer(PRICES, spend(PLAN, API), DAY)

        def price(self, rows: Any) -> Spend:
            return self.pricer.price(rows)

    class Role:
        def __init__(self) -> None:
            self.usage = [TokenUsage("S", "T", "gpt-x", calls=1, input_tokens=1_000_000)]

        def on(self, model: str) -> list[TokenUsage]:
            return [TokenUsage("S", "T", model, calls=1, input_tokens=1_000_000)]

    price = _Prices(Ctx(), {})  # type: ignore[arg-type]
    role: Any = Role()  # a stand-in for a RoleModel
    assert _saving(role, "claude-sonnet-x", price) == 0.0
    assert _saving(role, "gpt-x", price) == 0.0  # same model: nothing saved
    assert _saving(role, "gpt-mini", price) == pytest.approx(1.5)  # both billed: $2 → $0.5


def test_the_root_cause_digest_follows_the_modes() -> None:
    from kelpie.report.root_cause import digest

    plans_only = digest(report(spend(PLAN), PLAN_ONLY)).data
    assert plans_only["spend_usd"] is None and plans_only["plan_tokens"] == 1_000_000
    billed = digest(report(spend(API), [])).data
    assert billed["spend_usd"] == 0.0 and billed["plan_tokens"] == 0  # a quiet API day


def test_unpriced_billed_tokens_still_show_plan_tokens_per_task() -> None:
    spans = [*PLAN_ONLY, *calls("S-9", "T-2", "gpt-unknown-model")]
    head = render_summary(report(spend(PLAN, API), spans))
    assert "2 tasks completed, 500,000 plan tokens per completed task." in head


def test_a_subscription_over_every_provider_covers_models_of_no_known_provider() -> None:
    from kelpie.core.budgets import Budget, BudgetMode, BudgetScope, BudgetUnit

    every = Budget("plan", BudgetScope.DAY, BudgetUnit.TOKENS, timedelta(days=1),
                   hard=1e9, mode=BudgetMode.SUBSCRIPTION)  # fmt: skip
    config = SpendConfig(settings=SpendSettings(providers=(API,)), prices=PRICES, budgets=(every,))
    local = calls("S-7", "T-1", "local-model-x")
    r = report(config, local)
    assert r.spend.plan_tokens == r.spend.tokens == 1_000_000 and r.spend.plans == ()
    assert r.spend.usd == 0 and r.spend.unpriced_tokens == 0
    markdown = render_markdown(r)
    assert "1,000,000 of models of no known provider: no plan limit to compare" in markdown
    unpriced = report(spend(API), local)  # no such budget: its mode is not known
    assert unpriced.spend.plan_tokens == 0 and unpriced.spend.unpriced_tokens == 1_000_000
