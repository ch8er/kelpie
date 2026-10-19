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
    assert rule.cause(ret("needs tests", category="review")) == "review_findings"
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
    assert alert.params["dominant_cause"] == "review_findings"
    assert alert.params["shares"]["review_findings"] == 0.83
    assert "were review findings (by cause: review findings 83%, other 17%)" in alert.reason
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


# --- flaky tests (incident 18) --------------------------------------------------------------


def test_a_test_that_sends_several_tasks_back_is_flaky() -> None:
    found = run(
        task("T-1", ret("CI red: test_login_timeout failed")),
        task("T-2", ret("returned: test_login_timeout flaked again")),
        task("T-3", ret("test_upload failed: wrong size")),  # its own test
    )
    [alert] = found
    assert alert.params == {
        "finding": "flaky_test",
        "test": "test_login_timeout",
        "tasks": ["T-1", "T-2"],
        "headline": "Flaky test test_login_timeout sent 2 tasks back",
    }
    assert "fails other tasks' work" in alert.reason and alert.state


def test_flaky_returns_are_their_own_cause_and_xctest_names_count() -> None:
    xc = "LoginTests.testTimeout failed on CI"
    tasks = [task(f"T-{i}", ret(xc, category="review")) for i in range(5)]
    tasks.append(task("T-9", ret("changes requested", category="review")))
    found = run(*tasks)
    assert [a.params["finding"] for a in found] == ["flaky_test"]  # no "dominant" duplicate
    assert found[0].params["test"] == "LoginTests.testTimeout"
    assert (
        run(*tasks, rule=ReturnCauses(flaky_min_tasks=6))[0].params["finding"] == "dominant_cause"
    )


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"flaky_min_tasks": 1}, "at least 2"),
        ({"test_names": ["test_\\w+"]}, "named group 'test'"),
        ({"test_names": "x"}, "list of regular expressions"),
        ({"test_names": ["("]}, "invalid pattern"),
    ],
)
def test_flaky_config_is_validated(config: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build(config)
    assert build({"test_names": [r"(?P<test>\w+Spec)"], "flaky_min_tasks": 3}).flaky_min_tasks == 3


def test_test_files_in_conflicts_and_review_advice_are_not_flaky_tests() -> None:
    conflict = "merge conflict in tests/test_models.py"
    advice = "add a test like test_parse_empty"
    found = run(*(task(f"C-{i}", ret(conflict)) for i in range(3)))
    assert all(a.params.get("finding") != "flaky_test" for a in found)
    assert run(task("A-1", ret(advice)), task("A-2", ret(advice))) == []
    named = "failed: test_login_timeout."
    [alert] = run(task("F-1", ret(named)), task("F-2", ret(named)))
    assert alert.params["test"] == "test_login_timeout"


def test_pytest_summary_lines_and_unrelated_failures() -> None:
    line = "FAILED tests/unit/test_api.py::TestUser::test_create - AssertionError"
    [alert] = run(task("P-1", ret(line)), task("P-2", ret(line)))
    assert alert.params["test"] == "test_create"
    advice = "Please add test_parse_empty; lint failed on CI"
    assert run(task("A-1", ret(advice)), task("A-2", ret(advice))) == []


@pytest.mark.parametrize(
    ("reason", "flaky"),
    [
        ("Added test_parse_empty. Lint failed on CI.", False),  # another sentence
        ("Added test_parse_empty! CI is red.", False),
        ("test_login_timeout\nfailed 3 times in a row", True),  # the name on its own line
        ("- test_login_timeout:\n  timed out on CI", True),
        ("test_login_timeout\nadded; lint failed", False),
        ("LoginTests.testTimeout failed again.", True),  # a dot inside a name
        ("New tests:\ntest_parse_empty\nCI failed: mypy", False),  # the next line is no verdict
        ("Added tests:\n- test_a\n- test_b\nLint failed on CI", False),
        ("Tests failed:\n- test_login\n- test_logout", True),  # a list under its header
        ("Flaky:\n  test_login_timeout\n  test_signup", True),
        ("test_login (tests.test_auth.LoginTests) ... FAIL", True),  # no sentence end
        ("test_login_timeout\r\nfailed 3 times", True),  # CRLF
        ("test_login_timeout is fixed, but test_signup is flaky", True),
    ],
)
def test_failure_wording_must_be_in_the_names_clause(reason: str, flaky: bool) -> None:
    found = run(task("F-1", ret(reason)), task("F-2", ret(reason)))
    assert any(a.params.get("finding") == "flaky_test" for a in found) is flaky


def test_only_the_names_a_clause_says_failed() -> None:
    rule = ReturnCauses()
    assert rule._tests(ret("New tests:\n- test_a\n- test_b\nFailed:\n- test_c")) == {"test_c"}
    assert rule._tests(ret("Failing on CI:\n- test_login: AssertionError")) == {"test_login"}
    assert rule._tests(ret("Tests failed:\n- test_login (1/20)")) == {"test_login"}
    assert rule._tests(ret("Tests failed:\n\n- test_a\n- test_b")) == {"test_a", "test_b"}
    assert rule._tests(ret("Fixed the flaky tests:\n- test_login")) == set()
    assert rule._tests(ret("Added test_a... lint failed on CI")) == set()
    assert rule._tests(ret("test_a fixed, and test_b failed")) == {"test_b"}
    assert rule._tests(ret("test_y ... FAILED")) == {"test_y"}
    for quiet in ["Fixed TypeError in test_parse; lint failed",
                  "test_parse passed with no error; lint failed",
                  "test_parse: no error. Coverage dropped."]:  # fmt: skip
        assert rule._tests(ret(quiet)) == set(), quiet
    assert rule._tests(ret("New failures:\n- test_a\n- test_b")) == {"test_a", "test_b"}
    assert rule._tests(ret("Tests failed after fix:\n- test_a")) == {"test_a"}
    assert rule._tests(ret("test_login failed, not fixed yet")) == {"test_login"}
    assert rule._tests(ret("test_login failed even though I fixed the timeout")) == {"test_login"}
    for text in [
        "Wasn't fixed: test_login is flaky",
        "Not yet fixed: test_login fails",
        "After I fixed the timeout, test_login failed again",
        "Fixed test_login_timeout and test_login failed",
        "Hasn't been fixed: test_login is flaky",
        "Haven't fixed it: test_login fails",
        "Never got fixed: test_login still fails",
        "Nothing fixed yet: test_login fails",
        "Fixed the timeout and test_login failed",
        "Fixed the timeout, test_login is still flaky",
        "Fixed the setup and test_login ... FAILED",
        "Fixed the timeout, test_login: AssertionError",
        "Fixed the mock, test_login (still flaky)",
        "Fixed and verified test_login, it was flaky",
        "Fixed the lint (no errors left) and test_login failed",
        "Fixed the lint without errors, test_login failed again",
    ]:  # fmt: skip (safe side)
        assert "test_login" in rule._tests(ret(text)), text
    for text in ["Fixed the flaky timeout and teardown in test_login",
                 "Fixed race and flaky test_login"]:  # fmt: skip
        assert "test_login" not in rule._tests(ret(text)), text
    assert rule._tests(ret("Added:\n- test_a\n- test_b failed")) == {"test_b"}
    assert rule._tests(ret("test_login_timeout is fixed, but test_signup is flaky")) == {
        "test_signup"
    }
    assert rule._tests(ret("Failing tests:\n* test_login\n* test_logout")) == {
        "test_login",
        "test_logout",
    }


# --- causes, not the stage (live review: "95-100% from review") -------------------------


@pytest.mark.parametrize(
    ("reason", "cause"),
    [
        ("CI is red on the PR", "failing_tests"),
        ("unit tests failed after the rebase", "failing_tests"),
        ("FAILED tests/test_api.py::test_create - KeyError", "failing_tests"),
        ("3 failed, 120 passed", "failing_tests"),
        ("test_login_timeout fails on iOS 17", "failing_tests"),
        ("build still failing", "failing_tests"),
        ("no failing tests, but the naming is off", "review_findings"),
        ("all checks green; please split the view model", "review_findings"),
        ("0 failed, 12 passed; add a test for the empty case", "review_findings"),
        ("Merge conflict in src/app/routes.py", "merge_conflict"),
        ("changes requested", "review_findings"),
        # none failed, or it was fixed (review of #174)
        ("0 tests failed, 12 passed", "review_findings"),
        ("0 checks failed", "review_findings"),
        ("no unit tests failed", "review_findings"),
        ("none of the tests failed", "review_findings"),
        ("not failing tests, just naming", "review_findings"),
        ("no  failing tests", "review_findings"),
        ("Fixed failing tests, but rename the helper", "review_findings"),
        ("the failing test is now fixed", "review_findings"),
        ("tests were failing before, now fixed", "review_findings"),
        ("CI was red earlier, now green", "review_findings"),
        # prose about the code under review
        ("handle the case where the build fails", "review_findings"),
        ("the spec failed to describe X", "review_findings"),
        ("health check failed in prod code", "review_findings"),
        # still failing
        ("CI checks are red", "failing_tests"),
        ("the build is broken on main", "failing_tests"),
        ("Fixed the typo, but tests still fail", "failing_tests"),
        # second review: a request to fix, a fix of something else, an opener, a count
        ("CI is red, please fix", "failing_tests"),
        ("Tests failed - please fix before merge", "failing_tests"),
        ("Unit tests are failing, needs a fix", "failing_tests"),
        ("The build is broken: fix the import", "failing_tests"),
        ("tests still failing after your fix", "failing_tests"),
        ("CI is red, not fixed yet", "failing_tests"),
        ("unit tests failed even though I fixed the timeout", "failing_tests"),
        ("Not ready: unit tests failed", "failing_tests"),
        ("Not approved, CI is red", "failing_tests"),
        ("PR #12 failed to merge, retry", "review_findings"),
        ("Step 2 failed: lint", "review_findings"),
        ("Please add a failing test that reproduces the bug", "review_findings"),
        ("Test red banner on the login screen", "review_findings"),
        # third review: a fix of something else earlier in the clause; a wider negation
        ("Fixed the import, CI is still red", "failing_tests"),
        ("I resolved the merge conflict, unit tests failed", "failing_tests"),
        ("Rebased and fixed the conflict, CI is red now", "failing_tests"),
        ("Fixed the lint but the tests still fail", "failing_tests"),
        ("Not a single test failed, but the naming is off", "review_findings"),
        ("No flaky or failing tests, but rename it", "review_findings"),
        # fourth review: a conjunction ends the phrase a negation or a fix belongs to
        ("No changelog and CI is red", "failing_tests"),
        ("No docs and tests failed", "failing_tests"),
        ("No idea why CI is red", "failing_tests"),
        ("Not merged because CI is red", "failing_tests"),
        ("Fixed lint but tests still fail", "failing_tests"),
        ("Fixed lint and CI is red", "failing_tests"),
        ("Fixed typo but CI is red", "failing_tests"),
        ("Fixed it but tests still fail", "failing_tests"),
        ("Fixed the two flaky failing tests", "failing_tests"),  # 3 words: the safe side
        ("Never fixed failing tests from last round", "failing_tests"),
        ("No unit and integration tests failed", "review_findings"),
        ("Fixed lint and failing tests", "review_findings"),
        ("Resolved lint and failing CI", "review_findings"),
        ("Fixed flaky and failing tests", "review_findings"),
        ("Not yet fixed failing tests", "failing_tests"),
        ("Both unit and integration tests failed", "failing_tests"),
    ],
)
def test_a_review_return_is_classified_by_what_sent_it_back(reason: str, cause: str) -> None:
    assert ReturnCauses().cause(ret(reason, category="review")) == cause


def test_review_findings_are_not_dominant_when_every_return_went_through_review() -> None:
    reviews = [task(f"T{i}", ret("please rename the helper", category="review")) for i in range(9)]
    reviews.append(task("T9", ret("CI is red", category="review")))
    assert run(*reviews) == []  # 90% review findings: how a review farm works
    # A cause behind most of them is still reported, with the shares by cause.
    red = [task(f"R{i}", ret("tests failed on CI", category="review")) for i in range(6)]
    [alert] = run(*red, *reviews[:4])
    assert alert.params["dominant_cause"] == "failing_tests"
    assert alert.params["shares"] == {"failing_tests": 0.6, "review_findings": 0.4}
    assert "by cause: failing tests 60%, review findings 40%" in alert.reason
    # Returns of another path too: review findings dominating says something.
    [alert] = run(*reviews, task("C", ret("lint", category="ci")))
    assert alert.params["dominant_cause"] == "review_findings"


def test_stage_categories_are_config() -> None:
    rule = build({"stage_categories": ["qa"]})
    assert rule.cause(ret("looks off", category="qa")) == "qa_findings"
    assert rule.cause(ret("looks off", category="review")) == "review"
    with pytest.raises(ValueError, match="stage_categories must be a list"):
        build({"stage_categories": "review"})
    with pytest.raises(ValueError, match="stage_categories must be a list"):
        build({"stage_categories": [" "]})


def test_configured_categories_come_before_ci_wording() -> None:
    rule = build({"cause_patterns": {"infra": ["runner (?:lost|offline)", "no space left"]}})
    assert rule.cause(ret("CI is red: runner lost", category="review")) == "infra"
    assert rule.cause(ret("build failed: no space left on device", category="review")) == "infra"
    assert rule.cause(ret("tests failed", category="review")) == "failing_tests"


def test_ci_wording_can_be_off_and_named_tests_still_count() -> None:
    rule = build({"ci_wording": False})
    assert rule.cause(ret("CI is red", category="review")) == "review_findings"
    assert rule.cause(ret("test_login failed", category="review")) == "failing_tests"
    with pytest.raises(ValueError, match="ci_wording"):
        build({"ci_wording": "no"})


def test_several_stages_and_a_conflict_category() -> None:
    rule = ReturnCauses(stage_categories=("review", "qa"))
    staged = [task(f"T{i}", ret("rename it", category="review")) for i in range(4)]
    staged += [task(f"Q{i}", ret("looks off", category="qa")) for i in range(3)]
    assert run(*staged, rule=rule) == []  # every return through a stage
    # A platform-filed conflict names no failing test.
    conflict = ret("test_login conflicts", category="merge_conflict")
    assert ReturnCauses().cause(conflict) == "merge_conflict"


def test_ci_wording_is_linear_on_long_reasons() -> None:
    import time

    from kelpie.rules.return_causes import _ci_failed

    for reason in ("tests " + "keep " * 200 + "x", "CI " + "is " * 300, "no " * 400 + "tests"):
        started = time.perf_counter()
        _ci_failed(reason[:1000])
        assert time.perf_counter() - started < 0.1, reason[:20]
