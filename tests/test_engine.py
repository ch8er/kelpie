from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.engine import Engine
from kelpie.core.model import Snapshot, Task
from kelpie.core.ports import (
    Alerter,
    PlatformAdapter,
    PlatformEvent,
    SupportsPause,
    SupportsReassign,
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
class PausingAdapter(FakeAdapter):
    def pause(self, task_id: str, reason: str) -> None:
        self._record("pause", task_id)


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


def act(kind: ActionKind, task_id: str | None = "T-1", **params: object) -> Action:
    return Action(kind, "emit", "because", task_id=task_id, params=dict(params))


def run(actions: list[Action], adapter: FakeAdapter, alerter: FakeAlerter | None = None) -> None:
    Engine([Emit(actions)], adapter, alerter, dry_run=False).tick(lambda: Snapshot(now=NOW))


def test_fakes_satisfy_protocols() -> None:
    assert isinstance(FakeAdapter(), PlatformAdapter)
    assert not isinstance(FakeAdapter(), SupportsReassign)
    assert isinstance(ReassigningAdapter(), SupportsReassign)
    assert isinstance(PausingAdapter(), SupportsPause)
    assert not isinstance(FakeAdapter(), SupportsPause)
    assert isinstance(FakeAlerter(), Alerter)


def test_dry_run_does_not_touch_adapter() -> None:
    adapter = FakeAdapter()
    actions = Engine([Emit([act(ActionKind.REQUEUE)])], adapter).tick(lambda: Snapshot(now=NOW))
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
    assert alerter.sent == [("[emit] reassign T-1", Severity.INFO)]


def test_pause_uses_capability_when_available() -> None:
    adapter = PausingAdapter()
    run([act(ActionKind.PAUSE)], adapter)
    assert adapter.calls == [("pause", "T-1")]


def test_pause_falls_back_to_alert() -> None:
    adapter, alerter = FakeAdapter(), FakeAlerter()
    run([act(ActionKind.PAUSE)], adapter, alerter)
    assert adapter.calls == []
    assert alerter.sent == [("[emit] pause T-1", Severity.INFO)]


def test_alert_and_escalate_go_to_alerter() -> None:
    alerter = FakeAlerter()
    run([act(ActionKind.ALERT, task_id=None), act(ActionKind.ESCALATE)], FakeAdapter(), alerter)
    assert [t for t, _ in alerter.sent] == ["[emit] alert", "[emit] escalate T-1"]


def test_alert_without_alerter_is_dropped() -> None:
    run([act(ActionKind.ALERT)], FakeAdapter())


def test_task_action_without_task_id_is_skipped() -> None:
    adapter = FakeAdapter()
    run([act(ActionKind.REQUEUE, task_id=None), act(ActionKind.UNBLOCK)], adapter)
    assert adapter.calls == [("unblock", "T-1")]


def test_failing_rule_and_action_do_not_stop_the_loop() -> None:
    adapter = FakeAdapter(fail_on="requeue")
    engine = Engine(
        [Broken(), Emit([act(ActionKind.REQUEUE), act(ActionKind.COMMENT)])],
        adapter,
        dry_run=False,
    )
    actions = engine.tick(lambda: Snapshot(now=NOW))
    assert len(actions) == 2
    assert adapter.calls == [("comment", "T-1")]
