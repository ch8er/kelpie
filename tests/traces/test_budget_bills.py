"""Provider cost reports feed budgets (#205): a farm-wide dollar budget counts a reconciled
day at the bill, both figures stay visible, a disagreement is shown, and a hard threshold
only the bill reaches stops nothing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kelpie.app import BudgetBills, budget_sources
from kelpie.core.budgets import (
    BILL,
    CLAUDE_CODE_METRICS,
    TRACES,
    Budget,
    BudgetConfig,
    BudgetMode,
    billable,
    evaluate,
    with_bills,
)
from kelpie.core.model import (
    BudgetScope,
    BudgetState,
    BudgetUnit,
    Reconciliation,
    Snapshot,
)
from kelpie.core.prices import DEFAULT_PRICES, PriceTable
from kelpie.core.spans import TokenUsage
from kelpie.metrics import Metrics

NOW = datetime(2026, 3, 4, 12, tzinfo=UTC)
DAY = datetime(2026, 3, 2, tzinfo=UTC)  # wholly within a 3-day window ending NOW
WINDOW = timedelta(days=3)


def cfg(budgets: tuple[Budget, ...]) -> BudgetConfig:
    return BudgetConfig(budgets, PriceTable(defaults=DEFAULT_PRICES))


def budget(**kw: object) -> Budget:
    values: dict[str, object] = {
        "name": "farm-usd",
        "scope": BudgetScope.DAY,
        "unit": BudgetUnit.USD,
        "window": WINDOW,
        "soft": 50.0,
        "hard": 100.0,
    }
    values.update(kw)
    return Budget(**values)  # type: ignore[arg-type]


def bill(
    reported: float, estimated: float, day: datetime = DAY, large: bool = False
) -> Reconciliation:
    return Reconciliation("anthropic", day, estimated, reported, estimated - reported, large=large)


def statuses(config: BudgetConfig, usd_tokens: int = 1_000_000) -> tuple:
    """Spend from traces: ``usd_tokens`` input tokens of a priced model."""
    rows = (TokenUsage("s-1", "T-1", "claude-opus-5", 3, usd_tokens, 0),)
    usage = {w: rows for w in config.windows}
    return evaluate(config, usage, Snapshot(now=NOW)).statuses


def test_a_farm_budget_counts_a_billed_day_at_the_bill_and_keeps_both_figures() -> None:
    config = cfg((budget(),))
    (before,) = statuses(config)
    assert before.sources == (TRACES,) and before.billed_usd is None
    (after,) = with_bills(config, (before,), (bill(20.0, 2.0),), NOW)
    assert after.spent == pytest.approx(before.spent - 2.0 + 20.0)
    assert (after.billed_usd, after.billed_estimate_usd) == (20.0, 2.0)
    assert after.billed_days == ("anthropic 2026-03-02",)
    assert after.sources == (TRACES, BILL)
    assert after.remaining == pytest.approx(100.0 - after.spent)
    assert not after.sources_disagree and not after.bill_only_hard


def test_a_large_gap_is_shown_not_picked_silently() -> None:
    config = cfg((budget(),))
    (after,) = with_bills(config, statuses(config), (bill(20.0, 2.0, large=True),), NOW)
    assert after.sources_disagree
    lines = budget_sources(after)
    assert "billed $20.00 for anthropic 2026-03-02 in place of the traces' $2.00" in lines[0]
    assert "disagree" in lines[1]


@pytest.mark.parametrize(
    "day",
    [
        NOW - WINDOW - timedelta(hours=1),  # starts before the window: cannot be split
        datetime(2026, 3, 4, tzinfo=UTC),  # today: not over
    ],
)
def test_a_day_not_wholly_in_the_window_keeps_the_traces(day: datetime) -> None:
    config = cfg((budget(),))
    before = statuses(config)
    assert with_bills(config, before, (bill(500.0, 2.0, day=day),), NOW) == before


@pytest.mark.parametrize(
    "other",
    [
        {"scope": BudgetScope.TASK},  # a bill has no tasks
        {"scope": BudgetScope.AGENT},
        {"match": "farm-*", "scope": BudgetScope.PROJECT},
        {"unit": BudgetUnit.TOKENS, "soft": 1.0, "hard": 2.0},
        {"mode": BudgetMode.SUBSCRIPTION},  # no bill
    ],
)
def test_other_budgets_keep_the_traces(other: dict[str, object]) -> None:
    b = budget(**other)
    assert not billable(b)
    config = cfg((b,))
    before = statuses(config)
    assert with_bills(config, before, (bill(500.0, 2.0),), NOW) == before


def test_a_hard_threshold_only_the_bill_passes_is_held_at_soft() -> None:
    config = cfg((budget(),))
    (after,) = with_bills(config, statuses(config), (bill(150.0, 2.0, large=True),), NOW)
    assert after.spent > 100.0
    assert after.state is BudgetState.SOFT and after.bill_only_hard
    assert any("held at soft" in line for line in budget_sources(after))


def test_a_hard_threshold_traces_pass_stays_hard() -> None:
    config = cfg((budget(hard=1.0, soft=None),))
    (before,) = statuses(config)
    assert before.state is BudgetState.HARD
    (after,) = with_bills(config, (before,), (bill(0.5, 0.5),), NOW)
    assert after.state is BudgetState.HARD and not after.bill_only_hard


def test_a_credit_lowers_spend_but_never_below_zero() -> None:
    config = cfg((budget(),))
    (after,) = with_bills(config, statuses(config), (bill(-500.0, 2.0),), NOW)
    assert after.spent == 0.0 and after.state is BudgetState.OK


def test_an_estimate_above_the_windows_spend_is_not_taken_twice() -> None:
    config = cfg((budget(),))
    (before,) = statuses(config, usd_tokens=1)  # almost nothing in the window
    (after,) = with_bills(config, (before,), (bill(10.0, 50.0),), NOW)
    assert after.spent == pytest.approx(10.0)


def test_claude_codes_metrics_are_named_as_a_source() -> None:
    config = cfg((budget(),))
    rows = (
        TokenUsage("s-1", "T-1", "claude-opus-5", 2, 1000, 0),
        TokenUsage("s-2", None, "claude-opus-5", 0, 1000, 0),  # metered: no call
    )
    (status,) = evaluate(config, {w: rows for w in config.windows}, Snapshot(now=NOW)).statuses
    assert status.sources == (TRACES, CLAUDE_CODE_METRICS)
    assert budget_sources(status) == ["from traces, Claude Code's metrics"]
    only = evaluate(config, {w: rows[1:] for w in config.windows}, Snapshot(now=NOW))
    assert only.statuses[0].sources == (CLAUDE_CODE_METRICS,)


def test_the_step_puts_the_bill_in_the_tick_and_on_metrics() -> None:
    config = cfg((budget(),))
    metrics = Metrics()
    metrics.set_budgets_enabled(True)
    step = BudgetBills(config, metrics)
    snap = Snapshot(now=NOW, budgets=statuses(config))
    assert step(snap) is snap  # no reconciliation yet: nothing changes
    billed = step(Snapshot(now=NOW, budgets=snap.budgets, reconciliations=(bill(20.0, 2.0),)))
    assert billed.budgets[0].billed_usd == 20.0
    text = metrics.render()
    assert 'kelpie_budget_billed_usd{budget="farm-usd",subject="farm",unit="usd"} 20.000000' in text
    assert "kelpie_budget_sources_disagree{" in text
    # Budgets the span store could not read this tick stay as they were on /metrics.
    stale = Snapshot(
        now=NOW, budgets=snap.budgets, budgets_ok=False, reconciliations=(bill(30.0, 2.0),)
    )
    assert step(stale).budgets[0].billed_usd == 30.0
    assert 'kelpie_budget_billed_usd{budget="farm-usd",subject="farm",unit="usd"} 20.000000' in (
        metrics.render()
    )
