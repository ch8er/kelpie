from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import ActionLedger, Dispatched, MemoryLedgerStore, Outcome
from kelpie.core.model import (
    Owner,
    PullRequest,
    Session,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.rules import PLANNED, REGISTRY
from kelpie.rules.checkpoints import Checkpoints, build
from tests.fakes import FakeAdapter

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)


def task(status: Status = Status.IN_PROGRESS, since: datetime = NOW - 120 * M) -> Task:
    return Task("T-1", "Do the thing", status, since, branch="task/T-1")


def live(**kw: object) -> Session:
    base = {"started_at": NOW - 90 * M, "last_heartbeat": NOW - M, "dirty_worktree": True}
    return Session("S-1", "T-1", "dev", **{**base, **kw})  # type: ignore[arg-type]


def run(
    t: Task,
    *sessions: Session,
    rule: Checkpoints | None = None,
    prs: tuple[PullRequest, ...] = (),
    known: bool | None = True,
    spans: tuple[Span, ...] = (),
    owners: tuple[Owner, ...] = (),
) -> list[Action]:
    snap = Snapshot(
        now=NOW,
        tasks=(t,),
        sessions=sessions,
        pull_requests=prs,
        pull_requests_complete=known,
        pull_requests_at=NOW if known else None,
        spans=spans,
        owners=owners,
    )
    return list((rule or Checkpoints()).evaluate(snap))


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
    ended = live(ended_at=NOW - 5 * M, branch="b", summary="half done")
    [a, resume] = run(task(), ended)
    assert a.kind is ActionKind.WIP_COMMIT and a.once
    assert a.reason.startswith("WIP: Do the thing [skip ci]")
    assert "half done" in a.reason
    assert a.params == {"session_id": "S-1", "branch": "b", "skip_ci": True, "draft_pr": False}
    # The same tick tells the next session where to resume (rule 1 requeues the task).
    assert resume.kind is ActionKind.COMMENT and resume.once
    assert "Resume from the WIP commit Kelpie is saving on b" in resume.reason
    assert "If git log does not show that commit" in resume.reason
    assert "half done" in resume.reason and resume.params["commit"] is None
    silent = live(last_heartbeat=NOW - 35 * M)  # gone since 5 minutes (stale after 30)
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
    rule = Checkpoints(act_within=24 * 60 * M, wip_without_pr_check=True)
    started = task(since=NOW - 25 * 60 * M)
    assert kinds(run(started, live(ended_at=NOW - 23 * 60 * M), rule=rule)) == [
        ActionKind.WIP_COMMIT,
        ActionKind.COMMENT,
    ]
    # At act_within Kelpie stops: one closing word, on the same once-key as the commit, so
    # after a WIP commit made earlier the ledger keeps it quiet.
    [last] = run(started, live(ended_at=NOW - 24 * 60 * M), rule=rule)
    wip = run(started, live(ended_at=NOW - 23 * 60 * M), rule=rule)[0]
    assert last.key == wip.key and last.once_per_key and wip.once_per_key
    assert run(started, live(ended_at=NOW - 26 * 60 * M), rule=rule) == []


def test_declared_end_gets_wip_commit_but_no_resume_note() -> None:
    ended = live(ended_at=NOW - 5 * M, final_status=Status.BLOCKED)
    blocked = task(Status.BLOCKED, since=NOW - 5 * M)
    assert kinds(run(blocked, ended)) == [ActionKind.WIP_COMMIT]


def test_draft_pr_mode_and_note_limit() -> None:
    rule = Checkpoints(wip_ci="draft_pr")
    [a, _] = run(task(), live(ended_at=NOW - 5 * M, summary="x" * 900), rule=rule)
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
    # PR checks off: rule 1 requeues at 10 minutes, after rule 5's own decision window.
    own = {"wip_ci": "draft_pr", "wip_without_pr_check": True}
    rules = [REGISTRY[p.name](own if p.name == "checkpoints" else {})
             for p in PLANNED if p.name in REGISTRY]  # fmt: skip
    adapter = FakeAdapter()
    engine = Engine(
        rules, adapters=[adapter], config=EngineConfig(dry_run=False, default_mode=Mode.ACT)
    )
    engine.tick(
        lambda: Snapshot(
            now=NOW,
            tasks=(task(),),
            sessions=(ended,),
            pull_requests_complete=True,
            pull_requests_at=NOW,
        )
    )
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
    more = build({"wip_without_pr_check": True, "tool_grace": "1h",
                  "tool_long_after": "2m", "wip_pr_margin": "30s"})  # fmt: skip
    assert more.wip_without_pr_check and more.tool_grace == 60 * M


# --- live 24 h review ---------------------------------------------------------------------


def test_no_resume_after_a_declared_block_even_once_unblocked() -> None:
    blocked = Session(
        "S-1", "T-1", "dev", NOW - 180 * M, ended_at=NOW - 60 * M, final_status=Status.BLOCKED,
        last_commit="abc1234", last_commit_at=NOW - 70 * M, branch="task/T-1",
    )  # fmt: skip
    # Unblocked and queued again: whoever unblocked it sends it back, not a lost session.
    assert run(task(Status.READY, since=NOW - 30 * M), blocked) == []
    waited = Session(
        "S-1", "T-1", "dev", NOW - 180 * M, ended_at=NOW - 60 * M, final_status=Status.WAITING,
        last_commit="abc1234", last_commit_at=NOW - 70 * M,
    )  # fmt: skip
    assert run(task(Status.READY, since=NOW - 30 * M), waited) == []


def open_pr(branch: str = "b", **kw: object) -> PullRequest:
    return PullRequest("example/repo", 41, "T-1: work", branch, **kw)  # type: ignore[arg-type]


def test_never_a_wip_commit_to_a_branch_with_an_open_pr() -> None:
    ended = live(ended_at=NOW - 5 * M, branch="b", last_commit="abc1234",
                 last_commit_at=NOW - 20 * M)  # fmt: skip
    unsaved, resume = run(task(), ended, prs=(open_pr(labels=("automerge",)),))
    assert unsaved.kind is ActionKind.ALERT and unsaved.once and unsaved.key == "wip-decision:S-1"
    assert "b has an open PR, example/repo#41" in unsaved.reason
    assert resume.kind is ActionKind.COMMENT
    assert "Resume from commit abc1234 on b" in resume.reason
    assert "did not commit (b has an open PR, example/repo#41)" in resume.reason
    # Another branch's PR, or a fork's, does not count.
    [wip, _] = run(task(), ended, prs=(open_pr("other"),))
    assert wip.kind is ActionKind.WIP_COMMIT and wip.params["branch"] == "b"
    assert kinds(run(task(), ended, prs=(open_pr(fork=True),)))[0] is ActionKind.WIP_COMMIT
    # The task's own branch, or the task's own PR on any branch, counts too.
    assert ActionKind.WIP_COMMIT not in kinds(run(task(), ended, prs=(open_pr("task/T-1"),)))
    linked = PullRequest("example/repo", 42, "T-1: fix", "T-1-fix", task_ids=("T-1",))
    assert ActionKind.WIP_COMMIT not in kinds(run(task(), ended, prs=(linked,)))
    # Drafts count: the requirement is any open PR.
    draft = open_pr(draft=True)
    assert ActionKind.WIP_COMMIT not in kinds(run(task(), ended, prs=(draft,)))
    # No branch known at all: nothing tells where the commit would land; it waits.
    nameless = live(ended_at=NOW - 5 * M)
    bare = Task("T-1", "Do the thing", Status.IN_PROGRESS, NOW - 120 * M)
    assert ActionKind.WIP_COMMIT not in kinds(run(bare, nameless))


@pytest.mark.parametrize("known", [None, False])
def test_no_wip_commit_until_open_prs_are_known_after_the_exit(known: bool | None) -> None:
    ended = live(ended_at=NOW - 5 * M, branch="b")
    assert run(task(), ended, known=known) == []  # waits: neither commit nor note
    found = run(task(), ended, known=known, rule=Checkpoints(wip_without_pr_check=True))
    assert kinds(found) == [ActionKind.WIP_COMMIT, ActionKind.COMMENT]
    # Unknown never refuses, however long: no alert that a later commit would contradict.
    late = live(ended_at=NOW - 20 * 60 * M, branch="b", started_at=NOW - 21 * 60 * M)
    early = task(since=NOW - 22 * 60 * M)  # nobody moved it since
    assert run(early, late, known=known) == []
    # Once the lists are known after the exit, it commits (also for a crashed session).
    assert kinds(run(early, late))[0] is ActionKind.WIP_COMMIT
    # Still waiting at act_within: one alert, and nothing after.
    gave_up = live(ended_at=NOW - 24 * 60 * M - 5 * M, branch="b", started_at=NOW - 26 * 60 * M)
    [alert] = run(task(since=NOW - 27 * 60 * M), gave_up, known=known)
    assert alert.key == "wip-decision:S-1" and "never known in time" in alert.reason
    much_later = live(ended_at=NOW - 26 * 60 * M, branch="b", started_at=NOW - 27 * 60 * M)
    assert run(task(since=NOW - 28 * 60 * M), much_later, known=known) == []
    # The closed list's horizon has passed the session by then: the cause is still the PR
    # data that never came.
    snap = Snapshot(now=NOW, tasks=(task(since=NOW - 27 * 60 * M),), sessions=(gave_up,),
                    closed_pull_requests_since=NOW - 24 * 60 * M)  # fmt: skip
    [alert] = list(Checkpoints().evaluate(snap))
    assert "never known in time" in alert.reason
    crashed = live(last_heartbeat=NOW - 50 * M, branch="b")
    assert kinds(run(task(), crashed))[0] is ActionKind.WIP_COMMIT


def test_a_pr_list_older_than_the_exit_does_not_count() -> None:
    """The agent's last act may have been to open a PR: the list must come after."""
    ended = live(ended_at=NOW - 5 * M, branch="b")
    snap = Snapshot(now=NOW, tasks=(task(),), sessions=(ended,), pull_requests_complete=True,
                    pull_requests_at=NOW - 5 * M + 30 * timedelta(seconds=1))  # fmt: skip
    assert list(Checkpoints().evaluate(snap)) == []  # within wip_pr_margin of the exit
    fresh = replace(snap, pull_requests_at=NOW - 3 * M)
    assert kinds(list(Checkpoints().evaluate(fresh)))[0] is ActionKind.WIP_COMMIT


def test_the_no_commit_threshold_is_per_role() -> None:
    rule = build({"commit_every": "30m", "commit_every_by_role": {"reviewer": "2h"}})
    reviewing = Task("T-1", "Review", Status.IN_PROGRESS, NOW - 120 * M, role="reviewer")
    assert run(reviewing, live(), rule=rule) == []  # 90 minutes: fine for a reviewer
    assert run(task(), live(), rule=rule)  # a developer is reminded
    # Without a task role, the owner's role decides.
    owners = (Owner("dev", "reviewer"),)
    assert run(task(), live(), rule=rule, owners=owners) == []
    # A task role not in the table wins over the owner's: the default applies.
    tester = Task("T-1", "Test", Status.IN_PROGRESS, NOW - 120 * M, role="tester")
    assert run(tester, live(), rule=rule, owners=owners)
    assert kinds(run(reviewing, live(started_at=NOW - 150 * M), rule=rule)) == [ActionKind.COMMENT]


def test_no_reminder_while_a_long_tool_call_is_in_progress() -> None:
    building = live(running_tool="xcodebuild", running_tool_since=NOW - 40 * M)
    assert run(task(), building) == []
    span = Span("t", "s", "simulator", SpanKind.TOOL, NOW - 20 * M, session_id="S-1")
    assert run(task(), live(), spans=(span,)) == []
    finished = Span("t", "s", "build", SpanKind.TOOL, NOW - 20 * M, NOW - 5 * M, session_id="S-1")
    assert run(task(), live(), spans=(finished,))  # it is over: commit now
    # Past tool_grace a call no longer holds the reminder (it may be hung).
    assert run(task(), live(running_tool="xcodebuild", running_tool_since=NOW - 150 * M))
    # The adapter's tool run (a build process) holds from its start; a short span does not.
    assert run(task(), live(running_tool="xcodebuild", running_tool_since=NOW - 2 * M)) == []
    short = Span("t", "s", "ls", SpanKind.TOOL, NOW - 2 * M, session_id="S-1")
    assert run(task(), live(), spans=(short,))
    # One with no start time, or one starting in the future, holds nothing.
    assert run(task(), live(running_tool="sim"))
    assert run(task(), live(running_tool="sim", running_tool_since=NOW + 300 * M))


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"commit_every_by_role": ["reviewer"]}, "table of role = duration"),
        ({"commit_every_by_role": {"reviewer": "soon"}}, "commit_every_by_role.reviewer"),
        ({"wip_without_pr_check": "yes"}, "true or false"),
        ({"tool_grace": "0s"}, "positive"),
    ],
)
def test_bad_live_review_config(config: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build(config)


def test_a_refusal_stands_after_the_pr_merges() -> None:
    """Refused for an open automerge PR; the PR merges; no WIP commit onto its branch."""
    ended = live(ended_at=NOW - 5 * M, branch="b", started_at=NOW - 60 * M)
    merged = replace(open_pr(), closed_at=NOW - 2 * M, merged_at=NOW - 2 * M)
    snap = Snapshot(now=NOW, tasks=(task(Status.REVIEW),), sessions=(ended,),
                    closed_pull_requests=(merged,), pull_requests_complete=True,
                    pull_requests_at=NOW)  # fmt: skip
    [alert] = list(Checkpoints().evaluate(snap))
    assert alert.key == "wip-decision:S-1" and "open after the session started" in alert.reason
    # A PR that closed before the session started is history.
    old = replace(merged, closed_at=NOW - 90 * M)
    found = list(Checkpoints().evaluate(replace(snap, closed_pull_requests=(old,))))
    assert kinds(found)[0] is ActionKind.WIP_COMMIT


def test_the_margin_must_fit_in_act_within() -> None:
    with pytest.raises(ValueError, match="wip_pr_margin must be shorter than act_within"):
        build({"wip_pr_margin": "25h"})


def test_no_wip_commit_once_the_task_was_claimed_again() -> None:
    """The next session works in that worktree: a commit then would run under it."""
    ended = live(ended_at=NOW - 20 * M, branch="b")
    claimed = task(Status.IN_PROGRESS, since=NOW - 5 * M)
    assert ActionKind.WIP_COMMIT not in kinds(run(claimed, ended))
    # Claimed after the exit, even by a second: skip. At the exit itself (a silent session's
    # exit is its own claim): commit.
    late = task(Status.IN_PROGRESS, since=NOW - 20 * M + timedelta(seconds=1))
    assert ActionKind.WIP_COMMIT not in kinds(run(late, ended))
    same = task(Status.IN_PROGRESS, since=NOW - 20 * M)
    assert kinds(run(same, ended))[0] is ActionKind.WIP_COMMIT
    # A requeue is no claim: the crash sweep requeues as it closes the run, at the exit or
    # after its last sign of life; the adapter refuses while a run is active.
    for since in (NOW - 20 * M, NOW - 20 * M + timedelta(seconds=1), NOW - 5 * M):
        assert kinds(run(task(Status.READY, since=since), ended))[0] is ActionKind.WIP_COMMIT
    # Handed on to review by the session itself: still saved.
    review = task(Status.REVIEW, since=NOW - 20 * M)
    assert kinds(run(review, ended))[0] is ActionKind.WIP_COMMIT


def test_a_new_live_session_ends_the_wait_and_refusals_need_no_complete_lists() -> None:
    ended = live(ended_at=NOW - 20 * M, branch="b")
    working = Session("S-2", "T-1", "dev", NOW - 5 * M, last_heartbeat=NOW - M)
    assert ActionKind.WIP_COMMIT not in kinds(run(task(), ended, working, known=None))
    # An open PR in an incomplete list refuses, and so does a closed one since the start.
    [alert] = run(task(), ended, prs=(open_pr(),), known=False)
    assert "has an open PR" in alert.reason
    gone = replace(open_pr(), closed_at=NOW - 10 * M)
    snap = Snapshot(now=NOW, tasks=(task(),), sessions=(ended,), closed_pull_requests=(gone,))
    [alert] = list(Checkpoints().evaluate(snap))
    assert "open after the session started" in alert.reason


def test_a_skip_tells_the_next_session_the_truth() -> None:
    ended = live(ended_at=NOW - 20 * M, branch="b", last_commit="abc1234",
                 last_commit_at=NOW - 30 * M)  # fmt: skip
    claimed = task(Status.IN_PROGRESS, since=NOW - 5 * M)
    snap = Snapshot(now=NOW, tasks=(claimed,), sessions=(ended,))
    [alert] = list(Checkpoints().evaluate(snap))  # claimed and handed on: no resume note
    assert alert.key == "wip-decision:S-1" and "claimed again" in alert.reason
    alert, resume = run(task(Status.READY, since=NOW - 5 * M), ended, prs=(open_pr(),))
    assert "Resume from commit abc1234 on b" in resume.reason
    assert "did not commit (b has an open PR" in resume.reason
    assert resume.params["commit"] == "abc1234"


def test_a_session_older_than_the_closed_list_is_refused() -> None:
    long = live(ended_at=NOW - 5 * M, branch="b", started_at=NOW - 30 * 60 * M)
    snap = Snapshot(now=NOW, tasks=(task(Status.REVIEW),), sessions=(long,),
                    pull_requests_complete=True, pull_requests_at=NOW,
                    closed_pull_requests_since=NOW - 24 * 60 * M)  # fmt: skip
    [alert] = list(Checkpoints().evaluate(snap))
    assert "would no longer be listed" in alert.reason


def test_an_unknown_branch_waits() -> None:
    nameless = live(ended_at=NOW - 5 * M)
    bare = Task("T-1", "Do the thing", Status.REVIEW, NOW - 120 * M)
    assert run(bare, nameless) == []


def test_the_first_decision_stands_in_the_ledger() -> None:
    """Refused for an open PR on one tick; on a later tick the PR is gone from the lists:
    the WIP commit has the alert's once-key, so the ledger suppresses it."""
    ended = live(ended_at=NOW - 5 * M, branch="b")
    [refused, *_] = run(task(Status.REVIEW), ended, prs=(open_pr(),))
    [commit, *_] = run(task(Status.REVIEW), ended)
    assert commit.kind is ActionKind.WIP_COMMIT and refused.kind is ActionKind.ALERT
    ledger = ActionLedger(MemoryLedgerStore(), cooldown=30 * M)
    ledger.record(Dispatched(refused, Mode.ACT, Outcome.ALERTED), NOW)
    assert ledger.check(commit, Mode.ACT, NOW + 60 * M) is not None  # suppressed
