"""Which tasks a pull request belongs to (incident 17)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from kelpie.core.model import PullRequest, Status, Task
from kelpie.core.pr_links import MAX_TITLE, compile_task_code, is_code, link_tasks, task_codes

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
REPO = "example/repo"
CODE = compile_task_code(r"^[A-Z][A-Z0-9]+-[0-9]+")


def t(tid: str, title: str = "", branch: str | None = None) -> Task:
    return Task(tid, title or tid, Status.REVIEW, NOW, branch=branch)


def pr(number: int, title: str, branch: str | None, **kw: object) -> PullRequest:
    return PullRequest(REPO, number, title, branch, **kw)  # type: ignore[arg-type]


def ids(pulls: tuple[PullRequest, ...]) -> dict[int, tuple[str, ...]]:
    return {p.number: p.task_ids for p in pulls}


def test_codes_in_the_title_or_branch_link() -> None:
    tasks = (t("t_1a"), t("t_12a"), t("t_x9", "IOS-036 Store products"), t("t_y9", "No code"))
    pulls = (
        pr(1, "Fix login", "hermes/t_1a"),  # id in the branch
        pr(2, "T_12A: refactor", "feature/x"),  # id in the title, any case
        pr(3, "Store", "ios/IOS-036"),  # a code from the task title
        pr(4, "Fixes ios-0361 and t_1ab", "misc"),  # not whole tokens
        pr(5, "Follow-up", "chore/cleanup"),  # names no task
        pr(6, "Retry", "hermes/t_1a_retry"),  # an underscore ends a token
    )
    assert ids(link_tasks(pulls, tasks, CODE)) == {
        1: ("t_1a",),
        2: ("t_12a",),
        3: ("t_x9",),
        4: (),
        5: (),
        6: ("t_1a",),
    }
    assert ids(link_tasks(pulls, tasks, None))[3] == ()  # no task_code: ids only


def test_the_tasks_own_branch_links_without_a_code() -> None:
    tasks = (t("t_5a", branch="feature/settings"),)
    pulls = (pr(1, "Settings page", "feature/settings"), pr(2, "Other", "feature/other"))
    assert ids(link_tasks(pulls, tasks, None)) == {1: ("t_5a",), 2: ()}


def test_a_forks_pr_links_nothing() -> None:
    tasks = (t("t_1a", branch="main"),)
    pulls = (pr(1, "t_1a: fix", None, fork=True),)
    assert ids(link_tasks(pulls, tasks, None)) == {1: ()}


@pytest.mark.parametrize("task_id", ["42", "a", "fix", "t_1", "1234"])
def test_short_or_generic_ids_never_link(task_id: str) -> None:
    pulls = (pr(1, f"Fix {task_id} flaky tests and add a button", "misc/42"),)
    assert ids(link_tasks(pulls, (t(task_id),), None)) == {1: ()}
    assert not is_code(task_id)


def test_codes_with_regex_characters_and_title_limits() -> None:
    tasks = (t("t+1a.b"),)
    pulls = (pr(1, "t+1a.b done", "x"), pr(2, "t+1aXb done", "x"))
    assert ids(link_tasks(pulls, tasks, None)) == {1: ("t+1a.b",), 2: ()}
    far = (pr(3, "x" * MAX_TITLE + " t+1a.b", "y"),)
    assert ids(link_tasks(far, tasks, None)) == {3: ()}  # beyond the searched title


def test_an_anchored_task_code_ignores_acronyms_later_in_titles() -> None:
    task = t("t_1a", "IOS-036 Switch to UTF-8 and SHA-256")
    assert task_codes(task, CODE) == {"t_1a", "IOS-036"}
    loose = compile_task_code(r"[A-Z][A-Z0-9]+-[0-9]+")
    assert "UTF-8" in task_codes(task, loose)  # why the documented example is anchored


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "characters"),
        ("x" * 201, "characters"),
        ("(", "missing"),
        ("(IOS)-1", "capturing"),
        ("(?:a+)+b", "repeat a group"),
        ("(?:[A-Z]+-?)+[0-9]+", "repeat a group"),
        ("(?:a|aa)*b", ""),  # accepted below only if it does not repeat a repeat
        ("(?=IOS)IOS-1", "lookarounds"),
        ("(?P<x>a)(?P=x)", "capturing"),
        ("[A-Z]*", "empty string"),
    ],
)
def test_unsafe_task_code_patterns_are_refused(text: str, message: str) -> None:
    if text == "(?:a|aa)*b":
        compile_task_code(text)  # alternation without nested repeats is fine
        return
    with pytest.raises(ValueError, match=message):
        compile_task_code(text)


def test_accepted_patterns_stay_fast_on_hostile_titles() -> None:
    title = "A-" * 250 + "!"
    started = time.monotonic()
    task_codes(t("t_1a", title), CODE)
    task_codes(t("t_1b", title), compile_task_code(r"[A-Z]+-[A-Z]+-[0-9]+"))
    assert time.monotonic() - started < 0.5
