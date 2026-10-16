from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.model import (
    Dependency,
    Owner,
    OwnerKind,
    PullRequest,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules import REGISTRY
from kelpie.rules.orphan_status import OrphanStatus, build

NOW = datetime(2026, 3, 2, 16, tzinfo=UTC)
M = timedelta(minutes=1)
DEV = Owner("dev-1", "developer")


def task(
    tid: str = "T-1", status: Status = Status.IN_PROGRESS, age: int = 60, **kw: object
) -> Task:
    return Task(tid, tid, status, NOW - age * M, role="developer", **kw)  # type: ignore[arg-type]


def act(
    *tasks: Task,
    deps: tuple[Dependency, ...] = (),
    owners: tuple[Owner, ...] = (DEV,),
    sessions: tuple[Session, ...] = (),
    rule: OrphanStatus | None = None,
    prs: tuple[PullRequest, ...] = (),
    closed: tuple[PullRequest, ...] | None = None,
    complete: bool = True,
) -> list:
    snap = Snapshot(
        now=NOW,
        tasks=tasks,
        owners=owners,
        dependencies=deps,
        sessions=sessions,
        pull_requests=prs,
        closed_pull_requests=LANDED if closed is None else closed,
        pull_requests_complete=complete,
    )
    return list((rule or OrphanStatus()).evaluate(snap))


def merged_pr(number: int, *task_ids: str, merged: bool = True) -> PullRequest:
    at = NOW - 6 * 60 * M
    return PullRequest(
        "example/repo",
        number,
        f"{task_ids[0]}: work",
        f"feature/{task_ids[0]}",
        task_ids=task_ids,
        closed_at=at,
        merged_at=at if merged else None,
        into_default=True,
    )


LANDED = (merged_pr(1, "S1"), merged_pr(2, "S2"))  # the subtasks' PRs, merged


def kinds(actions: list) -> list[str]:
    return [a.kind for a in actions]


def test_registered() -> None:
    assert isinstance(REGISTRY["orphan_status"]({}), OrphanStatus)


def test_threshold_boundary() -> None:
    assert act(task(age=29)) == []
    assert kinds(act(task(age=30))) == [ActionKind.ALERT, ActionKind.REASSIGN]


@pytest.mark.parametrize("status", [Status.REVIEW, Status.ACCEPTED])
def test_queue_statuses_use_the_longer_threshold(status: Status) -> None:
    assert act(task(status=status, age=119)) == []
    assert kinds(act(task(status=status, age=120))) == [ActionKind.ALERT, ActionKind.REASSIGN]


@pytest.mark.parametrize(
    "status",
    [Status.BACKLOG, Status.READY, Status.BLOCKED, Status.WAITING, Status.DONE, Status.CANCELLED],
)
def test_statuses_left_alone(status: Status) -> None:
    assert act(task(status=status, age=600)) == []


def test_owned_task_is_fine_and_unknown_owner_only_alerts() -> None:
    assert act(task(owner_id="dev-1")) == []
    [alert] = act(task(owner_id="ghost"))
    assert "no longer known" in alert.reason and "human" in alert.reason
    rule = OrphanStatus(reassign_unknown_owner=True)
    assert kinds(act(task(owner_id="ghost"), rule=rule)) == [ActionKind.ALERT, ActionKind.REASSIGN]
    assert act(task(owner_id="ghost"), owners=()) == []  # no directory: cannot tell


def test_live_session_means_someone_is_on_it() -> None:
    live = Session("S-1", "T-1", "dev-2", NOW - 50 * M, last_heartbeat=NOW - 2 * M)
    assert act(task(), sessions=(live,)) == []
    stale = Session("S-1", "T-1", "dev-2", NOW - 300 * M, last_heartbeat=NOW - 200 * M,
                    final_status=Status.IN_PROGRESS)  # fmt: skip
    assert act(task(), sessions=(stale,)) != []  # silent for 3h20m: not live


def test_task_left_by_a_session_without_status_is_rule_1s() -> None:
    ended = Session("S-1", "T-1", "dev-2", NOW - 90 * M, ended_at=NOW - 45 * M)
    assert act(task(), sessions=(ended,)) == []
    declared = Session("S-1", "T-1", "dev-2", NOW - 90 * M, ended_at=NOW - 45 * M,
                       final_status=Status.REVIEW)  # fmt: skip
    assert act(task(), sessions=(declared,)) != []


def test_severity_transitive_dependents_and_cycles() -> None:
    deps = (
        Dependency("T-2", "T-1"),
        Dependency("T-3", "T-2"),
        Dependency("T-4", "T-3"),
        Dependency("T-5", "T-1"),
        Dependency("T-1", "T-3"),  # cycle back to the orphan
    )
    downstream = [
        task("T-2", Status.BACKLOG, owner_id="dev-1"),
        task("T-3", Status.BACKLOG, owner_id="dev-1"),
        task("T-4", Status.DONE, owner_id="dev-1"),
        task("T-5", Status.READY, owner_id="dev-1"),
    ]
    alert = act(task(), *downstream, deps=deps)[0]
    assert alert.params["dependents"] == 3
    assert alert.params["dependent_ids"] == ["T-2", "T-3", "T-5"]
    assert alert.severity is Severity.CRITICAL and alert.key.endswith(":critical")
    assert act(task(), *downstream[:1], deps=deps[:1])[0].severity is Severity.WARNING


def test_only_idle_agents_of_the_role_are_proposed() -> None:
    owners = (
        Owner("dev-a", "developer"),
        Owner("dev-b", "developer"),
        Owner("dev-c", "developer"),
        Owner("lead", "developer", OwnerKind.HUMAN),
        Owner("rev", "reviewer"),
    )
    busy_task = task("T-9", owner_id="dev-a", age=5)
    busy_session = Session("S-9", "T-8", "dev-b", NOW - 10 * M, last_heartbeat=NOW - M)
    alert, reassign = act(task(), busy_task, owners=owners, sessions=(busy_session,))
    assert reassign.params == {"owner_id": "dev-c"}
    assert "Candidate owner: dev-c" in alert.reason
    all_busy = (Owner("dev-a", "developer"),)
    [alert] = act(task(), busy_task, owners=all_busy)
    assert "No idle agent with role 'developer'" in alert.reason


def test_no_role_or_disabled_means_alert_only() -> None:
    no_role = Task("T-1", "t", Status.IN_PROGRESS, NOW - 60 * M)
    [alert] = act(no_role)
    assert "no role" in alert.reason
    [alert] = act(task(), rule=OrphanStatus(reassign=False))
    assert alert.reason.endswith("A human needs to pick it up.")


def test_build_from_config() -> None:
    rule = build(
        {
            "orphan_after": "45m",
            "queue_after": "3h",
            "critical_dependents": 5,
            "owned_statuses": ["in_progress", "accepted"],
            "queue_statuses": ["review"],
            "reassign": False,
            "reassign_unknown_owner": True,
            "session_stale_after": "1h",
        }
    )
    assert rule.orphan_after == 45 * M and rule.queue_after == 180 * M
    assert rule.owned_statuses == (Status.IN_PROGRESS, Status.ACCEPTED)
    assert rule.queue_statuses == (Status.REVIEW,)
    assert not rule.reassign and rule.reassign_unknown_owner


@pytest.mark.parametrize(
    "config",
    [
        {"orphan_after": [1]},
        {"orphan_after": "0s"},
        {"queue_after": -5},
        {"critical_dependents": 0},
        {"owned_statuses": "in_progress"},
        {"owned_statuses": ["done"]},
        {"owned_statuses": ["blocked"]},
        {"queue_statuses": ["nope"]},
        {"reassign": "yes"},
        {"after": "1m"},
    ],
)
def test_bad_config(config: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        build(config)


def test_hand_off_during_session_then_exit_is_rule_3s() -> None:
    # The developer moved the task to review mid-session and left without final_status:
    # rule 1 sees a declared hand-off, so the unpicked review queue is rule 3's.
    ended = Session("S-1", "T-1", "dev-2", NOW - 400 * M, ended_at=NOW - 290 * M)
    review = task(status=Status.REVIEW, age=300)
    assert kinds(act(review, sessions=(ended,))) == [ActionKind.ALERT, ActionKind.REASSIGN]


def test_one_idle_agent_gets_one_task_most_urgent_first() -> None:
    deps = tuple(Dependency(f"D-{i}", "T-2") for i in range(3))
    downstream = [task(f"D-{i}", Status.BACKLOG, owner_id="x") for i in range(3)]
    t1, t2 = task("T-1", age=300), task("T-2", age=60)  # T-2 is newer but blocks 3 tasks
    actions = act(t1, t2, *downstream, deps=deps)
    reassigns = [a for a in actions if a.kind is ActionKind.REASSIGN]
    assert [(a.task_id, a.params["owner_id"]) for a in reassigns] == [("T-2", "dev-1")]
    [t1_alert] = [a for a in actions if a.task_id == "T-1"]
    assert "No idle agent with role 'developer'" in t1_alert.reason


def test_a_task_routed_to_someone_is_not_ownerless() -> None:
    """Hermes reports the profile a review task is routed to as ``assignee`` until a run
    claims it: waiting for dispatch is not an orphan."""
    assert act(task(status=Status.REVIEW, age=300, assignee="dev-1")) == []
    assert act(task(age=300, assignee="dev-1")) == []
    gone = act(task(status=Status.REVIEW, age=300, assignee="dev-9"))
    assert gone and "assignee dev-9 is no longer known" in gone[0].reason


# --- e) a parent left open, f) a card stuck before work (incident 17) -------------------


def parent(status: Status = Status.READY, age: int = 600, **kw: object) -> Task:
    return task("P", status, age, subtasks=("S1", "S2"), **kw)


def done(tid: str, age: int) -> Task:
    return task(tid, Status.DONE, age)


def test_a_parent_whose_subtasks_are_done_is_asked_to_close() -> None:
    deps = (Dependency("W1", "P"), Dependency("W2", "W1"))
    tasks = (
        parent(),
        done("S1", 300),
        done("S2", 120),
        task("W1", Status.BACKLOG),
        task("W2", Status.BACKLOG),
    )
    alert, close = act(*tasks, deps=deps)
    assert alert.kind is ActionKind.ALERT and close.kind is ActionKind.CLOSE
    assert alert.params["pattern"] == "parent-left-open"
    assert alert.params["waiting_tasks"] == ["W1", "W2"] and alert.params["subtasks"] == [
        "S1",
        "S2",
    ]
    assert "close it?" in alert.reason and "the last 2h ago" in alert.reason
    assert alert.since == NOW - 120 * M
    assert close.task_id == "P" and close.params["pattern"] == "parent-left-open"
    assert (
        alert.params["merged_prs"]
        == close.params["merged_prs"]
        == [
            "example/repo#1",
            "example/repo#2",
        ]
    )
    assert "their PRs are merged (example/repo#1, example/repo#2)" in alert.reason


def test_parent_thresholds_and_exceptions() -> None:
    assert act(parent(), done("S1", 300), done("S2", 59)) == []  # the last one just finished
    assert act(parent(), done("S1", 300), task("S2", Status.IN_PROGRESS, owner_id="dev-1")) == []
    # S2 left the board: finished, but its PRs cannot be linked, so nothing is closed.
    assert patterns(act(parent(), done("S1", 300), closed=LANDED[:1])) == ["parent-unverified"]
    assert act(task("P", Status.BACKLOG, 600, subtasks=("S9",))) == []  # none known
    # None known, but ready: the platform woke it when they were done (past lookback).
    alone = act(task("P", Status.READY, 600, subtasks=("S9",)), closed=(merged_pr(3, "P"),))
    assert patterns(alone) == ["parent-unverified"]  # S9's own PR is unknown
    live = (Session("x", "P", "dev-1", NOW - 10 * M, last_heartbeat=NOW - M),)
    assert act(parent(), done("S1", 300), done("S2", 300), sessions=live) == []
    off = OrphanStatus(parent_close_after=None)
    assert act(parent(), done("S1", 300), done("S2", 300), rule=off) == []
    assert act(parent(Status.DONE), done("S1", 300), done("S2", 300)) == []


def test_a_parent_with_an_open_pr_of_its_own_is_not_done() -> None:
    tasks = (parent(), done("S1", 300), done("S2", 300))
    pr = PullRequest("example/repo", 7, "P: integration", "feature/P", task_ids=("P",))
    assert act(*tasks, prs=(pr,)) == []


def test_a_finished_parent_gets_no_other_alert() -> None:
    # In progress and ownerless for hours: the parent check speaks for it, once.
    found = act(parent(Status.IN_PROGRESS), done("S1", 300), done("S2", 300))
    assert [a.params.get("pattern") for a in found] == ["parent-left-open", "parent-left-open"]


def patterns(actions: list) -> list[str]:
    """The patterns of the alerts, after checking nothing proposes closing."""
    assert ActionKind.CLOSE not in kinds(actions)
    return [a.params.get("pattern") for a in actions]


def test_a_card_stuck_before_work_with_dependents_alerts() -> None:
    card = task("C", Status.BACKLOG, 21 * 60, prework=True)
    deps = (Dependency("D1", "C"), Dependency("D2", "C"))
    [alert] = act(card, task("D1", Status.BACKLOG), task("D2", Status.BACKLOG), deps=deps)
    assert alert.params["pattern"] == "stuck-before-work"
    assert alert.params["waiting_tasks"] == ["D1", "D2"]
    assert "21h" in alert.reason and "only a person moves it out of" in alert.reason
    assert act(card) == []  # nothing waits on it
    young = task("C", Status.BACKLOG, 3 * 60, prework=True)
    assert act(young, task("D1", Status.BACKLOG), deps=deps[:1]) == []
    plain = task("C", Status.BACKLOG, 21 * 60)  # an ordinary backlog card
    assert act(plain, task("D1", Status.BACKLOG), deps=deps[:1]) == []
    off = OrphanStatus(prework_after=None)
    assert act(card, task("D1", Status.BACKLOG), deps=deps[:1], rule=off) == []


def test_build_parent_and_prework_thresholds() -> None:
    rule = build({"parent_close_after": "30m", "prework_after": False})
    assert rule.parent_close_after == 30 * M and rule.prework_after is None


def test_a_parent_held_by_more_than_its_subtasks_is_not_done() -> None:
    kids = (done("S1", 300), done("S2", 300))
    # The orchestrator added work under it.
    extra = (task("C", Status.IN_PROGRESS, owner_id="dev-1"),)
    assert act(parent(), *kids, *extra, deps=(Dependency("P", "C"),)) == []
    # A subtask's PR is still open.
    pr = PullRequest("example/repo", 9, "S2: polish", "feature/S2", task_ids=("S2",))
    assert act(parent(), *kids, prs=(pr,)) == []


def test_a_parent_is_closed_only_when_every_pr_of_its_family_is_merged() -> None:
    """Incident 17, live review: subtasks done, their PRs returned for conflicts."""
    kids = (done("S1", 300), done("S2", 300))
    returned = merged_pr(4, "S2", merged=False)  # closed without merging
    [alert] = act(parent(), *kids, closed=(*LANDED, returned))
    assert patterns([alert]) == ["parent-prs-unmerged"]
    assert alert.params["unmerged_prs"] == ["example/repo#4"] and "close it?" not in alert.reason
    assert "not merged" in alert.reason and "their work has not landed" in alert.reason
    # A merged revert undoes the work.
    revert = replace(merged_pr(7, "S1"), title='Revert "S1: work"')
    assert patterns(act(parent(), *kids, closed=(*LANDED, revert))) == ["parent-prs-unmerged"]
    # An open PR of the family: the parent is not finished at all.
    open_pr = PullRequest("example/repo", 5, "S1: follow-up", "feature/S1-b", task_ids=("S1",))
    assert act(parent(), *kids, prs=(open_pr,)) == []


@pytest.mark.parametrize(
    ("kw", "gap"),
    [
        ({"complete": False}, "the code host's PRs are not fully known"),
        ({"complete": None}, "the code host's PRs are not fully known"),
        ({"closed": ()}, "no merged PR for S1, S2"),
        ({"closed": LANDED[:1]}, "no merged PR for S2"),
        ({"closed": (replace(LANDED[0], into_default=False), LANDED[1])}, "no merged PR for S1"),
    ],
)
def test_a_parent_whose_work_is_not_known_to_have_landed_is_not_closed(
    kw: dict[str, object], gap: str
) -> None:
    [alert] = act(parent(), done("S1", 300), done("S2", 300), **kw)  # type: ignore[arg-type]
    assert patterns([alert]) == ["parent-unverified"]
    assert gap in alert.reason and "check its PRs" in alert.reason


def test_one_merged_pr_can_cover_several_subtasks() -> None:
    found = act(parent(), done("S1", 300), done("S2", 300), closed=(merged_pr(6, "S1", "S2"),))
    assert ActionKind.CLOSE in kinds(found)
    unknown_target = replace(merged_pr(6, "S1", "S2"), into_default=None)
    found = act(parent(), done("S1", 300), done("S2", 300), closed=(unknown_target,))
    assert patterns(found) == ["parent-unverified"]


@pytest.mark.parametrize("status", [Status.BLOCKED, Status.WAITING, Status.REVIEW])
def test_a_parent_with_a_reason_of_its_own_is_not_asked_to_close(status: Status) -> None:
    found = act(parent(status), done("S1", 300), done("S2", 300))
    assert all(a.params.get("pattern") != "parent-left-open" for a in found)


def test_the_close_timer_starts_after_the_parents_own_last_run() -> None:
    kids = (done("S1", 300), done("S2", 300))
    run = (Session("r", "P", "dev-1", NOW - 90 * M, ended_at=NOW - 20 * M),)
    assert act(parent(), *kids, sessions=run) == []  # its last run ended 20 minutes ago
    later = (Session("r", "P", "dev-1", NOW - 3 * 60 * M, ended_at=NOW - 2 * 60 * M),)
    assert act(parent(), *kids, sessions=later)


def test_the_alert_announces_its_close() -> None:
    from kelpie.core.alerts import ANNOUNCES

    alert, close = act(parent(), done("S1", 300), done("S2", 300))
    assert alert.params[ANNOUNCES] == close.key


def test_a_pre_work_card_waiting_for_its_own_parents_is_queued() -> None:
    card = task("C", Status.BACKLOG, 21 * 60, prework=True)
    deps = (Dependency("D1", "C"), Dependency("C", "U"))
    behind = (task("D1", Status.BACKLOG), task("U", Status.IN_PROGRESS, owner_id="dev-1"))
    assert act(card, *behind, deps=deps) == []
