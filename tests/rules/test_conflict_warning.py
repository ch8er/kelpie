from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind
from kelpie.core.conditions import check_task_status
from kelpie.core.executors import wait_conditions
from kelpie.core.model import Condition, ConditionKind, Dependency, Session, Snapshot, Status, Task
from kelpie.rules import REGISTRY
from kelpie.rules.conflict_warning import ConflictWarning, build
from kelpie.rules.wait_condition import WaitCondition

NOW = datetime(2026, 3, 2, 17, tzinfo=UTC)
M = timedelta(minutes=1)
SERIAL = ConflictWarning(serialize=True)


def task(
    tid: str, *files: str, status: Status = Status.IN_PROGRESS, since: timedelta = 60 * M
) -> Task:
    return Task(tid, f"title {tid}", status, NOW - since, "developer", files=files)


def run(
    *tasks: Task,
    rule: ConflictWarning | None = None,
    sessions: tuple[Session, ...] = (),
    deps: tuple[Dependency, ...] = (),
    conditions: tuple[Condition, ...] = (),
) -> list[Action]:
    snapshot = Snapshot(
        NOW, tasks=tasks, sessions=sessions, dependencies=deps, conditions=conditions
    )
    return list((rule or ConflictWarning()).evaluate(snapshot))


def comments(actions: list[Action]) -> dict[tuple[str, str], Action]:
    return {
        (str(a.task_id), a.params["conflicting_task"]): a
        for a in actions
        if a.kind is ActionKind.COMMENT
    }


def of(actions: list[Action], kind: ActionKind) -> dict[str, Action]:
    return {str(a.task_id): a for a in actions if a.kind is kind}


def test_registered() -> None:
    assert isinstance(REGISTRY["conflict_warning"]({}), ConflictWarning)


# --- comments --------------------------------------------------------------------------


def test_overlapping_live_tasks_both_get_one_comment() -> None:
    found = comments(run(task("A", "x.py", "y.py"), task("B", "./y.py", "x.py", "z.py")))
    assert set(found) == {("A", "B"), ("B", "A")}
    a = found[("A", "B")]
    assert a.once and a.key == "conflict_warning:A:B"
    assert a.params == {"conflicting_task": "B", "files": ["x.py", "y.py"]}
    assert "live task B ('title B', in_progress) changes the same files" in a.reason


@pytest.mark.parametrize(
    "status", [Status.IN_PROGRESS, Status.REVIEW, Status.ACCEPTED, Status.BLOCKED, Status.WAITING]
)
def test_every_unmerged_status_is_live(status: Status) -> None:
    found = comments(run(task("A", "x.py"), task("B", "x.py", status=status)))
    assert set(found) == {("A", "B"), ("B", "A")}


@pytest.mark.parametrize(
    "status", [Status.BACKLOG, Status.READY, Status.DONE, Status.FAILED, Status.CANCELLED]
)
def test_tasks_not_in_flight_do_not_conflict(status: Status) -> None:
    assert run(task("A", "x.py"), task("B", "x.py", status=status)) == []


def test_disjoint_files_and_no_files_are_quiet() -> None:
    assert run(task("A", "x.py"), task("B", "y.py"), task("C"), task("D", "", " ")) == []


def test_expected_shared_files_are_ignored() -> None:
    rule = ConflictWarning(expected_shared=("CHANGELOG.md", "docs/*"))
    shared = ("CHANGELOG.md", "docs/a/b.md")
    assert run(task("A", *shared), task("B", *shared), rule=rule) == []
    both = ("CHANGELOG.md", "src/x.py")
    [a, _] = run(task("A", *both), task("B", *both), rule=rule)
    assert a.params["files"] == ["src/x.py"]


def test_three_way_overlap_comments_once_per_pair() -> None:
    tasks = (task("A", "x.py"), task("B", "x.py"), task("C", "x.py", "y.py"), task("D", "y.py"))
    assert set(comments(run(*tasks))) == {
        ("A", "B"), ("A", "C"), ("B", "A"), ("B", "C"),
        ("C", "A"), ("C", "B"), ("C", "D"), ("D", "C"),
    }  # fmt: skip


def test_comment_key_is_the_pair_whatever_the_files_or_other_tasks() -> None:
    a, b = task("A", "x.py", since=90 * M), task("C", "x.py", since=30 * M)
    first = comments(run(a, b))[("A", "C")]
    grown = comments(run(replace(a, files=("x.py", "q.py")), replace(b, files=("x.py", "q.py"))))
    assert grown[("A", "C")].key == first.key  # the ledger sends it once
    middle = task("B", "x.py", since=60 * M)  # started between them, joins later
    found = comments(run(a, b, middle))
    assert found[("A", "B")].key == "conflict_warning:A:B"  # A is told about B too
    assert found[("A", "C")].key == first.key


def test_long_file_lists_are_shortened_in_the_message_only() -> None:
    files = [f"f{i}.py" for i in range(8)]
    [a, _] = run(task("A", *files), task("B", *files))
    assert len(a.params["files"]) == 8 and "+3 more" in a.reason


# --- serialization -----------------------------------------------------------------------


def test_serialization_is_off_by_default() -> None:
    assert of(run(task("A", "x.py"), task("B", "x.py")), ActionKind.WAIT) == {}


def test_later_task_waits_for_the_earlier_ones() -> None:
    tasks = (
        task("A", "x.py", since=90 * M),
        task("B", "x.py", "y.py", since=60 * M),
        task("C", "y.py", "x.py", since=30 * M),
    )
    waits = of(run(*tasks, rule=SERIAL), ActionKind.WAIT)
    assert set(waits) == {"B", "C"}
    c = waits["C"]
    assert c.once and c.params["waits_for"] == ["A", "B"] and c.params["files"] == ["x.py", "y.py"]
    assert c.params["conditions"] == [
        {
            "id": f"conflict_warning:C:{o}",
            "kind": "task_status",
            "params": {
                "task": o,
                "status": "done",
                "source": "conflict_warning",
                "terminal_ok": True,
            },
        }
        for o in ("A", "B")
    ]


def test_wait_comes_before_the_comment_for_the_rate_limit() -> None:
    actions = run(task("A", "x.py", since=90 * M), task("B", "x.py", since=10 * M), rule=SERIAL)
    kinds = [a.kind for a in actions if a.task_id == "B"]
    assert kinds == [ActionKind.WAIT, ActionKind.COMMENT]


def test_mixed_wait_with_an_ended_target_is_still_a_dead_end_for_rule_2() -> None:
    b, conditions = parked("A")
    other = Condition("C-ci", ConditionKind.CI_STATUS, "B", {}, registered_at=NOW - 20 * M)
    snapshot = check_task_status(
        Snapshot(
            NOW, tasks=(b, task("A", status=Status.CANCELLED)), conditions=(*conditions, other)
        )
    )
    assert ConflictWarning().evaluate(snapshot) == []  # not only ours: no release
    assert [a.kind for a in WaitCondition().evaluate(snapshot)] == [ActionKind.ALERT]


def test_start_is_the_first_session_and_ties_go_by_id() -> None:
    # B entered in_progress earlier, but A's work began first.
    sessions = (
        Session("S-A", "A", "d1", NOW - 180 * M),
        Session("S-B", "B", "d2", NOW - 120 * M),
    )
    tasks = (task("A", "x.py", since=10 * M), task("B", "x.py", since=50 * M))
    assert set(of(run(*tasks, rule=SERIAL, sessions=sessions), ActionKind.WAIT)) == {"B"}
    tie = of(run(task("A", "x.py"), task("B", "x.py"), rule=SERIAL), ActionKind.WAIT)
    assert set(tie) == {"B"}


@pytest.mark.parametrize("status", [Status.REVIEW, Status.ACCEPTED, Status.WAITING, Status.BLOCKED])
def test_only_tasks_in_progress_are_parked(status: Status) -> None:
    later = task("B", "x.py", status=status, since=10 * M)
    assert of(run(task("A", "x.py", since=90 * M), later, rule=SERIAL), ActionKind.WAIT) == {}


@pytest.mark.parametrize("status", [Status.WAITING, Status.BLOCKED])
def test_never_parked_behind_a_task_that_is_not_moving(status: Status) -> None:
    # B waits (maybe on A itself): parking A behind it could deadlock both.
    tasks = (task("A", "x.py", since=10 * M), task("B", "x.py", status=status, since=120 * M))
    assert of(run(*tasks, rule=SERIAL), ActionKind.WAIT) == {}


def test_never_parked_behind_a_task_that_depends_on_it() -> None:
    tasks = (task("A", "x.py", since=90 * M), task("B", "x.py", since=10 * M), task("M"))
    deps = (Dependency("M", "B"), Dependency("A", "M"))  # A needs B done first, via M
    assert of(run(*tasks, rule=SERIAL, deps=deps), ActionKind.WAIT) == {}


def parked(*targets: str, since: timedelta = 20 * M) -> tuple[Task, tuple[Condition, ...]]:
    """B, parked by this rule behind ``targets``, as an adapter registers the wait."""
    b = task("B", "x.py", status=Status.WAITING, since=since)
    conditions = tuple(
        Condition(
            f"conflict_warning:B:{t}",
            ConditionKind.TASK_STATUS,
            "B",
            {"task": t, "status": "done", "source": "conflict_warning", "terminal_ok": True},
            registered_at=NOW - since,
        )
        for t in targets
    )
    return b, conditions


@pytest.mark.parametrize(
    ("statuses", "wakes"),
    [
        ((Status.IN_PROGRESS,), False),  # still a conflict
        ((Status.DONE,), False),  # rule 2's: all done
        ((Status.CANCELLED,), True),
        ((Status.READY,), False),  # requeued: still holds its branch, keep waiting
        ((Status.BACKLOG,), False),
        ((Status.DONE, Status.FAILED), True),
        ((Status.DONE, Status.REVIEW), False),
    ],
)
def test_release_when_the_tasks_waited_for_left_the_live_set(
    statuses: tuple[Status, ...], wakes: bool
) -> None:
    targets = tuple(f"A{i}" for i in range(len(statuses)))
    b, conditions = parked(*targets)
    others = tuple(task(t, "y.py", status=s) for t, s in zip(targets, statuses, strict=True))
    woken = of(run(b, *others, conditions=conditions), ActionKind.WAKE)
    assert bool(woken) is wakes
    if wakes:
        assert woken["B"].params["waited_for"] == list(targets)


def test_ended_target_is_not_a_dead_end_for_rule_2() -> None:
    b, conditions = parked("A")
    snapshot = check_task_status(
        Snapshot(NOW, tasks=(b, task("A", status=Status.CANCELLED)), conditions=conditions)
    )
    assert snapshot.conditions[0].params["terminal"] is True
    assert WaitCondition().evaluate(snapshot) == []  # no "nothing will wake it" alert
    [wake] = ConflictWarning().evaluate(snapshot)
    assert wake.kind is ActionKind.WAKE


def test_release_when_the_task_waited_for_is_gone() -> None:
    b, conditions = parked("A")
    [wake] = run(b, conditions=conditions)
    assert wake.kind is ActionKind.WAKE and "A (gone)" in wake.reason


def test_no_release_for_waits_that_are_not_only_ours() -> None:
    b, conditions = parked("A")
    other = Condition("C-ci", ConditionKind.CI_STATUS, "B", {}, registered_at=NOW - 20 * M)
    assert run(b, conditions=(*conditions, other)) == []
    # Leftovers of an earlier wait (registered before the current one) do not count.
    old = tuple(replace(c, registered_at=NOW - 2 * 60 * M) for c in conditions)
    assert run(b, conditions=old) == []


def test_park_then_wake_end_to_end() -> None:
    """wait -> adapter registers the conditions -> task_status step -> rule 2 wakes."""
    a, b = task("A", "x.py", since=90 * M), task("B", "x.py", since=30 * M)
    [wait] = of(run(a, b, rule=SERIAL), ActionKind.WAIT).values()
    registered = tuple(replace(c, registered_at=NOW) for c in wait_conditions(wait))
    b_waiting = replace(b, status=Status.WAITING, status_since=NOW)
    later = NOW + 30 * M
    done = replace(a, status=Status.DONE, status_since=NOW + 20 * M)
    snapshot = check_task_status(Snapshot(later, tasks=(done, b_waiting), conditions=registered))
    [cond] = snapshot.conditions
    assert cond.satisfied is True and cond.checked_at == later
    [wake] = WaitCondition().evaluate(snapshot)
    assert wake.kind is ActionKind.WAKE and wake.task_id == "B"
    assert ConflictWarning(serialize=True).evaluate(snapshot) == []  # no second wake


# --- config ------------------------------------------------------------------------------


def test_build_validates_config() -> None:
    assert build({"expected_shared": ["CHANGELOG.md"], "serialize": True}) == ConflictWarning(
        ("CHANGELOG.md",), True
    )
    assert build({}) == ConflictWarning()


@pytest.mark.parametrize(
    "config",
    [
        {"expected_shared": "CHANGELOG.md"},
        {"expected_shared": [""]},
        {"expected_shared": [1]},
        {"serialize": "yes"},
        {"typo": 1},
    ],
)
def test_build_rejects_bad_config(config: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        build(config)
