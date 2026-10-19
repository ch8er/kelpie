from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome, Suppression
from kelpie.core.model import (
    BudgetScope,
    BudgetState,
    BudgetStatus,
    BudgetUnit,
    Owner,
    Session,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.rules import REGISTRY
from kelpie.rules.loop_breaker import LoopBreaker, Thresholds, build
from tests.fakes import FakeAlerter, StoppingAdapter

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
H = timedelta(hours=1)
POLL = {"ci_status": ["gh pr checks"]}


def session(sid: str = "S-1", owner: str = "agent-1", ended: bool = False, **kw: Any) -> Session:
    return Session(
        sid,
        kw.pop("task_id", "T-1"),
        owner,
        NOW - 5 * H,
        ended_at=NOW - 10 * M if ended else None,
        last_heartbeat=kw.pop("last_heartbeat", NOW - M),
        **kw,
    )


def call(
    i: int,
    at: datetime,
    args: str | None = "a",
    tool: str = "bash",
    outcome: str = "ok",
    sid: str = "S-1",
    command: str | None = None,
) -> Span:
    return Span(
        "TR",
        f"{sid}-{i}",
        f"execute_tool {tool}",
        SpanKind.TOOL,
        at,
        at + timedelta(seconds=1),
        session_id=sid,
        tool=tool,
        args_hash=args,
        outcome=outcome,
        attributes={"command": command} if command else {},
    )


def run(n: int, every: timedelta = M, **kw: Any) -> list[Span]:
    """``n`` calls ``every`` apart, the last one a minute ago."""
    return [call(i, NOW - M - (n - 1 - i) * every, **kw) for i in range(n)]


def snap(
    spans: list[Span] | None = None,
    sessions: tuple[Session, ...] | None = None,
    **kw: Any,
) -> Snapshot:
    return Snapshot(
        now=NOW,
        tasks=kw.pop("tasks", (Task("T-1", "t", Status.IN_PROGRESS, NOW - H, role="dev"),)),
        sessions=sessions if sessions is not None else (session(),),
        spans=tuple(spans or ()),
        **kw,
    )


def evaluate(snapshot: Snapshot, **config: Any) -> list[Action]:
    return list(build(config).evaluate(snapshot))


def kinds(actions: list[Action]) -> list[tuple[ActionKind, Severity]]:
    return [(a.kind, a.severity) for a in actions]


def stops(actions: list[Action]) -> list[Action]:
    return [a for a in actions if a.kind is ActionKind.STOP]


# --- config -----------------------------------------------------------------------------


def test_registered_with_agentwatchdog_defaults() -> None:
    rule = REGISTRY["loop_breaker"]({})
    assert isinstance(rule, LoopBreaker)
    assert rule.thresholds.identical_calls == 8
    assert rule.thresholds.identical_window == 10 * M
    assert rule.thresholds.consecutive_errors == 5
    assert rule.forecast_horizon == 2 * H


def test_config_parsing() -> None:
    rule = build(
        {
            "identical_calls": 3,
            "identical_window": "5m",
            "poll_grace": "1h",
            "forecast_horizon": "30m",
            "poll_patterns": POLL,
            "agents": {"a": {"consecutive_errors": 9}},
            "roles": {"r": {"identical_window": "20m"}},
        }
    )
    assert rule.thresholds.identical_calls == 3
    assert rule.thresholds.poll_grace == H
    assert rule.forecast_horizon == 30 * M
    assert rule.poll_patterns == {"ci_status": ("gh pr checks",)}
    assert rule.agents == {"a": {"consecutive_errors": 9}}
    assert rule.roles == {"r": {"identical_window": 20 * M}}


@pytest.mark.parametrize(
    "config",
    [
        {"nope": 1},
        {"identical_calls": 0},
        {"identical_calls": True},
        {"consecutive_errors": "5"},
        {"identical_window": "0s"},
        {"identical_window": object()},
        {"forecast_horizon": -5},
        {"poll_patterns": ["gh"]},
        {"agents": ["a"]},
        {"agents": {"a": "x"}},
        {"agents": {"a": {"bogus": 1}}},
        {"roles": {"r": {"identical_calls": 0}}},
        {"identical_calls": 40},  # above the default poll_hard_limit of 30
        {"agents": {"a": {"identical_calls": 50}}},
        {"roles": {"r": {"poll_hard_limit": 4}}},
        {"stop_on_shared_budgets": "yes"},
    ],
)
def test_bad_config_is_rejected(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        build(config)


# --- identical calls --------------------------------------------------------------------


def test_identical_calls_stop_the_session_and_alert() -> None:
    actions = evaluate(snap(run(8)))
    assert kinds(actions) == [
        (ActionKind.ALERT, Severity.CRITICAL),
        (ActionKind.STOP, Severity.INFO),
    ]
    stop = stops(actions)[0]
    assert stop.params["session_id"] == "S-1" and stop.task_id == "T-1"
    assert "8 identical consecutive `bash` calls" in stop.reason
    alert = actions[0]
    assert alert.state == "loop_breaker:identical_calls:S-1" and alert.since is not None
    assert alert.params["owner_id"] == "agent-1"


def test_old_history_is_not_a_loop_or_an_error_streak() -> None:
    old = [replace(s, start=s.start - 3 * H, end=None) for s in run(8)]
    assert evaluate(snap(old)) == []
    old_errors = [replace(s, start=s.start - 3 * H, end=None) for s in errors(5)]
    assert evaluate(snap(old_errors)) == []
    assert stops(evaluate(snap(old), identical_window="4h")) != []
    assert stops(evaluate(snap(old_errors), error_window="4h")) != []


def test_below_threshold_or_slow_is_not_a_loop() -> None:
    assert evaluate(snap(run(7))) == []
    assert evaluate(snap(run(8, every=2 * M))) == []  # 8 calls over 14 minutes
    assert stops(evaluate(snap(run(8, every=M)))) != []  # 7 minutes: a loop


def test_same_tool_with_different_args_or_unknown_args_is_not_a_loop() -> None:
    varied = [call(i, NOW - (10 - i) * 10 * M / 10, args=f"a{i}") for i in range(10)]
    assert evaluate(snap(varied)) == []
    unknown = [call(i, NOW - (10 - i) * 5 * M / 10, args=None) for i in range(10)]
    assert evaluate(snap(unknown)) == []


def test_a_different_call_at_the_end_breaks_the_streak() -> None:
    spans = run(10)
    spans.append(call(99, NOW - 1 * M / 2, args="other", tool="edit"))
    assert evaluate(snap(spans)) == []


def test_alert_reports_tokens_since_the_loop_began() -> None:
    spans = run(8)
    spans += [
        Span(
            "TR",
            f"L-{i}",
            "chat",
            SpanKind.LLM,
            s.start - timedelta(seconds=1),
            session_id="S-1",
            input_tokens=80_000,
            output_tokens=100,
        )
        for i, s in enumerate(spans[:8])
    ]
    alert = evaluate(snap(spans))[0]
    assert alert.params["tokens_since"] == 7 * 80_100  # the first call's own model call is older
    assert "About 561k tokens" in alert.reason


def test_ended_and_stale_sessions_are_not_stopped() -> None:
    spans = run(8)
    assert evaluate(snap(spans, sessions=(session(ended=True),))) == []
    stale = session(last_heartbeat=NOW - 2 * H)
    assert evaluate(snap(spans, sessions=(stale,))) == []
    assert stops(evaluate(snap(spans, sessions=(stale,)), session_stale_after="3h")) != []


def test_sessions_are_judged_separately() -> None:
    other = [call(i, NOW - (i + 1) * M, args=f"x{i}", sid="S-2") for i in range(10)]
    sessions = (session(), session("S-2", "agent-2", task_id="T-2"))
    actions = evaluate(snap(run(8) + other, sessions=sessions))
    assert [s.params["session_id"] for s in stops(actions)] == ["S-1"]


# --- error streaks ----------------------------------------------------------------------


def errors(n: int, *, same: bool = False, **kw: Any) -> list[Span]:
    return [
        call(i, NOW - (n - i) * M, args="e" if same else f"e{i}", outcome="error", **kw)
        for i in range(n)
    ]


def test_error_streak_stops_the_session() -> None:
    actions = evaluate(snap(errors(5)))
    stop = stops(actions)[0]
    assert "5 consecutive tool errors" in stop.reason and stop.params["trigger"] == "error_streak"
    assert actions[0].severity is Severity.CRITICAL
    assert actions[0].state == "loop_breaker:error_streak:S-1"


def test_shorter_or_interrupted_error_streaks_are_fine() -> None:
    assert evaluate(snap(errors(4))) == []
    spans = errors(6)
    spans[2] = replace(spans[2], outcome="ok")
    assert evaluate(snap(spans)) == []  # only 3 errors since the success


def test_one_parallel_batch_of_failures_is_not_a_streak() -> None:
    batch = [
        call(i, NOW - M + timedelta(seconds=i), args=f"r{i}", outcome="error") for i in range(6)
    ]
    assert evaluate(snap(batch)) == []
    assert stops(evaluate(snap(batch), min_error_span="3s")) != []


def test_loop_and_errors_together_give_one_stop() -> None:
    actions = evaluate(snap(errors(8, same=True)))
    assert len(stops(actions)) == 1
    assert sorted(a.params["trigger"] for a in actions if a.kind is ActionKind.ALERT) == [
        "error_streak",
        "identical_calls",
    ]


# --- precedence with rule 2 -------------------------------------------------------------


def polls(n: int, every: timedelta = M, **kw: Any) -> list[Span]:
    return run(n, every=every, args="gh-77", command="gh pr checks 77", **kw)


def test_polling_is_left_to_rule_2_within_the_grace_period() -> None:
    assert evaluate(snap(polls(12)), poll_patterns=POLL) == []
    # Without patterns the same calls are an ordinary loop.
    assert stops(evaluate(snap(polls(12)))) != []


def test_polling_ignored_past_the_grace_period_is_stopped() -> None:
    actions = evaluate(snap(polls(26)), poll_patterns=POLL)
    stop = stops(actions)[0]
    assert "26 identical" in stop.reason and "wait_condition coaching" in stop.reason
    assert evaluate(snap(polls(26)), poll_patterns=POLL, poll_grace="1h") == []


def test_polling_past_the_hard_limit_is_stopped_whatever_the_grace() -> None:
    every = 20 * M / 30
    assert evaluate(snap(polls(29, every)), poll_patterns=POLL, poll_grace="1h") == []
    actions = evaluate(snap(polls(30, every)), poll_patterns=POLL, poll_grace="1h")
    assert "hard limit of 30" in stops(actions)[0].reason


def test_errors_of_one_polled_check_follow_the_polling_rules() -> None:
    failing = polls(6, outcome="error")
    assert evaluate(snap(failing), poll_patterns=POLL) == []
    mixed = errors(6, command="gh pr checks 77")  # varied args: not the same check
    assert stops(evaluate(snap(mixed), poll_patterns=POLL)) != []


# --- overrides --------------------------------------------------------------------------


def test_overrides_per_agent_and_role() -> None:
    spans = run(10)
    assert evaluate(snap(spans), agents={"agent-1": {"identical_calls": 20}}) == []
    owners = (Owner("agent-1", "maintenance"),)
    assert (
        evaluate(snap(spans, owners=owners), roles={"maintenance": {"identical_calls": 20}}) == []
    )
    # A task role is used when the owner is unknown, or has no role of its own.
    assert evaluate(snap(spans), roles={"dev": {"identical_calls": 20}}) == []
    blank = (Owner("agent-1", ""),)
    assert evaluate(snap(spans, owners=blank), roles={"dev": {"identical_calls": 20}}) == []
    # The agent entry wins over the role entry.
    both = {
        "roles": {"maintenance": {"identical_calls": 20}},
        "agents": {"agent-1": {"identical_calls": 5}},
    }
    assert stops(evaluate(snap(spans, owners=owners), **both)) != []
    # Other agents keep the defaults.
    assert stops(evaluate(snap(spans), agents={"someone-else": {"identical_calls": 20}})) != []


def test_thresholds_dataclass_defaults_match_rule_2() -> None:
    assert Thresholds().poll_comment_after == 8


# --- budgets ----------------------------------------------------------------------------


def status(**kw: Any) -> BudgetStatus:
    base: dict[str, Any] = {
        "budget": "agent-daily",
        "scope": BudgetScope.AGENT,
        "subject": "agent-1",
        "unit": BudgetUnit.USD,
        "spent": 10.0,
        "soft": 40.0,
        "hard": 50.0,
        "remaining": 40.0,
        "session_ids": ("S-1",),
    }
    return BudgetStatus(**{**base, **kw})


def with_budgets(*budgets: BudgetStatus, **kw: Any) -> list[Action]:
    return evaluate(
        snap(budgets=budgets, **{k: v for k, v in kw.items() if k != "config"}),
        **kw.get("config", {}),
    )


def test_soft_state_is_a_lasting_warning() -> None:
    [alert] = with_budgets(status(state=BudgetState.SOFT, spent=41.0))
    assert alert.kind is ActionKind.ALERT and alert.severity is Severity.WARNING
    assert alert.state == "loop_breaker:budget:agent-daily:agent-1"
    assert "$41.00" in alert.reason and "$40.00" in alert.reason
    assert alert.params["owner_id"] == "agent-1" and alert.task_id is None


def test_hard_state_stops_live_sessions_only_and_alerts_with_the_spend() -> None:
    sessions = (
        session("S-1"),
        session("S-2", task_id="T-2"),
        session("S-3", task_id="T-3", ended=True),
        session("S-4", task_id="T-4"),  # live, but spent nothing within the window
    )
    actions = with_budgets(
        status(
            state=BudgetState.HARD,
            spent=55.5,
            remaining=0.0,
            runs_out_at=NOW,
            session_ids=("S-1", "S-2", "S-3"),
        ),
        sessions=sessions,
    )
    alert = actions[0]
    assert alert.severity is Severity.CRITICAL
    assert alert.state == "loop_breaker:budget:agent-daily:agent-1"  # same state as soft
    assert (
        alert.key.endswith(":hard:S-1, S-2")
        and "$55.50" in alert.reason
        and "$50.00" in alert.reason
    )
    assert "has run out" in alert.reason
    assert alert.params["session_ids"] == ["S-1", "S-2"] and alert.params["runs_out_at"]
    assert {s.params["session_id"] for s in stops(actions)} == {"S-1", "S-2"}


def test_shared_budgets_alert_but_stop_nothing_unless_asked() -> None:
    for scope in (BudgetScope.DAY, BudgetScope.PROJECT):
        shared = status(
            scope=scope, subject="farm", state=BudgetState.HARD, spent=900.0, hard=800.0
        )
        [alert] = with_budgets(shared)
        assert alert.severity is Severity.CRITICAL and alert.params["session_ids"] == []
        assert "no session is stopped automatically" in alert.reason
        actions = with_budgets(shared, config={"stop_on_shared_budgets": True})
        assert [s.params["session_id"] for s in stops(actions)] == ["S-1"]
    # Task and agent budgets stop by default.
    for scope in (BudgetScope.TASK, BudgetScope.AGENT):
        assert stops(with_budgets(status(scope=scope, state=BudgetState.HARD)))


def test_a_session_caught_later_changes_the_alert_key() -> None:
    hard = status(state=BudgetState.HARD, session_ids=("S-1", "S-2"))
    one = with_budgets(hard, sessions=(session("S-1"),))[0]
    both = with_budgets(hard, sessions=(session("S-1"), session("S-2", task_id="T-2")))[0]
    assert one.state == both.state and one.key != both.key
    assert "Live session(s) to stop: S-1, S-2" in both.reason


def test_hard_state_without_live_sessions_only_alerts() -> None:
    actions = with_budgets(status(state=BudgetState.HARD, session_ids=("S-9",)))
    assert kinds(actions) == [(ActionKind.ALERT, Severity.CRITICAL)]
    assert "No live session to stop" in actions[0].reason


def test_stale_budgets_emit_nothing() -> None:
    hard = status(state=BudgetState.HARD)
    assert with_budgets(hard, budgets_ok=False) == []


def test_under_budget_is_not_flagged() -> None:
    assert with_budgets(status(runs_out_at=NOW + 20 * H, burn_rate=1.0)) == []


def test_forecast_inside_the_horizon_is_an_info_alert() -> None:
    soon = status(runs_out_at=NOW + 90 * M, burn_rate=30.0, spent=20.0)
    [alert] = with_budgets(soon)
    assert (
        alert.severity is Severity.INFO
        and alert.state == "loop_breaker:forecast:agent-daily:agent-1"
    )
    assert "runs out at 2026-03-02 13:30 UTC" in alert.reason and "in 90 min" in alert.reason
    assert with_budgets(replace(soon, runs_out_at=NOW + 3 * H)) == []
    assert with_budgets(replace(soon, runs_out_at=NOW + 3 * H), config={"forecast_horizon": "4h"})
    assert with_budgets(replace(soon, runs_out_at=NOW - M)) == []  # a stale forecast
    assert with_budgets(replace(soon, soft=None, hard=None, runs_out_at=None)) == []


def test_token_budgets_and_task_scope() -> None:
    task_budget = status(
        scope=BudgetScope.TASK,
        subject="T-1",
        unit=BudgetUnit.TOKENS,
        state=BudgetState.SOFT,
        spent=5_200_000,
        soft=5_000_000,
        hard=8_000_000,
    )
    [alert] = with_budgets(task_budget)
    assert alert.task_id == "T-1" and "5.2M tokens" in alert.reason
    assert "owner_id" not in alert.params


def test_subscription_budgets_say_estimate_and_forecast_the_next_limit() -> None:
    sub = status(
        unit=BudgetUnit.TOKENS,
        spent=3_000_000,
        soft=8_000_000,
        hard=10_000_000,
        estimated=True,
        next_limit_at=NOW + 80 * M,
        runs_out_at=NOW + 150 * M,
    )
    actions = with_budgets(sub, config={"forecast_horizon": "3h"})
    assert {a.state for a in actions} == {
        "loop_breaker:limit:agent-daily:agent-1",
        "loop_breaker:forecast:agent-daily:agent-1",
    }
    assert all("stimate" in a.reason and a.severity is Severity.INFO for a in actions)
    assert (
        evaluate(snap(budgets=(replace(sub, next_limit_at=NOW + 5 * H, runs_out_at=None),))) == []
    )
    # The limit forecast only exists for estimated (subscription) budgets.
    assert with_budgets(replace(sub, estimated=False, runs_out_at=None)) == []
    # It is independent of the soft state.
    soft = replace(sub, state=BudgetState.SOFT, runs_out_at=None)
    assert {a.state.split(":")[1] for a in with_budgets(soft) if a.state} == {"budget", "limit"}


def test_hard_estimate_wording() -> None:
    [alert, stop] = with_budgets(status(state=BudgetState.HARD, estimated=True))
    assert alert.reason.startswith("Estimate, not a bill") and stop.kind is ActionKind.STOP


# --- engine -----------------------------------------------------------------------------


def test_hard_budget_stop_is_announced_once_not_every_tick() -> None:
    hard = status(state=BudgetState.HARD, spent=52.0, remaining=0.0, runs_out_at=NOW)
    snapshot = snap(budgets=(hard,))
    adapter, alerter = StoppingAdapter(), FakeAlerter()
    config = EngineConfig(dry_run=False, modes={"loop_breaker": Mode.ACT})
    engine = Engine([build({})], adapters=[adapter], alerter=alerter, config=config)  # type: ignore[list-item, arg-type]
    rule = build({})
    outcomes = []
    for i in range(4):
        later = replace(snapshot, now=NOW + i * timedelta(seconds=30))
        outcomes += [
            (d.action.kind, d.outcome, d.suppression)
            for d in engine.execute(list(rule.evaluate(later)), later)
        ]

    assert adapter.calls == [("stop", "S-1")]
    assert [t for t, _ in alerter.sent] == ["[loop_breaker] alert"]
    assert [o for k, o, _ in outcomes if k is ActionKind.ALERT] == [
        Outcome.ALERTED,
        Outcome.SUPPRESSED,
        Outcome.SUPPRESSED,
        Outcome.SUPPRESSED,
    ]
    assert [o for k, o, _ in outcomes if k is ActionKind.STOP] == [
        Outcome.EXECUTED,
        Outcome.SUPPRESSED,
        Outcome.SUPPRESSED,
        Outcome.SUPPRESSED,
    ]
    assert Suppression.STATE_OPEN in {r for _, _, r in outcomes}


def test_soft_to_hard_is_announced_again() -> None:
    adapter, alerter = StoppingAdapter(), FakeAlerter()
    config = EngineConfig(dry_run=False, modes={"loop_breaker": Mode.ACT})
    engine = Engine([build({})], adapters=[adapter], alerter=alerter, config=config)  # type: ignore[list-item, arg-type]
    rule = build({})
    for state, at in (
        (BudgetState.SOFT, NOW),
        (BudgetState.SOFT, NOW + M),
        (BudgetState.HARD, NOW + 2 * M),
    ):
        shot = replace(snap(budgets=(status(state=state, spent=45.0),)), now=at)
        engine.execute(list(rule.evaluate(shot)), shot)
    assert [sev for _, sev in alerter.sent] == [Severity.WARNING, Severity.CRITICAL]
    assert adapter.calls == [("stop", "S-1")]


# --- the bill -------------------------------------------------------------------------------


def _rec(provider: str, day: int, est: float, bill: float, large: bool, **kw: Any) -> Any:
    from kelpie.core.model import Reconciliation

    gap = est - bill
    return Reconciliation(provider, datetime(2026, 3, day, tzinfo=UTC), est, bill, gap,
                          gap / bill if bill else None, large, **kw)  # fmt: skip


def test_the_bill_within_thresholds_says_nothing() -> None:
    snap = Snapshot(now=NOW, reconciliations=(_rec("anthropic", 1, 82.1, 84.5, False),))
    assert LoopBreaker().evaluate(snap) == []


def test_traces_above_the_bill_name_other_causes() -> None:
    snap = Snapshot(now=NOW, reconciliations=(_rec("openai", 1, 30.0, 10.0, True),))
    (alert,) = LoopBreaker().evaluate(snap)
    assert alert.severity is Severity.WARNING and "(+200%)" in alert.reason
    assert "prices above the bill's" in alert.reason and alert.key == "bill:openai"


def test_a_bill_of_zero_has_no_ratio() -> None:
    snap = Snapshot(now=NOW, reconciliations=(_rec("openai", 1, 12.0, 0.0, True),))
    (alert,) = LoopBreaker().evaluate(snap)
    assert "%" not in alert.reason.split("cost report")[1].split(";")[0]
    assert alert.params["gap_ratio"] is None


def test_the_newest_day_decides_even_when_it_agrees() -> None:
    snap = Snapshot(
        now=NOW,
        reconciliations=(
            _rec("anthropic", 2, 84.0, 84.5, False),
            _rec("anthropic", 1, 1.0, 50.0, True),
        ),
    )
    assert LoopBreaker().evaluate(snap) == []


def test_a_bill_of_everything_names_non_token_items_first() -> None:
    snap = Snapshot(
        now=NOW,
        reconciliations=(
            _rec("openai", 1, 10.0, 30.0, True, bill_scope="total", unknown_tokens=900),
        ),
    )
    (alert,) = LoopBreaker().evaluate(snap)
    causes = alert.reason.split("traces show less than was billed: ")[1]
    assert causes.startswith("non-token items the bill includes")
    assert "calls whose provider is not known (900 tokens)" in causes
    assert "off by as much" not in alert.reason


def test_a_resolved_bill_says_what_the_newest_day_shows() -> None:
    rule = LoopBreaker()
    calm = Snapshot(now=NOW, reconciliations=(_rec("anthropic", 2, 84.0, 84.5, False),))
    text = rule.resolved("loop_breaker:bill:anthropic", calm)
    assert text is not None and "agree within the thresholds for 2026-03-02" in text
    assert "$84.00 priced from traces, $84.50 in the cost report (-1%)" in text
    gone = rule.resolved("loop_breaker:bill:anthropic", Snapshot(now=NOW))
    assert gone == "the anthropic bill is no longer reconciled with the traces."
    assert rule.resolved("loop_breaker:budget:x", calm) is None


def test_a_resolved_bill_that_is_a_credit_says_so() -> None:
    credit = Snapshot(now=NOW, reconciliations=(_rec("anthropic", 2, 5.0, -20.0, False),))
    text = LoopBreaker().resolved("loop_breaker:bill:anthropic", credit)
    assert text == "the anthropic bill for 2026-03-02 is a credit ($-20.00)."
