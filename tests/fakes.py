"""Test doubles shared by engine and ledger tests."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import Condition, Snapshot, Task
from kelpie.core.ports import PlatformEvent


@dataclass
class FakeAdapter:
    name: str = "fake"
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail_on: str | None = None
    wip_commits: list[tuple[str, str, str | None, bool]] = field(default_factory=list)

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

    def wip_commit(
        self, task_id: str, message: str, *, branch: str | None = None, draft_pr: bool = False
    ) -> None:
        self._record("wip_commit", task_id)
        self.wip_commits.append((task_id, message, branch, draft_pr))

    def events(self, since: datetime) -> Iterable[PlatformEvent]:
        return []


@dataclass
class WaitingAdapter(FakeAdapter):
    waits: list[tuple[str, tuple[Condition, ...]]] = field(default_factory=list)

    def wait(self, task_id: str, conditions: Sequence[Condition], reason: str) -> None:
        self._record("wait", task_id)
        self.waits.append((task_id, tuple(conditions)))


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
