"""The GitHub open pull requests step (``[connectors.github.pulls]``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.connectors.github.client import GitHubClient
from kelpie.connectors.github.connector import GitHubConnector
from kelpie.connectors.github.pulls import PullRequestSync, PullSettings, load_pulls
from kelpie.connectors.github.settings import load_github
from kelpie.core.model import LabelEvent, Snapshot
from tests.connectors.fake_github import TOKEN, Clock, FakeGitHub, serve

REPO = "example/repo"
NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
LIST = f"/repos/{REPO}/pulls?direction=desc&per_page=100&sort=updated&state=open"


def item(number: int, branch: str = "", **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "number": number,
        "title": f"Change {number}",
        "draft": False,
        "labels": [{"name": n} for n in fields.pop("labels", [])],
        "head": {
            "ref": branch or f"feature/{number}",
            "sha": fields.pop("sha", f"{number:040x}"),
            "repo": {"full_name": fields.pop("head_repo", REPO)},
        },
        "created_at": "2026-03-02T08:00:00Z",
        "updated_at": "2026-03-02T09:00:00Z",
    }
    body.update(fields)
    return body


def step(gh: FakeGitHub, clock: Clock | None = None, **kw: Any) -> PullRequestSync:
    connector = GitHubConnector(GitHubClient(gh.url, TOKEN, clock=clock or Clock(NOW)), [REPO])
    settings = kw.pop("settings", PullSettings())
    return PullRequestSync(connector, settings, **kw)


def detail(gh: FakeGitHub, number: int, state: str, sha: str = "") -> None:
    body = {"number": number, "mergeable_state": state, "head": {"sha": sha or f"{number:040x}"}}
    gh.reply(f"/repos/{REPO}/pulls/{number}", body)


# --- config ---------------------------------------------------------------------------


def test_load_defaults_and_values() -> None:
    assert load_pulls({}) == PullSettings()
    s = load_pulls({"enabled": False, "poll_interval": "5m", "max_details": 0})
    assert (s.enabled, s.poll_interval, s.max_details) == (False, timedelta(minutes=5), 0)
    gh = load_github({"repos": [REPO], "pulls": {}})
    assert gh.pulls == PullSettings()
    assert load_github({"repos": [REPO]}).pulls is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ([], "must be a table"),
        ({"extra": 1}, "unknown"),
        ({"enabled": "yes"}, "true or false"),
        ({"poll_interval": "10s"}, "at least 30s"),
        ({"poll_interval": "soon"}, "poll_interval"),
        ({"max_details": 101}, "0 to 100"),
        ({"max_details": True}, "0 to 100"),
    ],
)
def test_load_rejects(raw: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_pulls(raw)


# --- the step ---------------------------------------------------------------------------


def test_lists_open_prs_with_mergeable_state() -> None:
    with serve() as gh:
        gh.reply(
            LIST,
            [
                item(1, "hermes/t_1", labels=["merge-hold"]),
                item(2, draft=True),
                item(3),
                item(4),
                "junk",
                {"number": "5"},
            ],
        )
        detail(gh, 1, "blocked")
        detail(gh, 3, "clean")
        detail(gh, 4, "unknown")
        snap = step(gh)(Snapshot(now=NOW))
    by = {p.number: p for p in snap.pull_requests}
    assert sorted(by) == [1, 2, 3, 4]
    assert by[1].branch == "hermes/t_1" and by[1].labels == ("merge-hold",)
    assert by[1].mergeable is False and by[3].mergeable is True and by[4].mergeable is None
    assert by[2].draft and by[2].mergeable is False
    assert by[3].ref == f"{REPO}#3" and by[3].source == "github"
    assert by[3].updated_at == datetime(2026, 3, 2, 9, tzinfo=UTC)
    # Drafts are never looked up.
    assert f"/repos/{REPO}/pulls/2" not in gh.paths()


def test_fork_branch_links_nothing() -> None:
    with serve() as gh:
        gh.reply(LIST, [item(1, "main", head_repo="someone/repo"), item(2, head_repo=None)])
        snap = step(gh, settings=PullSettings(max_details=0))(Snapshot(now=NOW))
    assert [p.branch for p in snap.pull_requests] == [None, None]


def test_serves_the_last_scan_between_polls_and_keeps_it_on_errors() -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(LIST, [item(1)])
        detail(gh, 1, "clean")
        s = step(gh, clock)
        assert len(s(Snapshot(now=NOW)).pull_requests) == 1
        n = len(gh.requests)
        later = NOW + timedelta(minutes=1)
        assert len(s(Snapshot(now=later)).pull_requests) == 1
        assert len(gh.requests) == n  # within poll_interval: no request
        gh.reply(LIST, {"message": "boom"}, status=500)
        clock.advance(minutes=5)
        again = s(Snapshot(now=NOW + timedelta(minutes=5)))
    assert [p.number for p in again.pull_requests] == [1]
    assert again.pull_requests[0].mergeable is True


def test_mergeable_is_known_only_for_the_head_it_was_read_for() -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(LIST, [item(1, sha="a" * 40)])
        detail(gh, 1, "clean", "a" * 40)
        s = step(gh, clock, settings=PullSettings(max_details=1))
        assert s(Snapshot(now=NOW)).pull_requests[0].mergeable is True
        gh.reply(LIST, [item(1, sha="b" * 40)])
        gh.reply(f"/repos/{REPO}/pulls/1", {"message": "down"}, status=500)
        clock.advance(minutes=3)
        snap = s(Snapshot(now=NOW + timedelta(minutes=3)))
    assert snap.pull_requests[0].mergeable is None  # the new head has not been looked up


def test_an_answer_about_another_head_is_not_used() -> None:
    with serve() as gh:
        gh.reply(LIST, [item(1, sha="a" * 40)])
        detail(gh, 1, "clean", "b" * 40)  # the PR moved between the list and the lookup
        snap = step(gh)(Snapshot(now=NOW))
    assert snap.pull_requests[0].mergeable is None


def test_max_details_looks_up_unknown_heads_first() -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(LIST, [item(1), item(2)])
        detail(gh, 1, "clean")
        detail(gh, 2, "dirty")
        s = step(gh, clock, settings=PullSettings(max_details=1))
        s(Snapshot(now=NOW))
        clock.advance(minutes=3)
        snap = s(Snapshot(now=NOW + timedelta(minutes=3)))
    looked = [p for p in gh.paths() if p.startswith(f"/repos/{REPO}/pulls/")]
    assert looked == [f"/repos/{REPO}/pulls/1", f"/repos/{REPO}/pulls/2"]
    assert {p.number: p.mergeable for p in snap.pull_requests} == {1: True, 2: False}


def events(*entries: tuple[str, str, str]) -> list[dict[str, Any]]:
    return [{"event": e, "label": {"name": n}, "created_at": at} for e, n, at in entries]


def test_hold_label_times_are_read_once_per_label() -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(LIST, [item(1, labels=["merge-hold", "bug"]), item(2, labels=["bug"])])
        gh.reply(
            f"/repos/{REPO}/issues/1/events?per_page=100",
            events(
                ("labeled", "merge-hold", "2026-03-02T07:00:00Z"),
                ("unlabeled", "merge-hold", "2026-03-02T07:30:00Z"),
                ("labeled", "merge-hold", "2026-03-02T08:00:00Z"),
                ("labeled", "bug", "2026-03-02T08:30:00Z"),
            ),
        )
        s = step(gh, clock, settings=PullSettings(max_details=0), watch_labels={"merge-hold"})
        first = s(Snapshot(now=NOW)).pull_requests
        clock.advance(minutes=3)
        s(Snapshot(now=NOW + timedelta(minutes=3)))
    held = {p.number: p.labeled for p in first}
    assert held[1] == (LabelEvent("merge-hold", datetime(2026, 3, 2, 8, tzinfo=UTC)),)
    assert held[2] == ()  # not watched: never looked up
    assert gh.paths().count(f"/repos/{REPO}/issues/1/events?per_page=100") == 1
    assert f"/repos/{REPO}/issues/2/events?per_page=100" not in gh.paths()


@pytest.mark.parametrize(
    "body",
    [
        events(
            ("labeled", "merge-hold", "2026-03-02T07:00:00Z"),
            ("unlabeled", "merge-hold", "2026-03-02T07:30:00Z"),
        ),
        events(("labeled", "merge-hold", "yesterday")),
        events(*[("labeled", "merge-hold", "2026-03-02T07:00:00Z")] * 100),  # a full page
        {"message": "odd"},
    ],
)
def test_unknown_label_time(body: Any) -> None:
    with serve() as gh:
        gh.reply(LIST, [item(1, labels=["merge-hold"])])
        gh.reply(f"/repos/{REPO}/issues/1/events?per_page=100", body)
        s = step(gh, settings=PullSettings(max_details=0), watch_labels={"merge-hold"})
        snap = s(Snapshot(now=NOW))
    assert snap.pull_requests[0].labeled == ()


def test_a_label_put_back_is_read_again() -> None:
    clock = Clock(NOW)
    path = f"/repos/{REPO}/issues/1/events?per_page=100"
    with serve() as gh:
        gh.reply(LIST, [item(1, labels=["merge-hold"])])
        gh.reply(path, events(("labeled", "merge-hold", "2026-03-02T08:00:00Z")))
        s = step(gh, clock, settings=PullSettings(max_details=0), watch_labels={"merge-hold"})
        s(Snapshot(now=NOW))
        gh.reply(LIST, [item(1, updated_at="2026-03-02T11:00:00Z")])  # label taken off
        clock.advance(minutes=3)
        s(Snapshot(now=NOW + timedelta(minutes=3)))
        gh.reply(LIST, [item(1, labels=["merge-hold"])])
        gh.reply(path, events(("labeled", "merge-hold", "2026-03-02T11:30:00Z")))
        clock.advance(minutes=3)
        snap = s(Snapshot(now=NOW + timedelta(minutes=6)))
    assert snap.pull_requests[0].labeled[0].at == datetime(2026, 3, 2, 11, 30, tzinfo=UTC)
    assert gh.paths().count(path) == 2


def test_a_failed_label_lookup_is_retried() -> None:
    clock = Clock(NOW)
    path = f"/repos/{REPO}/issues/1/events?per_page=100"
    with serve() as gh:
        gh.reply(LIST, [item(1, labels=["merge-hold"])])
        gh.reply(path, {"message": "down"}, status=500)
        s = step(gh, clock, settings=PullSettings(max_details=0), watch_labels={"merge-hold"})
        assert s(Snapshot(now=NOW)).pull_requests[0].labeled == ()
        gh.reply(path, events(("labeled", "merge-hold", "2026-03-02T08:00:00Z")))
        clock.advance(minutes=3)
        snap = s(Snapshot(now=NOW + timedelta(minutes=3)))
    assert len(snap.pull_requests[0].labeled) == 1


def test_a_bug_in_the_scan_does_not_cost_the_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    with serve() as gh:
        s = step(gh)

        def broken(now: datetime) -> None:
            raise RuntimeError("bug")

        monkeypatch.setattr(PullRequestSync, "_scan", lambda self, now: broken(now))
        snap = s(Snapshot(now=NOW))
    assert snap.pull_requests == ()


def test_fork_prs_are_marked() -> None:
    with serve() as gh:
        gh.reply(LIST, [item(1, "main", head_repo="someone/repo"), item(2)])
        snap = step(gh, settings=PullSettings(max_details=0))(Snapshot(now=NOW))
    assert [(p.number, p.fork) for p in snap.pull_requests] == [(1, True), (2, False)]


def test_full_pages_are_followed_up_to_three() -> None:
    page = "/repos/example/repo/pulls?direction=desc&page={}&per_page=100&sort=updated&state=open"
    with serve() as gh:
        gh.reply(LIST, [item(n) for n in range(1, 101)])
        gh.reply(page.format(2), [item(n) for n in range(101, 201)])
        gh.reply(page.format(3), [item(n) for n in range(201, 301)])
        snap = step(gh, settings=PullSettings(max_details=0))(Snapshot(now=NOW))
    assert len(snap.pull_requests) == 300
    assert page.format(4) not in gh.paths()


def test_a_repository_that_stops_answering_is_dropped_after_three_scans(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(LIST, [item(1)])
        s = step(gh, clock, settings=PullSettings(max_details=0))
        assert len(s(Snapshot(now=NOW)).pull_requests) == 1
        gh.reply(LIST, {"message": "Bad credentials"}, status=401)
        kept = []
        for minutes in (2, 4, 6, 8):
            clock.now = NOW + timedelta(minutes=minutes)
            kept.append(len(s(Snapshot(now=clock.now)).pull_requests))
    assert kept == [1, 1, 1, 0]  # 3 x poll_interval of grace, then left out
    assert "no fresh list of open PRs" in caplog.text


def test_an_old_cached_label_answer_is_not_remembered() -> None:
    clock = Clock(NOW)
    path = f"/repos/{REPO}/issues/1/events?per_page=100"
    with serve() as gh:
        gh.reply(LIST, [item(1, labels=["merge-first"])])
        gh.reply(path, events(("labeled", "merge-first", "2026-03-02T08:00:00Z")))
        s = step(gh, clock, settings=PullSettings(max_details=0), watch_labels={"merge-first"})
        s.connector.client.get(f"/repos/{REPO}/issues/1/events", {"per_page": "100"})  # cached
        clock.advance(minutes=5)
        gh.reply(path, {"message": "down"}, status=502)  # GitHub fails: the cache is stale
        snap = s(Snapshot(now=clock.now))
        assert snap.pull_requests[0].labeled == ()  # the stale page is not used
        gh.reply(path, events(("labeled", "merge-first", "2026-03-02T09:00:00Z")))
        clock.advance(minutes=60)
        snap = s(Snapshot(now=clock.now))
    assert snap.pull_requests[0].labeled[0].at == datetime(2026, 3, 2, 9, tzinfo=UTC)


def test_an_unknown_label_time_is_asked_again_later() -> None:
    clock = Clock(NOW)
    path = f"/repos/{REPO}/issues/1/events?per_page=100"
    with serve() as gh:
        gh.reply(LIST, [item(1, labels=["merge-first"])])
        gh.reply(path, [])  # no labeled event yet (or not on the first page)
        s = step(gh, clock, settings=PullSettings(max_details=0), watch_labels={"merge-first"})
        s(Snapshot(now=NOW))
        clock.advance(minutes=3)
        s(Snapshot(now=clock.now))
        assert gh.paths().count(path) == 1  # not every scan
        gh.reply(path, events(("labeled", "merge-first", "2026-03-02T08:00:00Z")))
        clock.advance(minutes=30)
        snap = s(Snapshot(now=clock.now))
    assert gh.paths().count(path) == 2 and len(snap.pull_requests[0].labeled) == 1


def test_a_spent_budget_is_reported_once(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(LIST, [item(1), item(2)])
        detail(gh, 1, "clean")
        detail(gh, 2, "clean")
        s = step(gh, clock)
        s.connector.client.begin_tick(2, timedelta(seconds=10))  # the list and one PR
        snap = s(Snapshot(now=NOW))
        clock.advance(minutes=3)
        s.connector.client.begin_tick(2, timedelta(seconds=10))
        s(Snapshot(now=clock.now))
    assert sum(p.mergeable is True for p in snap.pull_requests) == 1
    assert caplog.text.count("request budget ran out") == 1


# --- which task a PR belongs to (incident 17) -------------------------------------------


def test_the_step_links_prs_to_the_snapshot_tasks() -> None:
    from kelpie.core.model import Status, Task

    tasks = (
        Task("t_7a", "Fix it", Status.REVIEW, NOW),
        Task("t_8b", "Other", Status.REVIEW, NOW),
    )
    with serve() as gh:
        gh.reply(
            LIST,
            [
                item(1, "hermes/t_7a"),
                # Its description names t_8b: a mention never links (incident 17).
                item(2, "feature/other", body="Unblocks t_8b, see also t_7a"),
            ],
        )
        s = step(gh, settings=PullSettings(max_details=0))
        snap = s(Snapshot(now=NOW, tasks=tasks))
        again = s(Snapshot(now=NOW + timedelta(seconds=30), tasks=tasks))
    assert {p.number: p.task_ids for p in snap.pull_requests} == {1: ("t_7a",), 2: ()}
    assert again.pull_requests == snap.pull_requests  # worked out once, served again


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("(", "task_code"),
        ("", "task_code"),
        (" ", "task_code"),
        ("(IOS)-[0-9]+", "capturing"),
        ("(?:[A-Z]+-?)+[0-9]+", "repeat a group"),
        (3, "task_code"),
    ],
)
def test_task_code_is_validated(raw: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_pulls({"task_code": raw})
    assert load_pulls({"task_code": "[A-Z]+-[0-9]+"}).task_code is not None
