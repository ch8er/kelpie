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
        "figures: `local-big` 300,000, `(no model)` 100,000, `local-small` 100,000."
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


def test_the_bill_check_shows_each_gap_with_what_may_explain_it() -> None:
    bills = (
        bill(large=True, unpriced_tokens=5_000, unknown_tokens=7_000),
        bill(
            provider="anthropic", estimated_usd=12.0, reported_usd=10.0, gap_usd=2.0, gap_ratio=0.2
        ),
        bill(provider="google", estimated_usd=3.0, reported_usd=3.0, gap_usd=0.0),
        bill(
            provider="other",
            bill_scope="total",
            estimated_usd=1.0,
            reported_usd=0.0,
            gap_usd=1.0,
            gap_ratio=None,
        ),
        bill(provider="zeta", bill_scope="total"),
    )
    text = markdown(reconciliations=bills)
    assert "**Bill check** (each provider's cost report against Kelpie's estimate" in text
    lines = [line for line in text.splitlines() if line.startswith("- ") and "billed $" in line]
    assert lines[0].startswith(
        "- anthropic, 2026-03-01: estimated $12.00, billed $10.00; the estimate $2.00 (20%) "
        "above the bill."
    )
    assert (
        "- google, 2026-03-01: estimated $3.00, billed $3.00; the estimate matches the bill."
        in text
    )
    first = next(line for line in lines if line.startswith("- openai"))
    assert "the estimate $1.20 (12%) below the bill. **Large gap**" in first
    assert "5,000 tokens of models without a price are not estimated." in first
    assert "7,000 tokens of calls with no known provider may belong to this bill." in first
    other = next(line for line in lines if line.startswith("- other"))
    assert other.endswith("the estimate $1.00 above the bill.")  # no ratio over a $0 bill
    zeta = next(line for line in lines if line.startswith("- zeta"))
    assert "so an estimate below it is expected" in zeta and "Large gap" not in zeta


def test_no_bills_no_check_and_an_error_is_said() -> None:
    assert "Bill check" not in markdown()
    text = markdown(reconciliations="cannot open the cost reports in kelpie.db: *locked*")
    assert "**Bill check**: cannot read the cost reports (cannot open the cost reports" in text
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
    assert timedelta(hours=12) <= end - ended < timedelta(hours=36)


def test_consecutive_reports_show_consecutive_days() -> None:
    zone = ZoneInfo("Europe/Berlin")
    shown = [bill_days(day_bounds(DAY + timedelta(days=i), zone)[1])[0] for i in range(5)]
    assert shown == [date(2026, 3, 1) + timedelta(days=i) for i in range(5)]


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
