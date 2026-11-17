"""The CI outcome of a PR's head in its conditions, for rule 4's CI-red check."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.connectors.github.blocks import ConditionSync, load_block_conditions
from kelpie.connectors.github.ci_runs import judge
from kelpie.connectors.github.client import GitHubClient
from kelpie.connectors.github.connector import GitHubConnector
from kelpie.core.model import Block, Condition, ConditionKind, Snapshot, Status, Task
from kelpie.rules.stale_blocks import StaleBlocks
from tests.connectors.fake_github import TOKEN, Clock, FakeGitHub, serve

REPO = "example/repo"
SHA = "d" * 40
NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
PATTERNS = [
    {"pattern": r"automerge PR #(?P<pr>\d+)", "kind": "pr_merged", "repo": REPO},
    {"pattern": r"exact-head CI of PR #(?P<pr>\d+)", "kind": "ci_green", "repo": REPO},
]


def run(name: str, conclusion: str | None, at: str = "2026-03-02T11:00:00Z") -> dict[str, object]:
    done = conclusion is not None
    return {
        "name": name,
        "status": "completed" if done else "in_progress",
        "conclusion": conclusion,
        "completed_at": at if done else None,
    }


def connector(gh: FakeGitHub) -> GitHubConnector:
    return GitHubConnector(GitHubClient(gh.url, TOKEN, clock=Clock(NOW)), [REPO])


def sync(gh: FakeGitHub, head_ci: bool = True, **kw: Any) -> ConditionSync:
    patterns = load_block_conditions(PATTERNS, [REPO])
    return ConditionSync(connector(gh), patterns, head_ci=head_ci, **kw)


def blocked(reason: str) -> Task:
    since = NOW - timedelta(hours=2)
    return Task("T-1", "t", Status.BLOCKED, since, owner_id="dev-1", block=Block(reason, since))


def test_a_red_head_names_its_checks_and_when_ci_finished() -> None:
    with serve() as gh:
        gh.pull(REPO, 63, sha=SHA)
        failed = {"context": "pr-rules", "state": "failure", "updated_at": "2026-03-02T10:30:00Z"}
        runs = [run("test (linux)", "failure"), run("lint", "success", "2026-03-02T10:00:00Z")]
        gh.ci(REPO, SHA, statuses=[failed], status_state="failure", runs=runs)
        out = sync(gh)(Snapshot(now=NOW, tasks=(blocked("automerge PR #63"),)))
    (c,) = out.conditions
    assert c.kind is ConditionKind.PR_MERGED and c.satisfied is False
    assert c.params["head_ci"] == "failure"
    assert c.params["head_ci_failed"] == ["pr-rules", "test (linux)"]
    assert c.params["head_ci_red_since"] == "2026-03-02T11:00:00+00:00"
    # Rule 4 says so once the grace period has passed (observe by default).
    (alert,) = StaleBlocks().evaluate(out)
    assert "PR #63 CI failed on its head (pr-rules, test (linux))" in alert.reason


def test_a_red_head_with_a_check_still_running_has_no_red_since() -> None:
    with serve() as gh:
        gh.pull(REPO, 63, sha=SHA)
        gh.ci(REPO, SHA, runs=[run("test", "failure"), run("e2e", None)])
        (c,) = sync(gh)(Snapshot(now=NOW, tasks=(blocked("automerge PR #63"),))).conditions
    assert c.params["head_ci"] == "failure" and "head_ci_red_since" not in c.params


def test_ci_green_conditions_carry_the_head_ci_and_green_has_no_failure_keys() -> None:
    with serve() as gh:
        gh.pull(REPO, 95, sha=SHA)
        gh.ci(REPO, SHA, runs=[run("ci", "success")])
        out = sync(gh, head_ci=False)(
            Snapshot(now=NOW, tasks=(blocked("Waiting for exact-head CI of PR #95"),))
        )
    (c,) = out.conditions
    assert c.kind is ConditionKind.CI_STATUS and c.satisfied is True
    assert c.params["head_ci"] == "success" and "head_ci_failed" not in c.params


def test_head_ci_off_and_merged_prs_read_no_ci() -> None:
    with serve() as gh:
        gh.pull(REPO, 63, sha=SHA)
        (c,) = sync(gh, head_ci=False)(
            Snapshot(now=NOW, tasks=(blocked("automerge PR #63"),))
        ).conditions
        assert "head_ci" not in c.params
        gh.pull(REPO, 64, state="closed", merged=True, merged_at="2026-03-02T11:00:00Z")
        (m,) = sync(gh)(Snapshot(now=NOW, tasks=(blocked("automerge PR #64"),))).conditions
        assert m.satisfied is True and "head_ci" not in m.params
        assert not any("/commits/" in p for p in gh.paths())


def test_unreadable_head_ci_leaves_the_condition_as_it_was() -> None:
    with serve() as gh:
        gh.pull(REPO, 63, sha=SHA)  # no CI answers: 404
        (c,) = sync(gh)(Snapshot(now=NOW, tasks=(blocked("automerge PR #63"),))).conditions
    assert c.satisfied is False and "head_ci" not in c.params and "error" not in c.params


def test_registered_conditions_get_the_head_ci_and_old_results_are_dropped() -> None:
    stale = {"head_ci": "failure", "head_ci_failed": ["old"], "head_ci_red_since": "x"}
    cond = Condition("c-1", ConditionKind.PR_MERGED, "T-1", {"repo": REPO, "pr": 63, **stale})
    with serve() as gh:
        gh.pull(REPO, 63, sha=SHA)
        gh.ci(REPO, SHA, runs=[run("ci", "success")])
        task = blocked("held on c-1")
        (c,) = sync(gh)(Snapshot(now=NOW, tasks=(task,), conditions=(cond,))).conditions
        assert c.params["head_ci"] == "success" and "head_ci_failed" not in c.params
        # A condition of a task that is not blocked: no head CI lookup.
        idle = Condition("c-3", ConditionKind.PR_MERGED, "T-9", {"repo": REPO, "pr": 63})
        (d,) = sync(gh)(Snapshot(now=NOW, conditions=(idle,))).conditions
        assert "head_ci" not in d.params
    # Anything else comes back unchanged.
    other = Condition("c-2", ConditionKind.CI_STATUS, "T-1", {"repo": REPO, "pr": 63})
    with serve() as gh:
        assert connector(gh).head_ci(other) is other


def test_workflow_runs_name_their_red_runs() -> None:
    def wf(name: str, conclusion: str | None, n: int) -> dict[str, object]:
        return {
            "id": n,
            "workflow_id": n,
            "name": name,
            "event": "push",
            "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion,
            "updated_at": "2026-03-02T11:00:00Z",
            "run_started_at": "2026-03-02T10:00:00Z",
        }

    verdict = judge([wf("ci", "failure", 1), wf("docs", "success", 2)])
    assert verdict.state == "failure" and verdict.failed == ("ci",) and not verdict.running
    assert verdict.done_at == datetime(2026, 3, 2, 11, tzinfo=UTC)
    running = judge([wf("ci", "failure", 1), wf("e2e", None, 2)])
    assert running.running and running.failed == ("ci",)


def test_head_ci_only_spends_what_the_pull_lookups_left() -> None:
    tasks = tuple(replace(blocked(f"automerge PR #{n}"), id=f"T-{n}") for n in (61, 62, 63))
    with serve() as gh:
        for n in (61, 62, 63):
            gh.pull(REPO, n, sha=SHA)
        gh.ci(REPO, SHA, runs=[run("test", "failure")])
        out = sync(gh, max_requests=3)(Snapshot(now=NOW, tasks=tasks))
    # Every PR was confirmed and attached: the head CI lookups came after them.
    assert len(out.conditions) == 3
    assert all("head_ci" not in c.params for c in out.conditions)


def test_a_github_error_keeps_the_head_ci_it_last_knew() -> None:
    known = {"head_ci": "failure", "head_ci_failed": ["test"], "head_ci_red_since": "x"}
    cond = Condition("c-1", ConditionKind.PR_MERGED, "T-1", {"repo": REPO, "pr": 63, **known})
    with serve() as gh:
        gh.reply(f"/repos/{REPO}/pulls/63", {"message": "boom"}, 500)
        (c,) = sync(gh)(Snapshot(now=NOW, conditions=(cond,))).conditions
    assert c.params["error"] and c.params["head_ci_failed"] == ["test"]
