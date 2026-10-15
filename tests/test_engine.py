from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.engine import Engine
from kelpie.core.model import Snapshot, Status, Task
from kelpie.core.ports import (
    ActionConnector,
    Alerter,
    PlatformAdapter,
    PlatformEvent,
    SupportsReassign,
    SupportsStop,
)

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)


@dataclass
class FakeAdapter:
    name: str = "fake"
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_on: str | None = None

    def _record(self, op: str, task_id: str) -> None:
        if op == self.fail_on:
            raise RuntimeError(f"{op} failed")
        self.calls.append((op, task_id))

    def list_tasks(self) -> Sequence[Task]:
        return []

    def get_task(self, task_id: str) -> Task | None:
        return None

    def requeue(self, task_id: str, reason: str) -> None:
        self._record("requeue", task_id)

    def unblock(self, task_id: str, reason: str) -> None:
        self._record("unblock", task_id)

    def comment(self, task_id: str, body: str) -> None:
        self._record("comment", task_id)

    def wip_commit(self, task_id: str, message: str) -> None:
        self._record("wip_commit", task_id)

    def events(self, since: datetime) -> Iterable[PlatformEvent]:
        return []


@dataclass
class ReassigningAdapter(FakeAdapter):
    def reassign(self, task_id: str, owner_id: str, reason: str) -> None:
        self._record(f"reassign:{owner_id}", task_id)


@dataclass
class StoppingAdapter(FakeAdapter):
    def stop(self, session_id: str, reason: str) -> None:
        self._record("stop", session_id)


@dataclass
class FakeAlerter:
    sent: list[tuple[str, str]] = field(default_factory=list)

    def send(self, title: str, body: str, severity: str) -> None:
        self.sent.append((title, severity))


@dataclass
class Emit:
    actions: list[Action]
    name: str = "emit"

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        return self.actions


@dataclass
class Broken:
    name: str = "broken"

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        raise ValueError("boom")


@dataclass
class FakeCI:
    name: str = "ci"
    actions: frozenset[ActionKind] = frozenset({ActionKind.CANCEL_BUILD})
    cancelled: list[str] = field(default_factory=list)

    def execute(self, action: Action) -> None:
        self.cancelled.append(str(action.params["build_id"]))


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
    engine = Engine([Emit(actions)], adapters=[adapter], alerter=alerter, dry_run=False)
    engine.tick(lambda: snapshot or Snapshot(now=NOW))


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeAdapter(), PlatformAdapter)
    assert not isinstance(FakeAdapter(), SupportsReassign)
    assert isinstance(ReassigningAdapter(), SupportsReassign)
    assert isinstance(StoppingAdapter(), SupportsStop)
    assert not isinstance(FakeAdapter(), SupportsStop)
    assert isinstance(FakeAlerter(), Alerter)
    assert isinstance(FakeCI(), ActionConnector)


def test_dry_run_does_not_touch_adapter() -> None:
    adapter = FakeAdapter()
    engine = Engine([Emit([act(ActionKind.REQUEUE)])], adapters=[adapter])
    actions = engine.tick(lambda: Snapshot(now=NOW))
    assert [a.kind for a in actions] == [ActionKind.REQUEUE]
    assert adapter.calls == []


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
        dry_run=False,
    )
    engine.tick(lambda: snapshot)
    assert other.calls == [("requeue", "T-1")]
    assert hermes.calls == [("unblock", "T-2")]


def test_explicit_executor_routes_to_connector() -> None:
    ci, adapter = FakeCI(), FakeAdapter()
    engine = Engine(
        [Emit([act(ActionKind.CANCEL_BUILD, task_id=None, executor="ci", build_id=42)])],
        adapters=[adapter],
        connectors=[ci],
        dry_run=False,
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
    ],
    ids=["unknown-executor", "ambiguous", "unsupported-by-connector"],
)
def test_unroutable_action_becomes_alert(action: Action) -> None:
    a, b, alerter = FakeAdapter(name="a"), FakeAdapter(name="b"), FakeAlerter()
    engine = Engine(
        [Emit([action])], adapters=[a, b], connectors=[FakeCI()], alerter=alerter, dry_run=False
    )
    engine.tick(lambda: Snapshot(now=NOW))
    assert a.calls == b.calls == []
    assert len(alerter.sent) == 1


def test_duplicate_executor_names_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate executor"):
        Engine([], adapters=[FakeAdapter(name="x")], connectors=[FakeCI(name="x")])


def test_failing_rule_and_action_do_not_stop_the_loop() -> None:
    adapter = FakeAdapter(fail_on="requeue")
    engine = Engine(
        [Broken(), Emit([act(ActionKind.REQUEUE), act(ActionKind.COMMENT)])],
        adapters=[adapter],
        dry_run=False,
    )
    actions = engine.tick(lambda: Snapshot(now=NOW))
    assert len(actions) == 2
    assert adapter.calls == [("comment", "T-1")]
