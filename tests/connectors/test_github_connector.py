"""GitHub connector: PR refs, pr_merged and ci_status evaluation, degradation."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kelpie.connectors.github.client import GitHubClient
from kelpie.connectors.github.connector import GitHubConnector, PrRef, parse_pr_ref, web_host
from kelpie.core.model import Condition, ConditionKind
from kelpie.core.ports import ConditionConnector
from tests.connectors.fake_github import TOKEN, Clock, FakeGitHub, serve

REPO = "example/repo"
SHA = "a" * 40


def connector(gh: FakeGitHub, clock: Clock | None = None) -> GitHubConnector:
    return GitHubConnector(GitHubClient(gh.url, TOKEN, clock=clock or Clock()), [REPO])


def cond(kind: ConditionKind, **params: object) -> Condition:
    return Condition("C-1", kind, task_id="T-1", params={"repo": REPO, "pr": 7, **params})


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("#123", PrRef(REPO, 123)),
        ("123", PrRef(REPO, 123)),
        ("Other/Repo#5", PrRef("other/repo", 5)),
        ("https://github.com/example/repo/pull/9", PrRef(REPO, 9)),
        ("https://github.com/example/repo/pull/9/files).", PrRef(REPO, 9)),
        ("https://evil.example/example/repo/pull/9", None),  # another host
        ("https://github.com/example/repo/issues/9", None),
        ("#0", None),
        ("example/../x#1", None),
        ("abc", None),
        ("https://[oops/pull/1", None),
        ("https://exa\uff0emple/example/repo/pull/2", None),
        ("#\u0661\u0662", None),
        ("\u00b2", None),
    ],
)
def test_parse_pr_ref(text: str, expected: PrRef | None) -> None:
    assert parse_pr_ref(text, REPO, "github.com") == expected


def test_bare_ref_needs_a_default_repo() -> None:
    assert parse_pr_ref("#5", None, "github.com") is None


def test_web_host() -> None:
    assert web_host("https://api.github.com") == "github.com"
    assert web_host("https://ghe.example.test/api/v3") == "ghe.example.test"


def test_implements_the_port_and_repr_has_no_token() -> None:
    c = GitHubConnector(GitHubClient("https://api.github.com", TOKEN), [REPO])
    assert isinstance(c, ConditionConnector)
    assert TOKEN not in repr(c)


def test_pr_merged() -> None:
    clock = Clock()
    with serve() as gh:
        gh.pull(REPO, 7, state="closed", merged=True, merged_at="2026-03-02T11:00:00Z")
        out = connector(gh, clock).check(cond(ConditionKind.PR_MERGED))
    assert out.satisfied is True
    assert out.satisfied_at == datetime(2026, 3, 2, 11, tzinfo=UTC)
    assert out.checked_at == clock.now
    assert out.params["pr_state"] == "merged"


@pytest.mark.parametrize(("state", "pr_state"), [("open", "open"), ("closed", "closed")])
def test_pr_not_merged(state: str, pr_state: str) -> None:
    with serve() as gh:
        gh.pull(REPO, 7, state=state)
        out = connector(gh).check(cond(ConditionKind.PR_MERGED))
    assert out.satisfied is False and out.satisfied_at is None
    assert out.params["pr_state"] == pr_state
    assert out.params.get("terminal") is (True if state == "closed" else None)


def test_ci_green_from_statuses_and_check_runs() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(
            REPO,
            SHA,
            statuses=[{"state": "success", "updated_at": "2026-03-02T10:00:00Z"}],
            runs=[
                {
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-03-02T10:30:00Z",
                },
                {"status": "completed", "conclusion": "skipped", "completed_at": None},
            ],
        )
        out = connector(gh).check(cond(ConditionKind.CI_STATUS, expect="success"))
    assert out.satisfied is True
    assert out.satisfied_at == datetime(2026, 3, 2, 10, 30, tzinfo=UTC)
    assert out.params["ci_state"] == "success" and out.params["sha"] == SHA


@pytest.mark.parametrize(
    ("statuses", "runs", "state", "satisfied"),
    [
        ([{"state": "pending"}], [], "pending", False),
        ([], [{"status": "in_progress"}], "pending", False),
        (
            [{"state": "success"}],
            [{"status": "completed", "conclusion": "failure"}],
            "failure",
            False,
        ),
        ([{"state": "error"}], [], "failure", False),
        ([], [], "none", False),
    ],
)
def test_ci_not_green(
    statuses: list[dict[str, str]], runs: list[dict[str, str]], state: str, satisfied: bool
) -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, SHA, statuses, runs)
        out = connector(gh).check(cond(ConditionKind.CI_STATUS))
    assert out.satisfied is satisfied and out.params["ci_state"] == state


def test_ci_with_more_check_runs_than_one_page_is_unknown() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, SHA, runs=[{"status": "completed", "conclusion": "success"}], total=150)
        out = connector(gh).check(cond(ConditionKind.CI_STATUS))
    assert out.satisfied is None and out.params["ci_state"] == "incomplete"


def test_ci_uses_the_named_commit() -> None:
    sha = "b" * 40
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, sha, statuses=[{"state": "success"}])
        out = connector(gh).check(cond(ConditionKind.CI_STATUS, sha=sha))
    assert out.satisfied is True and out.params["sha"] == sha


def test_ci_with_a_bad_head_sha_is_unknown() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha="not-a-sha")
        out = connector(gh).check(cond(ConditionKind.CI_STATUS))
    assert out.satisfied is None and out.params["error"] == "bad_response"


def test_failures_keep_what_the_condition_last_knew() -> None:
    checked = datetime(2026, 3, 2, 11, tzinfo=UTC)
    with serve() as gh:
        url = gh.url
    c = GitHubConnector(GitHubClient(url, TOKEN, clock=Clock()), [REPO])
    before = Condition(
        "C-1",
        ConditionKind.PR_MERGED,
        params={"repo": REPO, "pr": 7},
        satisfied=True,
        satisfied_at=checked,
        checked_at=checked,
    )
    out = c.check(before)
    assert out.params["error"] == "network"
    assert (out.satisfied, out.satisfied_at, out.checked_at) == (True, checked, checked)


def test_ci_reads_a_full_page_of_statuses_and_the_combined_state() -> None:
    green = [{"state": "success"}] * 40
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, SHA, statuses=green, status_state="failure")
        red = connector(gh).check(cond(ConditionKind.CI_STATUS))
        gh.ci(REPO, SHA, statuses=green, status_total=140)
        partial = connector(gh).check(cond(ConditionKind.CI_STATUS))
        gh.ci(REPO, SHA, statuses=green)
        ok = connector(gh).check(cond(ConditionKind.CI_STATUS))
        assert f"/repos/{REPO}/commits/{SHA}/status?per_page=100" in gh.paths()
    assert red.satisfied is False and red.params["ci_state"] == "failure"
    assert partial.satisfied is None and partial.params["ci_state"] == "incomplete"
    assert ok.satisfied is True


def test_unicode_digits_are_not_pr_numbers() -> None:
    with serve() as gh:
        c = connector(gh)
        for pr in ("\u00b2", "\u0661\u0662", "1" * 10):
            assert c.check(cond(ConditionKind.PR_MERGED, pr=pr)).params.get("pr_state") is None
        assert gh.requests == []


def test_unaddressable_conditions_come_back_unchanged() -> None:
    with serve() as gh:
        c = connector(gh)
        others = [
            Condition("C-1", ConditionKind.QUOTA_RESET, params={"repo": REPO, "pr": 7}),
            cond(ConditionKind.PR_MERGED, repo="other/repo"),  # not configured
            cond(ConditionKind.PR_MERGED, pr="x"),
            cond(ConditionKind.PR_MERGED, pr=True),
        ]
        assert [c.check(o) for o in others] == others
    assert gh.requests == []


def test_string_pr_numbers_are_accepted() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, merged=True, merged_at="2026-03-02T11:00:00Z")
        out = connector(gh).check(cond(ConditionKind.PR_MERGED, pr="7"))
    assert out.satisfied is True


def test_unexpected_response_shape_is_unknown() -> None:
    with serve() as gh:
        gh.reply(f"/repos/{REPO}/pulls/7", ["not", "a", "pull"])
        out = connector(gh).check(cond(ConditionKind.PR_MERGED))
    assert out.satisfied is None and out.params["error"] == "bad_response"
