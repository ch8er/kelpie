"""Open pull requests of the configured repositories, for rule 6 (``bottlenecks``).

A snapshot step (``[connectors.github.pulls]``) lists the open PRs of each repository in
``[connectors.github] repos`` every ``poll_interval`` (default 2 minutes) and serves the last
scan in between. Each becomes a :class:`~kelpie.core.model.PullRequest`: number, title,
head branch (which links it to the task on that branch; none for a fork's PR, which is
marked ``fork``), labels, draft state, head commit, times. Rule 6 uses them two ways:

- **Holds on PR labels.** A label in ``holds``/``hold_labels`` on a PR holds the task on its
  branch, and the PR counts as held work. When the label was put on is read from the PR's
  issue events (``labeled``), once per PR and label (remembered while the label stays), so a
  hold's age is real rather than inferred. Only the first 100 events are read: past that the
  time is unknown (asked again every 30 minutes) and rule 6 falls back to how long the work
  has not moved. Only labels the app passes as watched (rule 6's hold labels) are looked up.
- **Mergeable PRs as ready work.** ``mergeable`` is True when GitHub's ``mergeable_state`` is
  ``clean`` (no conflicts, required checks and reviews satisfied), False for ``dirty``,
  ``blocked``, ``behind``, ``unstable`` or ``draft``, None when unknown. It needs one request
  per PR, so at most ``max_details`` non-draft PRs are looked up per scan, those never
  looked up (or whose head moved) first, then the oldest answers; others keep their last
  known value for the same head. A stale answer, or one about another head, is not used.

Requests share the GitHub step's per-tick budget and cache (``max_requests_per_tick``,
``cache_ttl``); an unchanged list comes back as a 304. At most 300 open PRs of each
repository are read, the most recently updated first. A repository GitHub cannot answer
(or answers only from cache) keeps its PRs from the last fresh list for three scans, then
they are left out, with a warning, until it answers again; an old cached answer is never
used for the label times or mergeable states. The step never raises.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from kelpie.connectors.github.client import GitHubError
from kelpie.connectors.github.connector import GitHubConnector
from kelpie.core.model import LabelEvent, PullRequest, Snapshot
from kelpie.core.serde import parse_duration

log = logging.getLogger(__name__)

DEFAULT_POLL_INTERVAL = timedelta(minutes=2)
DEFAULT_MAX_DETAILS = 20
MAX_DETAILS = 100
PAGE = 100
MAX_PAGES = 3  # open PRs read per repository: at most 300
STALE_SCANS = 3  # a repository's PRs are dropped after this many scans without a fresh list
LABEL_RETRY = timedelta(minutes=30)  # an unknown label time is asked again after this
_KEYS = {"enabled", "poll_interval", "max_details"}
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
    return PullSettings(enabled, interval, details)


@dataclass(slots=True)
class PullRequestSync:
    """The snapshot step; ``watch_labels`` are the labels whose time is read (holds)."""

    connector: GitHubConnector
    settings: PullSettings = field(default_factory=PullSettings)
    watch_labels: frozenset[str] = frozenset()
    name: str = "github_pulls"
    _pulls: dict[str, tuple[PullRequest, ...]] = field(default_factory=dict, init=False)
    _listed_at: dict[str, datetime] = field(default_factory=dict, init=False)  # last fresh list
    _scanned_at: datetime | None = field(default=None, init=False)
    _exhausted: bool = field(default=False, init=False)
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
                log.exception("[connectors.github.pulls] scan failed")
            self._scanned_at = now
        pulls = tuple(p for repo in sorted(self._pulls) for p in self._pulls[repo])
        return replace(snapshot, pull_requests=snapshot.pull_requests + pulls)

    def _scan(self, now: datetime) -> None:
        for repo in sorted(self.connector.repos):
            pulls = self._list(repo)
            if pulls is not None:
                self._pulls[repo], self._listed_at[repo] = pulls, now
                continue
            listed = self._listed_at.get(repo)
            if listed is not None and now - listed > STALE_SCANS * self.settings.poll_interval:
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
        for repo, pulls in self._pulls.items():
            self._pulls[repo] = tuple(self._complete(p) for p in pulls)
        exhausted = self.connector.client.budget_exhausted
        if exhausted and not self._exhausted:
            log.warning(
                "[connectors.github.pulls] the per-tick request budget ran out: some PRs keep "
                "older answers; raise [connectors.github] max_requests_per_tick or lower "
                "[connectors.github.pulls] max_details"
            )
        self._exhausted = exhausted

    def _list(self, repo: str) -> tuple[PullRequest, ...] | None:
        """The open PRs of ``repo`` (up to ``MAX_PAGES`` pages), or ``None`` without a fresh
        answer (an error, or an old answer GitHub could not confirm)."""
        found: list[PullRequest] = []
        for page in range(1, MAX_PAGES + 1):
            query = {
                "state": "open",
                "per_page": str(PAGE),
                "sort": "updated",
                "direction": "desc",
            }
            if page > 1:
                query["page"] = str(page)
            try:
                response = self.connector.client.get(f"/repos/{repo}/pulls", query)
            except GitHubError as exc:
                log.debug("[connectors.github.pulls] %s not listed: %s", repo, exc)
                return None
            if response.stale:
                return None
            body = response.body if isinstance(response.body, list) else []
            found.extend(p for item in body if (p := _pull(repo, item)) is not None)
            if len(body) < PAGE:
                break
        return tuple(found)

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
