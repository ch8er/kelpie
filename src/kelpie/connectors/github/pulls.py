"""Open and recently closed pull requests of the configured repositories, for rules 6
(``bottlenecks``) and 3 (``orphan_status``).

A snapshot step (``[connectors.github.pulls]``) lists the open PRs of each repository in
``[connectors.github] repos`` every ``poll_interval`` (default 2 minutes), and those closed
within ``closed_lookback`` (default 7 days), and serves the last scan in between. Each
becomes a :class:`~kelpie.core.model.PullRequest`: number, title, head branch (none for a
fork's PR, which is marked ``fork``), labels, draft state, head commit, times (``closed_at``
and ``merged_at`` for a closed one), and the tasks it belongs to (``task_ids``). Open PRs go to
``Snapshot.pull_requests``, closed ones to ``Snapshot.closed_pull_requests``;
``Snapshot.pull_requests_complete`` holds only when the last two scans read both lists of
every repository in full: no error, no page GitHub last confirmed before the scan began, no
page limit reached, no PR seen twice across pages, the same pages when a list longer than
one page is read again, and no PR the scan before saw missing from both lists. So rule 3
proposes closing a parent only when every PR of its family is known to be merged. Keep
``[connectors.github] cache_ttl`` below ``poll_interval``, or the pages come from the cache
and the lists are never complete.

**Which task a PR belongs to** (incident 17), see :mod:`kelpie.core.pr_links`: the task's
own branch, or a task code as a whole token of the PR's title or head branch (the task id;
with ``task_code``, a pattern such as ``'^[A-Z][A-Z0-9]+-[0-9]+'``, its matches in the task
title); never a mention in its description or comments.

Rule 6 uses the PRs two ways:

- **Holds on PR labels.** A label in ``holds``/``hold_labels`` on a PR holds the tasks it belongs
  to, and a PR that belongs to no open task counts as held work itself. When the label was put on
  is read from the PR's issue events (``labeled``), once per PR and label (remembered while the
  label stays), so a hold's age is real rather than inferred. Only the first 100 events are read:
  past that the time is unknown (asked again every 30 minutes) and rule 6 falls back to how long
  the work has not moved. Only labels the app passes as watched (rule 6's hold labels) are looked
  up.
- **Mergeable PRs as ready work.** ``mergeable`` is True when GitHub's ``mergeable_state`` is
  ``clean`` (no conflicts, required checks and reviews satisfied), False for ``dirty``,
  ``blocked``, ``behind``, ``unstable`` or ``draft``, None when unknown. It needs one request
  per PR, so at most ``max_details`` non-draft PRs are looked up per scan, those never
  looked up (or whose head moved) first, then the oldest answers; others keep their last
  known value for the same head. A stale answer, or one about another head, is not used.

Requests share the GitHub step's per-tick budget and cache (``max_requests_per_tick``,
``cache_ttl``); an unchanged list comes back as a 304. At most 300 open and 300 closed PRs
of each repository are read, the most recently updated first (closed ones until they are
older than ``closed_lookback``). A repository GitHub cannot answer (or answers only from
cache) keeps its PRs from the last fresh list for three scans, then they are left out, with
a warning, until it answers again, and the PRs are not complete meanwhile; an old cached
answer is never used for the label times or mergeable states. The step never raises.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from kelpie.connectors.github.client import GitHubError
from kelpie.connectors.github.connector import GitHubConnector
from kelpie.core.model import LabelEvent, PullRequest, Snapshot
from kelpie.core.pr_links import compile_task_code, link_tasks, shared_local_ids
from kelpie.core.serde import parse_duration

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = timedelta(minutes=2)
DEFAULT_CLOSED_LOOKBACK = timedelta(days=7)
MAX_CLOSED_LOOKBACK = timedelta(days=30)
DEFAULT_MAX_DETAILS = 20
MAX_DETAILS = 100
PAGE = 100
MAX_PAGES = 3  # open PRs read per repository: at most 300
STALE_SCANS = 3  # a repository's PRs are dropped after this many scans without a fresh list
UNKNOWN_WARN = 10  # scans in a row without whole lists before a warning
LABEL_RETRY = timedelta(minutes=30)  # an unknown label time is asked again after this
_KEYS = {"enabled", "poll_interval", "max_details", "task_code", "closed_lookback"}
# GitHub's mergeable_state: only "clean" is mergeable now. "unstable" (non-required checks
# failing) counts as not mergeable and anything else ("unknown", "has_hooks") as unknown: a
# PR is ready work only when GitHub is sure.
_MERGEABLE = {"clean": True}
_NOT_MERGEABLE = {"dirty", "blocked", "behind", "unstable", "draft"}


@dataclass(frozen=True, slots=True)
class PullSettings:
    """``[connectors.github.pulls]``."""

    enabled: bool = True
    poll_interval: timedelta = DEFAULT_POLL_INTERVAL
    max_details: int = DEFAULT_MAX_DETAILS
    task_code: re.Pattern[str] | None = None  # task codes in task titles, besides the id
    closed_lookback: timedelta = DEFAULT_CLOSED_LOOKBACK  # closed PRs listed this far back


def load_pulls(raw: Any) -> PullSettings:
    if not isinstance(raw, Mapping):
        raise ValueError("[connectors.github.pulls] must be a table")
    if extra := set(raw) - _KEYS:
        raise ValueError(f"unknown [connectors.github.pulls] keys: {sorted(extra)}")
    enabled = raw.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("[connectors.github.pulls] enabled must be true or false")
    try:
        interval = parse_duration(raw.get("poll_interval", DEFAULT_POLL_INTERVAL))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"[connectors.github.pulls] poll_interval: {exc}") from None
    if interval < timedelta(seconds=30):
        raise ValueError("[connectors.github.pulls] poll_interval must be at least 30s")
    details = raw.get("max_details", DEFAULT_MAX_DETAILS)
    if isinstance(details, bool) or not isinstance(details, int) or not 0 <= details <= MAX_DETAILS:
        raise ValueError(f"[connectors.github.pulls] max_details must be 0 to {MAX_DETAILS}")
    try:
        lookback = parse_duration(raw.get("closed_lookback", DEFAULT_CLOSED_LOOKBACK))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"[connectors.github.pulls] closed_lookback: {exc}") from None
    if not timedelta(hours=1) <= lookback <= MAX_CLOSED_LOOKBACK:
        raise ValueError("[connectors.github.pulls] closed_lookback must be 1h to 30d")
    code = raw.get("task_code")
    pattern = None
    if code is not None:
        if not isinstance(code, str):
            raise ValueError("[connectors.github.pulls] task_code must be a regular expression")
        try:
            pattern = compile_task_code(code)
        except ValueError as exc:
            raise ValueError(f"[connectors.github.pulls] task_code {exc}") from None
    return PullSettings(enabled, interval, details, pattern, lookback)


@dataclass(frozen=True, slots=True)
class _View:
    """One list of a repository's PRs as a scan read it."""

    pulls: tuple[PullRequest, ...]
    numbers: frozenset[int]  # every PR read, also those closed before the lookback
    whole: bool
    cutoff: datetime
    fresh: bool  # every page confirmed by GitHub during this scan


@dataclass(slots=True)
class PullRequestSync:
    """The snapshot step; ``watch_labels`` are the labels whose time is read (holds)."""

    connector: GitHubConnector
    settings: PullSettings = field(default_factory=PullSettings)
    watch_labels: frozenset[str] = frozenset()
    name: str = "github_pulls"
    _pulls: dict[str, tuple[PullRequest, ...]] = field(default_factory=dict, init=False)
    # when the open list was last read (from GitHub or, within cache_ttl, the cache)
    _listed_at: dict[str, datetime] = field(default_factory=dict, init=False)
    _closed: dict[str, tuple[PullRequest, ...]] = field(default_factory=dict, init=False)
    _closed_at: dict[str, datetime] = field(default_factory=dict, init=False)  # last fresh list
    # repositories whose open and closed PRs the last scan listed in full
    _whole: set[str] = field(default_factory=set, init=False)
    # repo -> (that scan's lists were whole, its open PR numbers)
    _seen: dict[str, tuple[bool, frozenset[int]]] = field(default_factory=dict, init=False)
    # repo -> when its closed list was last read with every page fresh
    _closed_fresh: dict[str, datetime] = field(default_factory=dict, init=False)
    _closed_before: dict[str, tuple[PullRequest, ...]] = field(default_factory=dict, init=False)
    _unknown: dict[str, int] = field(default_factory=dict, init=False)  # scans not whole in a row
    _scanned_at: datetime | None = field(default=None, init=False)
    _exhausted: bool = field(default=False, init=False)
    _version: int = field(default=0, init=False)  # bumped on every scan
    _link_key: object = field(default=None, init=False)
    # Multi-host: local task ids on several machines when all were last observed.
    _shared_local: frozenset[str] = field(default=frozenset(), init=False)
    _links: tuple[PullRequest, ...] = field(default=(), init=False)
    _closed_links: tuple[PullRequest, ...] = field(default=(), init=False)
    # (repo, number, label) -> (when it was put on, when that was read);
    # (repo, number) -> (head, mergeable, at)
    _labeled: dict[tuple[str, int, str], tuple[datetime | None, datetime]] = field(
        default_factory=dict, init=False
    )
    _mergeable: dict[tuple[str, int], tuple[str | None, bool | None, datetime]] = field(
        default_factory=dict, init=False
    )

    def __call__(self, snapshot: Snapshot) -> Snapshot:
        now = snapshot.now
        if self._scanned_at is None or now - self._scanned_at >= self.settings.poll_interval:
            try:
                self._scan(now)
            except Exception:  # a bug must not cost the tick
                self._whole = set()
                log.exception("[connectors.github.pulls] scan failed")
            self._scanned_at = now
        pulls, closed = self._linked(snapshot)
        repos = self.connector.repos
        complete = bool(repos) and self._whole.issuperset(repos)
        known = snapshot.pull_requests_complete  # another code host's, if any
        return replace(
            snapshot,
            pull_requests=snapshot.pull_requests + pulls,
            closed_pull_requests=snapshot.closed_pull_requests + closed,
            pull_requests_complete=complete if known is None else known and complete,
        )

    def _linked(
        self, snapshot: Snapshot
    ) -> tuple[tuple[PullRequest, ...], tuple[PullRequest, ...]]:
        """The open and closed PRs with the tasks they belong to, worked out again only when
        the PRs or the tasks' ids, titles or branches changed. On a multi-host farm, local
        ids seen on several machines while all were observed stay ambiguous while one is
        not (a look-alike may be on the missing machine)."""
        if not snapshot.unobserved:
            self._shared_local = shared_local_ids(snapshot.tasks)
        key = (
            self._version,
            tuple((t.id, t.title, t.branch) for t in snapshot.tasks),
            self._shared_local,
        )
        if key != self._link_key:
            code, shared = self.settings.task_code, self._shared_local
            closed = tuple(p for repo in sorted(self._closed) for p in self._closed[repo])
            # A PR closed between the two lists of one scan counts as closed; an older closed
            # list says nothing about a PR open now (it may have been reopened).
            gone = {
                (p.repo, p.number)
                for p in closed
                if self._closed_fresh.get(p.repo) == self._listed_at.get(p.repo)
            }
            pulls = tuple(
                p
                for repo in sorted(self._pulls)
                for p in self._pulls[repo]
                if (p.repo, p.number) not in gone
            )
            self._links = link_tasks(pulls, snapshot.tasks, code, shared)
            self._closed_links = link_tasks(closed, snapshot.tasks, code, shared)
            self._link_key = key
        return self._links, self._closed_links

    def _scan(self, now: datetime) -> None:
        self._whole = set()  # until this scan proves otherwise
        started = self.connector.client.clock()
        views: dict[str, _View] = {}
        for repo in sorted(self.connector.repos):
            listed_now = self._list(repo, started)
            listed = self._listed_at.get(repo)
            if listed_now is not None:
                self._pulls[repo], self._listed_at[repo] = listed_now.pulls, now
                views[repo] = listed_now
            elif listed is not None and now - listed > STALE_SCANS * self.settings.poll_interval:
                log.warning(
                    "[connectors.github.pulls] %s: no fresh list of open PRs since %s; its PRs "
                    "are left out until GitHub answers again",
                    repo,
                    listed.isoformat(),
                )
                self._pulls.pop(repo, None)
                del self._listed_at[repo]
        live = {(p.repo, p.number) for ps in self._pulls.values() for p in ps}
        self._mergeable = {k: v for k, v in self._mergeable.items() if k in live}
        self._labeled = {k: v for k, v in self._labeled.items() if (k[0], k[1]) in live}
        self._read_details(now)
        self._read_label_times(now)
        # Last: only rule 3's parent check needs it, and it waits safely for a fresh list.
        whole: set[str] = set()
        for repo in sorted(self.connector.repos):
            opened = views.get(repo)
            closed = self._scan_closed(repo, now, started)
            if opened is not None and closed is not None and self._conserved(repo, opened, closed):
                whole.add(repo)
            # What this scan saw, for the next one's check (nothing seen: nothing to check).
            self._seen[repo] = (
                (opened.whole and closed is not None and closed.whole) if opened else False,
                opened.numbers if opened else frozenset(),
            )
        for repo, pulls in self._pulls.items():
            self._pulls[repo] = tuple(self._complete(p) for p in pulls)
        self._version += 1
        self._whole = whole
        for repo in sorted(self.connector.repos):
            self._unknown[repo] = 0 if repo in whole else self._unknown.get(repo, 0) + 1
            if self._unknown[repo] == UNKNOWN_WARN:
                log.warning(
                    "[connectors.github.pulls] %s: its open and closed PRs were not read in full "
                    "for %d scans (errors, cached answers, more than %d PRs, the request budget): "
                    "rule 3 proposes closing no parent meanwhile",
                    repo,
                    UNKNOWN_WARN,
                    MAX_PAGES * PAGE,
                )
        exhausted = self.connector.client.budget_exhausted
        if exhausted and not self._exhausted:
            log.warning(
                "[connectors.github.pulls] the per-tick request budget ran out: some PRs keep "
                "older answers; raise [connectors.github] max_requests_per_tick or lower "
                "[connectors.github.pulls] max_details"
            )
        self._exhausted = exhausted

    def _conserved(self, repo: str, opened: _View, closed: _View) -> bool:
        """Whether both lists of ``repo`` are whole and, with the last scan's, account for
        every PR: a PR the last scan saw (open, or closed within the lookback) that neither
        list shows now moved while the lists were read (closed or reopened between the two,
        or pushed up past a page already read). The first scan has nothing to compare with,
        and neither has a scan after one that was not whole."""
        if not (opened.whole and closed.whole):
            return False
        before = self._seen.get(repo)
        if before is None or not before[0]:
            return False
        # The closed PRs the last scan listed (its closed list was whole: see before[0]).
        cutoff = closed.cutoff
        recent = {
            p.number
            for p in self._closed_before.get(repo, ())
            if p.closed_at is not None and p.closed_at >= cutoff
        }
        now_seen = opened.numbers | closed.numbers
        return (before[1] | recent) <= now_seen

    def _scan_closed(self, repo: str, now: datetime, started: datetime) -> _View | None:
        """Lists the closed PRs of ``repo``; the view, or None without a fresh list."""
        self._closed_before[repo] = self._closed.get(repo, ())
        cutoff = now - self.settings.closed_lookback
        listed = self._list(repo, started, "closed", cutoff)
        if listed is not None:
            self._closed[repo], self._closed_at[repo] = listed.pulls, now
            if listed.fresh:
                self._closed_fresh[repo] = now
            return listed
        listed_at = self._closed_at.get(repo)
        if listed_at is not None and now - listed_at > STALE_SCANS * self.settings.poll_interval:
            log.warning(
                "[connectors.github.pulls] %s: no fresh list of closed PRs since %s; they are "
                "left out until GitHub answers again",
                repo,
                listed_at.isoformat(),
            )
            self._closed.pop(repo, None)
            del self._closed_at[repo]
        return None

    def _list(
        self,
        repo: str,
        started: datetime,
        state: str = "open",
        cutoff: datetime | None = None,
    ) -> _View | None:
        """The ``state`` PRs of ``repo`` (closed ones: closed since ``cutoff``), up to
        ``MAX_PAGES`` pages, the most recently updated first; ``None`` without a fresh answer
        (an error, or an old answer GitHub could not confirm).

        Not whole when the last page read was full (more were left unread); when a PR came
        twice (the pages are read one by one, and a PR updated in between moves to the front,
        pushing another onto the next page); when a page is an answer GitHub last confirmed
        before this scan started (a cached page can be older than the other list); or when
        an answer or an entry could not be read."""
        found: dict[int, PullRequest] = {}
        seen: set[int] = set()
        whole = fresh = True
        pages: list[tuple[dict[str, str], tuple[int, ...]]] = []
        for page in range(1, MAX_PAGES + 1):
            query = {"state": state, "per_page": str(PAGE), "sort": "updated", "direction": "desc"}
            if page > 1:
                query["page"] = str(page)
            try:
                response = self.connector.client.get(f"/repos/{repo}/pulls", query)
            except GitHubError as exc:
                log.debug("[connectors.github.pulls] %s PRs of %s not listed: %s", state, repo, exc)
                return None
            if response.stale:
                return None
            if response.fetched_at < started:
                whole = fresh = False
            body = response.body if isinstance(response.body, list) else None
            if body is None:
                body, whole = [], False
            pulls = [p for item in body if (p := _pull(repo, item)) is not None]
            if len(pulls) < len(body):
                whole = False
            pages.append((query, tuple(p.number for p in pulls)))
            for p in pulls:
                if p.number in seen:
                    whole = False
                    continue
                seen.add(p.number)
                if cutoff is None:
                    found[p.number] = p
                elif p.closed_at is None:
                    whole = False
                elif p.closed_at >= cutoff:
                    found[p.number] = p
            if len(body) < PAGE:
                break
            if cutoff is not None:
                # Updated no earlier than closed: a page ending before the cutoff ends the list.
                last = pulls[-1].updated_at if pulls else None
                if last is not None and last < cutoff:
                    break
            if page == MAX_PAGES:
                whole = False
        if whole and len(pages) > 1 and not self._same_pages(repo, pages):
            whole = False
        return _View(tuple(found.values()), frozenset(seen), whole, cutoff or started, fresh)

    def _same_pages(self, repo: str, pages: list[tuple[dict[str, str], tuple[int, ...]]]) -> bool:
        """Whether the pages read again show the same PRs in the same order: a PR opened since
        the last scan and pushed up past a page already read (while another left the list)
        comes twice nowhere and is missing from no earlier view, so only a second read shows
        that the list moved. Asked only for lists longer than one page, outside the per-tick
        budget (at most ``MAX_PAGES`` requests per list); an unchanged page is a free 304."""
        for query, numbers in pages:
            try:
                # Past the cache (which would answer with the page just read), conditionally:
                # an unchanged page costs GitHub nothing.
                response = self.connector.client.get(f"/repos/{repo}/pulls", query, fresh=True)
            except GitHubError:
                return False
            body = response.body if isinstance(response.body, list) else []
            again = tuple(p.number for item in body if (p := _pull(repo, item)) is not None)
            if response.stale or again != numbers:
                return False
        return True

    def _read_details(self, now: datetime) -> None:
        """``mergeable_state`` of up to ``max_details`` non-draft PRs, those with no answer for
        their current head first, then the oldest answers."""
        candidates: list[tuple[tuple[int, datetime], PullRequest]] = []
        for pulls in self._pulls.values():
            for p in pulls:
                if p.draft:
                    continue
                known = self._mergeable.get((p.repo, p.number))
                # GitHub computes the state after a push: "unknown" is asked again first.
                fresh = known is not None and known[0] == p.head_sha and known[1] is not None
                candidates.append(((int(fresh), known[2] if fresh and known else now), p))
        candidates.sort(key=lambda c: (c[0][0], c[0][1], c[1].repo, c[1].number))
        for _, p in candidates[: self.settings.max_details]:
            try:
                response = self.connector.client.get(f"/repos/{p.repo}/pulls/{p.number}")
            except GitHubError as exc:
                log.debug("[connectors.github.pulls] %s not read: %s", p.ref, exc)
                continue
            body = response.body if isinstance(response.body, Mapping) else {}
            head = body.get("head") if isinstance(body.get("head"), Mapping) else {}
            if response.stale or head.get("sha") != p.head_sha:
                continue  # an old answer, or about another head: not this head's state
            state = body.get("mergeable_state")
            mergeable = _MERGEABLE.get(state) if state not in _NOT_MERGEABLE else False
            self._mergeable[(p.repo, p.number)] = (p.head_sha, mergeable, now)

    def _read_label_times(self, now: datetime) -> None:
        """When each watched label was put on its PR, read once per PR and label (an unknown
        time is asked again after ``LABEL_RETRY``; an old answer is not used)."""
        for pulls in self._pulls.values():
            for p in pulls:
                for label in sorted(self.watch_labels.intersection(p.labels)):
                    key = (p.repo, p.number, label)
                    known = self._labeled.get(key)
                    if known is not None and (known[0] is not None or now - known[1] < LABEL_RETRY):
                        continue
                    try:
                        response = self.connector.client.get(
                            f"/repos/{p.repo}/issues/{p.number}/events", {"per_page": str(PAGE)}
                        )
                    except GitHubError as exc:
                        log.debug("[connectors.github.pulls] labels of %s: %s", p.ref, exc)
                        continue  # asked again next scan
                    if response.stale:
                        continue
                    self._labeled[key] = (_labeled_at(response.body, label), now)
        # A label taken off and put back is read again.
        current = {
            (p.repo, p.number, label)
            for ps in self._pulls.values()
            for p in ps
            for label in p.labels
        }
        self._labeled = {k: v for k, v in self._labeled.items() if k in current}

    def _complete(self, p: PullRequest) -> PullRequest:
        known = self._mergeable.get((p.repo, p.number))
        mergeable = known[1] if known is not None and known[0] == p.head_sha else None
        labeled = tuple(
            LabelEvent(label, at)
            for label in sorted(self.watch_labels.intersection(p.labels))
            if (at := self._labeled.get((p.repo, p.number, label), (None, None))[0]) is not None
        )
        return replace(p, mergeable=False if p.draft else mergeable, labeled=labeled)


def _pull(repo: str, item: Any) -> PullRequest | None:
    if not isinstance(item, Mapping):
        return None
    number = item.get("number")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    head = item.get("head") if isinstance(item.get("head"), Mapping) else {}
    labels = item.get("labels") if isinstance(item.get("labels"), list) else []
    names = tuple(
        str(label["name"])
        for label in labels
        if isinstance(label, Mapping) and isinstance(label.get("name"), str)
    )
    title = item.get("title")
    branch, sha = head.get("ref"), head.get("sha")
    # A fork's branch names are its own (often ``main``): they name no task of ours.
    origin = head.get("repo") if isinstance(head.get("repo"), Mapping) else {}
    full = origin.get("full_name")
    fork = not isinstance(full, str) or full.lower() != repo.lower()
    if fork:
        branch = None
    base = item.get("base") if isinstance(item.get("base"), Mapping) else {}
    target = base.get("repo") if isinstance(base.get("repo"), Mapping) else {}
    default, into = target.get("default_branch"), base.get("ref")
    into_default = into == default if isinstance(into, str) and isinstance(default, str) else None
    return PullRequest(
        repo=repo,
        number=number,
        title=title if isinstance(title, str) else "",
        branch=branch if isinstance(branch, str) and branch else None,
        labels=names,
        draft=item.get("draft") is True,
        fork=fork,
        head_sha=sha if isinstance(sha, str) and sha else None,
        created_at=_time(item.get("created_at")),
        updated_at=_time(item.get("updated_at")),
        source="github",
        closed_at=_time(item.get("closed_at")),
        merged_at=_time(item.get("merged_at")),
        into_default=into_default,
    )


def _labeled_at(body: Any, label: str) -> datetime | None:
    """When ``label`` was last put on, from the first page of an issue's events (oldest
    first). Unknown (None) when the page is full (later events are not read) or the label
    was taken off after it."""
    events = body if isinstance(body, Sequence) and not isinstance(body, str) else ()
    if len(events) >= PAGE:
        return None
    last: datetime | None = None
    for e in events:
        if not isinstance(e, Mapping) or not isinstance(e.get("label"), Mapping):
            continue
        if e["label"].get("name") != label:
            continue
        if e.get("event") == "labeled":
            last = _time(e.get("created_at"))
        elif e.get("event") == "unlabeled":
            last = None
    return last


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else None
