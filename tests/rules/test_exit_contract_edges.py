"""Edge cases of rule 1 (exit contract): boundaries, host sleeps, scan order, action fields."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import (
    Artifact,
    Block,
    Comment,
    HostSleep,
    PullRequest,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules._acceptance import Criterion
from kelpie.rules.exit_contract import Evidence, ExitContract, build

NOW = datetime(2026, 3, 2, 16, tzinfo=UTC)
H = timedelta(hours=1)
M = timedelta(minutes=1)
GRACE = ExitContract().session_exit_grace
WITHIN = ExitContract().done_evidence_within


def task(
    status: Status = Status.IN_PROGRESS,
    since: datetime = NOW - 2 * H,
    tid: str = "T-101",
    **kw: Any,
) -> Task:
    return Task(tid, f"title {tid}", status, since, "developer", **kw)


def session(
    ended: datetime | None = NOW - H, started: datetime = NOW - 3 * H, tid: str = "T-101", **kw: Any
) -> Session:
    return Session("S-1", tid, "agent-dev-1", started, ended_at=ended, **kw)


def run(
    *tasks: Task,
    sessions: tuple[Session, ...] = (),
    sleeps: tuple[HostSleep, ...] = (),
    rule: ExitContract | None = None,
    **kw: Any,
) -> list[Action]:
    snapshot = Snapshot(NOW, tasks=tasks, sessions=sessions, host_sleeps=sleeps, **kw)
    return list((rule or ExitContract()).evaluate(snapshot))


# --- "done" needs evidence ---------------------------------------------------------------


def evidence(*globs: str, **kw: Any) -> ExitContract:
    return ExitContract(done_evidence=(Evidence("tests", artifacts=globs),), **kw)


def done(
    *paths: str,
    age: timedelta = H,
    ended: datetime | None = None,
    final: Status | None = Status.DONE,
    artifacts_at: datetime = NOW - 2 * H,
    **kw: Any,
) -> tuple[Task, Session]:
    since = NOW - age
    t = task(Status.DONE, since=since, owner_id="agent-dev-1", **kw)
    s = session(
        ended=since if ended is None else ended,
        started=since - 3 * H,
        final_status=final,
        artifacts=tuple(Artifact(p, artifacts_at) for p in paths),
    )
    return t, s


def judged(t: Task, s: Session, rule: ExitContract | None = None) -> list[Action]:
    return run(t, sessions=(s,), rule=rule or evidence("pytest*"))


def test_a_file_name_glob_matches_a_file_in_a_directory() -> None:
    assert judged(*done("reports/pytest.txt")) == []


def test_a_whole_path_glob_matches_in_any_case() -> None:
    assert judged(*done("Reports/Run.XML"), rule=evidence("reports/*.xml")) == []
    assert judged(*done("Reports/Run.XML"), rule=evidence("REPORTS/*.Xml")) == []


def test_an_artifact_stamped_at_the_reopen_is_from_before_it() -> None:
    """Evidence counts since the task was last reopened: strictly after it."""
    reopened = NOW - 2 * H
    at_reopen = done("pytest.txt", artifacts_at=reopened, reopened_at=(reopened,))
    assert [a.kind for a in judged(*at_reopen)] == [ActionKind.REQUEUE]
    after = done("pytest.txt", artifacts_at=reopened + M, reopened_at=(reopened,))
    assert judged(*after) == []


def test_done_exactly_done_evidence_within_ago_is_still_judged() -> None:
    assert [a.kind for a in judged(*done(age=WITHIN))] == [ActionKind.REQUEUE]
    assert judged(*done(age=WITHIN + M)) == []


def test_done_exactly_session_exit_grace_ago_is_judged() -> None:
    assert [a.kind for a in judged(*done(age=GRACE))] == [ActionKind.REQUEUE]
    assert judged(*done(age=GRACE - M)) == []


def test_agent_completion_needs_a_declared_done_near_the_status_change() -> None:
    since = NOW - H
    assert judged(*done(final=None)) == []  # ended then, but declared nothing
    assert judged(*done(ended=since - 2 * H)) == []  # declared done long before
    edge = done(ended=since - GRACE)  # exactly session_exit_grace apart: still its claim
    assert [a.kind for a in judged(*edge)] == [ActionKind.REQUEUE]


def test_done_without_evidence_action_fields() -> None:
    since = (NOW - H).isoformat()
    [requeue] = judged(*done())
    assert requeue.rule == "exit_contract" and requeue.task_id == "T-101"
    assert requeue.severity is Severity.WARNING and requeue.key == f"evidence:T-101:{since}"
    assert isinstance(requeue.reason, str) and "T-101" in requeue.reason
    [alert] = judged(*done(), rule=evidence("pytest*", done_evidence_action="alert"))
    assert alert.kind is ActionKind.ALERT and alert.rule == "exit_contract"
    assert alert.severity is Severity.WARNING and alert.key == f"evidence:T-101:{since}"
    assert isinstance(alert.reason, str) and "T-101" in alert.reason


@pytest.mark.parametrize("globs", [[""], [3], ["*test*", ""]])
def test_build_rejects_empty_or_non_string_globs(globs: list[Any]) -> None:
    with pytest.raises(ValueError):
        build({"done_evidence": [{"name": "tests", "artifacts": globs}]})


# --- the exit itself -----------------------------------------------------------------------


def test_a_finished_parent_skipped_does_not_end_the_scan() -> None:
    parent = task(tid="T-100", subtasks=("T-103",))
    child = task(Status.DONE, tid="T-103")
    [action] = run(parent, child, task(), sessions=(session(),))
    assert action.kind is ActionKind.REQUEUE and action.task_id == "T-101"


@pytest.mark.parametrize(
    ("agent", "source"), [(None, None), ("laptop", "laptop/hermes")], ids=["server", "agent"]
)
def test_no_requeue_while_the_host_slept_through_the_grace(
    agent: str | None, source: str | None
) -> None:
    s = session(ended=NOW - 30 * M, source=source)
    sleep = HostSleep(NOW - 28 * M, NOW - M, agent=agent)
    assert run(task(), sessions=(s,), sleeps=(sleep,)) == []
    other = HostSleep(sleep.start, sleep.end, agent="desktop")  # another host slept
    assert [a.kind for a in run(task(), sessions=(s,), sleeps=(other,))] == [ActionKind.REQUEUE]


def test_requeue_exit_action_fields() -> None:
    [action] = run(task(), sessions=(session(),))
    assert action.rule == "exit_contract" and action.task_id == "T-101"
    assert action.severity is Severity.WARNING and action.key == "exit:S-1"


def quiet(**kw: Any) -> Session:
    """Silent since two hours ago: no end, last heartbeat then."""
    return session(ended=None, last_heartbeat=NOW - 2 * H, **kw)


def test_a_silent_session_ended_in_a_sleep_within_the_stale_bar() -> None:
    """A silent exit is placed with ``session_stale_after``: a sleep starting 20 min after
    its last heartbeat (past the 10 min grace, within the 30 min stale bar) is the one."""
    sleep = HostSleep(NOW - 100 * M, NOW - 90 * M)
    [action] = run(task(), sessions=(quiet(),), sleeps=(sleep,))
    assert "host_slept" in action.params


def test_an_ended_session_is_placed_with_the_grace_only() -> None:
    sleep = HostSleep(NOW - 100 * M, NOW - 90 * M)  # 20 min after the end: past the grace
    [action] = run(task(), sessions=(session(ended=NOW - 2 * H),), sleeps=(sleep,))
    assert "host_slept" not in action.params


def test_resume_needs_both_a_commit_and_a_branch() -> None:
    sleep = HostSleep(NOW - 100 * M, NOW - 90 * M)
    [action] = run(task(), sessions=(quiet(last_commit="abc123"),), sleeps=(sleep,))
    assert "host_slept" in action.params and "resume" not in action.params
    full = quiet(last_commit="abc123", branch="feature/t-101", dirty_worktree=True)
    [action] = run(task(), sessions=(full,), sleeps=(sleep,))
    assert action.params["resume"] == {"branch": "feature/t-101", "commit": "abc123"}
    assert action.params["dirty_worktree"] is True
    assert "Resume from commit abc123 on feature/t-101." in action.reason
    assert "uncommitted changes" in action.reason


# --- blocks left behind --------------------------------------------------------------------


def blocked(since: datetime = NOW - 90 * M, reason: str = "dunno") -> Task:
    return task(Status.BLOCKED, since=since, block=Block(reason, since))


def test_a_block_is_judged_with_the_configured_stale_bar() -> None:
    s = session(ended=None, last_heartbeat=NOW - H)
    assert [a.kind for a in run(blocked(), sessions=(s,))] == [ActionKind.REQUEUE]
    patient = ExitContract(session_stale_after=2 * H)  # the session is still live
    assert run(blocked(), sessions=(s,), rule=patient) == []


def test_a_block_stamped_at_the_session_start_is_the_sessions() -> None:
    s = session()
    [action] = run(blocked(since=s.started_at), sessions=(s,))
    assert action.kind is ActionKind.REQUEUE
    assert run(blocked(since=s.started_at - M), sessions=(s,)) == []  # predates it


def test_requeue_block_action_fields() -> None:
    since = NOW - 90 * M
    [action] = run(blocked(since), sessions=(session(final_status=Status.BLOCKED),))
    assert action.kind is ActionKind.REQUEUE and action.rule == "exit_contract"
    assert action.task_id == "T-101" and action.severity is Severity.WARNING
    assert action.key == f"block:T-101:{since.isoformat()}"
    assert action.params == {
        "session_id": "S-1",
        "owner_id": "agent-dev-1",
        "block_reason": "dunno",
    }
    assert "with 'dunno'," in action.reason


# --- a declared status the platform did not apply ------------------------------------------


def test_unapplied_status_is_judged_with_the_configured_stale_bar() -> None:
    s = session(ended=None, last_heartbeat=NOW - H, final_status=Status.DONE)
    [action] = run(task(), sessions=(s,))
    assert action.kind is ActionKind.ALERT
    assert run(task(), sessions=(s,), rule=ExitContract(session_stale_after=2 * H)) == []


def test_unapplied_status_stamped_at_the_exit_and_alert_fields() -> None:
    end = NOW - H
    [action] = run(task(since=end), sessions=(session(final_status=Status.DONE),))
    assert action.kind is ActionKind.ALERT and action.rule == "exit_contract"
    assert action.task_id == "T-101" and action.severity is Severity.WARNING
    assert action.key == "unapplied:S-1" and action.state == "exit_contract:unapplied:S-1"
    assert action.since == end
    assert run(task(since=end + M), sessions=(session(final_status=Status.DONE),)) == []


# --- acceptance criteria -------------------------------------------------------------------

PATTERNS = (re.compile(r"\b\d+\s+runs?\b", re.IGNORECASE),)


def ask(at: datetime) -> Comment:
    return Comment("lead", "Before merge: 20 runs in a row.", at, human=True)


def acceptance(
    t: Task, sessions: tuple[Session, ...] = (), **kw: Any
) -> tuple[list[Action], list[Action]]:
    """(alerts, comments) of the acceptance check."""
    found = run(t, sessions=sessions, rule=ExitContract(acceptance_patterns=PATTERNS), **kw)
    alerts = [a for a in found if a.kind is ActionKind.ALERT]
    return alerts, [a for a in found if a.kind is ActionKind.COMMENT]


def merged(at: datetime, task_ids: tuple[str, ...] = ("T-101",)) -> PullRequest:
    return PullRequest(
        "example/repo", 5, "T-101: fix", "b", task_ids=task_ids, closed_at=at, merged_at=at
    )


def test_only_the_tasks_own_recent_merge_lands_it() -> None:
    t = task(Status.REVIEW, comments=(ask(NOW - 30 * H),))
    alerts, _ = acceptance(t, closed_pull_requests=(merged(NOW - H, ("T-999",)),))
    assert alerts == []  # another task's PR
    alerts, _ = acceptance(t, closed_pull_requests=(merged(NOW - 3 * 24 * H),))
    assert alerts == []  # merged long ago
    [a] = acceptance(t, closed_pull_requests=(merged(NOW - WITHIN),))[0]  # exactly within
    assert a.severity is Severity.CRITICAL and "was merged" in a.reason


def test_accepted_exactly_within_ago_lands_without_merging() -> None:
    t = task(Status.ACCEPTED, since=NOW - WITHIN, comments=(ask(NOW - 30 * H),))
    [a] = acceptance(t)[0]
    assert a.params["landing"] == "it was accepted" and a.severity is Severity.WARNING
    assert a.key is not None and a.key.endswith(":warning")


@pytest.mark.parametrize("age", [GRACE, WITHIN], ids=["grace", "within"])
def test_done_by_an_agent_lands_at_both_boundaries(age: timedelta) -> None:
    since = NOW - age
    t = task(Status.DONE, since=since, comments=(ask(since - H),))
    s = session(ended=since, started=since - 2 * H, final_status=Status.DONE)
    [a] = acceptance(t, sessions=(s,))[0]
    assert a.params["landing"] == "it was moved to done"


def test_a_criterion_stated_at_the_landing_moment_is_judged() -> None:
    since = NOW - 2 * H
    [a] = acceptance(task(Status.ACCEPTED, since=since, comments=(ask(since),)))[0]
    ref = Criterion("Before merge: 20 runs in a row.", "lead", since).ref
    assert a.rule == "exit_contract" and a.params["ref"] == ref
    assert a.params["headline"] == f"Acceptance criterion {ref} of T-101 not shown as met"
    assert a.params["needed"].startswith(f'Comment "{ref}: met, <result>"')
    later = task(Status.ACCEPTED, since=since, comments=(ask(since + M),))
    assert acceptance(later)[0] == []  # asked after it landed


def test_announcement_names_the_rule() -> None:
    _, [note] = acceptance(task(Status.REVIEW, comments=(ask(NOW - H),)))
    assert note.rule == "exit_contract" and note.task_id == "T-101"
