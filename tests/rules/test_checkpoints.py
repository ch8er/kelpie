from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import ActionLedger, Dispatched, MemoryLedgerStore, Outcome
from kelpie.core.model import Session, Snapshot, Status, Task
from kelpie.rules import PLANNED, REGISTRY
from kelpie.rules.checkpoints import Checkpoints, build
from tests.fakes import FakeAdapter

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)


def task(status: Status = Status.IN_PROGRESS, since: datetime = NOW - 120 * M) -> Task:
    return Task("T-1", "Do the thing", status, since)


def live(**kw: object) -> Session:
    base = {"started_at": NOW - 90 * M, "last_heartbeat": NOW - M, "dirty_worktree": True}
    return Session("S-1", "T-1", "dev", **{**base, **kw})  # type: ignore[arg-type]


def run(t: Task, *sessions: Session, rule: Checkpoints | None = None) -> list[Action]:
    return list((rule or Checkpoints()).evaluate(Snapshot(now=NOW, tasks=(t,), sessions=sessions)))


def test_registered_and_first_in_plan_order() -> None:
    assert isinstance(REGISTRY["checkpoints"]({}), Checkpoints)
    assert PLANNED[0].name == "checkpoints"


def test_reminder_boundaries() -> None:
    rule = Checkpoints(commit_every=30 * M, max_changed_files=20)
    assert run(task(), live(last_commit_at=NOW - 29 * M), rule=rule) == []
    [a] = run(task(), live(last_commit_at=NOW - 30 * M), rule=rule)
    assert a.kind is ActionKind.COMMENT and "no commit for 30 minutes" in a.reason
    assert run(task(), live(last_commit_at=NOW - M, changed_files=20), rule=rule) == []
    [a] = run(task(), live(last_commit_at=NOW - M, changed_files=21), rule=rule)
    assert "21 changed files" in a.reason
    # Never committed: counts from the session start.
    [a] = run(task(), live(), rule=rule)
    assert "no commit for 90 minutes" in a.reason
    # HEAD is the base commit from days ago: still counts from the session start.
    young = live(started_at=NOW - 5 * M, last_commit_at=NOW - 3 * 24 * 60 * M)
    assert run(task(), young, rule=rule) == []


def test_clean_worktree_gets_no_reminder() -> None:
    assert run(task(), live(dirty_worktree=False, changed_files=0)) == []
    [a] = run(task(), live(dirty_worktree=False, changed_files=3))  # adapter counts files only
    assert a.kind is ActionKind.COMMENT


def kinds(actions: list[Action]) -> list[ActionKind]:
    return [a.kind for a in actions]


def test_wip_commit_on_ended_or_silent_dirty_session() -> None:
    ended = live(ended_at=NOW - 10 * M, branch="b", summary="half done")
    [a, resume] = run(task(), ended)
    assert a.kind is ActionKind.WIP_COMMIT and a.once
    assert a.reason.startswith("WIP: Do the thing [skip ci]")
    assert "half done" in a.reason
    assert a.params == {"session_id": "S-1", "branch": "b", "skip_ci": True, "draft_pr": False}
    # The same tick tells the next session where to resume (rule 1 requeues the task).
    assert resume.kind is ActionKind.COMMENT and resume.once
    assert "Resume from the WIP commit Kelpie saved on b" in resume.reason
    assert "half done" in resume.reason and resume.params["commit"] is None
    silent = live(last_heartbeat=NOW - 45 * M)
    assert kinds(run(task(), silent)) == [ActionKind.WIP_COMMIT, ActionKind.COMMENT]


def test_wip_commit_only_for_the_last_exit_with_nobody_live() -> None:
    old = live(ended_at=NOW - 60 * M)
    # A new session is working in the same worktree: committing would grab its work.
    working = Session("S-2", "T-1", "dev", NOW - 30 * M, last_heartbeat=NOW - M)
    assert run(task(), old, working) == []
    # A later clean exit: the earlier dirty flag is history, the worktree is clean now.
    clean = Session("S-2", "T-1", "dev", NOW - 30 * M, ended_at=NOW - 5 * M)
    assert ActionKind.WIP_COMMIT not in kinds(run(task(), old, clean))


def test_wip_commit_and_resume_stop_after_act_within() -> None:
    rule = Checkpoints(act_within=24 * 60 * M)
    started = task(since=NOW - 25 * 60 * M)
    assert kinds(run(started, live(ended_at=NOW - 23 * 60 * M), rule=rule)) == [
        ActionKind.WIP_COMMIT,
        ActionKind.COMMENT,
    ]
    assert run(started, live(ended_at=NOW - 24 * 60 * M), rule=rule) == []


def test_declared_end_gets_wip_commit_but_no_resume_note() -> None:
    ended = live(ended_at=NOW - 10 * M, final_status=Status.BLOCKED)
    blocked = task(Status.BLOCKED, since=NOW - 10 * M)
    assert kinds(run(blocked, ended)) == [ActionKind.WIP_COMMIT]


def test_draft_pr_mode_and_note_limit() -> None:
    rule = Checkpoints(wip_ci="draft_pr")
    [a, _] = run(task(), live(ended_at=NOW, summary="x" * 900), rule=rule)
    assert "[skip ci]" not in a.reason and a.params["draft_pr"] is True
    assert a.reason.endswith("…") and len(a.reason) < 800


def test_terminal_tasks_and_unknown_tasks_are_skipped() -> None:
    assert run(task(Status.DONE), live(ended_at=NOW)) == []
    orphan_session = Session("S-9", "T-9", "dev", NOW - 60 * M, ended_at=NOW, dirty_worktree=True)
    assert run(task(), orphan_session) == []


def test_resume_note_from_the_last_commit() -> None:
    previous = Session(
        "S-1", "T-1", "dev", NOW - 180 * M, ended_at=NOW - 60 * M,
        last_commit="abc1234", last_commit_at=NOW - 70 * M, branch="task/T-1",
    )  # fmt: skip
    queued = task(Status.READY, since=NOW - 50 * M)
    [a] = run(queued, previous)
    assert a.kind is ActionKind.COMMENT and a.once
    assert "Resume from commit abc1234 on task/T-1" in a.reason and "git log" in a.reason
    assert a.key == "resume:T-1:S-1"
    # Requeued while the session was still winding down (a platform reclaim) still counts.
    assert len(run(task(Status.READY, since=NOW - 90 * M), previous)) == 1
    # Queued before the session even started: not sent back by it.
    assert run(task(Status.READY, since=NOW - 200 * M), previous) == []
    # A live session on it: nothing to resume yet.
    working = Session("S-2", "T-1", "dev", NOW - 5 * M, last_heartbeat=NOW - M)
    assert run(queued, previous, working) == []
    # Rule 1 is about to requeue an undeclared exit: the note comes in the same tick.
    [a] = run(task(Status.IN_PROGRESS, since=NOW - 180 * M), previous)
    assert "abc1234" in a.reason
    # HEAD is only the base commit the session started from: nothing to resume from.
    base = Session(
        "S-1", "T-1", "dev", NOW - 180 * M, ended_at=NOW - 60 * M,
        last_commit="0ld0ba5", last_commit_at=NOW - 3 * 24 * 60 * M,
    )  # fmt: skip
    assert run(queued, base) == []
    # Nothing committed and a clean worktree: nothing to resume from.
    bare = Session("S-1", "T-1", "dev", NOW - 180 * M, ended_at=NOW - 60 * M)
    assert run(queued, bare) == []


def test_once_actions_are_not_repeated_after_the_cooldown() -> None:
    store = MemoryLedgerStore()
    ledger = ActionLedger(store, cooldown=30 * M)
    comment = Action(ActionKind.COMMENT, "checkpoints", "resume", task_id="T-1", key="k", once=True)
    ledger.record(Dispatched(comment, Mode.ACT, Outcome.EXECUTED), NOW)
    assert ledger.check(comment, Mode.ACT, NOW + 6 * 60 * M) is not None
    assert ledger.check(comment, Mode.ACT, NOW + 8 * 24 * 60 * M) is None  # once_window: 7d
    # Done in observe does not count as done in act.
    assert ledger.check(comment, Mode.OBSERVE, NOW + 60 * M) is None


def test_once_failures_are_retried_a_few_times_then_given_up() -> None:
    ledger = ActionLedger(MemoryLedgerStore(), once_attempts=3)
    failed = Action(ActionKind.WIP_COMMIT, "checkpoints", "x", task_id="T-2", key="f", once=True)
    for i in range(3):
        at = NOW + i * 10 * M
        assert ledger.check(failed, Mode.ACT, at) is None
        ledger.record(Dispatched(failed, Mode.ACT, Outcome.FAILED), at)
    assert ledger.check(failed, Mode.ACT, NOW + 6 * 60 * M) is not None


def test_once_drop_is_retried() -> None:
    ledger = ActionLedger(MemoryLedgerStore(), cooldown=30 * M)
    dropped = Action(ActionKind.WIP_COMMIT, "checkpoints", "x", task_id="T-3", key="d", once=True)
    ledger.record(Dispatched(dropped, Mode.ACT, Outcome.DROPPED), NOW)
    assert ledger.check(dropped, Mode.ACT, NOW + 31 * M) is None


def test_wip_commit_runs_before_the_requeue_with_its_params() -> None:
    ended = Session(
        "S-1", "T-1", "dev", NOW - 90 * M, ended_at=NOW - 30 * M, dirty_worktree=True,
        branch="task/T-1",
    )  # fmt: skip
    rules = [REGISTRY[p.name]({"wip_ci": "draft_pr"} if p.name == "checkpoints" else {})
             for p in PLANNED if p.name in REGISTRY]  # fmt: skip
    adapter = FakeAdapter()
    engine = Engine(
        rules, adapters=[adapter], config=EngineConfig(dry_run=False, default_mode=Mode.ACT)
    )
    engine.tick(lambda: Snapshot(now=NOW, tasks=(task(),), sessions=(ended,)))
    ops = [op for op, _ in adapter.calls]
    assert ops == ["wip_commit", "comment", "requeue"]
    [(tid, message, branch, draft_pr)] = adapter.wip_commits
    assert (tid, branch, draft_pr) == ("T-1", "task/T-1", True)
    assert message.startswith("WIP: Do the thing")


@pytest.mark.parametrize(
    "config",
    [
        {"commit_every": "0s"},
        {"commit_every": [1]},
        {"max_changed_files": 0},
        {"act_within": "-1h"},
        {"wip_ci": "push"},
        {"every": "1m"},
    ],
)
def test_bad_config(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build(config)


def test_build() -> None:
    rule = build({"commit_every": "45m", "max_changed_files": 10, "wip_ci": "draft_pr"})
    assert (rule.commit_every, rule.max_changed_files, rule.wip_ci) == (45 * M, 10, "draft_pr")
