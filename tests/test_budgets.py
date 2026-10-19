"""Budgets: config validation, price matching, spend per scope, forecasts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.budgets import (
    Budget,
    BudgetConfig,
    BudgetMode,
    BudgetScope,
    BudgetState,
    BudgetUnit,
    LimitHit,
    Price,
    PriceTable,
    evaluate,
    load_budgets,
    next_limit_at,
    threshold_forecast,
)
from kelpie.core.model import Session, Snapshot, Status, Task
from kelpie.core.prices import DEFAULT_PRICES
from kelpie.core.serde import snapshot_from_dict
from kelpie.core.spans import Attribution, TokenUsage

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
WEEK = timedelta(days=7)
H = timedelta(hours=1)
PRICES = {"model-a": {"input": 3, "output": 15, "cached_input": 0.3}}


def budget(**kw: Any) -> dict[str, Any]:
    return {"name": "b", "scope": "day", "unit": "usd", "hard": 10, **kw}


def load(*budgets: dict[str, Any], prices: Any = PRICES) -> BudgetConfig:
    return load_budgets(list(budgets), prices, WEEK)


# --- config ------------------------------------------------------------------------------


def test_defaults_and_full_budget() -> None:
    assert load_budgets(None, None, WEEK, default_prices=False) == BudgetConfig()
    assert load_budgets(None, None, WEEK).prices == PriceTable((), DEFAULT_PRICES)
    config = load(
        budget(),
        budget(name="t", scope="task", unit="tokens", soft=1000, hard=None),
        budget(
            name="a",
            scope="agent",
            match="agent-dev-*",
            window="12h",
            burn_window="30m",
            soft=5,
            hard=8,
            mode="subscription",
        ),
    )
    day, task, agent = config.budgets
    assert day == Budget("b", BudgetScope.DAY, BudgetUnit.USD, 24 * H, hard=10.0)
    assert (day.burn_window, day.mode, day.limit) == (H, BudgetMode.API, 10.0)
    assert task.window == WEEK and task.limit == 1000  # task: lifetime within retention
    assert (agent.window, agent.burn_window, agent.mode) == (
        12 * H,
        timedelta(minutes=30),
        BudgetMode.SUBSCRIPTION,
    )
    assert config.windows == {24 * H, H, WEEK, 12 * H, timedelta(minutes=30)}
    # a window shorter than an hour also bounds the default burn window
    short = load(budget(window="20m")).budgets[0]
    assert short.burn_window == timedelta(minutes=20)


@pytest.mark.parametrize(
    ("budgets", "prices", "message"),
    [
        ({"name": "x"}, PRICES, "must be an array of tables"),
        ([budget(), 3], PRICES, "must be an array of tables"),
        ([budget(name="")], PRICES, "needs a non-empty name"),
        ([budget(limit=3)], PRICES, "unknown budget 'b' keys: ['limit']"),
        ([{"name": "b", "unit": "usd", "hard": 1}], PRICES, "needs scope"),
        ([{"name": "b", "scope": "day", "hard": 1}], PRICES, "needs unit"),
        ([budget(scope="week")], PRICES, "scope must be one of day | task | agent | project"),
        ([budget(unit="eur")], PRICES, "unit must be one of usd | tokens"),
        ([budget(mode="free")], PRICES, "mode must be one of api | subscription"),
        ([budget(match="x")], PRICES, "a day budget covers the whole farm"),
        ([budget(scope="project")], PRICES, "a project budget needs match"),
        ([budget(scope="task", match=" ")], PRICES, "match must be a non-empty string"),
        ([budget(scope="agent", match="label:x")], PRICES, "label: matches are for project"),
        ([budget(scope="project", match="label:")], PRICES, "needs a label name"),
        ([budget(window="8d")], PRICES, "longer than [traces] retention"),
        ([budget(window="0s")], PRICES, "window must be positive"),
        ([budget(window="soon")], PRICES, "invalid duration"),
        ([budget(burn_window="2d")], PRICES, "burn_window must not be longer than window"),
        ([budget(hard=None)], PRICES, "needs soft, hard or both"),
        ([budget(soft=10, hard=10)], PRICES, "hard must be greater than soft"),
        ([budget(hard=0)], PRICES, "hard must be positive"),
        ([budget(hard=-1)], PRICES, "hard must be positive"),
        ([budget(hard=True)], PRICES, "hard must be a number"),
        ([budget(hard="10")], PRICES, "hard must be a number"),
        ([budget(hard=float("inf"))], PRICES, "hard must be a number"),
        ([budget(), budget()], PRICES, "names must be unique: ['b']"),
        ([], ["model-a"], "[prices] must be a table of models"),
        ([], {"model-a": 3}, "must be a table with input"),
        ([], {"model-a": {"input": 1}}, "needs output"),
        ([], {"model-a": {"input": 1, "output": 2, "batch": 1}}, "keys: ['batch']"),
        ([], {"model-a": {"input": -1, "output": 2}}, "input must be non-negative"),
        ([], {"model-a": {"input": 1, "output": "2"}}, "output must be a number"),
        ([], {" ": {"input": 1, "output": 2}}, "model names must be non-empty"),
    ],
)
def test_bad_config_is_refused(budgets: Any, prices: Any, message: str) -> None:
    with pytest.raises(ValueError) as exc:
        load_budgets(budgets, prices, WEEK)
    assert message in str(exc.value)


def test_usd_budgets_need_some_prices() -> None:
    """The default table prices usd budgets; without it they need [prices]."""
    (b,) = load_budgets([budget()], None, WEEK).budgets
    assert b.unit is BudgetUnit.USD
    with pytest.raises(ValueError, match=r"budgets in usd need prices .*\['b'\]"):
        load_budgets([budget()], None, WEEK, default_prices=False)


def test_token_budgets_need_no_prices() -> None:
    (b,) = load(budget(unit="tokens"), prices=None).budgets
    assert b.unit is BudgetUnit.TOKENS


# --- prices ------------------------------------------------------------------------------


def test_price_matching_exact_then_first_glob_in_order() -> None:
    table = load_budgets(
        [],
        {
            "model-*": {"input": 1, "output": 2},
            "model-a*": {"input": 5, "output": 6},
            "model-a-mini": {"input": 0.5, "output": 1, "cached_input": 0.05},
        },
        WEEK,
    ).prices
    assert table.price_for("model-a-mini") == Price(0.5, 1, 0.05)
    assert table.price_for("model-a-large") == Price(1, 2, 1)  # first glob; cached = input
    assert table.price_for("other") is None
    assert table.price_for(None) is None
    assert PriceTable().price_for("model-a") is None


def test_cost_prices_cached_input_separately() -> None:
    price = Price(input=3, output=15, cached_input=0.3)
    usage = TokenUsage(None, None, "m", 1, 1_000_000, 100_000, 400_000)
    assert price.cost(usage) == pytest.approx(600_000 * 3 / 1e6 + 0.4 * 0.3 + 0.1 * 15)
    # a source that reports more cached than input tokens is not priced below zero
    odd = TokenUsage(None, None, "m", 1, 10, 0, 50)
    assert price.cost(odd) == pytest.approx(10 * 0.3 / 1e6)


# --- spend per scope -----------------------------------------------------------------------


def row(session: str | None, task: str | None, tokens: int, model: str = "model-a") -> TokenUsage:
    return TokenUsage(session, task, model, 1, tokens, tokens // 10, 0)


def snapshot(**kw: Any) -> Snapshot:
    sessions = (
        Session("run-1", "T-1", "agent-dev-1", NOW - 2 * H),
        Session("run-2", "T-2", "agent-dev-2", NOW - 2 * H),
        Session("run-3", "T-3", "reviewer-1", NOW - 2 * H),
    )
    tasks = (
        Task("T-1", "one", Status.IN_PROGRESS, NOW, labels=("web",)),
        Task("T-2", "two", Status.IN_PROGRESS, NOW),
    )
    return Snapshot(now=NOW, sessions=sessions, tasks=tasks, **kw)


ROWS = (
    row("run-1", "T-1", 1_000_000),
    row("run-1", None, 500_000),  # task from the session
    row("run-2", "T-2", 200_000),
    row("run-3", "T-3", 100_000, model="unpriced"),
    row("run-x", "T-9", 50_000),  # session no adapter knows
    row(None, None, 10_000),
)


def by_subject(config: BudgetConfig, usage: Any = None) -> dict[tuple[str, str], Any]:
    usage = usage if usage is not None else {w: ROWS for w in config.windows}
    return {(s.budget, s.subject): s for s in evaluate(config, usage, snapshot()).statuses}


def cost(tokens: int) -> float:
    return tokens * 3 / 1e6 + tokens // 10 * 15 / 1e6


def test_day_budget_covers_the_whole_farm() -> None:
    got = by_subject(load(budget(hard=100)))
    (farm,) = got.values()
    assert farm.subject == "farm" and farm.scope is BudgetScope.DAY
    priced = 1_000_000 + 500_000 + 200_000 + 50_000 + 10_000
    assert farm.spent == pytest.approx(cost(priced))
    assert farm.cost_unknown_tokens == 110_000
    assert farm.session_ids == ("run-1", "run-2", "run-3", "run-x")


def test_agent_budget_per_owner_and_matched() -> None:
    got = by_subject(
        load(
            budget(name="each", scope="agent", unit="tokens"),
            budget(name="devs", scope="agent", match="agent-dev-*", hard=100),
            budget(name="one", scope="agent", match="agent-dev-9", hard=100),
        )
    )
    assert sorted(got) == [
        ("devs", "agent-dev-1"),
        ("devs", "agent-dev-2"),
        ("each", "agent-dev-1"),
        ("each", "agent-dev-2"),
        ("each", "reviewer-1"),
        ("one", "agent-dev-9"),  # a named subject is reported with no spend
    ]
    assert got["each", "agent-dev-1"].spent == 1_650_000
    assert got["each", "reviewer-1"].spent == 110_000  # tokens count without a price
    assert got["each", "reviewer-1"].cost_unknown_tokens == 110_000
    assert got["devs", "agent-dev-2"].spent == pytest.approx(cost(200_000))
    assert got["one", "agent-dev-9"].spent == 0 and got["one", "agent-dev-9"].remaining == 100


def test_task_budget_uses_the_session_task_when_the_span_has_none() -> None:
    got = by_subject(load(budget(scope="task", unit="tokens", hard=2_000_000)))
    assert {k[1]: v.spent for k, v in got.items()} == {
        "T-1": 1_650_000,
        "T-2": 220_000,
        "T-3": 110_000,
        "T-9": 55_000,
    }
    matched = by_subject(load(budget(scope="task", match="T-1", unit="tokens")))
    assert list(matched) == [("b", "T-1")]


def test_project_budget_by_label_or_task_glob() -> None:
    got = by_subject(
        load(
            budget(name="web", scope="project", match="label:web", unit="tokens"),
            budget(name="all-t", scope="project", match="T-*", unit="tokens"),
            budget(name="none", scope="project", match="label:mobile", unit="tokens"),
        )
    )
    assert got["web", "label:web"].spent == 1_650_000
    assert got["all-t", "T-*"].spent == 1_650_000 + 220_000 + 110_000 + 55_000
    assert got["none", "label:mobile"].spent == 0


def test_unattributed_spend_is_reported_per_budget() -> None:
    config = load(
        budget(name="day", unit="tokens"),
        budget(name="agents", scope="agent", unit="tokens"),
        budget(name="tasks", scope="task", unit="tokens"),
        budget(name="web", scope="project", match="label:web", unit="tokens"),
        budget(name="glob", scope="project", match="T-*", unit="tokens"),
    )
    result = evaluate(config, {w: ROWS for w in config.windows}, snapshot())
    assert dict(result.unattributed) == {
        "agents": 55_000 + 11_000,  # run-x has no known owner; one row has no session
        "tasks": 11_000,  # no task at all
        "web": 110_000 + 55_000 + 11_000,  # T-3, T-9: labels never seen
        "glob": 11_000,
    }


def test_remembered_attribution_keeps_spend_of_dropped_sessions_and_tasks() -> None:
    config = load(
        budget(name="agents", scope="agent", unit="tokens"),
        budget(name="web", scope="project", match="label:web", unit="tokens"),
    )
    remembered = Attribution(
        owners={"run-x": "agent-old", "run-1": "stale-owner"},
        session_tasks={"run-x": "T-9"},
        labels={"T-9": ("web",), "T-3": (), "T-1": ()},
    )
    result = evaluate(config, {w: ROWS for w in config.windows}, snapshot(), remembered)
    got = {(s.budget, s.subject): s.spent for s in result.statuses}
    assert got["agents", "agent-old"] == 55_000
    assert got["agents", "agent-dev-1"] == 1_650_000  # the snapshot wins over memory
    assert got["web", "label:web"] == 1_650_000 + 55_000  # T-1 labels from the snapshot
    assert dict(result.unattributed) == {"agents": 11_000, "web": 11_000}


def test_states_remaining_and_forecast() -> None:
    config = load(
        budget(name="soft", unit="tokens", soft=1_000_000, hard=None),
        budget(name="hard", unit="tokens", soft=100_000, hard=1_000_000),
        budget(name="ok", unit="tokens", soft=3_000_000, hard=4_000_000),
    )
    recent = (row("run-1", "T-1", 100_000),)  # 110k tokens in the last hour
    usage = {24 * H: ROWS, H: recent}
    got = by_subject(config, usage)
    assert got["soft", "farm"].state is BudgetState.SOFT
    assert got["hard", "farm"].state is BudgetState.HARD
    assert got["hard", "farm"].remaining == 0
    assert got["hard", "farm"].runs_out_at == NOW  # already reached
    ok = got["ok", "farm"]
    assert ok.state is BudgetState.OK
    assert ok.spent == 2_046_000 and ok.remaining == 1_954_000
    assert ok.burn_rate == 110_000
    assert ok.runs_out_at == NOW + timedelta(hours=1_954_000 / 110_000)
    assert not ok.estimated and ok.window == 24 * H


def test_missing_usage_counts_as_none() -> None:
    (status,) = evaluate(load(budget()), {}, snapshot()).statuses
    assert (status.spent, status.burn_rate, status.runs_out_at) == (0, 0, None)


def test_threshold_forecast_math() -> None:
    assert threshold_forecast(10, 2, NOW) == NOW + timedelta(hours=5)
    assert threshold_forecast(0, 2, NOW) == NOW
    assert threshold_forecast(10, 0, NOW) is None
    assert threshold_forecast(1e12, 1e-6, NOW) is None  # too far out to mean anything


# --- subscription mode -------------------------------------------------------------------


def hit(at_h: float, reset_h: float | None = None, limit: str = "5h") -> LimitHit:
    reset = None if reset_h is None else NOW + timedelta(hours=reset_h)
    return LimitHit(limit, NOW + timedelta(hours=at_h), reset)


def test_next_limit_from_observed_hits() -> None:
    # periods began at resets -20h and -10h; limits hit 3h and 4h later; the current period
    # began at -1h, so the next limit is expected 3.5h after it
    hits = [hit(-23, -20), hit(-17, -10), hit(-6, -1)]
    assert next_limit_at(hits, NOW) == NOW + timedelta(hours=2.5)
    # odd number of samples (3h, 4h, 0.5h): the median is the middle one
    assert next_limit_at([*hits, hit(-0.5, -0.25)], NOW) == NOW + timedelta(hours=2.75)


def test_next_limit_edge_cases() -> None:
    assert next_limit_at([], NOW) is None
    assert next_limit_at([hit(-3, -1)], NOW) is None  # one hit: no time-to-limit yet
    assert next_limit_at([hit(-5, -4), hit(-1, 2)], NOW) == NOW - H  # limited right now
    assert next_limit_at([hit(-30, -28), hit(-25, -20)], NOW) is None  # overdue: lighter use
    assert next_limit_at([hit(-5, -4), hit(-1, -0.5), hit(1)], NOW) is not None  # future hit
    # without known resets, periods start at the previous hit
    assert next_limit_at([hit(-3), hit(-1)], NOW) == NOW + H
    # per limit, and the earliest forecast wins
    weekly = [hit(-200, -100, "weekly"), hit(-50, -40, "weekly")]
    assert next_limit_at(weekly, NOW) == NOW + 10 * H
    assert next_limit_at([*weekly, hit(-3), hit(-1)], NOW) == NOW + H


def test_subscription_budget_is_an_estimate_forecast_from_limit_hits() -> None:
    config = load(budget(mode="subscription", hard=100))
    usage = {w: ROWS for w in config.windows}
    (plain,) = evaluate(config, usage, snapshot()).statuses
    assert plain.estimated and plain.next_limit_at is None  # no limit hits yet
    hits = (hit(-23, -20), hit(-17, -10), hit(-6, -1))
    (sub,) = evaluate(config, usage, snapshot(limit_hits=hits)).statuses
    assert sub.next_limit_at == NOW + timedelta(hours=2.5)
    assert sub.runs_out_at == plain.runs_out_at  # the threshold forecast stays
    assert sub.spent == plain.spent  # thresholds still apply to the estimate
    # api budgets ignore limit hits
    (api,) = evaluate(load(budget(hard=100)), usage, snapshot(limit_hits=hits)).statuses
    assert api.next_limit_at is None and not api.estimated


def test_subscription_forecast_uses_only_its_providers_and_long_limit_windows() -> None:
    usage_hits = (hit(-23, -20), hit(-17, -10), hit(-6, -1))  # limit "5h"
    probe_hits = tuple(
        LimitHit("provider-main", NOW - timedelta(hours=h), NOW - timedelta(hours=h - 0.05))
        for h in (2, 1, 0.5)  # per-minute style limits: 3-minute windows
    )
    hits = usage_hits + probe_hits
    config = load(budget(mode="subscription", hard=100))
    usage = {w: ROWS for w in config.windows}
    (all_,) = evaluate(config, usage, snapshot(limit_hits=hits)).statuses
    assert all_.next_limit_at == NOW + timedelta(hours=2.5)  # short windows are ignored
    only = load(budget(mode="subscription", hard=100, providers=["provider-main"]))
    (none,) = evaluate(only, usage, snapshot(limit_hits=hits)).statuses
    assert none.next_limit_at is None
    short = load(
        budget(mode="subscription", hard=100, providers=["provider-main"], min_limit_window="1m")
    )
    (probes,) = evaluate(short, usage, snapshot(limit_hits=hits)).statuses
    assert probes.next_limit_at is not None
    assert only.budgets[0].providers == ("provider-main",)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"providers": []}, "non-empty list"),
        ({"providers": "main"}, "non-empty list"),
        ({"providers": [""]}, "non-empty list"),
        ({"min_limit_window": "0s"}, "min_limit_window"),
    ],
)
def test_subscription_provider_keys_are_checked(extra: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load(budget(mode="subscription", **extra))
    with pytest.raises(ValueError, match="are for mode = subscription"):
        load(budget(providers=["main"]))


def test_snapshot_fixtures_can_carry_budget_state() -> None:
    snap = snapshot_from_dict(
        {
            "now": "2026-03-02T12:00:00+00:00",
            "budgets": [
                {
                    "budget": "daily",
                    "scope": "agent",
                    "subject": "agent-dev-1",
                    "unit": "usd",
                    "spent": 120.5,
                    "state": "hard",
                    "soft": 50,
                    "hard": 100,
                    "runs_out_at": "2026-03-02T11:00:00+00:00",
                    "window": "24h",
                    "session_ids": ["run-1"],
                }
            ],
            "limit_hits": [{"limit": "5h", "at": "2026-03-02T10:00:00+00:00"}],
        }
    )
    (status,) = snap.budgets
    assert status.state is BudgetState.HARD and status.window == 24 * H
    assert status.session_ids == ("run-1",)
    assert snap.limit_hits[0].limit == "5h"


def test_evaluation_names_the_models_without_a_price() -> None:
    config = load(budget(), budget(name="t", unit="tokens", window="2d"))
    usage = {
        timedelta(days=1): ROWS[:2],
        timedelta(days=2): (*ROWS, row("run-1", "T-1", 7, model="other")),
        H: (),
    }
    assert evaluate(config, usage, snapshot()).unpriced == {"other": 7, "unpriced": 110_000}
    assert evaluate(config, {}, snapshot()).unpriced == {}
    assert evaluate(load(), {}, snapshot()).unpriced == {}  # no budgets: nothing looked at


def test_evaluation_prices_from_the_default_table_by_day() -> None:
    """No [prices]: the default table prices usd budgets, as of the snapshot's day."""
    config = load(budget(), prices=None)
    usage = {w: (row("run-1", "T-1", 1_000_000, model="gemini-4-argon"),) for w in config.windows}
    (before,) = evaluate(config, usage, snapshot()).statuses
    assert before.spent == pytest.approx(2 + 0.1 * 10) and before.cost_unknown_tokens == 0
    late = Snapshot(now=datetime(2026, 12, 2, tzinfo=UTC))
    (after,) = evaluate(config, usage, late).statuses
    assert after.spent == pytest.approx(4 + 0.1 * 20)
