from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.model import Block, Session, Snapshot, Status, Task
from kelpie.rules import REGISTRY
from kelpie.rules.exit_contract import ExitContract, build

NOW = datetime(2026, 3, 2, 16, tzinfo=UTC)
H = timedelta(hours=1)


def task(status: Status = Status.IN_PROGRESS, since: datetime = NOW - 2 * H, **kw: object) -> Task:
    return Task("T-1", "t", status, since, **kw)  # type: ignore[arg-type]


def session(sid: str = "S-1", ended: datetime | None = NOW - H, **kw: object) -> Session:
    return Session(sid, "T-1", "agent-1", NOW - 3 * H, ended_at=ended, **kw)  # type: ignore[arg-type]


def run(t: Task, *sessions: Session, rule: ExitContract | None = None) -> list[str]:
    snap = Snapshot(now=NOW, tasks=(t,), sessions=sessions)
    return [a.kind for a in (rule or ExitContract()).evaluate(snap)]


def test_registered() -> None:
    assert isinstance(REGISTRY["exit_contract"]({}), ExitContract)


def test_in_progress_task_without_final_status_is_requeued() -> None:
    assert run(task(), session()) == [ActionKind.REQUEUE]


@pytest.mark.parametrize("status", [Status.REVIEW, Status.ACCEPTED])
def test_hand_off_during_the_session_is_a_declared_end(status: Status) -> None:
    # Developer moved the task to review mid-session, then exited without final_status.
    assert run(task(status, since=NOW - 2 * H), session()) == []
    # Stamped at the same moment as the exit (one end hook): still the session's hand-off.
    assert run(task(status, since=NOW - H), session()) == []


@pytest.mark.parametrize("status", [Status.REVIEW, Status.ACCEPTED])
def test_reviewer_leaving_without_verdict_is_requeued(status: Status) -> None:
    # The task was already in review when the reviewer's session started.
    assert run(task(status, since=NOW - 4 * H), session()) == [ActionKind.REQUEUE]


@pytest.mark.parametrize(
    "status", [Status.DONE, Status.READY, Status.BACKLOG, Status.FAILED, Status.CANCELLED]
)
def test_other_statuses_are_ignored(status: Status) -> None:
    assert run(task(status), session()) == []


def test_no_sessions_no_action() -> None:
    assert run(task()) == []


def test_final_status_recorded_is_fine() -> None:
    assert run(task(), session(final_status=Status.DONE)) == []


def test_grace_period_boundary() -> None:
    rule = ExitContract(session_exit_grace=timedelta(minutes=10))
    assert run(task(), session(ended=NOW - timedelta(minutes=9)), rule=rule) == []
    assert run(task(), session(ended=NOW - timedelta(minutes=10)), rule=rule) == [
        ActionKind.REQUEUE
    ]


def test_a_live_session_means_someone_is_working() -> None:
    live = session("S-2", ended=None, last_heartbeat=NOW - timedelta(minutes=5))
    assert run(task(), session(), live) == []


def test_a_silent_open_session_counts_as_ended() -> None:
    # Crashed yesterday without an end hook, plus today's exit without a status.
    crashed = session("S-0", ended=None, last_heartbeat=NOW - 24 * H)
    assert run(task(), crashed, session()) == [ActionKind.REQUEUE]
    # The crashed session alone is the last one: requeued as gone silent.
    rule = ExitContract(session_stale_after=2 * H)
    snap = Snapshot(now=NOW, tasks=(task(since=NOW - 30 * H),), sessions=(crashed,))
    [action] = rule.evaluate(snap)
    assert "stopped sending heartbeats" in action.reason


def test_stale_boundary_uses_heartbeat_or_start() -> None:
    rule = ExitContract(session_stale_after=2 * H)
    fresh = session("S-2", ended=None, last_heartbeat=NOW - 119 * timedelta(minutes=1))
    assert run(task(), fresh, rule=rule) == []
    never_beat = Session("S-3", "T-1", "a", NOW - 2 * H)  # started 2h ago, no heartbeat
    assert run(task(since=NOW - 3 * H), never_beat, rule=rule) == [ActionKind.REQUEUE]


def test_only_the_last_session_counts() -> None:
    older = session("S-0", ended=NOW - 2 * H)
    assert run(task(), older, session(final_status=Status.REVIEW)) == []
    assert run(task(), session("S-0", ended=NOW - 2 * H, final_status=Status.DONE), session()) == [
        ActionKind.REQUEUE
    ]


def test_status_changed_after_the_exit_is_respected() -> None:
    assert run(task(since=NOW - timedelta(minutes=30)), session()) == []


def test_reason_reports_utc() -> None:
    from datetime import timezone

    plus3 = timezone(timedelta(hours=3))
    ended = (NOW - H).astimezone(plus3)
    snap = Snapshot(now=NOW, tasks=(task(),), sessions=(session(ended=ended),))
    [action] = ExitContract().evaluate(snap)
    assert "2026-03-02 15:00 UTC" in action.reason


def test_requeue_details() -> None:
    snap = Snapshot(
        now=NOW, tasks=(task(),), sessions=(session(dirty_worktree=True, exit_reason="oom"),)
    )
    [action] = ExitContract().evaluate(snap)
    assert action.task_id == "T-1"
    assert action.params == {"session_id": "S-1", "owner_id": "agent-1", "dirty_worktree": True}
    assert "oom" in action.reason and "final status" in action.reason
    assert action.key == "exit:S-1"


def blocked(reason: str | None, condition_id: str | None = None, since: datetime = NOW - H) -> Task:
    block = None if reason is None else Block(reason, since, condition_id)
    return task(Status.BLOCKED, since=since, block=block)


@pytest.mark.parametrize(
    "reason", ["guard", "GUARD", "guard: wait for CI", "waiting_ci #12", " quota "]
)
def test_allowed_block_reasons(reason: str) -> None:
    assert run(blocked(reason)) == []


@pytest.mark.parametrize("reason", ["", "stuck", "guardian", "waiting", None])
def test_disallowed_or_missing_block_reason_is_requeued(reason: str | None) -> None:
    assert run(blocked(reason), session()) == [ActionKind.REQUEUE]


def test_blocks_not_left_by_a_session_are_ignored() -> None:
    assert run(blocked("legal sign-off pending")) == []  # no session at all
    live = session("S-2", ended=None, last_heartbeat=NOW - timedelta(minutes=1))
    assert run(blocked("stuck"), session(), live) == []  # agent still on it
    # Blocked before the last session started and not by it.
    assert run(blocked("stuck", since=NOW - 5 * H), session()) == []
    # ...unless that session reported ending blocked.
    assert run(blocked("stuck", since=NOW - 5 * H), session(final_status=Status.BLOCKED)) == [
        ActionKind.REQUEUE
    ]


def test_block_with_condition_is_allowed() -> None:
    assert run(blocked("anything at all", condition_id="C-1"), session()) == []


def test_block_grace_period() -> None:
    recent = NOW - timedelta(minutes=5)
    assert run(blocked("", since=recent), session(ended=recent)) == []


def test_custom_allowed_reasons() -> None:
    rule = build({"allowed_block_reasons": ["vendor"], "session_exit_grace": "1m"})
    assert rule.session_exit_grace == timedelta(minutes=1)
    assert run(blocked("vendor: waiting on API key"), session(), rule=rule) == []
    assert run(blocked("guard"), session(), rule=rule) == [ActionKind.REQUEUE]


@pytest.mark.parametrize(
    "config",
    [
        {"allowed_block_reasons": "guard"},
        {"allowed_block_reasons": [1]},
        {"allowed_block_reasons": ["guard", " "]},
        {"session_stale_after": [1]},
        {"grace": "1m"},
    ],
)
def test_bad_config(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build(config)
