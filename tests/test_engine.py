from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

from kelpie.core.actions import Action, ActionKind
from kelpie.core.engine import Engine
from kelpie.core.model import Snapshot, Task
from kelpie.core.ports import PlatformAdapter, PlatformEvent

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)


@dataclass
class FakeAdapter:
    name: str = "fake"
    calls: list[tuple[str, str]] = field(default_factory=list)

    def list_tasks(self) -> Sequence[Task]:
        return []

    def get_task(self, task_id: str) -> Task | None:
        return None

    def requeue(self, task_id: str, reason: str) -> None:
        self.calls.append(("requeue", task_id))

    def unblock(self, task_id: str, reason: str) -> None:
        self.calls.append(("unblock", task_id))

    def comment(self, task_id: str, body: str) -> None:
        self.calls.append(("comment", task_id))

    def wip_commit(self, task_id: str, message: str) -> None:
        self.calls.append(("wip_commit", task_id))

    def events(self, since: datetime) -> Iterable[PlatformEvent]:
        return []


@dataclass
class AlwaysRequeue:
    name: str = "always_requeue"

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        return [Action(ActionKind.REQUEUE, self.name, "test", task_id="T-1")]


def test_fake_adapter_satisfies_protocol() -> None:
    assert isinstance(FakeAdapter(), PlatformAdapter)


def test_dry_run_does_not_touch_adapter() -> None:
    adapter = FakeAdapter()
    actions = Engine([AlwaysRequeue()], adapter).tick(lambda: Snapshot(now=NOW))
    assert [a.kind for a in actions] == [ActionKind.REQUEUE]
    assert adapter.calls == []


def test_live_run_dispatches_through_adapter() -> None:
    adapter = FakeAdapter()
    Engine([AlwaysRequeue()], adapter, dry_run=False).tick(lambda: Snapshot(now=NOW))
    assert adapter.calls == [("requeue", "T-1")]
