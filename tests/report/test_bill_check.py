"""The report's spend checks (track E's budgets item, report side): the models without a
price are named, and each provider's bill is compared with the estimate from traces
(``kelpie.app.read_reconciliations``)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from kelpie.core.budgets import Price, PriceTable
from kelpie.core.model import Reconciliation, Snapshot, Span, SpanKind
from kelpie.core.spend import ProviderSpend, SpendConfig, SpendSettings
from kelpie.report.config import ReportSettings
from kelpie.report.daily import ReportInputs, build_report, day_bounds, render_markdown
from kelpie.report.run import ReportSources, bill_days, load_inputs
from kelpie.traces.outputs import OutputSettings

DAY = date(2026, 3, 2)
START = datetime(2026, 3, 2, 8, tzinfo=UTC)
PRICES = PriceTable((("gpt-x", Price(input=2.0, output=8.0, cached_input=0.2)),))
BILL_DAY = datetime(2026, 3, 1, tzinfo=UTC)


def calls(model: str | None, n: int = 1, session: str = "S-1") -> list[Span]:
    """``n`` calls of 90k input and 10k output tokens."""
    return [
        Span(
            trace_id=f"tr-{session}",
            span_id=f"{session}-{model}-{i}",
            name="chat",
            kind=SpanKind.LLM,
            start=START + timedelta(minutes=i),
            end=START + timedelta(minutes=i, seconds=4),
            session_id=session,
            model=model,
            input_tokens=90_000,
            output_tokens=10_000,
        )
        for i in range(n)
    ]


def markdown(**kw: Any) -> str:
    return render_markdown(build_report(ReportInputs(day=DAY, tz=UTC, prices=PRICES, **kw)))


def bill(**kw: Any) -> Reconciliation:
    base: dict[str, Any] = {
        "provider": "openai",
        "day": BILL_DAY,
        "estimated_usd": 8.8,
        "reported_usd": 10.0,
        "gap_usd": -1.2,
        "gap_ratio": -0.12,
    }
    return Reconciliation(**{**base, **kw})


def test_models_without_a_price_are_named_most_tokens_first() -> None:
    spans = [*calls("gpt-x"), *calls("local-small"), *calls("local-big", 3), *calls(None)]
    text = markdown(spans=spans)
    assert (
        "500,000 tokens are of models without a price in [prices] and are not in the dollar "
        "figures: `local-big` 300,000, calls with no model 100,000, `local-small` 100,000."
    ) in text


def test_a_long_list_of_unpriced_models_is_cut() -> None:
    spans = [s for i in range(12) for s in calls(f"m-{i:02}", 12 - i)]
    report = build_report(ReportInputs(day=DAY, tz=UTC, prices=PRICES, spans=spans))
    assert [m for m, _ in report.unpriced_models][:2] == ["m-00", "m-01"]
    assert "`m-09` 300,000 and 2 more." in render_markdown(report)


def test_subscription_models_are_not_unpriced() -> None:
    plan = ProviderSpend("anthropic", mode="subscription", models=("claude-*",))
    config = SpendConfig(settings=SpendSettings(providers=(plan,)), prices=PRICES)
    report = build_report(
        ReportInputs(day=DAY, tz=UTC, prices=PRICES, spend=config, spans=calls("claude-x"))
    )
    assert report.unpriced_models == () and "without a price" not in render_markdown(report)


def lines_of(*bills: Reconciliation) -> dict[str, str]:
    text = markdown(reconciliations=bills)
    found = [x for x in text.splitlines() if x.startswith("- ") and "estimated $" in x]
    return {x[2:].split(",")[0]: x for x in found}


def test_the_bill_check_shows_each_gap_and_what_the_estimate_leaves_out() -> None:
    text = markdown(reconciliations=(bill(),))
    assert "**Bill check** (each provider's cost report against Kelpie's estimate" in text
    lines = lines_of(
        bill(unpriced_tokens=5_000, unknown_tokens=7_000),
        bill(provider="anthropic", estimated_usd=12.0, reported_usd=10.0, gap_usd=2.0,
             gap_ratio=0.2),
        bill(provider="google", estimated_usd=3.0, reported_usd=3.0, gap_usd=0.0),
        bill(provider="other", estimated_usd=1.0, reported_usd=0.0, gap_usd=1.0, gap_ratio=None),
        bill(provider="zeta", bill_scope="total"),
    )  # fmt: skip
    assert list(lines) == ["anthropic", "google", "openai", "other", "zeta"]
    assert lines["anthropic"] == (
        "- anthropic, 2026-03-01: estimated $12.00, billed $10.00; the estimate $2.00 (20%) "
        "above the bill."
    )
    assert lines["google"].endswith("billed $3.00; the estimate matches the bill.")
    assert lines["openai"] == (
        "- openai, 2026-03-01: estimated $8.80, billed $10.00; the estimate $1.20 (12%) below "
        "the bill. 5,000 tokens of models without a price are not estimated. 7,000 tokens of "
        "calls with no known provider may belong to this bill."
    )
    assert lines["other"].endswith("the estimate $1.00 above the bill.")  # no ratio over $0
    assert lines["zeta"].endswith("so part of the gap may be expected.")


def test_a_large_gap_names_the_causes_for_its_sign() -> None:
    lines = lines_of(
        bill(large=True, unpriced_tokens=5_000),
        bill(provider="anthropic", estimated_usd=30.0, reported_usd=10.0, gap_usd=20.0,
             gap_ratio=2.0, large=True),
        bill(provider="zeta", bill_scope="total", estimated_usd=20.0, reported_usd=40.0,
             gap_usd=-20.0, gap_ratio=-0.5, large=True),
    )  # fmt: skip
    below = lines["openai"]
    assert "**Large gap**: traces show less than was billed: models without a price" in below
    assert below.count("5,000 tokens") == 1  # said once, in the causes
    above = lines["anthropic"]
    assert "**Large gap**: traces show more than was billed: prices above the bill's" in above
    assert "not sending" not in above and "other users" not in above
    total = lines["zeta"]
    assert "**Large gap**: traces show less than was billed: non-token items" in total
    assert total.endswith(
        "if a gap like this is routine, raise `warn_gap` ([[connectors.cost_reports]])."
    )
    assert "is expected" not in total


def test_a_credit_is_said_as_a_credit() -> None:
    line = lines_of(bill(estimated_usd=1.0, reported_usd=-5.0, gap_usd=6.0, gap_ratio=None))
    assert (
        "estimated $1.00, a credit of $5.00; the estimate $6.00 above the bill." in line["openai"]
    )


def test_provider_and_model_names_are_escaped() -> None:
    text = markdown(reconciliations=(bill(provider="*evil*|[x](y)"),), spans=calls("m`|x"))
    assert "*evil*" not in text and "[x](y)" not in text
    line = next(x for x in text.splitlines() if "without a price in [prices]" in x)
    assert "``m`|x`` 100,000" in line  # a fence longer than the backtick inside


def test_no_bills_no_check_and_an_error_is_said() -> None:
    assert "Bill check" not in markdown()
    text = markdown(reconciliations="cannot open the cost reports in kelpie.db: *locked*")
    assert "**Bill check**: cannot open the cost reports in kelpie.db: " in text
    assert "*locked*" not in text  # escaped


@pytest.mark.parametrize(
    ("tz", "expected"),
    [
        ("UTC", date(2026, 3, 1)),
        ("Europe/Moscow", date(2026, 3, 1)),  # UTC+3
        ("America/New_York", date(2026, 3, 1)),  # UTC-5
        ("America/Los_Angeles", date(2026, 3, 1)),  # UTC-8
        ("Pacific/Auckland", date(2026, 2, 28)),  # UTC+13 in March
    ],
)
def test_each_report_shows_one_settled_utc_day(tz: str, expected: date) -> None:
    _, end = day_bounds(DAY, ZoneInfo(tz))
    assert bill_days(end) == (expected, expected)
    ended = datetime.combine(expected + timedelta(days=1), datetime.min.time(), UTC)
    assert timedelta(hours=18) <= end - ended < timedelta(hours=42)


@pytest.mark.parametrize(
    ("tz", "first"),
    [
        ("Europe/Berlin", date(2026, 3, 25)),  # DST from 29 March
        ("America/New_York", date(2026, 3, 5)),  # DST from 8 March
        ("Pacific/Auckland", date(2026, 4, 1)),  # DST ends 5 April
        ("Pacific/Auckland", date(2026, 9, 24)),  # DST starts 27 September
        ("Pacific/Chatham", date(2026, 4, 1)),  # +12:45 / +13:45
        ("Australia/Lord_Howe", date(2026, 4, 1)),  # half-hour DST
    ],
)
def test_consecutive_reports_show_consecutive_days(tz: str, first: date) -> None:
    zone = ZoneInfo(tz)
    shown = [bill_days(day_bounds(first + timedelta(days=i), zone)[1])[0] for i in range(10)]
    assert shown == [shown[0] + timedelta(days=i) for i in range(10)]


def test_load_inputs_reads_the_bills_of_that_day(tmp_path: Path) -> None:
    asked: list[tuple[date, date]] = []

    def reconcile(since: date, until: date) -> tuple[Reconciliation, ...]:
        asked.append((since, until))
        return (bill(),)

    sources = ReportSources(
        tmp_path / "none.db", ReportSettings(), PRICES, OutputSettings(), False, {},
        reconcile=reconcile,
    )  # fmt: skip
    inputs = load_inputs(sources, DAY, Snapshot(now=START))
    assert asked == [(date(2026, 3, 1), date(2026, 3, 1))]
    assert list(inputs.reconciliations) == [bill()]
    plain = ReportSources(
        tmp_path / "none.db", ReportSettings(), PRICES, OutputSettings(), False, {}
    )
    assert load_inputs(plain, DAY, Snapshot(now=START)).reconciliations == ()
