"""ci_status when the token may not read check runs or statuses (HTTP 403): the commit's
GitHub Actions runs stand in."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.connectors.github import ci_runs
from kelpie.connectors.github.client import GitHubClient
from kelpie.connectors.github.connector import GitHubConnector
from kelpie.connectors.github.settings import load_github
from kelpie.core.model import Condition, ConditionKind
from tests.connectors.fake_github import TOKEN, Clock, FakeGitHub, serve

REPO = "example/repo"
SHA = "a" * 40
T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)


def wf(
    name: str = "CI",
    conclusion: str | None = "success",
    *,
    status: str | None = None,
    attempt: int = 1,
    started: int = 0,
    workflow: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """A workflow run; ``started`` is minutes after T0 (a re-run starts later)."""
    when = (T0 + timedelta(minutes=started)).strftime("%Y-%m-%dT%H:%M:%SZ")
    run = {
        "id": 100 + sum(map(ord, name)),
        "name": name,
        "workflow_id": workflow if workflow is not None else 1 + sum(map(ord, name)),
        "status": status or ("completed" if conclusion else "in_progress"),
        "conclusion": conclusion,
        "run_attempt": attempt,
        "event": "push",
        "created_at": T0.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run_started_at": when,
        "updated_at": when,
    }
    run.update(extra)
    return run


def connector(
    gh: FakeGitHub, clock: Clock | None = None, workflows: tuple[str, ...] = ()
) -> GitHubConnector:
    client = GitHubClient(gh.url, TOKEN, clock=clock or Clock())
    return GitHubConnector(client, [REPO], ci_workflows=workflows)


def cond() -> Condition:
    params = {"repo": REPO, "pr": 7}
    return Condition("C-1", ConditionKind.CI_STATUS, task_id="T-1", params=params)


def check(gh: FakeGitHub, runs: list[dict[str, Any]], **kwargs: Any) -> Condition:
    """The condition after one check, with 403 on check runs and statuses."""
    gh.pull(REPO, 7, sha=SHA)
    gh.forbid_ci(REPO, SHA)
    gh.actions(REPO, SHA, runs)
    return connector(gh, **kwargs).check(cond())


def test_all_workflows_green() -> None:
    with serve() as gh:
        out = check(gh, [wf("CI", started=5), wf("Lint", started=9)])
        assert f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100" in gh.paths()
    assert out.satisfied is True
    assert out.params["ci_state"] == "success" and out.params["ci_source"] == "actions"
    assert out.satisfied_at == T0 + timedelta(minutes=9)
    assert "error" not in out.params


@pytest.mark.parametrize(
    "conclusion",
    ["failure", "cancelled", "timed_out", "startup_failure", "action_required", "stale"],
)
def test_a_failed_workflow_is_red(conclusion: str) -> None:
    with serve() as gh:
        out = check(gh, [wf("CI"), wf("Tests", conclusion)])
    assert out.satisfied is False and out.params["ci_state"] == "failure"


@pytest.mark.parametrize("status", ["queued", "in_progress", "waiting"])
def test_a_workflow_still_running_is_pending(status: str) -> None:
    with serve() as gh:
        out = check(gh, [wf("CI"), wf("Tests", None, status=status)])
    assert out.satisfied is False and out.params["ci_state"] == "pending"


def test_red_wins_over_pending() -> None:
    with serve() as gh:
        out = check(gh, [wf("Tests", None), wf("Lint", "failure")])
    assert out.params["ci_state"] == "failure"


def test_a_completed_run_with_an_unknown_conclusion_is_pending() -> None:
    with serve() as gh:
        out = check(gh, [wf("CI", "something_new")])
    assert out.satisfied is False and out.params["ci_state"] == "pending"


def test_no_runs_is_never_green() -> None:
    with serve() as gh:
        out = check(gh, [])
    assert out.satisfied is False and out.params["ci_state"] == "none"
    assert out.satisfied_at is None


def test_the_latest_attempt_wins_over_an_earlier_failure() -> None:
    first, rerun = wf("CI", "failure"), wf("CI", "success", attempt=2, started=30)
    with serve() as gh:
        out = check(gh, [first, rerun])
    assert out.satisfied is True and out.satisfied_at == T0 + timedelta(minutes=30)


def test_the_latest_attempt_wins_over_an_earlier_success() -> None:
    first, rerun = wf("CI", "success"), wf("CI", "failure", attempt=2, started=30)
    with serve() as gh:
        out = check(gh, [rerun, first])  # the order of the list does not matter
    assert out.params["ci_state"] == "failure"


def test_a_rerun_in_progress_is_pending_not_the_old_result() -> None:
    first, rerun = wf("CI", "failure"), wf("CI", None, attempt=2, started=30)
    with serve() as gh:
        out = check(gh, [first, rerun])
    assert out.params["ci_state"] == "pending"


def test_attempt_number_decides_when_times_are_missing() -> None:
    bare = {"workflow_id": 5, "name": "CI", "status": "completed", "event": "push"}
    runs = [
        {**bare, "conclusion": "failure", "run_attempt": 1},
        {**bare, "conclusion": "success", "run_attempt": 2},
    ]
    with serve() as gh:
        out = check(gh, runs)
    assert out.params["ci_state"] == "success"


def test_a_later_run_of_another_event_does_not_hide_a_failed_pull_request_run() -> None:
    failed = wf("CI", "failure", event="pull_request", pull_requests=[{"number": 7}])
    manual = wf("CI", "success", started=30, event="workflow_dispatch")
    with serve() as gh:
        out = check(gh, [failed, manual])
    assert out.params["ci_state"] == "failure"


def test_a_malformed_event_does_not_break_the_grouping() -> None:
    runs = [wf("CI", event=["push"]), wf("Lint", event={"x": 1})]
    assert ci_runs.judge(runs).state == "none"  # no known event: not counted, nothing breaks


def test_a_short_commit_id_cannot_use_the_actions_runs() -> None:
    short = "a" * 7
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, short)
        c = cond()
        c.params["sha"] = short
        out = connector(gh).check(c)
        assert not any("actions/runs" in p for p in gh.paths())
    assert out.satisfied is None and out.params["error"] == "bad_response"


def test_the_repository_is_remembered_whatever_its_case() -> None:
    mixed = "Example/REPO"
    with serve() as gh:
        for name in (REPO, mixed):
            gh.forbid_ci(name, SHA)
            gh.actions(name, SHA, [wf("CI")])
        c = connector(gh)
        c.ci(REPO, SHA)
        c.ci(mixed, SHA)
        assert list(c._access) == [REPO]
        assert len([p for p in gh.paths() if p.endswith("/status?per_page=100")]) == 1


def test_the_list_names_the_workflows_that_are_required() -> None:
    runs = [wf("CI Gate"), wf("Docs", None)]  # an unlisted workflow still running
    with serve() as gh:
        required = check(gh, runs, workflows=("CI Gate",))
    with serve() as gh:
        everything = check(gh, runs)
    assert required.satisfied is True and required.params["ci_state"] == "success"
    assert everything.params["ci_state"] == "pending"  # without a list it all counts


def test_an_allowlisted_workflow_that_did_not_run_is_not_green() -> None:
    with serve() as gh:
        out = check(gh, [wf("Docs")], workflows=("CI Gate",))
    assert out.satisfied is False and out.params["ci_state"] == "pending"


def test_listed_names_match_exactly() -> None:
    with serve() as gh:
        out = check(gh, [wf("ci gate"), wf("CI  Gate")], workflows=("CI Gate",))
    assert out.satisfied is False and out.params["ci_state"] == "pending"


def test_skipped_and_neutral_do_not_block_but_are_no_evidence() -> None:
    with serve() as gh:
        ok = check(gh, [wf("CI"), wf("Deploy", "skipped"), wf("Report", "neutral")])
    with serve() as gh:
        only = check(gh, [wf("Deploy", "skipped"), wf("Report", "neutral")])
    assert ok.params["ci_state"] == "success"
    assert only.satisfied is False and only.params["ci_state"] == "none"


def test_more_runs_than_one_page_is_unknown_unless_red_is_visible() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, [wf("CI")], total=150)
        unknown = connector(gh).check(cond())
        gh.actions(REPO, SHA, [wf("CI", "failure")], total=150)
        red = connector(gh).check(cond())
    assert unknown.satisfied is None and unknown.params["ci_state"] == "incomplete"
    assert red.satisfied is False and red.params["ci_state"] == "failure"


def test_actions_are_read_only_when_something_is_refused() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}], runs=[])
        out = connector(gh).check(cond())
        assert not any("actions/runs" in p for p in gh.paths())
    assert out.params["ci_source"] == "statuses+check-runs"


def test_statuses_refused_check_runs_readable_both_count() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, SHA, runs=[{"status": "completed", "conclusion": "success"}])
        gh.forbid_ci(REPO, SHA, checks=False)
        gh.actions(REPO, SHA, [wf("CI", "failure")])
        red = connector(gh).check(cond())
        gh.actions(REPO, SHA, [wf("CI")])
        green = connector(gh).check(cond())
    assert red.params["ci_state"] == "failure" and red.params["ci_source"] == "check-runs+actions"
    assert green.satisfied is True


def test_check_runs_refused_statuses_readable_both_count() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, SHA, statuses=[{"state": "failure"}], status_state="failure")
        gh.forbid_ci(REPO, SHA, statuses=False)
        gh.actions(REPO, SHA, [wf("CI")])
        red = connector(gh).check(cond())
    assert red.satisfied is False and red.params["ci_source"] == "statuses+actions"
    assert red.params["ci_state"] == "failure"


def test_refusal_is_remembered_and_asked_again_every_hour() -> None:
    clock = Clock()
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, [wf("CI")])
        c = connector(gh, clock)

        def refused_calls() -> int:
            return sum(("/status" in p or "/check-runs" in p) for p in gh.paths())

        assert c.check(cond()).satisfied is True
        assert refused_calls() == 2
        for _ in range(5):  # the client cache is over each time; the refusal is not asked
            clock.advance(minutes=10)
            assert c.check(cond()).satisfied is True
        assert refused_calls() == 2
        # Permissions granted: the next hourly probe finds check runs and statuses readable.
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        clock.advance(minutes=11)
        out = c.check(cond())
        assert refused_calls() == 4
        later = len(gh.requests)
        clock.advance(minutes=2)
        again = c.check(cond())
        assert not any("actions/runs" in s.path for s in gh.requests[later:])
    assert out.params["ci_source"] == "statuses+check-runs" == again.params["ci_source"]


def test_still_refused_after_the_hour_stays_on_actions() -> None:
    clock = Clock()
    with serve() as gh:
        c = connector(gh, clock)
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, [wf("CI")])
        c.check(cond())
        clock.advance(minutes=61)
        out = c.check(cond())
        calls = sum(("/status" in p or "/check-runs" in p) for p in gh.paths())
        clock.advance(minutes=5)
        c.check(cond())
        assert sum(("/status" in p or "/check-runs" in p) for p in gh.paths()) == calls
    assert calls == 4 and out.satisfied is True and out.params["ci_source"] == "actions"


@pytest.mark.parametrize(
    ("headers", "body"),
    [
        ({"retry_after": "30"}, {"message": "slow down"}),
        ({"x_ratelimit_remaining": "0"}, {"message": "API rate limit exceeded"}),
        ({}, {"message": "You have exceeded a secondary rate limit."}),
    ],
)
def test_a_403_for_a_rate_limit_is_not_a_missing_permission(
    headers: dict[str, str], body: dict[str, str]
) -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.reply(f"/repos/{REPO}/commits/{SHA}/status?per_page=100", body, 403, **headers)
        gh.actions(REPO, SHA, [wf("CI")])
        c = connector(gh)
        out = c.check(cond())
        assert not any("actions/runs" in p for p in gh.paths())
        assert c._access == {}  # nothing learned about the token
    assert out.satisfied is None and out.params["error"] == "rate_limited"


def test_a_missing_repository_or_token_is_not_a_missing_permission() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)  # no CI routes at all: 404 on both
        gh.actions(REPO, SHA, [wf("CI")])
        out = connector(gh).check(cond())
        assert not any("actions/runs" in p for p in gh.paths())
    assert out.satisfied is None and out.params["error"] == "not_found"


def test_a_red_status_stands_when_the_actions_runs_cannot_be_read() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, SHA, statuses=[{"state": "failure"}], status_state="failure")
        gh.forbid_ci(REPO, SHA, statuses=False)
        gh.reply(f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {}, 403)
        red = connector(gh).check(cond())
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        gh.forbid_ci(REPO, SHA, statuses=False)
        unknown = connector(gh).check(cond())
    assert red.satisfied is False and red.params["ci_source"] == "statuses"
    assert unknown.satisfied is None and unknown.params["error"] == "forbidden"


def test_actions_refused_too_names_the_permission_and_is_unknown() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.reply(f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {}, 403)
        c = connector(gh)
        out = c.check(cond())
        with pytest.raises(Exception, match="Actions: read") as caught:
            c.ci(REPO, SHA)
    assert out.satisfied is None and out.params["error"] == "forbidden"
    assert TOKEN not in str(caught.value)
    assert getattr(caught.value, "status", None) == 403


def test_probe_asks_again_whatever_was_remembered() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, [wf("CI")])
        c = connector(gh)
        c.ci(REPO, SHA)
        probed = c.probe_ci(REPO, SHA)
    assert probed.sources == ("actions",)
    assert probed.refused == ("statuses", "check-runs")


# --- the verdict itself, without HTTP ---------------------------------------------------


def test_runs_without_ids_count_one_each() -> None:
    base = {"status": "completed", "event": "push"}
    runs = [{**base, "conclusion": "success"}] * 2 + [{**base, "conclusion": "failure"}]
    assert len(ci_runs.latest_runs(runs)) == 3
    assert ci_runs.judge(runs).state == "failure"


def test_runs_of_one_workflow_found_by_path_or_name() -> None:
    old = {
        "path": ".github/workflows/ci.yml",
        "event": "push",
        "status": "completed",
        "conclusion": "failure",
    }
    new = {**old, "conclusion": "success", "run_attempt": 2}
    assert ci_runs.judge([old, new]).state == "success"
    named = [
        {"name": "CI", "event": "push", "status": "completed", "conclusion": c, "run_attempt": a}
        for c, a in (("failure", 1), ("success", 2))
    ]
    assert len(ci_runs.latest_runs(named)) == 1


def test_a_boolean_is_not_a_workflow_id_or_a_number() -> None:
    run = {"workflow_id": True, "run_attempt": True, "status": "completed", "conclusion": "success"}
    runs = [run, {**run, "conclusion": "failure"}]
    assert len(ci_runs.latest_runs(runs)) == 2  # nothing tells them apart: each counts


def test_malformed_times_are_ignored() -> None:
    run = wf("CI", run_started_at="yesterday", updated_at=5)
    verdict = ci_runs.judge([run])
    assert verdict.state == "success" and verdict.completed_at is None


# --- [connectors.github] ci_workflows ------------------------------------------------------


def test_ci_workflows_default_is_every_workflow() -> None:
    assert load_github({"repos": [REPO]}).ci_workflows == ()


def test_ci_workflows_are_read_and_reach_the_connector() -> None:
    settings = load_github({"repos": [REPO], "ci_workflows": ["CI Gate", "Tests", "CI Gate"]})
    assert settings.ci_workflows == ("CI Gate", "Tests")
    built = settings.build(None, Clock())
    assert built.ci_workflows == ("CI Gate", "Tests")


@pytest.mark.parametrize(
    "value",
    ["CI", [""], [" CI"], [1], [None], ["a\nb"], ["x" * 256], ["w"] * 51 + ["z"], {"CI": 1}],
)
def test_bad_ci_workflows_are_refused(value: object) -> None:
    with pytest.raises(ValueError, match="ci_workflows"):
        load_github({"repos": [REPO], "ci_workflows": value})


# --- a readable source must not turn green while the required CI is out of sight ----------


def legacy_green(gh: FakeGitHub, runs: list[dict[str, Any]], *workflow_states: str) -> None:
    """One legacy status ``success``, check runs refused, these Actions runs, and a
    repository with workflows in these states."""
    gh.pull(REPO, 7, sha=SHA)
    gh.ci(REPO, SHA, statuses=[{"state": "success"}])
    gh.forbid_ci(REPO, SHA, statuses=False)
    gh.actions(REPO, SHA, runs)
    gh.workflows(REPO, *workflow_states)


def test_a_green_status_without_runs_is_pending_while_the_repository_has_workflows() -> None:
    with serve() as gh:
        legacy_green(gh, [], "active")
        out = connector(gh).check(cond())
        assert f"/repos/{REPO}/actions/workflows?per_page=100" in gh.paths()
    assert out.satisfied is False and out.params["ci_state"] == "pending"


@pytest.mark.parametrize("states", [(), ("disabled_manually",), ("disabled_inactivity",)])
def test_a_green_status_decides_when_the_repository_has_no_active_workflow(
    states: tuple[str, ...],
) -> None:
    with serve() as gh:
        legacy_green(gh, [], *states)
        out = connector(gh).check(cond())
    assert out.satisfied is True and out.params["ci_source"] == "statuses+actions"


def test_only_skipped_runs_do_not_let_a_status_turn_green() -> None:
    with serve() as gh:
        legacy_green(gh, [wf("Deploy", "skipped")], "active")
        out = connector(gh).check(cond())
    assert out.params["ci_state"] == "pending"


def test_runs_that_count_are_enough_and_the_workflows_are_not_asked() -> None:
    with serve() as gh:
        legacy_green(gh, [wf("CI")], "active")
        out = connector(gh).check(cond())
        assert not any("/actions/workflows" in p for p in gh.paths())
    assert out.satisfied is True


def test_the_workflow_list_is_remembered_for_an_hour() -> None:
    clock = Clock()
    with serve() as gh:
        legacy_green(gh, [], "active")
        c = connector(gh, clock)
        for _ in range(3):
            clock.advance(minutes=5)
            c.check(cond())
        asked = [p for p in gh.paths() if "/actions/workflows" in p]
        clock.advance(minutes=50)
        c.check(cond())
        asked_later = [p for p in gh.paths() if "/actions/workflows" in p]
    assert len(asked) == 1 and len(asked_later) == 2


def test_an_unreadable_workflow_list_is_unknown_not_green() -> None:
    with serve() as gh:
        legacy_green(gh, [], "active")
        gh.reply(f"/repos/{REPO}/actions/workflows?per_page=100", {}, 403)
        out = connector(gh).check(cond())
    assert out.satisfied is None and out.params["error"] == "forbidden"


@pytest.mark.parametrize(
    ("required", "runs", "state"),
    [
        (("CI Gate",), [], "pending"),
        (("Build", "Test"), [wf("Build")], "pending"),
        (("Build", "Test"), [wf("Lint")], "pending"),  # only an unlisted workflow ran
        (("Build", "Test"), [wf("Build"), wf("Test"), wf("Lint", None)], "success"),
        (("Build", "Test"), [wf("Build"), wf("Test"), wf("Lint", "failure")], "failure"),
        (("Build", "Test"), [wf("Build"), wf("Test", "failure")], "failure"),
        (("Build", "Test"), [wf("Build"), wf("Test", None)], "pending"),
        ((".github/workflows/ci.yml",), [wf("CI", path=".github/workflows/ci.yml")], "success"),
        ((".github/workflows/ci.yml",), [wf("CI", path=".github/workflows/other.yml")], "pending"),
    ],
)
def test_every_listed_workflow_must_have_a_run_even_beside_a_green_status(
    required: tuple[str, ...], runs: list[dict[str, Any]], state: str
) -> None:
    with serve() as gh:
        legacy_green(gh, runs, "active")
        out = connector(gh, workflows=required).check(cond())
    assert out.params["ci_state"] == state


def test_workflows_can_be_listed_per_repository() -> None:
    client = GitHubClient("http://127.0.0.1:9", TOKEN, clock=Clock())
    c = GitHubConnector(client, [REPO, "example/other"], ci_workflows={"Example/Repo": ("A",)})
    assert c.workflows_for(REPO) == c.workflows_for("EXAMPLE/REPO") == ("A",)
    assert c.workflows_for("example/other") == ()
    assert GitHubConnector(client, [REPO], ci_workflows=("B",)).workflows_for("x/y") == ("B",)


# --- a refusal is believed only when it is a missing permission --------------------------


@pytest.mark.parametrize(
    ("body", "headers"),
    [
        ({"message": "Resource protected by organization SAML enforcement."}, {}),
        ({"message": "Request from an IP not on the IP allow list."}, {}),
        ({"message": "Forbidden"}, {"x_github_sso": "required; url=https://x.invalid/?s=SECRET"}),
    ],
)
def test_a_policy_403_is_unknown_and_teaches_nothing(
    body: dict[str, str], headers: dict[str, str]
) -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.reply(f"/repos/{REPO}/commits/{SHA}/status?per_page=100", body, 403, **headers)
        gh.actions(REPO, SHA, [wf("CI")])
        c = connector(gh)
        out = c.check(cond())
        with pytest.raises(Exception, match="organization policy") as caught:
            c.ci(REPO, SHA)
        assert not any("actions/runs" in p for p in gh.paths())
    assert out.satisfied is None and out.params["error"] == "policy"
    assert c._access == {}
    assert "SECRET" not in str(caught.value) and TOKEN not in str(caught.value)


def test_a_refusal_is_not_remembered_when_the_runs_cannot_be_read() -> None:
    clock = Clock()
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.reply(f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {}, 403)
        c = connector(gh, clock)
        first = c.check(cond())
        assert c._access == {}  # a blanket 403 (a policy, say) says nothing about the token
        clock.advance(minutes=2)  # the policy is lifted; the red status is there to see
        gh.ci(REPO, SHA, statuses=[{"state": "failure"}], status_state="failure")
        gh.forbid_ci(REPO, SHA, statuses=False)
        gh.actions(REPO, SHA, [wf("CI")])
        second = c.check(cond())
    assert first.satisfied is None and first.params["error"] == "forbidden"
    assert second.satisfied is False and second.params["ci_state"] == "failure"
    assert list(c._access) == [REPO]  # now that the runs could be read


def test_a_red_source_stands_but_the_refused_runs_are_recorded() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, SHA, statuses=[{"state": "failure"}], status_state="failure")
        gh.forbid_ci(REPO, SHA, statuses=False)
        gh.reply(f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {}, 403)
        c = connector(gh)
        got = c.ci(REPO, SHA)
    assert got.state == "failure"
    assert got.sources == ("statuses",) and got.refused == ("check-runs", "actions")
    assert c._access == {}


def test_an_uppercase_commit_id_is_lowercased_for_the_runs() -> None:
    upper = "A" * 40
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, upper)
        gh.actions(REPO, SHA, [wf("CI")])
        c = cond()
        c.params["sha"] = upper
        out = connector(gh).check(c)
        assert f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100" in gh.paths()
    assert out.satisfied is True


# --- which runs count --------------------------------------------------------------------


def test_a_later_skipped_run_does_not_hide_an_earlier_failure() -> None:
    first = wf("Test", "failure", event="pull_request", id=1)
    later = wf("Test", "skipped", event="pull_request", started=30, id=2)
    assert ci_runs.judge([first, later, wf("Lint")]).state == "failure"
    assert ci_runs.judge([later, first]).state == "failure"
    earlier_ok = wf("Test", "success", event="pull_request", id=1)
    assert ci_runs.judge([earlier_ok, later]).state == "success"
    assert ci_runs.judge([later]).state == "none"  # never anything but skipped


def test_only_runs_of_the_pull_request_can_make_it_green_but_any_red_counts() -> None:
    other_red = wf("CI", "failure", event="pull_request", pull_requests=[{"number": 8}])
    other_green = wf("CI", event="pull_request", pull_requests=[{"number": 8}])
    mine = wf("CI", event="pull_request", pull_requests=[{"number": 7}], started=5)
    # the check-run path would show the other PR's red on this head too
    assert ci_runs.judge([other_red, mine], pr=7).state == "failure"
    assert ci_runs.judge([other_red, mine], pr=8).state == "failure"
    assert ci_runs.judge([other_red, mine]).state == "success"  # no PR known: nothing is other
    assert ci_runs.judge([other_green], pr=7).state == "none"  # a green of another PR is not ours
    assert ci_runs.judge([other_green, mine], pr=7).state == "success"
    rerun = wf("CI", started=30, attempt=2, event="pull_request", pull_requests=[{"number": 8}])
    assert ci_runs.judge([other_red, rerun, mine], pr=7).state == "success"  # its re-run went green
    fork = wf("CI", event="pull_request", pull_requests=[])
    assert ci_runs.judge([fork], pr=7).state == "none"  # whose PR it is, is not known
    push = wf("CI", event="push")
    assert ci_runs.judge([push, other_red], pr=7).state == "failure"


def test_the_connector_judges_for_the_pull_request_of_the_condition() -> None:
    with serve() as gh:
        green9 = [wf("CI", event="pull_request", pull_requests=[{"number": 9}])]
        green_elsewhere = check(gh, green9)
    with serve() as gh:
        red9 = [wf("CI", "failure", event="pull_request", pull_requests=[{"number": 9}])]
        red_elsewhere = check(gh, red9)
    assert green_elsewhere.params["ci_state"] == "none"  # PR 9's green is not PR 7's
    assert red_elsewhere.params["ci_state"] == "failure"  # PR 9's red is on this head too


# --- paging ------------------------------------------------------------------------------


def runs_page(first: int, count: int, conclusion: str = "success") -> list[dict[str, Any]]:
    return [wf(f"W{i}", conclusion, workflow=i) for i in range(first, first + count)]


def test_a_second_page_of_runs_is_read() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, runs_page(0, 100), total=150)
        gh.actions(REPO, SHA, runs_page(100, 50), total=150, page=2)
        out = connector(gh).check(cond())
        assert f"/repos/{REPO}/actions/runs?head_sha={SHA}&page=2&per_page=100" in gh.paths()
    assert out.satisfied is True


def test_a_failure_on_a_later_page_is_seen() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, runs_page(0, 100), total=150)
        gh.actions(REPO, SHA, runs_page(100, 50, "failure"), total=150, page=2)
        out = connector(gh).check(cond())
    assert out.params["ci_state"] == "failure"


def test_three_pages_are_the_limit_beyond_it_the_state_is_unknown() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        for page in (1, 2, 3):
            gh.actions(REPO, SHA, runs_page(100 * page, 100), total=350, page=page)
        out = connector(gh).check(cond())
        assert not any("page=4" in p for p in gh.paths())
    assert out.satisfied is None and out.params["ci_state"] == "incomplete"


def test_a_later_page_that_cannot_be_read_leaves_the_state_unknown() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, runs_page(0, 100), total=150)  # page 2: the fake answers 404
        out = connector(gh).check(cond())
    assert out.satisfied is None and out.params["ci_state"] == "incomplete"


# --- [connectors.github] ci_workflows per repository -------------------------------------


def test_ci_workflows_can_be_a_table_per_repository() -> None:
    section = {
        "repos": [REPO, "example/other"],
        "ci_workflows": {"Example/Repo": ["CI Gate"], "example/other": [".github/workflows/a.yml"]},
    }
    settings = load_github(section)
    assert settings.ci_workflows == ()
    assert dict(settings.ci_workflows_by_repo) == {
        REPO: ("CI Gate",),
        "example/other": (".github/workflows/a.yml",),
    }
    assert settings.build(None, Clock()).workflows_for("example/other") == (
        ".github/workflows/a.yml",
    )


def test_a_table_may_leave_repositories_out_and_they_then_count_every_run() -> None:
    settings = load_github({"repos": [REPO, "example/other"], "ci_workflows": {REPO: ["CI"]}})
    assert settings.build(None, Clock()).workflows_for("example/other") == ()


@pytest.mark.parametrize(
    "table",
    [
        {"example/unknown": ["CI"]},  # not among repos
        {REPO: ["CI"], "EXAMPLE/REPO": ["CI"]},  # twice
        {REPO: "CI"},
        {REPO: [""]},
        {1: ["CI"]},
    ],
)
def test_a_bad_per_repository_table_is_refused(table: object) -> None:
    with pytest.raises(ValueError, match="ci_workflows"):
        load_github({"repos": [REPO], "ci_workflows": table})


def test_a_listed_workflow_that_only_skipped_has_not_produced_a_result() -> None:
    runs = [wf("Lint"), wf("Tests", "skipped")]
    with serve() as gh:
        legacy_green(gh, runs, "active")
        out = connector(gh, workflows=("Lint", "Tests")).check(cond())
    assert out.params["ci_state"] == "pending"
    assert ci_runs.judge(runs, ("Lint",)).state == "success"  # unlisted, it does not matter


def test_a_runs_error_that_is_no_403_is_not_reported_as_a_refusal() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, SHA, statuses=[{"state": "failure"}], status_state="failure")
        gh.forbid_ci(REPO, SHA, statuses=False)
        gh.reply(f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {"message": "x"}, 500)
        got = connector(gh).ci(REPO, SHA)
    assert got.state == "failure" and got.refused == ("check-runs",)


def test_probing_asks_for_the_workflows_again() -> None:
    with serve() as gh:
        legacy_green(gh, [], "disabled_manually")
        clock = Clock()
        c = connector(gh, clock)
        assert c.check(cond()).satisfied is True
        gh.workflows(REPO, "active")  # the repository got its first workflow
        clock.advance(minutes=5)  # (the client's own cache is over)
        assert c.check(cond()).satisfied is True  # remembered for the hour
        assert c.probe_ci(REPO, SHA).state == "pending"


def test_other_organization_policies_are_not_missing_permissions() -> None:
    body = {"message": "The organization forbids access via a personal access token (classic)."}
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.reply(f"/repos/{REPO}/commits/{SHA}/status?per_page=100", body, 403)
        c = connector(gh)
        out = c.check(cond())
    assert out.params["error"] == "policy" and c._access == {}


# --- the "no counted run" guard only applies where check runs are out of sight -----------

DYNAMIC = {"name": "Dependabot Updates", "path": "dynamic/dependabot/dependabot-updates"}
OWN = {"name": "Nightly", "path": ".github/workflows/nightly.yml"}
GREEN_CHECK = {
    "status": "completed",
    "conclusion": "success",
    "completed_at": "2026-03-02T10:00:00Z",
}


def test_readable_check_runs_are_not_held_back_by_workflows_that_do_not_run_on_prs() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, SHA, runs=[GREEN_CHECK])  # a third-party check app, say
        gh.forbid_ci(REPO, SHA, checks=False)  # only statuses refused
        gh.actions(REPO, SHA, [])
        gh.workflows(REPO, {**OWN, "state": "active"})
        out = connector(gh).check(cond())
        assert not any("/actions/workflows" in p for p in gh.paths())
    assert out.satisfied is True and out.params["ci_source"] == "check-runs+actions"


def test_builtin_dynamic_workflows_do_not_count_as_workflows_of_the_repository() -> None:
    with serve() as gh:
        legacy_green(gh, [], "active")
        gh.workflows(REPO, {**DYNAMIC, "state": "active"}, {**OWN, "state": "disabled_manually"})
        only_builtin = connector(gh).check(cond())
        gh.workflows(REPO, {**DYNAMIC, "state": "active"}, {**OWN, "state": "active"})
        with_own = connector(gh).check(cond())
    assert only_builtin.satisfied is True  # the status decides
    assert with_own.params["ci_state"] == "pending"


# --- ci_workflows is required on every path -----------------------------------------------


def both_readable(gh: FakeGitHub, runs: list[dict[str, Any]]) -> None:
    """Check runs and statuses readable and green (a fast DCO check), these Actions runs."""
    gh.pull(REPO, 7, sha=SHA)
    gh.ci(REPO, SHA, statuses=[{"state": "success"}], runs=[{**GREEN_CHECK, "name": "DCO"}])
    gh.actions(REPO, SHA, runs)


def test_a_listed_workflow_is_required_even_when_check_runs_and_statuses_are_readable() -> None:
    with serve() as gh:
        both_readable(gh, [wf("Labeler")])
        waiting = connector(gh, workflows=("CI Gate",)).check(cond())
        gh.actions(REPO, SHA, [wf("Labeler"), wf("CI Gate")])
        done = connector(gh, workflows=("CI Gate",)).check(cond())
        gh.actions(REPO, SHA, [wf("CI Gate", "failure")])
        red = connector(gh, workflows=("CI Gate",)).check(cond())
        unlisted = connector(gh).check(cond())  # nothing required: as before
    assert waiting.params["ci_state"] == "pending" and waiting.satisfied is False
    assert done.satisfied is True and done.params["ci_source"] == "statuses+check-runs+actions"
    assert red.params["ci_state"] == "failure"
    assert unlisted.satisfied is True


def test_readable_sources_are_not_asked_about_runs_when_nothing_is_required() -> None:
    with serve() as gh:
        both_readable(gh, [])
        connector(gh).check(cond())
        assert not any("actions/runs" in p for p in gh.paths())


def test_unreadable_runs_make_a_required_workflow_unknown_not_green() -> None:
    with serve() as gh:
        both_readable(gh, [])
        gh.reply(f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {}, 403)
        c = connector(gh, workflows=("CI Gate",))
        out = c.check(cond())
    assert out.satisfied is None and out.params["error"] == "forbidden"
    assert list(c._access) == [REPO]  # check runs and statuses were readable: that is known


def test_a_red_check_run_stands_when_the_required_runs_are_unreadable() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.ci(REPO, SHA, runs=[{"status": "completed", "conclusion": "failure"}])
        gh.reply(f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {}, 403)
        got = connector(gh, workflows=("CI Gate",)).ci(REPO, SHA)
    assert got.state == "failure" and got.refused == ("actions",)


def test_the_missing_workflows_are_named() -> None:
    with serve() as gh:
        both_readable(gh, [wf("Labeler")])
        got = connector(gh, workflows=("CI Gate", "Labeler")).ci(REPO, SHA, 7)
    assert got.state == "pending" and got.missing == ("CI Gate",)


# --- which events count -------------------------------------------------------------------


def test_only_push_pull_request_and_merge_group_runs_count_unless_listed() -> None:
    for event in ("workflow_run", "pull_request_target", "schedule", "workflow_dispatch"):
        assert ci_runs.judge([wf("CI", event=event)]).state == "none", event
        assert ci_runs.judge([wf("CI", event=event)], ("CI",)).state == "success", event
    assert ci_runs.judge([wf("CI", event="merge_group")]).state == "success"
    assert ci_runs.judge([wf("CI", event="push")]).state == "success"


def test_a_green_workflow_run_does_not_cover_a_failed_fork_pull_request_run() -> None:
    fork = wf("CI", "failure", event="pull_request", pull_requests=[])
    follow_up = wf("Post CI", event="workflow_run", started=10)
    assert ci_runs.judge([fork, follow_up], pr=7).state == "failure"
    with serve() as gh:
        out = check(gh, [fork, follow_up])
    assert out.satisfied is False and out.params["ci_state"] == "failure"


def test_a_listed_workflow_counts_whatever_triggered_it() -> None:
    manual = wf("Release gate", event="workflow_dispatch")
    assert ci_runs.judge([manual], ("Release gate",)).state == "success"


# --- paging, again ------------------------------------------------------------------------


def test_a_run_inserted_between_pages_is_counted_once_and_the_grown_total_is_seen() -> None:
    page1 = runs_page(0, 100)
    for i, run in enumerate(page1):
        run["id"] = 1000 + i
    page2 = [page1[-1], *runs_page(100, 50)]  # the last run of page 1 came again
    for i, run in enumerate(page2[1:], 100):
        run["id"] = 1000 + i
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, page1, total=150)
        gh.actions(REPO, SHA, page2, total=151, page=2)  # 151 runs now, one never seen
        out = connector(gh).check(cond())
    assert out.satisfied is None and out.params["ci_state"] == "incomplete"


def test_a_page_with_a_repeated_run_and_a_steady_total_is_complete() -> None:
    page1 = runs_page(0, 100)
    for i, run in enumerate(page1):
        run["id"] = 1000 + i
    page2 = [page1[-1], *runs_page(100, 50)]
    for i, run in enumerate(page2[1:], 100):
        run["id"] = 1000 + i
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, page1, total=150)
        gh.actions(REPO, SHA, page2, total=150, page=2)
        out = connector(gh).check(cond())
    assert out.satisfied is True


# --- organization policies ----------------------------------------------------------------

SAML = (
    "Resource protected by organization SAML enforcement. You must grant your Personal Access "
    "token access to this organization."
)
IP_LIST = (
    "Although you appear to have the correct authorization credentials, the `example` "
    "organization has an IP allow list enabled, and 203.0.113.9 is not permitted to access "
    "this resource."
)
CLASSIC = (
    "The `example` organization forbids access via a personal access token (classic). "
    "Please use or request a fine-grained personal access token."
)
NO_ACTIONS = "Actions is disabled for this repository by an enterprise policy."
NO_PERMISSION = "Resource not accessible by personal access token"


@pytest.mark.parametrize("message", [SAML, IP_LIST, CLASSIC])
def test_known_organization_policy_bodies_are_the_policy_kind(message: str) -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.reply(f"/repos/{REPO}/commits/{SHA}/status?per_page=100", {"message": message}, 403)
        out = connector(gh).check(cond())
    assert out.params["error"] == "policy"


@pytest.mark.parametrize("message", [NO_PERMISSION, NO_ACTIONS, "Must have admin rights."])
def test_other_403_bodies_are_a_refusal_that_falls_back_to_the_runs(message: str) -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.reply(f"/repos/{REPO}/commits/{SHA}/status?per_page=100", {"message": message}, 403)
        gh.reply(f"/repos/{REPO}/commits/{SHA}/check-runs?per_page=100", {"message": message}, 403)
        gh.actions(REPO, SHA, [wf("CI")])
        c = connector(gh)
        out = c.check(cond())
    assert out.satisfied is True and out.params["ci_source"] == "actions"


def test_a_disabled_actions_body_on_the_runs_is_unknown_and_not_remembered() -> None:
    with serve() as gh:
        gh.pull(REPO, 7, sha=SHA)
        gh.forbid_ci(REPO, SHA)
        gh.reply(
            f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {"message": NO_ACTIONS}, 403
        )
        c = connector(gh)
        out = c.check(cond())
    assert out.satisfied is None and out.params["error"] == "forbidden"
    assert c._access == {}


def test_missing_workflows_are_reported_only_while_the_state_is_pending() -> None:
    with serve() as gh:
        both_readable(gh, [wf("Labeler", "failure")])
        got = connector(gh, workflows=("CI Gate", "Labeler")).ci(REPO, SHA, 7)
    assert got.state == "failure" and got.missing == ()


# --- other events count one-sided ---------------------------------------------------------


@pytest.mark.parametrize("event", ["workflow_dispatch", "workflow_run", "schedule"])
def test_an_unlisted_failed_run_of_another_event_makes_the_commit_red(event: str) -> None:
    green = wf("CI", started=0, event="push")
    red = wf("Nightly", "failure", started=5, event=event)
    assert ci_runs.judge([green, red]).state == "failure"
    with serve() as gh:  # nothing readable but the runs, no workflows listed
        out = check(gh, [green, red])
    assert out.satisfied is False and out.params["ci_state"] == "failure"


def test_an_unlisted_run_of_another_event_never_makes_it_pending_or_green() -> None:
    running = wf("Post CI", None, event="workflow_run")
    assert ci_runs.judge([running]).state == "none"
    assert ci_runs.judge([wf("CI"), running]).state == "success"
    assert ci_runs.judge([wf("CI", None), wf("Post CI", event="workflow_run")]).state == "pending"


def test_a_rerun_that_went_green_clears_an_unlisted_red_run_of_another_event() -> None:
    first = wf("Nightly", "failure", event="workflow_dispatch", id=1)
    rerun = wf("Nightly", started=30, attempt=2, event="workflow_dispatch", id=1)
    assert ci_runs.judge([wf("CI"), first]).state == "failure"
    assert ci_runs.judge([wf("CI"), first, rerun]).state == "success"
    assert ci_runs.judge([wf("CI"), rerun, first]).state == "success"


def test_with_a_list_an_unlisted_red_run_still_counts() -> None:
    for event in ("workflow_dispatch", "push", "pull_request", "merge_group"):
        red = wf("Lint", "failure", event=event, pull_requests=[{"number": 7}])
        assert ci_runs.judge([wf("CI"), red], ("CI",), pr=7).state == "failure", event


def test_a_failed_unlisted_push_run_is_red_when_check_runs_are_refused() -> None:
    with serve() as gh:
        out = check(gh, [wf("CI Gate"), wf("Lint", "failure")], workflows=("CI Gate",))
    assert out.satisfied is False and out.params["ci_state"] == "failure"


def test_with_a_list_an_unlisted_run_in_progress_is_not_pending() -> None:
    runs = [wf("CI Gate"), wf("Lint", None), wf("Report", event="workflow_run", status="queued")]
    assert ci_runs.judge(runs, ("CI Gate",)).state == "success"
    with serve() as gh:
        out = check(gh, runs, workflows=("CI Gate",))
    assert out.satisfied is True


def test_with_a_list_an_unlisted_rerun_that_went_green_clears_its_red() -> None:
    first = wf("Lint", "failure", id=1)
    rerun = wf("Lint", started=30, attempt=2, id=1)
    gate = wf("CI Gate")
    assert ci_runs.judge([gate, first], ("CI Gate",)).state == "failure"
    assert ci_runs.judge([gate, first, rerun], ("CI Gate",)).state == "success"
    with serve() as gh:
        out = check(gh, [gate, rerun], workflows=("CI Gate",))
    assert out.satisfied is True


def test_an_unlisted_red_run_of_another_pr_still_makes_it_red() -> None:
    other = wf("Lint", "failure", event="pull_request", pull_requests=[{"number": 8}])
    assert ci_runs.judge([wf("CI Gate"), other], ("CI Gate",), pr=7).state == "failure"


def test_a_failed_fork_run_is_not_covered_by_a_green_run_of_the_same_workflow() -> None:
    fork = wf("CI", "failure", event="pull_request", pull_requests=[])
    follow_up = wf("Post CI", event="workflow_run", started=10)
    assert ci_runs.judge([wf("CI"), fork, follow_up], pr=7).state == "failure"
    assert ci_runs.judge([wf("CI"), fork], ("CI",), pr=7).state == "failure"  # even if listed


def test_a_listed_workflow_is_satisfied_only_by_a_run_that_counts_in_full() -> None:
    fork_green = wf("CI", event="pull_request", pull_requests=[])
    assert ci_runs.judge([fork_green], ("CI",), pr=7).state == "pending"
    assert ci_runs.judge([fork_green], ("CI",), pr=7).missing == ("CI",)
    assert ci_runs.judge([fork_green, wf("CI")], ("CI",), pr=7).state == "success"


def test_a_rerun_of_a_red_run_in_progress_keeps_the_commit_pending() -> None:
    # the runs list shows only the latest attempt: attempt 2 hides the red of attempt 1
    rerun = wf("Lint", None, attempt=2, event="workflow_dispatch")
    first_try = wf("Lint", None, attempt=1, event="workflow_dispatch")
    assert ci_runs.judge([wf("CI"), rerun]).state == "pending"
    assert ci_runs.judge([wf("CI"), rerun], ("CI",)).state == "pending"
    assert ci_runs.judge([wf("CI"), first_try]).state == "success"  # a first run: never pending
    done = wf("Lint", attempt=2, event="workflow_dispatch")  # it concluded: it cannot get stuck
    assert ci_runs.judge([wf("CI"), done]).state == "success"
    with serve() as gh:
        out = check(gh, [wf("CI"), rerun])
    assert out.params["ci_state"] == "pending"
