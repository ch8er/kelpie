from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import Session, Snapshot, Status, Task
from kelpie.rules import REGISTRY
from kelpie.rules.conflict_warning import ConflictWarning, build

NOW = datetime(2026, 3, 2, 17, tzinfo=UTC)
M = timedelta(minutes=1)


def task(
    tid: str, *files: str, status: Status = Status.IN_PROGRESS, since: timedelta = 60 * M
) -> Task:
    return Task(tid, f"title {tid}", status, NOW - since, "developer", files=files)


def run(
    *tasks: Task, rule: ConflictWarning | None = None, sessions: tuple[Session, ...] = ()
) -> list[Action]:
    return list((rule or ConflictWarning()).evaluate(Snapshot(NOW, tasks=tasks, sessions=sessions)))


def of(actions: list[Action], kind: ActionKind) -> dict[str, Action]:
    return {a.task_id: a for a in actions if a.kind is kind and a.task_id}


def test_registered() -> None:
    assert isinstance(REGISTRY["conflict_warning"]({}), ConflictWarning)


def test_overlapping_live_tasks_both_get_one_comment() -> None:
    comments = of(
        run(task("A", "x.py", "y.py"), task("B", "y.py", "x.py", "z.py")), ActionKind.COMMENT
    )
    assert set(comments) == {"A", "B"}
    a = comments["A"]
    assert a.once and a.params == {"conflicting_tasks": ["B"], "files": ["x.py", "y.py"]}
    assert "B ('title B', in_progress) changes x.py, y.py" in a.reason


@pytest.mark.parametrize(
    "status", [Status.IN_PROGRESS, Status.REVIEW, Status.ACCEPTED, Status.BLOCKED, Status.WAITING]
)
def test_every_unmerged_status_is_live(status: Status) -> None:
    assert set(
        of(run(task("A", "x.py"), task("B", "x.py", status=status)), ActionKind.COMMENT)
    ) == {"A", "B"}


@pytest.mark.parametrize(
    "status", [Status.BACKLOG, Status.READY, Status.DONE, Status.FAILED, Status.CANCELLED]
)
def test_tasks_not_in_flight_do_not_conflict(status: Status) -> None:
    assert run(task("A", "x.py"), task("B", "x.py", status=status)) == []


def test_disjoint_files_and_no_files_are_quiet() -> None:
    assert run(task("A", "x.py"), task("B", "y.py"), task("C"), task("D", "")) == []


def test_expected_shared_files_are_ignored() -> None:
    rule = ConflictWarning(expected_shared=("CHANGELOG.md", "docs/*"))
    assert (
        run(
            task("A", "CHANGELOG.md", "docs/a/b.md"),
            task("B", "CHANGELOG.md", "docs/a/b.md"),
            rule=rule,
        )
        == []
    )
    [a, _] = run(
        task("A", "CHANGELOG.md", "src/x.py"), task("B", "CHANGELOG.md", "src/x.py"), rule=rule
    )
    assert a.params["files"] == ["src/x.py"]


def test_three_way_overlap_lists_every_other_task() -> None:
    comments = of(
        run(task("A", "x.py"), task("B", "x.py"), task("C", "x.py", "y.py"), task("D", "y.py")),
        ActionKind.COMMENT,
    )
    assert comments["A"].params["conflicting_tasks"] == ["B", "C"]
    assert comments["C"].params == {"conflicting_tasks": ["A", "B", "D"], "files": ["x.py", "y.py"]}


def test_comment_key_changes_only_when_the_overlaps_change() -> None:
    first = of(run(task("A", "x.py"), task("B", "x.py")), ActionKind.COMMENT)["A"]
    again = of(run(task("A", "x.py", "q.py"), task("B", "x.py")), ActionKind.COMMENT)["A"]
    more = of(run(task("A", "x.py"), task("B", "x.py"), task("C", "x.py")), ActionKind.COMMENT)["A"]
    assert first.key == again.key != more.key


def test_long_lists_are_shortened_in_the_message_only() -> None:
    files = [f"f{i}.py" for i in range(8)]
    others = [task(f"O{i}", "hot.py") for i in range(7)]
    comments = of(run(task("A", "hot.py", *files), task("B", *files), *others), ActionKind.COMMENT)
    a = comments["A"]
    assert len(a.params["conflicting_tasks"]) == 8 and "+3 more task(s)" in a.reason
    assert "+3 more" in comments["B"].reason  # 8 shared files, 5 named


def test_serialization_is_off_by_default() -> None:
    assert of(run(task("A", "x.py"), task("B", "x.py")), ActionKind.WAIT) == {}


def test_later_task_waits_for_the_earlier_ones() -> None:
    rule = ConflictWarning(serialize=True)
    tasks = (
        task("A", "x.py", since=90 * M),
        task("B", "x.py", "y.py", since=60 * M),
        task("C", "y.py", "x.py", since=30 * M),
    )
    waits = of(run(*tasks, rule=rule), ActionKind.WAIT)
    assert set(waits) == {"B", "C"}
    c = waits["C"]
    assert c.params["waits_for"] == ["A", "B"] and c.params["files"] == ["x.py", "y.py"]
    assert c.params["conditions"] == [
        {
            "id": "conflict_warning:C:A",
            "kind": "task_status",
            "params": {"task": "A", "status": "done", "source": "conflict_warning"},
        },
        {
            "id": "conflict_warning:C:B",
            "kind": "task_status",
            "params": {"task": "B", "status": "done", "source": "conflict_warning"},
        },
    ]
    assert not c.once and c.key == "conflict_warning:wait:C:A,B"


def test_start_is_the_first_session_and_ties_go_by_id() -> None:
    rule = ConflictWarning(serialize=True)
    # B entered in_progress earlier, but A's work began first.
    sessions = (
        Session("S-A", "A", "d1", NOW - 3 * 60 * M),
        Session("S-B", "B", "d2", NOW - 2 * 60 * M),
    )
    waits = of(
        run(
            task("A", "x.py", since=10 * M),
            task("B", "x.py", since=50 * M),
            rule=rule,
            sessions=sessions,
        ),
        ActionKind.WAIT,
    )
    assert set(waits) == {"B"}
    tie = of(run(task("A", "x.py"), task("B", "x.py"), rule=rule), ActionKind.WAIT)
    assert set(tie) == {"B"}


@pytest.mark.parametrize("status", [Status.REVIEW, Status.ACCEPTED, Status.WAITING, Status.BLOCKED])
def test_only_tasks_in_progress_are_parked(status: Status) -> None:
    rule = ConflictWarning(serialize=True)
    later = task("B", "x.py", status=status, since=10 * M)
    assert of(run(task("A", "x.py", since=90 * M), later, rule=rule), ActionKind.WAIT) == {}


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
