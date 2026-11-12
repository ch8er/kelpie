"""Provider cost reports feed budgets (#205): a farm-wide dollar budget counts a reconciled
day at the bill, both figures stay visible, a disagreement is shown, and a hard threshold
only the bill reaches stops nothing."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.app import budget_sources
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


def test_claude_codes_metrics_on_a_billed_day_are_taken_out_with_the_estimate() -> None:
    """The bill holds the calls Claude Code's metrics reported: counting both would count
    them twice."""
    config = cfg((budget(),))
    (before,) = statuses(config)
    metered = Reconciliation("anthropic", DAY, 2.0, 20.0, -18.0, metered_usd=1.5)
    (after,) = with_bills(config, (before,), (metered,), NOW)
    assert after.spent == pytest.approx(before.spent - 2.0 - 1.5 + 20.0)
    assert after.billed_estimate_usd == pytest.approx(3.5)


def test_without_a_soft_threshold_a_bill_only_hard_keeps_the_traces_state() -> None:
    config = cfg((budget(soft=None),))
    (after,) = with_bills(config, statuses(config), (bill(150.0, 2.0),), NOW)
    assert after.state is BudgetState.OK and after.bill_only_hard


def test_a_24_hour_window_never_holds_a_settled_day() -> None:
    """A day settles 6 hours after it ends (``SETTLE``): it never lies wholly in 24 hours
    that also end at least 6 hours after it."""
    from kelpie.app import SETTLE
    from kelpie.core.budgets import BILLABLE_WINDOW

    assert timedelta(days=1) + SETTLE == BILLABLE_WINDOW
    config = cfg((budget(window=timedelta(hours=24)),))
    for hour in range(24):
        now = datetime(2026, 3, 4, hour, 30, tzinfo=UTC)
        settled = now.replace(hour=0, minute=0) - timedelta(days=1)  # newest settled at best
        if settled + timedelta(days=1) + SETTLE > now:
            settled -= timedelta(days=1)
        before = evaluate(config, {}, Snapshot(now=now)).statuses
        assert with_bills(config, before, (bill(500.0, 2.0, day=settled),), now) == before


def test_rule_10_never_stops_for_spend_only_the_bill_shows() -> None:
    """Negative case for the stop: a farm budget the bill alone takes past ``hard`` gives
    rule 10 no stop, even with ``stop_on_shared_budgets``."""
    from kelpie.core.actions import ActionKind
    from kelpie.core.model import Session, Status, Task
    from kelpie.rules.loop_breaker import build

    config = cfg((budget(),))
    (after,) = with_bills(config, statuses(config), (bill(150.0, 2.0),), NOW)
    snap = Snapshot(
        now=NOW,
        tasks=(Task("T-1", "t", Status.IN_PROGRESS, NOW),),
        sessions=(Session("s-1", "T-1", "agent-a", NOW - timedelta(hours=1)),),
        budgets=(after,),
    )
    actions = build({"stop_on_shared_budgets": True}).evaluate(snap)
    assert not [a for a in actions if a.kind is ActionKind.STOP]


def test_the_tick_counts_the_bill_and_metrics_show_it() -> None:
    """End to end through the tick's assembly: the cost report step (an enrich step) runs
    before the budgets, which then count its bill; /metrics shows the billed figures."""
    from kelpie.app import merged_snapshot
    from kelpie.core.spans import MemorySpanStore
    from kelpie.traces.store import SpanWindow

    config = cfg((budget(),))
    metrics = Metrics()
    metrics.set_budgets_enabled(True)

    def cost_step(snapshot: Snapshot) -> Snapshot:
        return replace(snapshot, reconciliations=(bill(20.0, 2.0),))

    window = SpanWindow(MemorySpanStore(), timedelta(minutes=30), timedelta(hours=24), 1000)
    source = merged_snapshot(
        [], metrics, lambda: NOW, window, config, enrich=[("cost_reports", cost_step)]
    )
    snap = source()
    (status,) = snap.budgets
    assert status.billed_usd == 20.0 and status.spent == pytest.approx(20.0)
    text = metrics.render()
    assert 'kelpie_budget_billed_usd{budget="farm-usd",subject="farm",unit="usd"} 20.000000' in text
    assert 'kelpie_budget_spent{budget="farm-usd",subject="farm",unit="usd"} 20.000000' in text


def test_kelpie_status_counts_the_stored_bills_of_the_providers_days(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``kelpie status`` reads the stored bills of the days kelpie serve has (each
    provider's ``days``); a bill it cannot read leaves the traces and says why."""
    import kelpie.app as app

    config = cfg((budget(window=timedelta(days=30)),))
    before = statuses(config)
    reports = type("R", (), {"reports": (type("P", (), {"provider": "anthropic", "days": 2})(),)})
    settings = type("S", (), {"budgets": config, "cost_reports": reports()})()
    old = bill(500.0, 2.0, day=NOW.replace(hour=0) - timedelta(days=10))  # past ``days``
    recent = bill(20.0, 2.0)
    monkeypatch.setattr(app, "read_reconciliations", lambda *a, **k: (old, recent))
    (after,) = app._status_bills(settings, before, NOW)  # type: ignore[arg-type]
    assert after.billed_days == ("anthropic 2026-03-02",)
    monkeypatch.setattr(app, "read_reconciliations", lambda *a, **k: "cannot read")
    assert app._status_bills(settings, before, NOW) == before  # type: ignore[arg-type]
