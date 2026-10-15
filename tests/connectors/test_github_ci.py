"""GitHub Actions as a CI source: runs -> builds, superseded detection, cancel_build."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.connectors.github.builds import CiSettings, GitHubCI, load_ci, next_reset
from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import BuildStatus, ResourceKind, Snapshot
from kelpie.core.ports import ActionConnector
from tests.connectors.fake_github import TOKEN, Clock, FakeGitHub, serve

REPO = "example/repo"
OTHER = "example/other"
NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
RUNS = f"/repos/{REPO}/actions/runs"


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


def page_path(page: int, since: str = "2026-03-01", repo: str = REPO) -> str:
    return f"/repos/{repo}/actions/runs?created=%3E%3D{since}&page={page}&per_page=100"


def runs_page(gh: FakeGitHub, runs: list[dict[str, Any]], page: int = 1) -> None:
    gh.reply(page_path(page), {"total_count": len(runs), "workflow_runs": runs})


def ci(
    gh: FakeGitHub, clock: Clock | None = None, token: str | None = TOKEN, **settings: Any
) -> GitHubCI:
    client = GitHubClient(gh.url, token, clock=clock or Clock(NOW))
    return GitHubCI(client, CiSettings(repos=(REPO,), **settings))


def cancel(build_id: str) -> Action:
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
        snap = ci(gh)(Snapshot(now=NOW))
        builds = {b.id: b for b in snap.builds}
        assert set(builds) == {f"{REPO}:{i}" for i in (1, 2, 3, 4, 5, 6)}
        old, new = builds[f"{REPO}:1"], builds[f"{REPO}:2"]
        assert old.superseded_by == new.id and new.superseded_by is None
        assert builds[f"{REPO}:3"].superseded_by is None  # schedules are never marked
        assert builds[f"{REPO}:6"].superseded_by is None
        assert old.status is BuildStatus.RUNNING and old.source == "github"
        assert old.commit_message == "Fix parser"
        assert old.workflow == ".github/workflows/ci.yml" and old.minutes == 20.0
        assert builds[f"{REPO}:5"].status is BuildStatus.SUCCESS


def test_paging_stays_within_the_budget() -> None:
    with serve() as gh:
        runs_page(gh, [run(1000 + i, 1 + i / 10) for i in range(100)], page=1)
        runs_page(gh, [run(2000 + i, 20 + i / 10) for i in range(100)], page=2)
        runs_page(gh, [run(3000, 60)], page=3)
        source = ci(gh, max_requests_per_tick=2, max_pages=3)
        snap = source(Snapshot(now=NOW))
        assert len(snap.builds) == 200
        assert [p for p in gh.paths() if "/actions/runs?" in p] == [page_path(1), page_path(2)]


def test_paging_stops_at_the_window_and_at_max_pages() -> None:
    with serve() as gh:
        page = [run(1000 + i, i * 10) for i in range(99)] + [run(1999, 60 * 25)]
        runs_page(gh, page, page=1)
        snap = ci(gh, max_pages=3)(Snapshot(now=NOW))
        assert len(snap.builds) == 99
        assert [p for p in gh.paths() if "/actions/runs?" in p] == [page_path(1)]


def test_a_failing_run_list_gives_no_cancel_targets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with serve() as gh:
        superseded_pair(gh)
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        gh.reply(page_path(1), {"message": "boom"}, status=500)
        source.client.backoff_until = None
        source.client.cache_ttl = timedelta(0)
        with caplog.at_level(logging.WARNING):
            snap = source(Snapshot(now=NOW))
        assert len(snap.builds) == 2  # the cached list, stale
        assert source.execute(cancel(f"{REPO}:1")) is False
        gh.reply(page_path(1), {"message": "no"}, status=404)
        source.client.backoff_until = None
        with caplog.at_level(logging.WARNING):
            assert source(Snapshot(now=NOW)).builds == ()
        assert "runs not read" in caplog.text
        assert posts(gh) == []
        assert TOKEN not in caplog.text


def test_minutes_from_jobs_are_read_once() -> None:
    with serve() as gh:
        done = run(1, 30, status="completed", conclusion="failure")
        runs_page(gh, [done])
        jobs = f"{RUNS}/1/attempts/1/jobs?per_page=100"
        start = NOW - timedelta(minutes=30)
        gh.reply(
            jobs,
            {
                "total_count": 3,
                "jobs": [
                    {
                        "started_at": iso(start),
                        "completed_at": iso(start + timedelta(seconds=61)),
                        "conclusion": "success",
                    },
                    {
                        "started_at": iso(start),
                        "completed_at": iso(start + timedelta(seconds=5)),
                        "conclusion": "failure",
                    },
                    {"started_at": iso(start), "completed_at": iso(start), "conclusion": "skipped"},
                ],
            },
        )
        clock = Clock(NOW)
        source = ci(gh, clock)
        assert source(Snapshot(now=NOW)).builds[0].minutes == 3.0
        clock.advance(minutes=5)
        assert source(Snapshot(now=clock.now)).builds[0].minutes == 3.0
        assert gh.paths().count(jobs) == 1


def test_partial_job_list_falls_back_to_wall_clock() -> None:
    with serve() as gh:
        runs_page(gh, [run(1, 30, status="completed", conclusion="success")])
        gh.reply(f"{RUNS}/1/attempts/1/jobs?per_page=100", {"total_count": 200, "jobs": []})
        assert ci(gh)(Snapshot(now=NOW)).builds[0].minutes == 5.0


def test_draft_and_changed_paths_from_the_pull_request() -> None:
    with serve() as gh:
        sha = "b" * 40
        pr = [{"number": 7, "head": {"sha": sha}}]
        runs_page(gh, [run(1, 5, event="pull_request", head_sha=sha, pull_requests=pr)])
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
        pr = [{"number": 7}]
        runs_page(gh, [run(1, 5, event="pull_request", pull_requests=pr)])
        gh.pull(REPO, 7, sha="c" * 40, changed_files=2)
        (build,) = ci(gh)(Snapshot(now=NOW)).builds
        assert build.changed_paths is None and build.draft is False
    with serve() as gh:
        runs_page(gh, [run(1, 5, event="pull_request", pull_requests=pr)])
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
        assert source.execute(cancel(f"{REPO}:1")) is True
        assert posts(gh) == [f"{RUNS}/1/cancel"]
        post = next(s for s in gh.requests if s.method == "POST")
        assert post.headers["Authorization"] == f"Bearer {TOKEN}"
        # Idempotent: a second request does nothing.
        assert source.execute(cancel(f"{REPO}:1")) is True
        assert posts(gh) == [f"{RUNS}/1/cancel"]


def test_cancel_refused_when_not_allowed() -> None:
    with serve() as gh:
        superseded_pair(gh)
        for settings in ({}, {"allow_cancel": True, "cancel_repos": (OTHER,)}):
            source = ci(gh, **settings)
            source(Snapshot(now=NOW))
            assert source.execute(cancel(f"{REPO}:1")) is False
        source = ci(gh, cancel_workflows=(".github/workflows/deploy.yml",), **ALLOWED)
        source(Snapshot(now=NOW))
        assert source.execute(cancel(f"{REPO}:1")) is False
        assert posts(gh) == []


def test_cancel_refused_for_runs_the_scan_did_not_report_as_superseded() -> None:
    with serve() as gh:
        superseded_pair(gh)
        source = ci(gh, **ALLOWED)
        assert source.execute(cancel(f"{REPO}:1")) is False  # no scan yet
        source(Snapshot(now=NOW))
        for build_id in (f"{REPO}:2", f"{OTHER}:1", "../../x", 1):
            assert source.execute(cancel(build_id)) is False  # type: ignore[arg-type]
        assert source.execute(Action(ActionKind.STOP, "x", "r", params={})) is False
        assert posts(gh) == []


def test_recheck_before_cancel() -> None:
    with serve() as gh:
        superseded_pair(gh)
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        # Finished meanwhile: nothing to do.
        gh.reply(f"{RUNS}/1", run(1, 20, status="completed", conclusion="success"))
        assert source.execute(cancel(f"{REPO}:1")) is True
        assert posts(gh) == []
    with serve() as gh:
        superseded_pair(gh)
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        # The newer run is not on the same branch any more (or never was): refuse.
        gh.reply(f"{RUNS}/2", run(2, 5, head_branch="task/T-9"))
        assert source.execute(cancel(f"{REPO}:1")) is False
        gh.reply(f"{RUNS}/2", {"message": "Not Found"}, status=404)
        assert source.execute(cancel(f"{REPO}:1")) is False
        assert posts(gh) == []


def test_recheck_that_cannot_reach_github_refuses() -> None:
    with serve() as gh:
        superseded_pair(gh)
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        gh.reply(f"{RUNS}/1", run(1, 20))
        source.client.get(f"{RUNS}/1")  # cached
        source.client.backoff_until = NOW + timedelta(minutes=5)
        assert source.execute(cancel(f"{REPO}:1")) is False  # stale: refused
        assert posts(gh) == []


def test_cancel_conflict_counts_as_done() -> None:
    with serve() as gh:
        superseded_pair(gh)
        gh.reply(f"POST {RUNS}/1/cancel", {"message": "Cannot cancel"}, status=409)
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        assert source.execute(cancel(f"{REPO}:1")) is True


def test_cancel_redirect_is_refused_and_not_followed(caplog: pytest.LogCaptureFixture) -> None:
    with serve() as gh:
        superseded_pair(gh)
        gh.reply(f"POST {RUNS}/1/cancel", None, status=307, Location="http://127.0.0.1:1/steal")
        source = ci(gh, **ALLOWED)
        source(Snapshot(now=NOW))
        with caplog.at_level(logging.DEBUG):
            assert source.execute(cancel(f"{REPO}:1")) is False
        assert len(posts(gh)) == 1  # the original request only
        assert TOKEN not in caplog.text


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
            "max_pages": 2,
            "max_requests_per_tick": 10,
            "tick_budget": "5s",
            "included_minutes": 2000,
            "quota_reset_day": 3,
            "allow_cancel": True,
            "cancel_repos": [REPO],
            "cancel_workflows": [".github/workflows/ci.yml"],
        },
        [REPO, OTHER],
    )
    assert s.repos == (REPO,) and s.allow_cancel and s.cancel_repos == (REPO,)
    assert s.window == timedelta(hours=12) and s.included_minutes == 2000.0


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
        ({"window": "10m"}, "from 1h to 7d"),
        ({"window": "soon"}, "window"),
        ({"tick_budget": "2m"}, "tick_budget"),
        ({"included_minutes": 0}, "included_minutes"),
        ({"max_pages": 11}, "max_pages"),
        ({"quota_reset_day": 31}, "quota_reset_day"),
    ],
)
def test_load_ci_errors(section: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_ci(section, [REPO])
