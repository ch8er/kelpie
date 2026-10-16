import logging
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.model import ConditionKind, Session, Snapshot, Status, Task
from kelpie.core.ports import (
    ActionConnector,
    Alerter,
    PlatformAdapter,
    SupportsReassign,
    SupportsStop,
    SupportsWait,
    UnsupportedAction,
)
from tests.fakes import (
    Broken,
    Emit,
    FakeAdapter,
    FakeAlerter,
    FakeCI,
    ReassigningAdapter,
    StoppingAdapter,
    WaitingAdapter,
)

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
ACT = EngineConfig(dry_run=False, default_mode=Mode.ACT)


def act(
    kind: ActionKind, task_id: str | None = "T-1", executor: str | None = None, **params: object
) -> Action:
    return Action(kind, "emit", "because", task_id=task_id, executor=executor, params=dict(params))


def task(task_id: str, source: str | None) -> Task:
    return Task(task_id, "t", Status.IN_PROGRESS, NOW, source=source)


def run(
    actions: list[Action],
    adapter: FakeAdapter,
    alerter: FakeAlerter | None = None,
    snapshot: Snapshot | None = None,
) -> None:
    engine = Engine([Emit(actions)], adapters=[adapter], alerter=alerter, config=ACT)
    engine.tick(lambda: snapshot or Snapshot(now=NOW))


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeAdapter(), PlatformAdapter)
    assert not isinstance(FakeAdapter(), SupportsReassign)
    assert isinstance(ReassigningAdapter(), SupportsReassign)
    assert isinstance(StoppingAdapter(), SupportsStop)
    assert not isinstance(FakeAdapter(), SupportsStop)
    assert isinstance(FakeAlerter(), Alerter)
    assert isinstance(FakeCI(), ActionConnector)


@pytest.mark.parametrize(
    ("config", "outcomes", "sent"),
    [
        (EngineConfig(dry_run=False), [Outcome.OBSERVED, Outcome.OBSERVED], []),
        (
            EngineConfig(dry_run=False, default_mode=Mode.SUGGEST),
            [Outcome.SUGGESTED, Outcome.ALERTED],
            ["[emit] suggest requeue T-1", "[emit] alert T-1"],
        ),
        (
            EngineConfig(default_mode=Mode.ACT, dry_run=True),
            [Outcome.DRY_RUN, Outcome.DRY_RUN],
            [],
        ),
    ],
    ids=["default-observe", "suggest", "dry-run-overrides-act"],
)
def test_modes_never_touch_adapter_unless_act(
    config: EngineConfig, outcomes: list[Outcome], sent: list[str]
) -> None:
    adapter, alerter = FakeAdapter(), FakeAlerter()
    engine = Engine(
        [Emit([act(ActionKind.REQUEUE), act(ActionKind.ALERT)])],
        adapters=[adapter],
        alerter=alerter,
        config=config,
    )
    results = engine.tick(lambda: Snapshot(now=NOW))
    assert [r.outcome for r in results] == outcomes
    assert [t for t, _ in alerter.sent] == sent
    assert adapter.calls == []


def test_mode_is_per_rule() -> None:
    adapter = FakeAdapter()
    config = EngineConfig(dry_run=False, modes={"emit": Mode.ACT})
    other = Action(ActionKind.UNBLOCK, "other", "because", task_id="T-1")
    rules = [Emit([act(ActionKind.REQUEUE)]), Emit([other], name="other")]
    results = Engine(rules, adapters=[adapter], config=config).tick(lambda: Snapshot(now=NOW))
    assert [(r.mode, r.outcome) for r in results] == [
        (Mode.ACT, Outcome.EXECUTED),
        (Mode.OBSERVE, Outcome.OBSERVED),
    ]
    assert adapter.calls == [("requeue", "T-1")]


@pytest.mark.parametrize(
    ("kind", "op"),
    [
        (ActionKind.REQUEUE, "requeue"),
        (ActionKind.WAKE, "requeue"),
        (ActionKind.UNBLOCK, "unblock"),
        (ActionKind.COMMENT, "comment"),
        (ActionKind.WIP_COMMIT, "wip_commit"),
    ],
)
def test_task_actions_dispatch_through_adapter(kind: ActionKind, op: str) -> None:
    adapter = FakeAdapter()
    run([act(kind)], adapter)
    assert adapter.calls == [(op, "T-1")]


def test_reassign_uses_capability_when_available() -> None:
    adapter = ReassigningAdapter()
    run([act(ActionKind.REASSIGN, owner_id="agent-2")], adapter)
    assert adapter.calls == [("reassign:agent-2", "T-1")]


@pytest.mark.parametrize(
    ("adapter", "params"),
    [(FakeAdapter(), {"owner_id": "agent-2"}), (ReassigningAdapter(), {})],
    ids=["adapter-cannot-reassign", "no-target-owner"],
)
def test_reassign_falls_back_to_alert(adapter: FakeAdapter, params: dict[str, str]) -> None:
    alerter = FakeAlerter()
    run([act(ActionKind.REASSIGN, **params)], adapter, alerter)
    assert adapter.calls == []
    assert alerter.sent == [("[emit] reassign T-1 (not executed, needs a human)", Severity.WARNING)]


def test_stop_targets_session_even_without_task() -> None:
    adapter = StoppingAdapter()
    run([act(ActionKind.STOP, task_id=None, session_id="S-1")], adapter)
    assert adapter.calls == [("stop", "S-1")]


@pytest.mark.parametrize(
    ("adapter", "params"),
    [(FakeAdapter(), {"session_id": "S-1"}), (StoppingAdapter(), {})],
    ids=["adapter-cannot-stop", "no-session"],
)
def test_stop_falls_back_to_alert(adapter: FakeAdapter, params: dict[str, str]) -> None:
    alerter = FakeAlerter()
    run([act(ActionKind.STOP, **params)], adapter, alerter)
    assert adapter.calls == []
    assert alerter.sent == [("[emit] stop T-1 (not executed, needs a human)", Severity.WARNING)]


WAIT_CONDITIONS = [
    {"id": "C-1", "kind": "task_status", "params": {"task": "T-0", "status": "done"}},
    {"id": "C-bad-kind", "kind": "nope", "params": {}},
    {"id": "C-bad-params", "kind": "pr_merged", "params": "x"},
    "not-a-table",
]


def test_wait_parks_the_task_on_its_well_formed_conditions() -> None:
    adapter = WaitingAdapter()
    assert isinstance(adapter, SupportsWait) and not isinstance(FakeAdapter(), SupportsWait)
    run([act(ActionKind.WAIT, conditions=WAIT_CONDITIONS)], adapter)
    [(tid, conditions)] = adapter.waits
    assert tid == "T-1" and [c.id for c in conditions] == ["C-1"]
    assert conditions[0].kind is ConditionKind.TASK_STATUS and conditions[0].task_id == "T-1"
    assert conditions[0].params == {"task": "T-0", "status": "done"}


@pytest.mark.parametrize(
    ("adapter", "params"),
    [
        (FakeAdapter(), {"conditions": WAIT_CONDITIONS}),
        (WaitingAdapter(), {}),
        (WaitingAdapter(), {"conditions": [{"id": "C", "kind": "nope"}]}),
    ],
    ids=["adapter-cannot-wait", "no-conditions", "only-bad-conditions"],
)
def test_wait_falls_back_to_alert(adapter: FakeAdapter, params: dict[str, object]) -> None:
    alerter = FakeAlerter()
    run([act(ActionKind.WAIT, **params)], adapter, alerter)
    assert adapter.calls == []
    assert alerter.sent == [("[emit] wait T-1 (not executed, needs a human)", Severity.WARNING)]


def test_alert_and_escalate_go_to_alerter() -> None:
    alerter = FakeAlerter()
    run([act(ActionKind.ALERT, task_id=None), act(ActionKind.ESCALATE)], FakeAdapter(), alerter)
    assert [t for t, _ in alerter.sent] == ["[emit] alert", "[emit] escalate T-1"]


def test_alert_without_alerter_is_dropped() -> None:
    run([act(ActionKind.ALERT)], FakeAdapter())


def test_task_action_without_task_id_becomes_alert() -> None:
    adapter, alerter = FakeAdapter(), FakeAlerter()
    run([act(ActionKind.REQUEUE, task_id=None), act(ActionKind.UNBLOCK)], adapter, alerter)
    assert adapter.calls == [("unblock", "T-1")]
    assert alerter.sent == [("[emit] requeue (not executed, needs a human)", Severity.WARNING)]


def test_routes_by_task_source() -> None:
    hermes, other = FakeAdapter(name="hermes"), FakeAdapter(name="other")
    snapshot = Snapshot(now=NOW, tasks=(task("T-1", "other"), task("T-2", "hermes")))
    engine = Engine(
        [Emit([act(ActionKind.REQUEUE, "T-1"), act(ActionKind.UNBLOCK, "T-2")])],
        adapters=[hermes, other],
        config=ACT,
    )
    engine.tick(lambda: snapshot)
    assert other.calls == [("requeue", "T-1")]
    assert hermes.calls == [("unblock", "T-2")]


def test_stop_routes_by_session_task_source() -> None:
    hermes, other = StoppingAdapter(name="hermes"), StoppingAdapter(name="other")
    session = Session("S-9", "T-2", "agent", NOW)
    snapshot = Snapshot(now=NOW, tasks=(task("T-2", "other"),), sessions=(session,))
    engine = Engine(
        [Emit([act(ActionKind.STOP, task_id=None, session_id="S-9")])],
        adapters=[hermes, other],
        config=ACT,
    )
    engine.tick(lambda: snapshot)
    assert other.calls == [("stop", "S-9")]
    assert hermes.calls == []


def test_empty_executor_means_route_by_source() -> None:
    adapter = FakeAdapter()
    run([act(ActionKind.REQUEUE, executor="")], adapter)
    assert adapter.calls == [("requeue", "T-1")]


def test_explicit_executor_routes_to_connector() -> None:
    ci, adapter = FakeCI(), FakeAdapter()
    engine = Engine(
        [Emit([act(ActionKind.CANCEL_BUILD, task_id=None, executor="ci", build_id=42)])],
        adapters=[adapter],
        connectors=[ci],
        config=ACT,
    )
    engine.tick(lambda: Snapshot(now=NOW))
    assert ci.cancelled == ["42"]
    assert adapter.calls == []


@pytest.mark.parametrize(
    "action",
    [
        act(ActionKind.REQUEUE, executor="missing"),
        act(ActionKind.REQUEUE, "T-9"),  # two adapters, task unknown: ambiguous
        act(ActionKind.REQUEUE, executor="ci"),  # connector does not requeue
        act(ActionKind.REQUEUE, "T-5"),  # task source names an unregistered adapter
    ],
    ids=["unknown-executor", "ambiguous", "unsupported-by-connector", "unknown-source"],
)
def test_unroutable_action_becomes_alert(action: Action) -> None:
    a, b, alerter = FakeAdapter(name="a"), FakeAdapter(name="b"), FakeAlerter()
    engine = Engine(
        [Emit([action])], adapters=[a, b], connectors=[FakeCI()], alerter=alerter, config=ACT
    )
    results = engine.tick(lambda: Snapshot(now=NOW, tasks=(task("T-5", "gone"),)))
    assert a.calls == b.calls == []
    assert [d.outcome for d in results] == [Outcome.NOT_EXECUTED]
    assert len(alerter.sent) == 1


def test_duplicate_executor_names_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate executor"):
        Engine([], adapters=[FakeAdapter(name="x")], connectors=[FakeCI(name="x")])


def test_failing_rule_and_action_do_not_stop_the_loop() -> None:
    adapter = FakeAdapter(fail_on="requeue")
    engine = Engine(
        [Broken(), Emit([act(ActionKind.REQUEUE), act(ActionKind.COMMENT)])],
        adapters=[adapter],
        config=ACT,
    )
    results = engine.tick(lambda: Snapshot(now=NOW))
    assert [r.outcome for r in results] == [Outcome.FAILED, Outcome.EXECUTED]
    assert engine.rule_errors == {"broken": 1}
    assert adapter.calls == [("comment", "T-1")]


@pytest.mark.parametrize(
    ("config", "action", "outcome"),
    [
        (
            EngineConfig(dry_run=False, default_mode=Mode.SUGGEST),
            act(ActionKind.REQUEUE),
            Outcome.DROPPED,
        ),
        (ACT, act(ActionKind.ALERT), Outcome.DROPPED),
        (ACT, act(ActionKind.STOP), Outcome.DROPPED),  # not executable, nobody to tell
    ],
    ids=["suggest", "alert", "fallback"],
)
def test_undelivered_alerts_are_dropped_not_alerted(
    config: EngineConfig, action: Action, outcome: Outcome
) -> None:
    engine = Engine([Emit([action])], adapters=[FakeAdapter()], config=config)
    assert [d.outcome for d in engine.tick(lambda: Snapshot(now=NOW))] == [outcome]


def test_dry_run_records_effective_mode() -> None:
    config = EngineConfig(default_mode=Mode.ACT, dry_run=True)
    engine = Engine([Emit([act(ActionKind.REQUEUE)])], adapters=[FakeAdapter()], config=config)
    [result] = engine.tick(lambda: Snapshot(now=NOW))
    assert (result.mode, result.outcome) == (Mode.OBSERVE, Outcome.DRY_RUN)


def test_naive_now_rejected() -> None:
    engine = Engine([], adapters=[FakeAdapter()])
    with pytest.raises(ValueError, match="timezone"):
        engine.tick(lambda: Snapshot(now=datetime(2026, 3, 2, 10)))


class RaisingAlerter:
    def send(self, title: str, body: str, severity: str) -> None:
        raise ConnectionError("slack down")


def test_alerter_failure_is_a_failed_action_not_a_crash() -> None:
    engine = Engine(
        [Emit([act(ActionKind.ALERT)])],
        adapters=[FakeAdapter()],
        alerter=RaisingAlerter(),
        config=ACT,
    )
    assert [d.outcome for d in engine.tick(lambda: Snapshot(now=NOW))] == [Outcome.FAILED]


def _ticks(engine: Engine, adapter: FakeAdapter, fail: list[bool]) -> list[Outcome]:
    outcomes = []
    for i, failing in enumerate(fail):
        adapter.fail_on = "requeue" if failing else None
        now = NOW + timedelta(minutes=10 * i)  # past retry_after, inside the cooldown
        [d] = engine.tick(lambda now=now: Snapshot(now=now))
        outcomes.append(d.outcome)
    return outcomes


def test_once_action_alerts_only_when_last_attempt_fails(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter, alerter = FakeAdapter(), FakeAlerter()
    action = replace(act(ActionKind.REQUEUE), once=True)
    engine = Engine([Emit([action])], adapters=[adapter], alerter=alerter, config=ACT)
    with caplog.at_level(logging.INFO):
        outcomes = _ticks(engine, adapter, [True, True, False])
    assert outcomes == [Outcome.FAILED, Outcome.FAILED, Outcome.EXECUTED]
    assert alerter.sent == []
    assert not any(r.exc_info for r in caplog.records)  # no traceback above DEBUG


def test_once_action_failing_every_attempt_alerts_exactly_once() -> None:
    adapter, alerter = FakeAdapter(), FakeAlerter()
    action = replace(act(ActionKind.REQUEUE), once=True)
    engine = Engine([Emit([action])], adapters=[adapter], alerter=alerter, config=ACT)
    outcomes = _ticks(engine, adapter, [True, True, True, True])
    assert outcomes[:3] == [Outcome.FAILED] * 3
    assert outcomes[3] is Outcome.SUPPRESSED  # attempts used up
    assert [t for t, _ in alerter.sent] == [
        "[emit] failed requeue T-1 (not executed, needs a human)"
    ]


def test_repeated_failure_of_a_plain_action_alerts_once_per_cooldown() -> None:
    adapter, alerter = FakeAdapter(), FakeAlerter()
    engine = Engine(
        [Emit([act(ActionKind.REQUEUE)])], adapters=[adapter], alerter=alerter, config=ACT
    )
    assert _ticks(engine, adapter, [True, True, True]) == [Outcome.FAILED] * 3
    assert len(alerter.sent) == 1


class _Unsupported(FakeAdapter):
    def requeue(self, task_id: str, reason: str) -> None:
        raise UnsupportedAction("this platform cannot requeue")

    def wip_commit(
        self, task_id: str, message: str, *, branch: str | None = None, draft_pr: bool = False
    ) -> None:
        raise NotImplementedError("not written yet")  # not UnsupportedAction: a failure

    def stop(self, session_id: str, reason: str) -> None:
        raise UnsupportedAction("cannot stop this kind of session")


def test_unsupported_action_falls_back_to_an_alert(caplog: pytest.LogCaptureFixture) -> None:
    adapter, alerter = _Unsupported(), FakeAlerter()
    engine = Engine(
        [
            Emit(
                [
                    act(ActionKind.REQUEUE),
                    act(ActionKind.COMMENT),
                    act(ActionKind.STOP, session_id="s-1"),
                    act(ActionKind.WIP_COMMIT),
                ]
            )
        ],
        adapters=[adapter],
        alerter=alerter,
        config=ACT,
    )
    results = engine.tick(lambda: Snapshot(now=NOW))
    assert [r.outcome for r in results] == [
        Outcome.NOT_EXECUTED,
        Outcome.EXECUTED,
        Outcome.NOT_EXECUTED,
        Outcome.FAILED,  # a plain NotImplementedError is a failure, retried later
    ]
    assert adapter.calls == [("comment", "T-1")]
    assert [title for title, _ in alerter.sent][:2] == [
        "[emit] requeue T-1 (not executed, needs a human)",
        "[emit] stop T-1 (not executed, needs a human)",
    ]
    assert len(alerter.sent) == 3  # the failed wip_commit is reported too
    assert "fake does not support requeue: this platform cannot requeue" in caplog.text
    # Within the cooldown an unsupported action is held; a failure is retried (retry_after).
    later = engine.tick(lambda: Snapshot(now=NOW + timedelta(minutes=10)))
    assert [r.outcome for r in later] == [
        Outcome.SUPPRESSED,
        Outcome.SUPPRESSED,
        Outcome.SUPPRESSED,
        Outcome.FAILED,
    ]


@pytest.mark.parametrize(
    ("severity", "sent"),
    [
        (Severity.INFO, Severity.WARNING),
        (Severity.WARNING, Severity.WARNING),
        (Severity.CRITICAL, Severity.CRITICAL),
    ],
)
def test_a_failed_action_needs_a_human_at_warning_or_above(
    severity: Severity, sent: Severity
) -> None:
    """A failed stop of a runaway (the stop action itself is info) must reach channels that
    skip info; its own severity is kept when it is higher."""
    adapter, alerter = FakeAdapter(fail_on="requeue"), FakeAlerter()
    action = replace(act(ActionKind.REQUEUE), severity=severity)
    engine = Engine([Emit([action])], adapters=[adapter], alerter=alerter, config=ACT)
    assert [d.outcome for d in engine.tick(lambda: Snapshot(now=NOW))] == [Outcome.FAILED]
    assert alerter.sent == [("[emit] failed requeue T-1 (not executed, needs a human)", sent)]


def test_an_action_nothing_can_carry_out_needs_a_human_at_warning() -> None:
    alerter = FakeAlerter()
    engine = Engine(
        [Emit([act(ActionKind.REQUEUE, executor="missing")])],
        adapters=[FakeAdapter()],
        alerter=alerter,
        config=ACT,
    )
    engine.tick(lambda: Snapshot(now=NOW))
    assert alerter.sent == [("[emit] requeue T-1 (not executed, needs a human)", Severity.WARNING)]


def test_a_suggestion_keeps_its_own_severity() -> None:
    alerter = FakeAlerter()
    config = EngineConfig(dry_run=False, default_mode=Mode.SUGGEST)
    engine = Engine(
        [Emit([act(ActionKind.REQUEUE)])], adapters=[FakeAdapter()], alerter=alerter, config=config
    )
    engine.tick(lambda: Snapshot(now=NOW))
    assert alerter.sent == [("[emit] suggest requeue T-1", Severity.INFO)]
