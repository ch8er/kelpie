"""Reading GitHub Issues over HTTP (a local fake GitHub): lists, linked pull requests,
per-issue details, the request budget and errors."""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.adapters.issues.adapter import IssuesAdapter
from kelpie.adapters.issues.github import (
    DETAIL_TTL,
    MAX_PAGES,
    PAGE,
    PULL_PAGE,
    GitHubIssues,
    IssuesError,
    closes,
)
from kelpie.adapters.issues.mapping import Agent, Board
from kelpie.connectors.github.client import GitHubClient
from kelpie.core.model import Dependency, Status
from kelpie.core.ports import UnsupportedAction
from tests.connectors.fake_github import TOKEN, Clock, FakeGitHub, serve

REPO = "acme/app"
NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)


def iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def ago(**kw: float) -> str:
    return iso(NOW - timedelta(**kw))


def route(path: str, **query: str) -> str:
    return path + ("?" + urllib.parse.urlencode(sorted(query.items())) if query else "")


def issue(number: int, **fields: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "number": number,
        "title": f"Issue {number}",
        "state": "open",
        "state_reason": None,
        "created_at": ago(days=2),
        "updated_at": ago(minutes=5),
        "closed_at": None,
        "body": "Do the thing.",
        "user": {"login": "alice", "type": "User"},
        "labels": [],
        "assignees": [],
        "issue_dependencies_summary": {"blocked_by": 0, "total_blocked_by": 0},
        "sub_issues_summary": {"total": 0},
        "html_url": f"https://github.example/{REPO}/issues/{number}",
    }
    body.update(fields)
    return body


def pull(number: int, body: str, branch: str, **fields: Any) -> dict[str, Any]:
    out = {
        "number": number,
        "body": body,
        "draft": False,
        "created_at": ago(hours=1),
        "user": {"login": "copilot-swe-agent[bot]"},
        "head": {"ref": branch, "repo": {"full_name": REPO}},
    }
    out.update(fields)
    return out


class Repo:
    """The fake's answers for one repository's lists."""

    def __init__(self, gh: FakeGitHub, repo: str = REPO, label: str | None = None) -> None:
        self.gh, self.repo, self.label = gh, repo, label

    def lists(
        self,
        open_: list[dict[str, Any]],
        closed: list[dict[str, Any]] | None = None,
        pulls: list[dict[str, Any]] | None = None,
        at: datetime = NOW,
    ) -> None:
        base = f"/repos/{self.repo}/issues"
        scope = {"labels": self.label} if self.label else {}
        query = {"state": "open", "sort": "created", "direction": "desc", **scope}
        for page in range(1, MAX_PAGES + 1):
            chunk = open_[(page - 1) * PAGE : page * PAGE]
            self.gh.reply(route(base, **query, per_page=str(PAGE), page=str(page)), chunk)
        since = {"state": "closed", "since": iso(at - timedelta(days=7)), "sort": "updated"}
        since |= scope
        self.gh.reply(route(base, **since, per_page=str(PAGE), page="1"), closed or [])
        pulls = pulls or []
        for page in range(1, 3):
            chunk = pulls[(page - 1) * PULL_PAGE : page * PULL_PAGE]
            path = f"/repos/{self.repo}/pulls"
            self.gh.reply(route(path, state="open", per_page=str(PULL_PAGE), page=str(page)), chunk)

    def detail(self, number: int, what: str, body: Any, page: int = 1) -> None:
        path = f"/repos/{self.repo}/issues/{number}/{what}"
        self.gh.reply(route(path, per_page="100", page=str(page)), body)


@pytest.fixture
def gh() -> Iterator[FakeGitHub]:
    with serve() as fake:
        yield fake


def reader(gh: FakeGitHub, clock: Clock | None = None, **kw: Any) -> GitHubIssues:
    clock = clock or Clock(NOW)
    client = GitHubClient(gh.url, TOKEN, clock=clock, cache_ttl=timedelta(seconds=30))
    boards = kw.pop("boards", (Board(REPO),))
    return GitHubIssues(client, boards, timedelta(days=7), clock=clock, **kw)


AGENTS = {"copilot": Agent("copilot", assignee="Copilot", author="copilot-swe-agent[bot]")}


def test_closing_keywords() -> None:
    body = "Fixes #12, closes acme/app#13 and resolves: #14.\nSee #15; fixes other/repo#16"
    assert closes(body, REPO) == [12, 13, 14]
    assert closes("Prefix#12 and fixes#3", REPO) == []
    assert closes("FIXED #4 fixed #4", REPO) == [4]
    url = "Fixes https://github.com/acme/app/issues/21 and https://github.com/x/y/issues/22"
    assert closes(url + " fixes https://github.com/x/y/issues/23", REPO) == [21]


def test_issues_become_tasks(gh: FakeGitHub) -> None:
    repo = Repo(gh)
    held = issue(
        2,
        assignees=[{"login": "Copilot"}],
        labels=[{"name": "enhancement"}],
        issue_dependencies_summary={"blocked_by": 1, "total_blocked_by": 2},
    )
    repo.lists(
        [issue(1), held, issue(3, pull_request={"url": "x"})],
        closed=[issue(4, state="closed", state_reason="completed", closed_at=ago(hours=3))],
        pulls=[
            pull(9, "Fixes #2", "copilot/fix-2", draft=True),
            pull(10, "Fixes #1", "fork-branch", head={"ref": "x", "repo": {"full_name": "e/vil"}}),
        ],
    )
    repo.detail(
        2,
        "events",
        [
            {"event": "assigned", "created_at": ago(hours=1), "assignee": {"login": "Copilot"}},
            {"event": "labeled", "created_at": ago(days=1), "label": {"name": "enhancement"}},
            {"event": "mentioned", "created_at": ago(hours=1)},
        ],
    )
    repo.detail(
        2,
        "dependencies/blocked_by",
        [
            {"number": 1, "state": "open", "repository_url": f"https://api.x/repos/{REPO}"},
            {"number": 5, "state": "closed", "repository_url": f"https://api.x/repos/{REPO}"},
            {"number": 6, "state": "open", "repository_url": "https://api.x/repos/Acme/Lib"},
        ],
    )
    adapter = IssuesAdapter(reader(gh), (Board(REPO),), AGENTS)
    snap = adapter.snapshot(NOW)
    tasks = {t.id: t for t in snap.tasks}
    assert set(tasks) == {f"{REPO}#1", f"{REPO}#2", f"{REPO}#4"}  # the pull request is no task
    one, two, four = tasks[f"{REPO}#1"], tasks[f"{REPO}#2"], tasks[f"{REPO}#4"]
    assert (one.status, one.branch) == (Status.BACKLOG, None)  # a fork links nothing
    assert (two.status, two.owner_id, two.branch) == (
        Status.IN_PROGRESS,
        "copilot",
        "copilot/fix-2",
    )
    assert two.status_since == NOW - timedelta(hours=1)
    assert four.status is Status.DONE
    assert set(snap.dependencies) == {
        Dependency(f"{REPO}#2", f"{REPO}#1"),
        Dependency(f"{REPO}#2", "acme/lib#6"),
    }
    # Issues without labels or assignees need no events; nothing asks for #1's.
    assert not any("/issues/1/" in p for p in gh.paths())
    assert all(s.headers["Authorization"] == f"Bearer {TOKEN}" for s in gh.requests)
    assert adapter.check() is None
    assert adapter.status_detail(NOW)[-1] == "issues: 3 tasks, 1 held, read 0m ago"


def test_sub_issues(gh: FakeGitHub) -> None:
    repo = Repo(gh)
    repo.lists([issue(1, sub_issues_summary={"total": 2}), issue(2), issue(3)])
    repo.detail(
        1,
        "sub_issues",
        [
            {"number": 2, "state": "open", "repository_url": f"https://a/repos/{REPO}"},
            {"number": 3, "state": "closed", "repository_url": f"https://a/repos/{REPO}"},
        ],
    )
    items = {i.id: i for i in reader(gh).read(NOW)}
    assert items[f"{REPO}#1"].sub_items == (f"{REPO}#2", f"{REPO}#3")


def test_label_scope_is_asked_of_github(gh: FakeGitHub) -> None:
    Repo(gh, label="agent").lists([issue(1, labels=[{"name": "agent"}])])
    Repo(gh).lists([issue(1), issue(2)])
    items = reader(gh, boards=(Board(REPO, label="agent"),)).read(NOW)
    assert [i.id for i in items] == [f"{REPO}#1"]


def test_details_are_read_again_only_when_the_issue_changes(gh: FakeGitHub) -> None:
    clock = Clock(NOW)
    repo = Repo(gh)
    held = issue(1, assignees=[{"login": "Copilot"}])
    repo.lists([held])
    repo.detail(1, "events", [])
    issues = reader(gh, clock)
    issues.read(NOW)
    events = route(f"/repos/{REPO}/issues/1/events", per_page="100", page="1")
    count = lambda: sum(p == events for p in gh.paths())  # noqa: E731
    assert count() == 1
    clock.advance(minutes=1)
    issues.read(NOW)  # the client's clock moves; the closed-list window does not
    assert count() == 1  # unchanged: kept
    repo.lists([{**held, "updated_at": iso(clock.now)}])
    clock.advance(minutes=1)
    issues.read(NOW)
    assert count() == 2  # changed: read again
    clock.advance(seconds=DETAIL_TTL.total_seconds() + 1)
    issues.read(NOW)
    assert count() == 3  # too old: read again


def test_a_detail_that_fails_falls_back(gh: FakeGitHub) -> None:
    repo = Repo(gh)
    repo.lists([issue(1, assignees=[{"login": "Copilot"}], updated_at=ago(minutes=7))])
    gh.reply(route(f"/repos/{REPO}/issues/1/events", per_page="100", page="1"), {"m": "x"}, 500)
    (item,) = reader(gh).read(NOW)
    assert item.events is None  # the mapping then uses updated_at


def test_the_request_budget_caps_details(gh: FakeGitHub) -> None:
    repo = Repo(gh)
    many = [issue(n, assignees=[{"login": "Copilot"}]) for n in range(1, 31)]
    repo.lists(many)
    for n in range(1, 31):
        repo.detail(n, "events", [])
    items = reader(gh, max_requests=10).read(NOW)
    assert len(items) == 30
    asked = [p for p in gh.paths() if p.endswith("/events?page=1&per_page=100")]
    assert len(asked) == 10  # the lists are outside the request budget: details only
    assert sum(i.events is not None for i in items) == len(asked)


def test_a_list_that_cannot_be_read_fails_the_snapshot(gh: FakeGitHub) -> None:
    adapter = IssuesAdapter(reader(gh), (Board(REPO),), AGENTS)  # nothing routed: 404
    with pytest.raises(IssuesError) as err:
        adapter.snapshot(NOW)
    assert err.value.public_reason == "GitHub could not be read (not_found)"
    assert adapter.check() == "GitHub could not be read (not_found)"
    Repo(gh).lists([issue(1)])
    gh.reply(
        route(
            f"/repos/{REPO}/issues",
            state="open",
            sort="created",
            direction="desc",
            per_page=str(PAGE),
            page="1",
        ),
        {"not": "a list"},
    )
    with pytest.raises(IssuesError):
        reader(gh).read(NOW)


def test_long_lists_are_paged_and_capped(gh: FakeGitHub, caplog: pytest.LogCaptureFixture) -> None:
    repo = Repo(gh)
    repo.lists([issue(n, updated_at=ago(days=1)) for n in range(1, 521)])
    items = reader(gh, max_requests=100).read(NOW)
    assert len(items) == 500
    assert "more than 10 pages" in caplog.text


def test_bad_rows_are_skipped(gh: FakeGitHub, caplog: pytest.LogCaptureFixture) -> None:
    Repo(gh).lists([issue(1), {"number": "x"}, "junk", issue(2, created_at=None), issue(1)])
    items = reader(gh).read(NOW)
    assert [i.id for i in items] == [f"{REPO}#1"]
    assert "without times or title" in caplog.text


def test_snapshot_needs_an_aware_time(gh: FakeGitHub) -> None:
    with pytest.raises(ValueError):
        IssuesAdapter(reader(gh), (Board(REPO),), AGENTS).snapshot(datetime(2026, 1, 1))


def test_list_get_and_events(gh: FakeGitHub) -> None:
    Repo(gh).lists([issue(1)])
    adapter = IssuesAdapter(reader(gh), (Board(REPO),), AGENTS, clock=lambda: NOW)
    assert [t.id for t in adapter.list_tasks()] == [f"{REPO}#1"]
    assert adapter.get_task(f"{REPO}#1") is not None and adapter.get_task("x#9") is None
    assert list(adapter.events(NOW)) == []
    with pytest.raises(UnsupportedAction):
        adapter.wip_commit(f"{REPO}#1", "m")
    assert not any(s.method != "GET" for s in gh.requests)


# --- hostile input (security review) ---------------------------------------------------------


def test_closing_keyword_search_is_linear_and_capped() -> None:
    for gap in (" ", "\n", "\t"):
        start = time.monotonic()
        assert closes("fix" + gap * 65_533, REPO) == []
        assert closes("fix " + "https://" + "a" * 65_000, REPO) == []
        assert time.monotonic() - start < 1.0
    many = " ".join(f"fixes #{n}" for n in range(1, 7000))
    assert closes(many, REPO) == list(range(1, 21))  # one pull request closes at most 20
    assert closes("fixes #0 fixes #1", REPO) == [1]


def test_a_board_that_cannot_be_read_is_served_from_its_last_read_for_a_while(
    gh: FakeGitHub,
) -> None:
    clock = Clock(NOW)
    other = "acme/lib"
    Repo(gh).lists([issue(1)])
    Repo(gh, other).lists([issue(7)])
    boards = (Board(REPO), Board(other))
    issues = reader(gh, clock, boards=boards)
    assert len(issues.read(NOW)) == 2 and issues.problem is None
    broken = route(
        f"/repos/{REPO}/issues",
        state="open",
        sort="created",
        direction="desc",
        per_page=str(PAGE),
        page="1",
    )
    gh.reply(broken, {"message": "Not Found"}, 404)  # one repository only (no back-off)
    clock.advance(minutes=5)
    later = NOW + timedelta(minutes=5)
    Repo(gh, other).lists([issue(7)], at=later)
    assert {i.id for i in issues.read(later)} == {f"{REPO}#1", f"{other}#7"}
    assert issues.problem == f"{REPO}: last read {iso(NOW)} (GitHub could not be read (not_found))"
    clock.advance(minutes=6)
    gone = NOW + timedelta(minutes=11)
    Repo(gh, other).lists([issue(7)], at=gone)
    assert {i.id for i in issues.read(gone)} == {f"{other}#7"}  # past the grace: left out
    assert issues.problem is not None and issues.problem.startswith(f"{REPO}: not read")
    solo = reader(gh, clock)
    with pytest.raises(IssuesError):  # no board at all: the snapshot fails
        solo.read(gone)


def test_unreadable_pull_requests_degrade_and_say_so(gh: FakeGitHub) -> None:
    Repo(gh).lists([issue(1)], pulls=[pull(9, "Fixes #1", "b")])
    gh.reply(
        route(f"/repos/{REPO}/pulls", state="open", per_page=str(PULL_PAGE), page="1"),
        b"x" * 10,
    )
    adapter = IssuesAdapter(reader(gh), (Board(REPO),), AGENTS)
    (task,) = adapter.snapshot(NOW).tasks
    assert task.status is Status.BACKLOG and task.branch is None  # read without links
    assert (
        adapter.check()
        == f"{REPO}: pull requests not read (GitHub could not be read (bad_response))"
    )


def test_only_trusted_authors_state_acceptance_criteria(gh: FakeGitHub) -> None:
    Repo(gh).lists(
        [
            issue(1, author_association="NONE", user={"login": "stranger", "type": "User"}),
            issue(2, author_association="MEMBER"),
            issue(3, author_association="CONTRIBUTOR"),
        ]
    )
    tasks = IssuesAdapter(reader(gh), (Board(REPO),), AGENTS).snapshot(NOW).tasks
    assert [t.body_author for t in tasks] == [None, "alice", None]


def test_values_from_github_are_checked_and_bounded(gh: FakeGitHub) -> None:
    Repo(gh).lists(
        [
            issue(True),
            issue(-3),
            issue(10**10),
            issue(
                1,
                title="Fix \x1b[2Jlogin‮" + "x" * 400,
                labels=[{"name": "ok"}, {"name": "bad\nlabel"}, {"name": "y" * 51}, {"name": 3}],
                assignees=[{"login": "Copilot"}, {"login": "no such\x1blogin"}],
                sub_issues_summary={"total": 1},
                user={"login": "evil\nuser"},
            ),
        ],
        pulls=[
            pull(5, "Fixes #1", "feature/\x1bx", draft=True),
            pull(True, "Fixes #1", "b"),
        ],
    )
    Repo(gh).detail(
        1,
        "events",
        [
            {"event": "labeled", "created_at": ago(hours=1), "label": {"name": "bad\x00"}},
            {"event": "assigned", "created_at": ago(hours=2), "assignee": {"login": "Copilot"}},
        ],
    )
    Repo(gh).detail(
        1,
        "sub_issues",
        [
            {"number": 2, "state": "open", "repository_url": "https://a/repos/" + "z" * 5000},
            {"number": 3, "state": "open", "repository_url": "https://a/repos/a/b\n"},
            {"number": 4, "state": "open", "repository_url": f"https://a/repos/{REPO}"},
        ],
    )
    (item,) = reader(gh).read(NOW)
    assert item.id == f"{REPO}#1"
    assert len(item.title) == 300 and "\x1b" not in item.title and "‮" not in item.title
    assert item.labels == ("ok",)
    assert item.assignees == ("Copilot",)
    assert item.author is None
    assert item.events is not None and [e.kind for e in item.events] == ["assigned"]
    assert item.sub_items == (f"{REPO}#4",)
    (link,) = item.pulls
    assert (link.number, link.branch) == (5, None)


def test_a_list_from_a_backed_off_client_is_not_current(gh: FakeGitHub) -> None:
    clock = Clock(NOW)
    Repo(gh).lists([issue(1)])
    issues = reader(gh, clock)
    issues.read(NOW)
    broken = route(
        f"/repos/{REPO}/issues",
        state="open",
        sort="created",
        direction="desc",
        per_page=str(PAGE),
        page="1",
    )
    gh.reply(broken, {"message": "boom"}, 502)
    clock.advance(minutes=5)
    issues.read(NOW)  # the client hands back its old page marked stale: served as last read
    assert issues.problem == f"{REPO}: last read {iso(NOW)} (GitHub could not be read just now)"


def test_every_boards_lists_come_before_any_details(gh: FakeGitHub) -> None:
    boards = (Board(REPO), Board("acme/lib"))
    for board in boards:
        repo = Repo(gh, board.key)
        repo.lists([issue(n, assignees=[{"login": "Copilot"}]) for n in range(1, 31)])
        for n in range(1, 31):
            repo.detail(n, "events", [])
    items = reader(gh, max_requests=12, boards=boards).read(NOW)
    assert len(items) == 60  # both boards, though details alone could spend the budget
    lists = [p for p in gh.paths() if "/events" not in p]
    assert len(lists) == 6 and all(gh.paths().index(p) < 6 for p in lists)


def test_an_issue_url_links_only_on_githubs_own_host() -> None:
    assert closes("fixes https://evil.example/acme/app/issues/6", REPO) == []
    assert closes("fixes https://GitHub.com/acme/app/issues/6", REPO) == [6]
    ghe = "fixes https://ghe.example.com/acme/app/issues/7"
    assert closes(ghe, REPO, "ghe.example.com") == [7] and closes(ghe, REPO) == []
    assert closes("fixes acme/app/issues/8", REPO) == []  # no host: not GitHub's link


def test_unreadable_pull_requests_keep_their_last_links_for_a_while(gh: FakeGitHub) -> None:
    clock = Clock(NOW)
    Repo(gh).lists([issue(1)], pulls=[pull(9, "Fixes #1", "b")])
    issues = reader(gh, clock)
    (item,) = issues.read(NOW)
    assert [p.number for p in item.pulls] == [9]
    Repo(gh).lists([issue(1)], at=NOW + timedelta(minutes=5))
    gh.reply(
        route(f"/repos/{REPO}/pulls", state="open", per_page=str(PULL_PAGE), page="1"),
        {"message": "Not Found"},
        404,
    )
    clock.advance(minutes=5)
    (item,) = issues.read(NOW + timedelta(minutes=5))
    assert [p.number for p in item.pulls] == [9]
    assert (
        issues.problem
        == f"{REPO}: pull requests last read {iso(NOW)} (GitHub could not be read (not_found))"
    )


def test_first_seen_is_never_after_the_last_change(gh: FakeGitHub) -> None:
    Repo(gh).lists([issue(1, assignees=[{"login": "Copilot"}], updated_at=ago(hours=5))])
    gh.reply(route(f"/repos/{REPO}/issues/1/events", per_page="100", page="1"), {}, 500)
    adapter = IssuesAdapter(reader(gh), (Board(REPO),), AGENTS)
    (task,) = adapter.snapshot(NOW).tasks
    assert task.status_since == NOW - timedelta(hours=5)
    Repo(gh).lists([issue(1, assignees=[{"login": "Copilot"}], updated_at=ago(minutes=1))])
    (task,) = adapter.snapshot(NOW + timedelta(minutes=2)).tasks  # someone commented
    assert task.status_since == NOW - timedelta(hours=5)


def test_lists_are_read_whatever_the_request_budget(gh: FakeGitHub) -> None:
    boards = tuple(Board(f"acme/r{n}") for n in range(4))
    for board in boards:
        Repo(gh, board.key).lists([issue(1)])
    items = reader(gh, max_requests=1, boards=boards).read(NOW)
    assert len(items) == 4


def test_a_blocker_list_that_cannot_be_read_keeps_the_issue_out_of_ready(gh: FakeGitHub) -> None:
    Repo(gh).lists(
        [issue(1, labels=[{"name": "go"}], issue_dependencies_summary={"blocked_by": 1})]
    )
    gh.reply(route(f"/repos/{REPO}/issues/1/events", per_page="100", page="1"), [])
    board = (Board(REPO, ready_labels=("go",)),)
    (task,) = IssuesAdapter(reader(gh, boards=board), board, AGENTS).snapshot(NOW).tasks
    assert task.status is Status.BACKLOG and task.platform_status == "open, go, blockers not read"


def test_lists_are_read_whatever_the_time_budget(gh: FakeGitHub) -> None:
    boards = tuple(Board(f"acme/r{n}") for n in range(3))
    for board in boards:
        Repo(gh, board.key).lists([issue(1)])
    items = reader(gh, tick_budget=timedelta(microseconds=1), boards=boards).read(NOW)
    assert len(items) == 3


def test_an_enterprise_url_with_a_port_links() -> None:
    body = "fixes https://ghe.example.com:8443/acme/app/issues/9"
    assert closes(body, REPO, "ghe.example.com") == [9]
