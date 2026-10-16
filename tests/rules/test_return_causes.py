from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.model import Return, Snapshot, Status, Task
from kelpie.rules import REGISTRY
from kelpie.rules.return_causes import ReturnCauses, build

NOW = datetime(2026, 3, 2, 17, tzinfo=UTC)
H = timedelta(hours=1)


def ret(reason: str, ago: timedelta = H, category: str | None = None) -> Return:
    return Return(NOW - ago, reason, category)


def task(
    tid: str, *returns: Return, files: tuple[str, ...] = (), status: Status = Status.DONE
) -> Task:
    return Task(tid, tid, status, NOW - H, "dev", files=files, returns=returns)


def run(*tasks: Task, rule: ReturnCauses | None = None) -> list:
    return list((rule or ReturnCauses()).evaluate(Snapshot(now=NOW, tasks=tasks)))


ROUTES = "src/app/routes.py"
CONFLICTS = (
    task(
        "A",
        ret("merge conflict in src/app/routes.py"),
        ret("Merge conflict in routes.py"),
        files=(ROUTES, "src/app/a.py"),
    ),
    task("B", ret("CONFLICT (content): Merge conflict in src/app/routes.py"), ret("missing tests")),
    task("C", ret("conflicts with main: src/app/types.py"), ret("lint", category="ci")),
)


def test_registered() -> None:
    assert isinstance(REGISTRY["return_causes"]({}), ReturnCauses)


def test_dominant_conflicts_on_a_hot_file_alert_and_warn_live_tasks() -> None:
    live = task("L", files=("./src/app/routes.py", "x.py"), status=Status.IN_PROGRESS)
    other = task("M", files=("src/app/types.py",), status=Status.IN_PROGRESS)
    alert, comment = run(*CONFLICTS, live, other)
    assert alert.kind is ActionKind.ALERT and alert.state == "return_causes:hot-files"
    assert alert.params["hot_files"] == [ROUTES]
    assert alert.params["conflicts_by_file"] == {ROUTES: 3}
    assert alert.params["returns"] == 6 and alert.params["conflicts"] == 4
    assert alert.params["shares"] == {"ci": 0.17, "merge_conflict": 0.67, "other": 0.17}
    assert "4 of 6 task returns in the last 24h (67%)" in alert.reason
    assert comment.kind is ActionKind.COMMENT and comment.task_id == "L" and comment.once
    assert comment.params["hot_files"] == [ROUTES]


def test_the_reason_beats_the_category() -> None:
    rule = ReturnCauses()
    assert rule.cause(ret("merge conflict in a.py", category="review")) == "merge_conflict"
    assert rule.cause(ret("needs tests", category="review")) == "review"
    assert rule.cause(ret("needs tests")) == "other"


def findings(actions: list) -> list[str]:
    return [a.params.get("finding") for a in actions if a.kind is ActionKind.ALERT]


def test_thresholds() -> None:
    assert run(*CONFLICTS, rule=ReturnCauses(min_returns=7)) == []
    assert findings(run(*CONFLICTS, rule=ReturnCauses(conflict_ratio_threshold=0.7))) == [
        "dominant_cause"
    ]
    assert findings(run(*CONFLICTS, rule=ReturnCauses(hot_file_min_conflicts=4))) == [
        "dominant_cause"
    ]
    quiet = ReturnCauses(hot_file_min_conflicts=4, dominant_ratio=0.7)
    assert run(*CONFLICTS, rule=quiet) == []
    old = task("O", ret("merge conflict in src/app/routes.py", ago=30 * H))
    assert [a.params["returns"] for a in run(*CONFLICTS, old)[:1]] == [6]  # outside the window


def test_expected_shared_files_are_never_hot() -> None:
    rule = ReturnCauses(expected_shared=("src/app/*",))
    assert findings(run(*CONFLICTS, rule=rule)) == ["dominant_cause"]


def test_conflicts_spread_over_many_files_only_report_the_shares() -> None:
    spread = tuple(task(f"T{i}", ret(f"merge conflict in src/m{i}.py")) for i in range(6))
    [alert] = run(*spread)
    assert alert.params["dominant_cause"] == "merge_conflict" and alert.severity == "info"
    assert "spread over many files" in alert.reason


def test_another_dominant_cause_is_reported() -> None:
    reviews = tuple(task(f"T{i}", ret("needs tests", category="review")) for i in range(5))
    [alert] = run(*reviews, task("X", ret("why not")))
    assert alert.params["dominant_cause"] == "review" and alert.params["shares"]["review"] == 0.83
    others = tuple(task(f"T{i}", ret("no idea")) for i in range(6))
    assert run(*others) == []  # "other" is no finding


def test_custom_cause_patterns() -> None:
    rule = build({"cause_patterns": {"flaky": [r"flaky"]}})
    assert rule.cause(ret("flaky e2e")) == "flaky"
    assert rule.cause(ret("merge conflict in x.py")) == "merge_conflict"  # defaults kept


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"window": "0s"}, "window"),
        ({"min_returns": 0}, "min_returns"),
        ({"conflict_ratio_threshold": 1.5}, "conflict_ratio_threshold"),
        ({"dominant_ratio": 0}, "dominant_ratio"),
        ({"expected_shared": [""]}, "expected_shared"),
        ({"cause_patterns": {"x": ["("]}}, "cause_patterns"),
        ({"cause_patterns": ["x"]}, "cause_patterns"),
        ({"nope": 1}, "unknown"),
    ],
)
def test_build_rejects(config: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build(config)


def test_build_values() -> None:
    rule = build(
        {
            "window": "12h",
            "min_returns": 3,
            "conflict_ratio_threshold": 1,
            "hot_file_min_conflicts": 2,
            "expected_shared": ["CHANGELOG.md"],
        }
    )
    assert rule.window == 12 * H and rule.min_returns == 3 and rule.hot_file_min_conflicts == 2
    assert rule.conflict_ratio_threshold == 1.0 and rule.expected_shared == ("CHANGELOG.md",)


def test_words_that_are_not_files_never_become_hot() -> None:
    reasons = (
        "merge conflict in origin/main",
        "merge conflict in v1.2.3 bump, e.g. after the rebase",
        "merge conflict in https://example.com/pr/12",
        "merge conflict in 3.14",
    )
    tasks = tuple(task(f"T{i}", ret(r)) for i, r in enumerate(reasons * 2))
    assert findings(run(*tasks)) == ["dominant_cause"]  # conflicts, but no file to act on


def test_review_prose_is_not_a_merge_conflict() -> None:
    rule = ReturnCauses()
    assert rule.cause(ret("No merge conflicts; this conflicts with the API design doc")) == "other"
    assert rule.cause(ret("CONFLICT (modify/delete): a.py deleted in HEAD")) == "merge_conflict"
    assert rule.cause(ret("this branch conflicts with main")) == "merge_conflict"


def test_bare_file_names_resolve_across_tasks() -> None:
    owner = task("O", files=(ROUTES,), status=Status.REVIEW)
    bare = tuple(task(f"B{i}", ret("merge conflict in routes.py")) for i in range(5))
    live = task("L", files=(ROUTES,), status=Status.IN_PROGRESS)
    alert, comment = run(owner, *bare, live)
    assert alert.params["hot_files"] == [ROUTES] and comment.task_id == "L"


def test_an_ambiguous_bare_name_stays_bare_and_matches_no_live_task() -> None:
    a = task("A", files=("src/a/util.py",), status=Status.REVIEW)
    b = task("B", files=("src/b/util.py",), status=Status.REVIEW)
    bare = tuple(task(f"X{i}", ret("merge conflict in util.py")) for i in range(5))
    live = task("L", files=("src/a/util.py",), status=Status.IN_PROGRESS)
    [alert] = run(a, b, *bare, live)
    assert alert.params["hot_files"] == ["util.py"]


def test_returns_after_now_do_not_count() -> None:
    late = task("F", ret("merge conflict in src/app/routes.py", ago=-H))
    assert run(*CONFLICTS, late)[0].params["returns"] == 6


def test_a_new_hot_file_changes_the_alert_key() -> None:
    first = run(*CONFLICTS)[0]
    more = tuple(task(f"N{i}", ret("merge conflict in src/app/types.py")) for i in range(3))
    second = run(*CONFLICTS, *more)[0]
    assert first.state == second.state and first.key != second.key
    assert second.params["hot_files"] == [ROUTES, "src/app/types.py"]


def test_short_windows_read_well() -> None:
    rule = ReturnCauses(window=timedelta(minutes=30), min_returns=1)
    tasks = (task("A", ret("merge conflict in src/x.py", ago=timedelta(minutes=5))),)
    assert "in the last 30m" in run(*tasks, rule=rule)[0].reason
