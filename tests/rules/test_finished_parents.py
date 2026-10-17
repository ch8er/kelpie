"""A parent whose subtasks are all finished is closed (rule 3), never resumed or requeued
(rules 1 and 5; incident 17)."""

from datetime import UTC, datetime, timedelta

from kelpie.core.actions import Action, ActionKind
from kelpie.core.executors import PlatformExecutor
from kelpie.core.model import Session, Snapshot, Status, Task
from kelpie.rules.checkpoints import Checkpoints
from kelpie.rules.exit_contract import ExitContract
from tests.fakes import FakeAdapter

NOW = datetime(2026, 3, 2, 16, tzinfo=UTC)
H = timedelta(hours=1)


def board(subtasks: tuple[str, ...], ended: timedelta = 2 * H) -> Snapshot:
    parent = Task("P", "Epic", Status.IN_PROGRESS, NOW - 5 * H, "dev", subtasks=subtasks)
    kids = tuple(Task(s, s, Status.DONE, NOW - 3 * H, "dev") for s in ("S1", "S2"))
    # The parent's last run ended without a status, with uncommitted work left behind.
    run = Session(
        "run-1",
        "P",
        "dev-1",
        NOW - 4 * H,
        ended_at=NOW - ended,
        dirty_worktree=True,
        last_commit="5441b6e",
        branch="feature/P",
    )
    return Snapshot(
        now=NOW,
        tasks=(parent, *kids),
        sessions=(run,),
        pull_requests_complete=True,
        pull_requests_at=NOW,
    )


def test_rules_1_and_5_never_resume_a_finished_parent() -> None:
    assert ExitContract().evaluate(board(("S1", "S2"))) == []
    # The uncommitted work is still saved; only the resume note is left out.
    # Five minutes after the exit, the PR lists read since.
    found = Checkpoints().evaluate(board(("S1", "S2"), ended=H / 12))
    assert [a.kind for a in found] == [ActionKind.WIP_COMMIT]


def test_without_subtasks_they_still_act() -> None:
    assert [a.kind for a in ExitContract().evaluate(board(()))] == [ActionKind.REQUEUE]
    assert Checkpoints().evaluate(board(()))


class Closer(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.closed: list[str] = []

    def close(self, task_id: str, reason: str) -> None:
        self.closed.append(task_id)


def test_close_goes_to_an_adapter_that_supports_it() -> None:
    action = Action(ActionKind.CLOSE, "orphan_status", "close P", task_id="P")
    closer = Closer()
    assert PlatformExecutor(closer).execute(action) is True and closer.closed == ["P"]
    assert PlatformExecutor(FakeAdapter()).execute(action) is False  # a human is told
