"""Reading GitHub Issues as :class:`~kelpie.adapters.issues.mapping.Item` through the GitHub
connector's client (:class:`~kelpie.connectors.github.client.GitHubClient`): its cache with
conditional requests, its rate-limit and secondary-limit back-off and its per-tick budget.

Per repository and tick:

- ``GET /repos/{repo}/issues?state=open``, newest first (pages of 50, at most
  ``MAX_PAGES``), and the closed issues updated within ``lookback`` (``state=closed&since=``,
  one page). Pull requests in these lists are left out.
- ``GET /repos/{repo}/pulls?state=open`` (pages of 10, at most 30: their descriptions can be
  long): the pull requests whose description closes an issue of the same repository
  (``Fixes #12``). Forks link nothing. When they cannot be read, the last links are kept
  for ``BOARD_GRACE`` and the problem is reported.
- Every repository's lists first (outside ``max_requests`` and ``tick_budget``), then,
  within them, per issue and only when needed: its events (``/issues/{n}/events``, up to
  ``EVENT_PAGES`` pages: when an agent was assigned or labelled, when a block label was put
  on) for open issues with assignees or labels; the issues blocking it
  (``/dependencies/blocked_by``) when GitHub counts any; its sub-issues when it has some.
  What they say is kept until the issue changes (``updated_at``), at most ``DETAIL_TTL``.

A repository whose lists cannot be read as GitHub has them now is served from its last good
read for up to ``BOARD_GRACE`` (its tasks do not vanish and come back with every hiccup,
which would resolve and reopen their alerts), and left out after that; the problem is
reported either way.

An answer GitHub could not give (back-off, budget, a page too large) is never passed off as
current: the client's stale copy of an issue list fails the read. Issues and pull requests
are written by anyone who can open them, so every value taken from them is checked and
bounded (ids, logins, labels, branches, titles) and the "Fixes #n" search runs in linear
time. Only owners, members and collaborators write a task's acceptance criteria
(``author_trusted``).

Times are GitHub's.

**Writes** (the adapter's actions) re-read the issue first (``fresh``), go one at a time at
least ``WRITE_GAP`` apart (GitHub's guidance against secondary rate limits for requests that
create content), at most ``MAX_WRITES_PER_TICK`` a tick and ``MAX_WRITES_PER_HOUR`` an hour
(beyond, and while GitHub asks for a back-off, the action is deferred: ``Deferred``, not a
failed attempt), only to issues of the configured repositories, and make the next read of
that repository's lists ask GitHub again rather than the cache (within the tick's budget):

- labels: ``POST /issues/{n}/labels`` to add; ``DELETE /issues/{n}/labels/{name}`` to remove
  one whose name is path-safe, else ``PUT /issues/{n}/labels`` with the rest;
- assignees: ``POST`` / ``DELETE /issues/{n}/assignees``;
- comments: ``POST /issues/{n}/comments``;
- reopen: ``PATCH /issues/{n}`` (``state: open``).
"""

from __future__ import annotations

import logging
import re
import time
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.adapters.issues.mapping import (
    Board,
    Event,
    IssuesError,
    Item,
    LinkedPull,
    login_key,
)
from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.connectors.github.connector import valid_repo, web_host
from kelpie.core.ports import Deferred

log = logging.getLogger(__name__)

PAGE = 50  # issues per page
PULL_PAGE = 10  # pull requests per page: a description can be 64K characters (256 KB)
MAX_PAGES = 10  # pages of open issues per repository (500 issues)
MAX_PULL_PAGES = 30  # the newest 300 open pull requests
MAX_DETAILS = 40  # per-issue reads (events, blockers, sub-issues) per tick, beyond the lists
DETAIL_PAGE = 100
EVENT_PAGES = 3  # pages of an issue's events (oldest first): the newest are what count
BOARD_GRACE = timedelta(minutes=10)
DETAIL_TTL = timedelta(minutes=10)
MAX_PR_BODY = 64 * 1024  # characters of a pull request description searched for "Fixes #n"
MAX_LINKS = 20  # issues one pull request may close
MAX_TITLE = 300
MAX_LABELS = 100
MAX_LABEL = 50
MAX_BRANCH = 255
MAX_NUMBER = 10**9
WRITE_GAP = 1.0  # seconds between writes
MAX_WRITES_PER_TICK = 10  # beyond: deferred to the next tick (pacing blocks the loop)
MAX_WRITES_PER_HOUR = 300  # GitHub allows 500 content-creating requests an hour
MAX_COMMENT = 60_000  # characters of a comment (GitHub's limit is 65536)
TRUSTED = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})  # GitHub's author_association
EVENT_KINDS = frozenset({"labeled", "unlabeled", "assigned", "unassigned", "closed", "reopened"})
# GitHub's closing keywords: https://docs.github.com/issues/tracking-your-work-with-issues/
# using-issues/linking-a-pull-request-to-an-issue. The separator is atomic: no backtracking
# over long runs of whitespace.
_REPO = r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}"
_CLOSES = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?>\s*:?\s+)"
    rf"(?:https?://(?P<host>[A-Za-z0-9.-]{{1,255}})(?::\d{{1,5}})?/(?P<url_repo>{_REPO})/issues/"
    rf"|(?P<repo>{_REPO})?#)(?P<number>\d{{1,9}})\b",
    re.IGNORECASE,
)
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?")
_BRANCH = re.compile(r"[\w./-]{1,255}")


_LABEL_PATH = re.compile(r"(?!\.\.?$)[A-Za-z0-9_-][A-Za-z0-9._-]{0,49}")
_ID = re.compile(r"(?P<repo>[a-z0-9_.-]{1,100}/[a-z0-9_.-]{1,100})#(?P<number>[1-9][0-9]{0,8})")


def issue_id(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and 0 < value < MAX_NUMBER:
        return value
    return None


def _login(user: Any) -> str | None:
    login = user.get("login") if isinstance(user, Mapping) else None
    return login if isinstance(login, str) and _LOGIN.fullmatch(login) else None


def _text(value: str, limit: int) -> str:
    """``value`` cut to ``limit`` characters, control and format characters replaced."""
    return "".join(c if c.isprintable() else "?" for c in value[:limit])


def _labels(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    names = [v.get("name") if isinstance(v, Mapping) else None for v in values[:MAX_LABELS]]
    return tuple(
        n for n in names if isinstance(n, str) and 0 < len(n) <= MAX_LABEL and n.isprintable()
    )


def _logins(values: Any) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    return tuple(login for v in values[:MAX_LABELS] if (login := _login(v)) is not None)


def _count(summary: Any, key: str) -> int:
    value = summary.get(key) if isinstance(summary, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def closes(body: str, repo: str, host: str = "github.com") -> list[int]:
    """The issue numbers of ``repo`` a pull request description closes (at most
    ``MAX_LINKS``). An issue URL counts only on ``host``, GitHub's web host: GitHub links
    no other."""
    numbers: dict[int, None] = {}
    for match in _CLOSES.finditer(body[:MAX_PR_BODY]):
        if match.group("host") is not None and match.group("host").lower() != host:
            continue
        other = match.group("repo") or match.group("url_repo")
        if other is not None and other.lower() != repo:
            continue
        number = _number(int(match.group("number")))
        if number is not None:
            numbers[number] = None
            if len(numbers) >= MAX_LINKS:
                break
    return list(numbers)


@dataclass(slots=True)
class _Lists:
    """A repository's lists as read at ``at``."""

    at: datetime
    issues: list[Any]
    pulls: dict[int, tuple[LinkedPull, ...]]
    pulls_at: datetime  # when the pull requests were read


@dataclass(slots=True)
class _Detail:
    updated_at: datetime
    read_at: datetime
    value: Any  # what it says, parsed


@dataclass(slots=True)
class GitHubIssues:
    """Reads the boards' issues; one instance per adapter (its memo outlives ticks)."""

    client: GitHubClient
    boards: Sequence[Board]
    lookback: timedelta
    max_requests: int = 60  # per tick, lists and details together
    tick_budget: timedelta = timedelta(seconds=15)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    # What the last read could not see, for kelpie status (None: nothing missing).
    problem: str | None = field(default=None, init=False)
    _details: dict[str, _Detail] = field(default_factory=dict, init=False, repr=False)
    _warned: set[str] = field(default_factory=set, init=False, repr=False)
    _spent: int = field(default=0, init=False, repr=False)
    _good: dict[str, _Lists] = field(default_factory=dict, init=False, repr=False)
    _touched: set[str] = field(default_factory=set, init=False, repr=False)  # repos written
    _last_write: float | None = field(default=None, init=False, repr=False)
    _tick_writes: int = field(default=0, init=False, repr=False)
    _reserved: int = field(default=0, init=False, repr=False)  # writes left of this action's
    _written: int = field(default=0, init=False, repr=False)  # writes made by this action
    _raw_labels: dict[str, list[str]] = field(default_factory=dict, init=False, repr=False)
    _hour: deque[float] = field(default_factory=deque, init=False, repr=False)
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)

    def read(self, now: datetime) -> list[Item]:
        """Every board's items: all lists first, then per-issue details with the budget left.
        A board whose lists cannot be read now is served from its last good read for
        ``BOARD_GRACE``, then left out; raises :class:`IssuesError` when no board can be
        read at all."""
        self._spent = 0
        self._tick_writes = 0
        self._reserved = self._written = 0
        self._raw_labels.clear()
        self.problem = None
        # Lists are bounded by their page caps and each request's timeout, not by
        # max_requests or tick_budget (cutting them would drop boards); those are for details.
        pages = len(self.boards) * (MAX_PAGES + 1 + MAX_PULL_PAGES)
        cache = pages + MAX_DETAILS * EVENT_PAGES
        self.client.begin_tick(pages, self.client.timeout * pages, cache)
        lists: list[tuple[str, _Lists]] = []
        failures: list[IssuesError] = []
        for board in self.boards:
            try:
                got = self._lists(board, now)
            except IssuesError as exc:
                failures.append(exc)
                kept = self._good.get(board.key)
                if kept is None or now - kept.at > BOARD_GRACE:
                    self._report(f"{board.key}: not read ({exc.public_reason})")
                    continue
                self._report(f"{board.key}: last read {_iso(kept.at)} ({exc.public_reason})")
                got = kept
            else:
                self._good[board.key] = got
            lists.append((board.key, got))
        if not lists and failures:
            raise failures[0]
        self.client.begin_tick(self.max_requests, self.tick_budget, cache)
        items: list[Item] = []
        for repo, got in lists:
            items += self._items(repo, got)
        live = {i.id for i in items}
        self._details = {k: v for k, v in self._details.items() if k.split(" ", 1)[0] in live}
        return items

    # --- lists ---------------------------------------------------------------------------

    def _pages(
        self,
        path: str,
        query: Mapping[str, str],
        pages: int,
        per_page: int,
        revalidate: bool = False,
    ) -> Iterator[Any]:
        for page in range(1, pages + 1):
            paged = {**query, "per_page": str(per_page), "page": str(page)}
            body = self._get(path, paged, revalidate=revalidate)
            if not isinstance(body, list):
                raise IssuesError(f"GET {path}: not a list", "GitHub answered something else")
            yield from body
            if len(body) < per_page:
                return
        self._warn(path, "issues: %s has more than %d pages; the rest are left out", path, pages)

    def _get(
        self, path: str, query: Mapping[str, str], revalidate: bool = False, fresh: bool = False
    ) -> Any:
        try:
            answer = self.client.get(path, query, fresh=fresh, revalidate=revalidate)
        except GitHubError as exc:
            raise IssuesError(str(exc), f"GitHub could not be read ({exc.kind})", exc.kind) from exc
        if answer.stale:
            raise IssuesError(
                f"GET {path}: only an old cached answer",
                "GitHub could not be read just now",
                "stale",
            )
        return answer.body

    def _report(self, problem: str) -> None:
        self.problem = problem if self.problem is None else f"{self.problem}; {problem}"

    def _lists(self, board: Board, now: datetime) -> _Lists:
        repo = board.key
        revalidate = repo in self._touched  # written since: ask GitHub, not the cache
        base = f"/repos/{repo}/issues"
        query = {"state": "open", "sort": "created", "direction": "desc"}
        if board.label is not None:
            query["labels"] = board.label
        raw = list(self._pages(base, query, MAX_PAGES, PAGE, revalidate))
        closed = {"state": "closed", "since": _iso(now - self.lookback), "sort": "updated"}
        if board.label is not None:
            closed["labels"] = board.label
        raw += list(self._pages(base, closed, 1, PAGE, revalidate))
        self._touched.discard(repo)
        try:
            pulls = self._linked_pulls(repo, revalidate)
        except IssuesError as exc:
            self._warn(f"pulls:{repo}", "issues: %s: pull requests not read: %s", repo, exc)
            kept = self._good.get(repo)
            if kept is not None and now - kept.pulls_at <= BOARD_GRACE:
                self._report(
                    f"{repo}: pull requests last read {_iso(kept.pulls_at)} ({exc.public_reason})"
                )
                return _Lists(now, raw, kept.pulls, kept.pulls_at)
            self._report(f"{repo}: pull requests not read ({exc.public_reason})")
            pulls = {}
        return _Lists(now, raw, pulls, now)

    def _items(self, repo: str, lists: _Lists) -> list[Item]:
        raw, pulls = lists.issues, lists.pulls
        items: list[Item] = []
        seen: set[int] = set()
        for issue in raw:
            if not isinstance(issue, Mapping) or "pull_request" in issue:
                continue
            number = _number(issue.get("number"))
            if number is None or number in seen:
                continue
            seen.add(number)
            item = self._item(repo, number, issue, pulls.get(number, ()))
            if item is not None:
                items.append(item)
        return items

    def _linked_pulls(
        self, repo: str, revalidate: bool = False
    ) -> dict[int, tuple[LinkedPull, ...]]:
        linked: dict[int, list[LinkedPull]] = {}
        path = f"/repos/{repo}/pulls"
        pages = self._pages(path, {"state": "open"}, MAX_PULL_PAGES, PULL_PAGE, revalidate)
        for pull in pages:
            if not isinstance(pull, Mapping):
                continue
            number, body, head = _number(pull.get("number")), pull.get("body"), pull.get("head")
            if number is None or not isinstance(body, str):
                continue
            head = head if isinstance(head, Mapping) else {}
            source = head.get("repo")
            name = source.get("full_name") if isinstance(source, Mapping) else None
            if not isinstance(name, str) or name.lower() != repo:
                continue  # a fork's pull request links nothing
            branch = head.get("ref")
            link = LinkedPull(
                number=number,
                branch=branch if isinstance(branch, str) and _BRANCH.fullmatch(branch) else None,
                draft=pull.get("draft") is True,
                created_at=_time(pull.get("created_at")),
                author=_login(pull.get("user")),
            )
            for issue in closes(body, repo, web_host(self.client.api_url)):
                linked.setdefault(issue, []).append(link)
        return {n: tuple(sorted(p, key=lambda p: p.number)) for n, p in linked.items()}

    # --- one issue -----------------------------------------------------------------------

    def _item(
        self,
        repo: str,
        number: int,
        issue: Mapping[str, Any],
        pulls: tuple[LinkedPull, ...],
        details: bool = True,
    ) -> Item | None:
        created, updated = _time(issue.get("created_at")), _time(issue.get("updated_at"))
        title = issue.get("title")
        if created is None or updated is None or not isinstance(title, str):
            self._warn(f"bad:{repo}", "issues: %s has an issue without times or title", repo)
            return None
        open_ = issue.get("state") == "open"
        labels = _labels(issue.get("labels"))
        assignees = _logins(issue.get("assignees"))
        key = issue_id(repo, number)
        base = f"/repos/{repo}/issues/{number}"
        events = None
        if details and open_ and (labels or assignees):
            path = f"{base}/events"
            events = self._detail(key, "events", updated, path, _events, EVENT_PAGES)
        blocked_by: tuple[str, ...] = ()
        blockers_unknown = False
        if details and open_ and _count(issue.get("issue_dependencies_summary"), "blocked_by"):
            path = f"{base}/dependencies/blocked_by"
            read = self._detail(key, "blocked_by", updated, path, _open_issues)
            blocked_by = read if read is not None else ()
            blockers_unknown = read is None
        sub_items: tuple[str, ...] = ()
        open_subs: tuple[str, ...] = ()
        if details and _count(issue.get("sub_issues_summary"), "total") > 0:
            path = f"{base}/sub_issues"
            subs = self._detail(key, "sub_issues", updated, path, _subs)
            sub_items, open_subs = subs or ((), ())
        reason = issue.get("state_reason")
        body = issue.get("body")
        user = issue.get("user")
        url = issue.get("html_url")
        return Item(
            id=key,
            board=repo,
            title=_text(title, MAX_TITLE),
            open=open_,
            created_at=created,
            updated_at=updated,
            closed_reason=reason if isinstance(reason, str) else None,
            closed_at=_time(issue.get("closed_at")),
            body=body if isinstance(body, str) else "",
            author=_login(user),
            author_is_bot=isinstance(user, Mapping) and user.get("type") == "Bot",
            author_trusted=issue.get("author_association") in TRUSTED,
            labels=labels,
            assignees=assignees,
            events=events,
            blocked_by=blocked_by,
            blockers_unknown=blockers_unknown,
            sub_items=sub_items,
            open_sub_items=open_subs,
            pulls=pulls,
            url=url if isinstance(url, str) else None,
        )

    def _detail(
        self,
        key: str,
        what: str,
        updated: datetime,
        path: str,
        parse: Callable[[list[Any]], Any],
        pages: int = 1,
    ) -> Any:
        """What a per-issue list says (``parse``, over up to ``pages`` pages), read again only
        when the issue changed or ``DETAIL_TTL`` passed; None when it cannot be read (the
        caller falls back)."""
        memo_key = f"{key} {what}"
        now = self.clock()
        kept = self._details.get(memo_key)
        if kept is not None and kept.updated_at == updated and now - kept.read_at < DETAIL_TTL:
            return kept.value
        fallback = kept.value if kept is not None else None
        rows: list[Any] = []
        for page in range(1, pages + 1):
            if self._spent >= MAX_DETAILS:
                return fallback
            self._spent += 1
            query = {"per_page": str(DETAIL_PAGE), "page": str(page)}
            try:
                answer = self.client.get(path, query)
            except GitHubError as exc:
                self._warn(f"detail:{what}", "issues: cannot read %s: %s", path, exc)
                return fallback
            if answer.stale or not isinstance(answer.body, list):
                return fallback
            rows += answer.body
            if len(answer.body) < DETAIL_PAGE:
                break
        value = parse(rows)
        self._details[memo_key] = _Detail(updated, now, value)
        return value

    # --- writing -------------------------------------------------------------------------

    def fetch(self, item_id: str) -> Item:
        """The issue as GitHub has it now (labels, assignees, state; no events or links)."""
        repo, number = self._target(item_id)
        path = f"/repos/{repo}/issues/{number}"
        issue = self._get(path, {}, fresh=True)
        self._raw_labels[item_id] = _raw_labels(issue)
        item = None
        if isinstance(issue, Mapping) and "pull_request" not in issue:
            item = self._item(repo, number, issue, (), details=False)
        if item is None:
            raise IssuesError(f"GET {path}: not an issue", f"{item_id} is not an issue")
        return item

    def add_labels(self, item_id: str, labels: Sequence[str]) -> None:
        repo, number = self._target(item_id)
        self._send("POST", repo, f"/repos/{repo}/issues/{number}/labels", {"labels": [*labels]})

    def remove_labels(self, item_id: str, labels: Sequence[str]) -> None:
        """Drop ``labels`` (any case) the issue carries now. Each goes by itself
        (``DELETE /labels/{name}``) when its name can be a path; otherwise the rest are put
        back (``PUT``), right after reading them, which loses a label added in between."""
        drop = {label.casefold() for label in labels}
        repo, number = self._target(item_id)
        self._pace()  # before the read: no wait between it and the write
        self.fetch(item_id)
        current = self._raw_labels.pop(item_id, [])  # every label, also ones not shown
        present = [label for label in current if label.casefold() in drop]
        if not present:
            return
        if all(_LABEL_PATH.fullmatch(label) for label in present):
            for label in present:
                path = f"/repos/{repo}/issues/{number}/labels/{label}"
                self._send("DELETE", repo, path, None, gone_ok=True)
            return
        keep = [label for label in current if label.casefold() not in drop]
        self._send("PUT", repo, f"/repos/{repo}/issues/{number}/labels", {"labels": keep})

    def assign(self, item_id: str, logins: Sequence[str]) -> None:
        """Assign ``logins``; GitHub answers 201 also when it drops a login it cannot assign,
        so the issue it returns is checked."""
        repo, number = self._target(item_id)
        path = f"/repos/{repo}/issues/{number}/assignees"
        answer = self._send("POST", repo, path, {"assignees": [*logins]})
        if not isinstance(answer, Mapping):
            return  # nothing to check against (an answer too large or not JSON)
        assigned = {login_key(a) for a in _logins(answer.get("assignees"))}
        missing = [login for login in logins if login_key(login) not in assigned]
        if missing:
            raise IssuesError(
                f"POST {path}: GitHub did not assign {missing}",
                f"GitHub did not assign {', '.join(missing)} (can the token assign it?)",
            )

    def unassign(self, item_id: str, logins: Sequence[str]) -> None:
        repo, number = self._target(item_id)
        path = f"/repos/{repo}/issues/{number}/assignees"
        self._send("DELETE", repo, path, {"assignees": [*logins]})

    def comment(self, item_id: str, body: str) -> None:
        repo, number = self._target(item_id)
        path = f"/repos/{repo}/issues/{number}/comments"
        self._send("POST", repo, path, {"body": body[:MAX_COMMENT]})

    def reopen(self, item_id: str) -> None:
        repo, number = self._target(item_id)
        path = f"/repos/{repo}/issues/{number}"
        self._send("PATCH", repo, path, {"state": "open", "state_reason": "reopened"})

    def _target(self, item_id: str) -> tuple[str, int]:
        match = _ID.fullmatch(item_id)
        if match is None or match["repo"] not in {b.key for b in self.boards}:
            raise IssuesError(
                f"{item_id!r} is not an issue of a configured repository",
                "not an issue of a configured repository",
            )
        return match["repo"], int(match["number"])

    def _pace(self) -> None:
        if self._last_write is not None:
            wait = self._last_write + WRITE_GAP - self.monotonic()
            if wait > 0:
                self.sleep(min(wait, WRITE_GAP))

    def reserve(self, writes: int) -> None:
        """Hold ``writes`` for one action before its first one: an action is deferred whole
        (``Deferred``: not an attempt, asked again later) when the tick's or the hour's cap
        cannot take it or GitHub asks Kelpie to back off, never cut halfway. Once it has
        started, a refused write is an error that says what state it left."""
        now = self.monotonic()
        while self._hour and now - self._hour[0] > 3600:
            self._hour.popleft()
        backoff = self.client.backoff_until
        if backoff is not None and self.clock() < backoff:
            raise Deferred("issues: GitHub asks Kelpie to wait (backoff)")
        tick_full = self._tick_writes + writes > MAX_WRITES_PER_TICK
        if tick_full or len(self._hour) + writes > MAX_WRITES_PER_HOUR:
            raise Deferred(
                f"issues: write cap reached ({MAX_WRITES_PER_TICK} a tick, "
                f"{MAX_WRITES_PER_HOUR} an hour)"
            )
        self._reserved, self._written = writes, 0

    def _send(
        self,
        method: str,
        repo: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        gone_ok: bool = False,
    ) -> Any:
        """One write of the reserved ones; GitHub's answer. Before the action's first
        write, a rate limit or back-off defers it; after, it is an error."""
        if self._reserved <= 0:
            self.reserve(1)
        self._reserved -= 1
        first = self._written == 0
        self._pace()
        self._tick_writes += 1
        self._written += 1
        self._hour.append(self.monotonic())
        try:
            return self.client.send(method, path, body)
        except GitHubError as exc:
            if gone_ok and exc.kind == "not_found":
                return None
            if first and exc.kind in ("backoff", "rate_limited"):
                self._reserved = 0
                raise Deferred(f"issues: GitHub asks Kelpie to wait ({exc.kind})") from exc
            raise IssuesError(
                str(exc), f"GitHub refused the change ({exc.kind})", exc.kind
            ) from exc
        finally:
            self._last_write = self.monotonic()
            self._touched.add(repo)

    def _warn(self, key: str, message: str, *args: object) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)


def _events(raw: list[Any]) -> tuple[Event, ...]:
    events: list[Event] = []
    for event in raw:
        if not isinstance(event, Mapping) or event.get("event") not in EVENT_KINDS:
            continue
        at = _time(event.get("created_at"))
        if at is None:
            continue
        kind = event["event"]
        if kind in ("labeled", "unlabeled"):
            names = _labels([event.get("label")])
            name = names[0] if names else None
        elif kind in ("assigned", "unassigned"):
            name = _login(event.get("assignee"))
        else:
            name = ""
        if name is not None:
            events.append(Event(kind, at, name))
    return tuple(events)


def _repo_of(issue: Mapping[str, Any]) -> str | None:
    url = issue.get("repository_url")
    if not isinstance(url, str) or "/repos/" not in url:
        return None
    repo = url.rsplit("/repos/", 1)[1].lower()
    return repo if valid_repo(repo) else None


def _all_issues(raw: list[Any], only_open: bool = False) -> tuple[str, ...]:
    ids: list[str] = []
    for issue in raw:
        if not isinstance(issue, Mapping):
            continue
        number, repo = _number(issue.get("number")), _repo_of(issue)
        if number is None or repo is None:
            continue
        if only_open and issue.get("state") != "open":
            continue
        ids.append(issue_id(repo, number))
    return tuple(ids)


def _raw_labels(issue: Any) -> list[str]:
    """Every label name as GitHub has it (unfiltered: a write must keep them all)."""
    labels = issue.get("labels") if isinstance(issue, Mapping) else None
    names = [v.get("name") if isinstance(v, Mapping) else None for v in labels or []]
    return [n for n in names if isinstance(n, str) and n]


def _open_issues(raw: list[Any]) -> tuple[str, ...]:
    return _all_issues(raw, only_open=True)


def _subs(raw: list[Any]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Every sub-issue, and the open ones."""
    return _all_issues(raw), _all_issues(raw, only_open=True)
