from datetime import UTC, datetime

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.model import Session, Snapshot, Status, Task
from kelpie.core.ports import (
    ActionConnector,
    Alerter,
    PlatformAdapter,
    SupportsReassign,
    SupportsStop,
)
from tests.fakes import (
    Broken,
    Emit,
    FakeAdapter,
    FakeAlerter,
    FakeCI,
    ReassigningAdapter,
    StoppingAdapter,
)

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
ACT = EngineConfig(default_mode=Mode.ACT)


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
        (EngineConfig(), [Outcome.OBSERVED, Outcome.OBSERVED], []),
        (
            EngineConfig(default_mode=Mode.SUGGEST),
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
    config = EngineConfig(modes={"emit": Mode.ACT})
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
    assert alerter.sent == [("[emit] reassign T-1 (not executed, needs a human)", Severity.INFO)]


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
    assert alerter.sent == [("[emit] stop T-1 (not executed, needs a human)", Severity.INFO)]


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
    assert alerter.sent == [("[emit] requeue (not executed, needs a human)", Severity.INFO)]


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
    assert adapter.calls == [("comment", "T-1")]


@pytest.mark.parametrize(
    ("config", "action", "outcome"),
    [
        (EngineConfig(default_mode=Mode.SUGGEST), act(ActionKind.REQUEUE), Outcome.DROPPED),
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
