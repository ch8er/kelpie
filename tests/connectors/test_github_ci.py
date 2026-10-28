"""GitHub Actions as a CI source: runs -> builds, superseded detection, cancel_build."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.connectors.github.builds import (
    MAX_JOB_LOOKUPS,
    CiSettings,
    GitHubCI,
    load_ci,
    next_reset,
)
from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.core.actions import Action, ActionKind, ActionResult
from kelpie.core.model import BuildStatus, Job, ResourceKind, Snapshot
from kelpie.core.ports import ActionConnector
from tests.connectors.fake_github import TOKEN, Clock, FakeGitHub, serve

REPO = "example/repo"
OTHER = "example/other"
NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
RUNS = f"/repos/{REPO}/actions/runs"
CANCEL_TOKEN = "ghp_SyntheticCancelToken987654321"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def run(run_id: int, minutes_ago: float, **fields: Any) -> dict[str, Any]:
    created = NOW - timedelta(minutes=minutes_ago)
    body: dict[str, Any] = {
        "id": run_id,
        "workflow_id": 7,
        "path": ".github/workflows/ci.yml",
        "name": "ci",
        "head_branch": "task/T-1",
        "head_sha": f"{run_id:040x}",
        "head_repository": {"full_name": REPO},
        "event": "push",
        "status": "in_progress",
        "conclusion": None,
        "created_at": iso(created),
        "run_started_at": iso(created),
        "updated_at": iso(created + timedelta(minutes=5)),
        "run_attempt": 1,
        "head_commit": {"message": "Fix parser\n\nlong body"},
        "pull_requests": [],
    }
    body.update(fields)
    return body


def pr_run(
    run_id: int, minutes_ago: float, number: int, base: str, **fields: Any
) -> dict[str, Any]:
    entry = {"number": number, "head": {"sha": f"{run_id:040x}"}, "base": {"ref": base}}
    return run(run_id, minutes_ago, event="pull_request", pull_requests=[entry], **fields)


def page_path(page: int, repo: str = REPO, since: str = "2026-03-01") -> str:
    return f"/repos/{repo}/actions/runs?created=%3E%3D{since}&page={page}&per_page=100"


def runs_page(gh: FakeGitHub, runs: list[dict[str, Any]], page: int = 1, repo: str = REPO) -> None:
    gh.reply(page_path(page, repo), {"total_count": len(runs), "workflow_runs": runs})
    gh.reply(f"/repos/{repo}", {"default_branch": "main"})


def ci(
    gh: FakeGitHub,
    clock: Clock | None = None,
    repos: tuple[str, ...] = (REPO,),
    cancel_client: GitHubClient | None = None,
    **settings: Any,
) -> GitHubCI:
    client = GitHubClient(gh.url, TOKEN, clock=clock or Clock(NOW))
    return GitHubCI(client, CiSettings(repos=repos, **settings), cancel_client)


def cancel(build_id: Any) -> Action:
    params = {"build_id": build_id}
    return Action(ActionKind.CANCEL_BUILD, "ci_economy", "superseded", params=params)


def posts(gh: FakeGitHub) -> list[str]:
    return [s.path for s in gh.requests if s.method == "POST"]


ALLOWED = {"allow_cancel": True, "cancel_repos": (REPO,)}


def superseded_pair(gh: FakeGitHub) -> None:
    runs_page(gh, [run(2, 5), run(1, 20)])
    gh.reply(f"{RUNS}/1", run(1, 20))
    gh.reply(f"{RUNS}/2", run(2, 5))
    gh.reply(f"POST {RUNS}/1/cancel", {}, status=202)


def builds_by_id(source: GitHubCI, now: datetime = NOW) -> dict[str, Any]:
    return {b.id: b for b in source(Snapshot(now=now)).builds}


# --- reading ------------------------------------------------------------------------


def test_implements_the_port_and_declares_cancel_only_when_allowed() -> None:
    with serve() as gh:
        off, on = ci(gh), ci(gh, **ALLOWED)
        assert isinstance(on, ActionConnector)
        assert off.actions == frozenset()
        assert on.actions == frozenset({ActionKind.CANCEL_BUILD})
        assert TOKEN not in repr(on)


def test_runs_become_builds_and_superseded_is_marked() -> None:
    with serve() as gh:
        runs_page(
            gh,
            [
                run(4, 2, event="schedule"),
                run(3, 4, event="schedule"),
                run(2, 5),
                run(1, 20),
                run(5, 30, head_branch="task/T-2", status="completed", conclusion="success"),
                run(6, 40, head_repository={"full_name": "fork/repo"}),  # a fork's branch
                run(9, 60 * 30),  # outside the window
            ],
        )
        builds = builds_by_id(ci(gh, **ALLOWED))
        assert set(builds) == {f"{REPO}:{i}" for i in (1, 2, 3, 4, 5, 6)}
        old, new = builds[f"{REPO}:1"], builds[f"{REPO}:2"]
        assert old.superseded_by == new.id and new.superseded_by is None
        assert old.cancellable and not new.cancellable
        assert builds[f"{REPO}:3"].superseded_by is None  # schedules are never marked
        assert builds[f"{REPO}:6"].superseded_by is None
        assert old.status is BuildStatus.RUNNING and old.source == "github"
        assert old.commit_message == "Fix parser"
        assert old.workflow == ".github/workflows/ci.yml" and old.minutes == 20.0
        assert builds[f"{REPO}:5"].status is BuildStatus.SUCCESS


def test_superseded_false_positives() -> None:
    long_branch = "task/" + "x" * 300
    with serve() as gh:
        runs_page(
            gh,
            [
                # The default branch is never marked.
                run(11, 5, head_branch="main"),
                run(10, 20, head_branch="main"),
                # A human re-run is never marked, nor does it supersede.
                run(21, 5, head_branch="task/T-2"),
                run(20, 20, head_branch="task/T-2", run_attempt=2),
                # Two PRs from one head branch into different bases do not supersede.
                pr_run(31, 5, 101, "main", head_branch="task/T-3"),
                pr_run(30, 20, 102, "release", head_branch="task/T-3"),
                # Long branch names differ only past the display cut: not the same branch.
                run(41, 5, head_branch=long_branch + "a"),
                run(40, 20, head_branch=long_branch + "b"),
                # An unknown status is not active: marked, but never cancellable.
                run(51, 5, head_branch="task/T-5"),
                run(50, 20, head_branch="task/T-5", status="mystery"),
            ],
        )
        builds = builds_by_id(ci(gh, **ALLOWED))
        for i in (10, 20, 30, 40):
            assert builds[f"{REPO}:{i}"].superseded_by is None, i
        unknown = builds[f"{REPO}:50"]
        assert unknown.status is BuildStatus.UNKNOWN and not unknown.status.is_active
        assert unknown.superseded_by == f"{REPO}:51" and not unknown.cancellable
        assert len(builds[f"{REPO}:40"].branch or "") == 255  # cut for display only


def test_same_pr_and_base_supersedes() -> None:
    with serve() as gh:
        runs_page(gh, [pr_run(2, 5, 101, "main"), pr_run(1, 20, 101, "main")])
        builds = builds_by_id(ci(gh, **ALLOWED))
        assert builds[f"{REPO}:1"].superseded_by == f"{REPO}:2"
        assert builds[f"{REPO}:1"].cancellable


def test_nothing_is_marked_without_the_default_branch() -> None:
    with serve() as gh:
        runs_page(gh, [run(2, 5), run(1, 20)])
        gh.reply(f"/repos/{REPO}", {"message": "Not Found"}, status=404)
        builds = builds_by_id(ci(gh, **ALLOWED))
        assert builds[f"{REPO}:1"].superseded_by is None


def test_cancel_policy_marks_cancellable() -> None:
    with serve() as gh:
        runs_page(
            gh, [run(2, 5), run(1, 20), run(4, 5, head_branch="x/1"), run(3, 20, head_branch="x/1")]
        )
        for settings, expected in (
            ({}, set()),
            (ALLOWED, {1, 3}),
            ({**ALLOWED, "cancel_branches": ("task/*",)}, {1}),
            ({**ALLOWED, "cancel_workflows": (".github/workflows/deploy.yml",)}, set()),
            ({"allow_cancel": True, "cancel_repos": (OTHER,)}, set()),
        ):
            builds = builds_by_id(ci(gh, **settings))
            got = {int(b.id.split(":")[1]) for b in builds.values() if b.cancellable}
            assert got == expected, settings


def test_page_one_of_every_repo_first_and_rotation() -> None:
    with serve() as gh:
        for repo in (REPO, OTHER):
            runs_page(gh, [run(1000 + i, 1 + i / 10) for i in range(100)], 1, repo)
            runs_page(gh, [run(2000 + i, 20 + i / 10) for i in range(100)], 2, repo)
        clock = Clock(NOW)
        # Budget: page 1 of both repos, both default branches, then one more page.
        source = ci(gh, clock, repos=(REPO, OTHER), max_requests_per_tick=5, max_pages=3)
        source(Snapshot(now=NOW))
        pages = [p for p in gh.paths() if "/actions/runs?" in p]
        assert pages == [page_path(1, REPO), page_path(1, OTHER), page_path(2, REPO)]
        gh.requests.clear()
        clock.advance(minutes=5)
        source.client.cache_ttl = timedelta(0)
        source(Snapshot(now=clock.now))
        pages = [p for p in gh.paths() if "/actions/runs?" in p]
        assert pages[0] == page_path(1, OTHER)  # the next scan starts elsewhere


def test_paging_stops_at_the_window() -> None:
    with serve() as gh:
        page = [run(1000 + i, i * 10) for i in range(99)] + [run(1999, 60 * 25)]
        runs_page(gh, page, page=1)
        snap = ci(gh, max_pages=3)(Snapshot(now=NOW))
        assert len(snap.builds) == 99
        assert [p for p in gh.paths() if "/actions/runs?" in p] == [page_path(1)]


def test_poll_interval_serves_the_last_scan() -> None:
    with serve() as gh:
        runs_page(gh, [run(1, 5)])
        clock = Clock(NOW)
        source = ci(gh, clock, poll_interval=timedelta(minutes=2))
        source(Snapshot(now=NOW))
        asked = len(gh.requests)
        clock.advance(minutes=1)
        assert len(source(Snapshot(now=clock.now)).builds) == 1
        assert len(gh.requests) == asked
        clock.advance(minutes=1)
        source.client.cache_ttl = timedelta(0)
        source(Snapshot(now=clock.now))
        assert len(gh.requests) > asked


def test_rate_reserve_skips_the_scan(caplog: pytest.LogCaptureFixture) -> None:
    with serve() as gh:
        superseded_pair(gh)
        clock = Clock(NOW)
        source = ci(gh, clock, rate_reserve=1000, **ALLOWED)
        assert builds_by_id(source)[f"{REPO}:1"].cancellable
        source.client.rate_remaining = 500
        source.client.rate_reset = NOW + timedelta(minutes=30)
        clock.advance(minutes=5)
        asked = len(gh.requests)
        with caplog.at_level(logging.WARNING):
            builds = builds_by_id(source, clock.now)
        assert len(gh.requests) == asked and "reserve" in caplog.text
        assert not builds[f"{REPO}:1"].cancellable
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.REFUSED
        source.client.rate_reset = clock.now  # the window reset: scan again
        source.client.cache_ttl = timedelta(0)
        builds_by_id(source, clock.now)
        assert len(gh.requests) > asked


def test_a_failing_run_list_gives_no_cancel_targets(caplog: pytest.LogCaptureFixture) -> None:
    with serve() as gh:
        superseded_pair(gh)
        clock = Clock(NOW)
        source = ci(gh, clock, **ALLOWED)
        source(Snapshot(now=NOW))
        gh.reply(page_path(1), {"message": "boom"}, status=500)
        source.client.cache_ttl = timedelta(0)
        clock.advance(minutes=10)
        with caplog.at_level(logging.WARNING):
            builds = builds_by_id(source, clock.now)
        # The cached list, stale: kept, never cancellable, and not extrapolated to now.
        assert len(builds) == 2 and not any(b.cancellable for b in builds.values())
        assert builds[f"{REPO}:1"].minutes == 5.0
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.REFUSED
        gh.reply(page_path(1), {"message": "no"}, status=404)
        source.client.backoff_until = None
        clock.advance(minutes=10)
        with caplog.at_level(logging.WARNING):
            assert source(Snapshot(now=clock.now)).builds == ()
        assert "runs not read" in caplog.text
        assert posts(gh) == []
        assert TOKEN not in caplog.text


def test_minutes_from_jobs_are_read_once() -> None:
    with serve() as gh:
        done = run(1, 30, status="completed", conclusion="failure")
        runs_page(gh, [done])
        jobs = f"{RUNS}/1/attempts/1/jobs?per_page=100"
        start = NOW - timedelta(minutes=30)

        def job(seconds: int, conclusion: str) -> dict[str, Any]:
            end = start + timedelta(seconds=seconds)
            return {"started_at": iso(start), "completed_at": iso(end), "conclusion": conclusion}

        gh.reply(
            jobs,
            {"total_count": 3, "jobs": [job(61, "success"), job(5, "failure"), job(0, "skipped")]},
        )
        clock = Clock(NOW)
        source = ci(gh, clock)
        assert source(Snapshot(now=NOW)).builds[0].minutes == 3.0
        clock.advance(minutes=5)
        assert source(Snapshot(now=clock.now)).builds[0].minutes == 3.0
        assert gh.paths().count(jobs) == 1


def job_list(*jobs: dict[str, Any]) -> dict[str, Any]:
    return {"total_count": len(jobs), "jobs": list(jobs)}


def test_a_finished_build_says_which_job_and_step_failed_and_where() -> None:
    """Rule 9 tells a CI outage from a broken PR by the failing job, step and runner."""
    with serve() as gh:
        runs_page(gh, [run(1, 30, status="completed", conclusion="failure"), run(2, 1)])
        steps = [
            {"name": "Checkout", "conclusion": "success"},
            {"conclusion": "failure"},  # unnamed
            {"name": "Odd", "conclusion": ["failure"]},  # junk: not a string
            "not a step",
            {"name": "Select toolchain", "conclusion": "failure"},
            {"name": "Build", "conclusion": "skipped"},
        ]
        failed = {
            "name": "build",
            "status": "completed",
            "conclusion": "timed_out",
            "steps": steps,
            "runner_name": "runner-1",
            "labels": ["self-hosted", "linux", 7, ""],
        }
        soft = [{"name": "Optional lint", "conclusion": "failure"}]  # continue-on-error
        passed = {"name": "lint", "status": "completed", "conclusion": "success", "steps": soft,
                  "runner_name": "runner-2", "labels": ["linux"]}  # fmt: skip
        others = [
            {"name": "deploy", "status": "completed", "conclusion": "cancelled"},
            {"name": "docs", "status": "completed", "conclusion": "skipped"},
            {"name": "odd", "status": "completed", "conclusion": "new_word"},
            {"conclusion": "failure"},  # no name: left out
        ]
        gh.reply(f"{RUNS}/1/attempts/1/jobs?per_page=100", job_list(failed, passed, *others))
        done, active = sorted(ci(gh)(Snapshot(now=NOW)).builds, key=lambda b: b.id)
        assert done.jobs == (
            Job(
                "build",
                BuildStatus.FAILURE,
                "Select toolchain",
                "runner-1",
                ("self-hosted", "linux"),
            ),
            Job("lint", BuildStatus.SUCCESS),
            Job("deploy", BuildStatus.CANCELLED, started=False),
            Job("docs", BuildStatus.SKIPPED),
            Job("odd", BuildStatus.UNKNOWN, started=False),
        )
        assert [j.failed for j in done.jobs] == [True, False, False, False, False]
        assert active.jobs is None  # an active run's jobs are not read


def test_jobs_are_read_once_per_attempt_and_none_when_not_read() -> None:
    with serve() as gh:
        runs_page(gh, [run(1, 30, status="completed", conclusion="failure", run_attempt=2)])
        jobs = f"{RUNS}/1/attempts/2/jobs?per_page=100"
        gh.reply(jobs, job_list({"name": "build", "status": "completed", "conclusion": "failure"}))
        clock = Clock(NOW)
        source = ci(gh, clock)
        first = source(Snapshot(now=NOW)).builds[0].jobs
        clock.advance(minutes=5)
        assert source(Snapshot(now=clock.now)).builds[0].jobs == first
        assert (
            first == (Job("build", BuildStatus.FAILURE, started=False),)
            and gh.paths().count(jobs) == 1
        )
    with serve() as gh:  # the request fails: not read, and asked again next scan
        runs_page(gh, [run(1, 30, status="completed", conclusion="failure")])
        gh.reply(f"{RUNS}/1/attempts/1/jobs?per_page=100", {"message": "boom"}, status=500)
        assert ci(gh)(Snapshot(now=NOW)).builds[0].jobs is None
    with serve() as gh:  # no job list in the body
        runs_page(gh, [run(1, 30, status="completed", conclusion="failure")])
        gh.reply(f"{RUNS}/1/attempts/1/jobs?per_page=100", {"total_count": 0})
        assert ci(gh)(Snapshot(now=NOW)).builds[0].jobs is None


def test_jobs_past_the_lookup_budget_are_not_read() -> None:
    with serve() as gh:
        done = [run(i, 30 + i, status="completed", conclusion="failure") for i in range(1, 13)]
        runs_page(gh, done)
        for i in range(1, 13):
            gh.reply(f"{RUNS}/{i}/attempts/1/jobs?per_page=100", job_list())
        builds = ci(gh)(Snapshot(now=NOW)).builds
        assert sum(b.jobs is None for b in builds) == 12 - MAX_JOB_LOOKUPS


def test_partial_job_list_falls_back_to_wall_clock() -> None:
    with serve() as gh:
        runs_page(gh, [run(1, 30, status="completed", conclusion="success")])
        gh.reply(f"{RUNS}/1/attempts/1/jobs?per_page=100", {"total_count": 200, "jobs": []})
        [build] = ci(gh)(Snapshot(now=NOW)).builds
        assert build.minutes == 5.0 and build.jobs is None


def test_draft_and_changed_paths_from_the_pull_request() -> None:
    with serve() as gh:
        sha = f"{1:040x}"
        runs_page(gh, [pr_run(1, 5, 7, "main")])
        gh.pull(REPO, 7, sha=sha, draft=True, changed_files=2)
        files = f"/repos/{REPO}/pulls/7/files?per_page=100"
        gh.reply(files, [{"filename": "docs/a.md"}, {"filename": "README.md"}])
        clock = Clock(NOW)
        source = ci(gh, clock)
        (build,) = source(Snapshot(now=NOW)).builds
        assert build.draft is True
        assert build.changed_paths == ("docs/a.md", "README.md")
        clock.advance(minutes=5)
        source(Snapshot(now=clock.now))
        assert gh.paths().count(files) == 1  # remembered per PR head


def test_changed_paths_unknown_when_the_pr_moved_on_or_is_large() -> None:
    with serve() as gh:
        runs_page(gh, [pr_run(1, 5, 7, "main")])
        gh.pull(REPO, 7, sha="c" * 40, changed_files=2)
        (build,) = ci(gh)(Snapshot(now=NOW)).builds
        assert build.changed_paths is None and build.draft is False
    with serve() as gh:
        runs_page(gh, [pr_run(1, 5, 7, "main")])
        gh.pull(REPO, 7, sha=f"{1:040x}", changed_files=500)
        (build,) = ci(gh)(Snapshot(now=NOW)).builds
        assert build.changed_paths is None


def test_quota_resource_from_config() -> None:
    with serve() as gh:
        runs_page(gh, [])
        snap = ci(gh, included_minutes=3000.0, quota_reset_day=5)(Snapshot(now=NOW))
        (quota,) = snap.resources
        assert quota.kind is ResourceKind.CI_MINUTES and quota.limit == 3000.0
        assert quota.used is None and quota.resets_at == datetime(2026, 3, 5, tzinfo=UTC)


def test_next_reset() -> None:
    assert next_reset(NOW, 2) == datetime(2026, 4, 2, tzinfo=UTC)
    assert next_reset(datetime(2026, 12, 20, tzinfo=UTC), 1) == datetime(2027, 1, 1, tzinfo=UTC)


# --- cancel_build ---------------------------------------------------------------------


def test_cancel_a_superseded_running_run() -> None:
    with serve() as gh:
        superseded_pair(gh)
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.DONE
        assert posts(gh) == [f"{RUNS}/1/cancel"]
        post = next(s for s in gh.requests if s.method == "POST")
        assert post.headers["Authorization"] == f"Bearer {TOKEN}"
        # A second request does nothing.
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.NOTHING_TO_DO
        assert posts(gh) == [f"{RUNS}/1/cancel"]


def test_cancel_uses_the_separate_cancel_token() -> None:
    with serve() as gh:
        superseded_pair(gh)
        writer = GitHubClient(gh.url, CANCEL_TOKEN, clock=Clock(NOW))
        source = ci(gh, cancel_client=writer, **ALLOWED)
        source(Snapshot(now=NOW))
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.DONE
        for seen in gh.requests:
            expected = CANCEL_TOKEN if seen.method == "POST" else TOKEN
            assert seen.headers["Authorization"] == f"Bearer {expected}"


def test_cancel_refused_by_policy() -> None:
    with serve() as gh:
        superseded_pair(gh)
        source = ci(gh)  # allow_cancel off
        source(Snapshot(now=NOW))
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.REFUSED
        source = ci(gh, cancel_branches=("release/*",), **ALLOWED)
        source(Snapshot(now=NOW))
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.REFUSED
        assert posts(gh) == []


def test_cancel_refused_for_runs_the_scan_did_not_offer() -> None:
    with serve() as gh:
        superseded_pair(gh)
        source = ci(gh, **ALLOWED)
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.REFUSED  # no scan yet
        source(Snapshot(now=NOW))
        for build_id in (f"{REPO}:2", f"{OTHER}:1", "../../x", 1):
            assert source.execute(cancel(build_id)) is ActionResult.REFUSED
        assert source.execute(Action(ActionKind.STOP, "x", "r")) is ActionResult.REFUSED
        assert posts(gh) == []


def test_recheck_before_cancel() -> None:
    with serve() as gh:
        superseded_pair(gh)
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        # Finished meanwhile: nothing to do.
        gh.reply(f"{RUNS}/1", run(1, 20, status="completed", conclusion="success"))
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.NOTHING_TO_DO
        assert posts(gh) == []
    with serve() as gh:
        superseded_pair(gh)
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        # Re-run by a human since the scan: nothing to do.
        gh.reply(f"{RUNS}/1", run(1, 20, run_attempt=2))
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.NOTHING_TO_DO
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.REFUSED  # target dropped
        assert posts(gh) == []
    for newer in (
        run(2, 5, head_branch="task/T-9"),  # not on the same branch any more
        None,  # gone (404)
    ):
        with serve() as gh:
            superseded_pair(gh)
            source = ci(gh, **ALLOWED)
            source(Snapshot(now=NOW))
            if newer is None:
                gh.reply(f"{RUNS}/2", {"message": "Not Found"}, status=404)
            else:
                gh.reply(f"{RUNS}/2", newer)
            assert source.execute(cancel(f"{REPO}:1")) is ActionResult.NOTHING_TO_DO
            assert posts(gh) == []


def test_cancel_forbidden_is_refused_with_a_clear_log(caplog: pytest.LogCaptureFixture) -> None:
    with serve() as gh:
        superseded_pair(gh)
        gh.reply(f"POST {RUNS}/1/cancel", {"message": "Resource not accessible"}, status=403)
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        with caplog.at_level(logging.ERROR):
            assert source.execute(cancel(f"{REPO}:1")) is ActionResult.REFUSED
        assert "Actions: write" in caplog.text and TOKEN not in caplog.text


def test_rate_reserve_is_capped_by_the_limit() -> None:
    with serve() as gh:
        source = ci(gh, rate_reserve=1000)
        assert source.reserve() == 1000
        source.client.rate_limit = 60  # anonymous
        assert source.reserve() == 12
        source.client.rate_limit = 5000
        assert source.reserve() == 1000


def test_transient_failures_raise_for_a_retry() -> None:
    with serve() as gh:
        superseded_pair(gh)
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        gh.reply(f"{RUNS}/1", run(1, 20))
        source.client.get(f"{RUNS}/1")  # cached
        source.client.backoff_until = NOW + timedelta(minutes=5)
        with pytest.raises(GitHubError, match="re-checked"):  # stale
            source.execute(cancel(f"{REPO}:1"))
        source.client.backoff_until = None
        gh.reply(f"POST {RUNS}/1/cancel", {"message": "oops"}, status=502)
        with pytest.raises(GitHubError) as info:
            source.execute(cancel(f"{REPO}:1"))
        assert info.value.kind == "server"
        assert len(posts(gh)) == 1


def test_cancel_conflict_is_nothing_to_do() -> None:
    with serve() as gh:
        superseded_pair(gh)
        gh.reply(f"POST {RUNS}/1/cancel", {"message": "Cannot cancel"}, status=409)
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        assert source.execute(cancel(f"{REPO}:1")) is ActionResult.NOTHING_TO_DO


def test_cancel_redirect_is_refused_and_not_followed(caplog: pytest.LogCaptureFixture) -> None:
    with serve() as gh:
        superseded_pair(gh)
        gh.reply(f"POST {RUNS}/1/cancel", None, status=307, Location="http://127.0.0.1:1/steal")
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        with caplog.at_level(logging.DEBUG), pytest.raises(GitHubError) as info:
            source.execute(cancel(f"{REPO}:1"))
        assert info.value.kind == "redirect"
        assert len(posts(gh)) == 1  # the original request only
        assert TOKEN not in caplog.text and TOKEN not in str(info.value)


def test_post_errors_never_carry_the_token() -> None:
    with serve() as gh:
        gh.reply(f"POST {RUNS}/1/cancel", {"message": "Bad credentials"}, status=401)
        client = GitHubClient(gh.url, TOKEN, clock=Clock(NOW))
        with pytest.raises(GitHubError) as info:
            client.post(f"{RUNS}/1/cancel")
        assert info.value.kind == "unauthorized" and TOKEN not in str(info.value)
        with pytest.raises(GitHubError) as info:  # backing off now: nothing is sent
            client.post(f"{RUNS}/1/cancel")
        assert info.value.kind == "backoff"
        assert len(posts(gh)) == 1


def test_post_network_failure() -> None:
    client = GitHubClient("http://127.0.0.1:1", TOKEN, clock=Clock(NOW))
    with pytest.raises(GitHubError) as info:
        client.post(f"{RUNS}/1/cancel")
    assert info.value.kind == "network" and TOKEN not in str(info.value)


# --- configuration ------------------------------------------------------------------


def test_load_ci_defaults_and_values() -> None:
    assert load_ci({}, [REPO]) == CiSettings(repos=(REPO,))
    s = load_ci(
        {
            "repos": ["Example/Repo"],
            "window": "12h",
            "poll_interval": "5m",
            "rate_reserve": 500,
            "max_pages": 2,
            "max_requests_per_tick": 10,
            "tick_budget": "5s",
            "included_minutes": 2000,
            "quota_reset_day": 3,
            "allow_cancel": True,
            "cancel_repos": [REPO],
            "cancel_workflows": [".github/workflows/ci.yml"],
            "cancel_branches": ["task/*"],
            "cancel_token_env": "KELPIE_GH_CANCEL",
        },
        [REPO, OTHER],
    )
    assert s.repos == (REPO,) and s.allow_cancel and s.cancel_repos == (REPO,)
    assert s.window == timedelta(hours=12) and s.included_minutes == 2000.0
    assert s.cancel_branches == ("task/*",) and s.poll_interval == timedelta(minutes=5)
    assert s.cancel_token({"KELPIE_GH_CANCEL": CANCEL_TOKEN}) == CANCEL_TOKEN
    with pytest.raises(ValueError, match="is not set") as info:
        s.cancel_token({})
    with pytest.raises(ValueError, match="not allowed") as info:
        s.cancel_token({"KELPIE_GH_CANCEL": "bad token"})
    assert "bad token" not in str(info.value)
    assert CiSettings(repos=(REPO,), cancel_token_env="X").cancel_token({}) is None


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ([], "must be a table"),
        ({"nope": 1}, "unknown"),
        ({"repos": [OTHER]}, "not in the allowed"),
        ({"repos": []}, "must not be empty"),
        ({"repos": "x"}, "owner/name"),
        ({"allow_cancel": True}, "cancel_repos"),
        ({"allow_cancel": "yes"}, "true or false"),
        ({"cancel_repos": [OTHER]}, "not in the allowed"),
        ({"cancel_workflows": "ci.yml"}, "cancel_workflows"),
        ({"cancel_branches": [""]}, "cancel_branches"),
        ({"cancel_token_env": "A-B"}, "cancel_token_env"),
        ({"window": "10m"}, "from 1h to 7d"),
        ({"window": "soon"}, "window"),
        ({"poll_interval": "10s"}, "poll_interval"),
        ({"rate_reserve": 5000}, "rate_reserve"),
        ({"tick_budget": "2m"}, "tick_budget"),
        ({"included_minutes": 0}, "included_minutes"),
        ({"max_pages": 11}, "max_pages"),
        ({"max_requests_per_tick": 101}, "max_requests_per_tick"),
        ({"quota_reset_day": 31}, "quota_reset_day"),
    ],
)
def test_load_ci_errors(section: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_ci(section, [REPO])


def test_a_build_names_the_pull_request_it_builds() -> None:
    with serve() as gh:
        runs_page(gh, [pr_run(1, 5, 7, "feature"), run(2, 4)])
        gh.reply(f"/repos/{REPO}/pulls/7", {"number": 7, "draft": False, "head": {"sha": "x"}})
        by_id = {b.id: b for b in ci(gh)(Snapshot(now=NOW)).builds}
        assert by_id[f"{REPO}:1"].pr == 7 and by_id[f"{REPO}:2"].pr is None


def test_a_hosted_runner_has_no_machine_name() -> None:
    """GitHub reuses hosted pool names ("GitHub Actions 2"): they name no machine."""
    with serve() as gh:
        runs_page(gh, [run(1, 30, status="completed", conclusion="failure")])
        hosted = {"name": "test", "status": "completed", "conclusion": "failure",
                  "runner_name": "GitHub Actions 2", "runner_group_name": "GitHub Actions",
                  "labels": ["ubuntu-latest"]}  # fmt: skip
        mine = {**hosted, "name": "build", "runner_name": "mac-1", "runner_group_name": "Default"}
        gh.reply(f"{RUNS}/1/attempts/1/jobs?per_page=100", job_list(hosted, mine))
        jobs = ci(gh)(Snapshot(now=NOW)).builds[0].jobs or ()
        assert [(j.name, j.runner, j.labels) for j in jobs] == [
            ("test", None, ("ubuntu-latest",)),
            ("build", "mac-1", ("ubuntu-latest",)),
        ]


def test_a_job_the_ci_refused_never_started() -> None:
    """GitHub's answer to a spending limit or a failed payment, as seen on a live repository:
    the job "failed" in two seconds with no runner and no step."""
    with serve() as gh:
        runs_page(gh, [run(1, 30, status="completed", conclusion="failure")])
        ran = {"name": "test", "status": "completed", "conclusion": "failure", "steps": [
            {"name": "Run tests", "conclusion": "failure"}], "runner_name": "GitHub Actions 7",
            "runner_group_name": "GitHub Actions"}  # fmt: skip
        refused = {"name": "ci-ok", "status": "completed", "conclusion": "failure", "steps": [],
            "runner_name": "", "runner_group_name": "", "labels": ["ubuntu-24.04"]}  # fmt: skip
        gh.reply(f"{RUNS}/1/attempts/1/jobs?per_page=100", job_list(ran, refused))
        test, ci_ok = ci(gh)(Snapshot(now=NOW)).builds[0].jobs or ()
        assert test.started and test.failed and not test.refused
        assert not ci_ok.started and ci_ok.refused and not ci_ok.failed
