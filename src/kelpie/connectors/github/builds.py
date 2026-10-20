"""GitHub Actions as the CI source of rule 9 (``ci_economy``).

A snapshot step reads recent workflow runs of the configured repositories and maps them
onto the CI-agnostic :class:`~kelpie.core.model.Build` records.

Polling and budget. The step scans every ``poll_interval`` (default 2 minutes) and serves
the last scan in between. One scan spends at most ``max_requests_per_tick`` requests (at
most 100) and ``tick_budget`` of time, in this order: page 1 of every repository (the
starting repository rotates every scan, so none is starved), the default branch of each
repository (remembered for an hour), further pages, then pull requests, PR files and job
lists for the newest active runs first. The run query names a date, not a time, so its URL
stays the same for a day and an unchanged page comes back as a 304. Arithmetic: at most
100 requests every 2 minutes is 3,000 an hour against GitHub's 5,000 for a token; 304s do
not count against it. The step is skipped while ``X-RateLimit-Remaining`` is below
``rate_reserve`` (default 1,000, at most a fifth of ``X-RateLimit-Limit``) and the window
has not reset, so PR and CI conditions (the other GitHub step) keep working.

Mapping:

- only runs created within ``window`` (default 24 h);
- ``minutes``: for a finished run, the billable minutes of its jobs (each job rounded up
  to a minute, as GitHub bills; runner multipliers for Windows or macOS are not applied),
  read once per run attempt and remembered. For an active run, or when the job list does
  not fit in the budget, the wall-clock time of the run, which overstates parallel jobs
  and understates matrix fan-out; a run list served from the cache because GitHub could
  not be asked is never extrapolated to now;
- ``jobs``: from the same job list, read once per finished run attempt: each job's name and
  conclusion (mapped like the run's), and for a job that did not succeed its first named
  step that did not succeed, the runner it ran on and the labels it asked for (rule 9 tells
  a CI outage from a broken PR by them). None when not read: the run is active, the list is
  partial or missing, the request failed, or the scan's budget for job lists is spent;
- ``superseded_by``: a newer run of the same workflow, branch, head repository and event,
  and for ``pull_request`` runs the same PR number and base branch, was created after it.
  Only ``push`` and ``pull_request`` runs are marked (never deploys, schedules or manual
  runs), never on the repository's default branch, never re-runs (``run_attempt`` > 1,
  usually started by a human), and nothing at all in a repository whose default branch is
  not known;
- ``draft`` and ``changed_paths`` come from the pull request the run belongs to (its
  current draft state; its files only while the PR head is the run's commit and it touches
  at most 100 files, otherwise unknown);
- with ``included_minutes``, a ``ci_minutes`` resource carries the monthly quota and its
  reset (``quota_reset_day``, UTC). Minutes used come only from GitHub's billing usage
  report, when ``billing_user`` or ``billing_org`` is set with a token of its own
  (:mod:`kelpie.connectors.github.billing`); without it the rule projects the burn against
  the quota. Exhaustion is never guessed from runs: a run blocked by billing looks like any
  other run that failed at once, and a false "quota exhausted" would send humans to the
  wrong place.

Two writes. ``propose_ci_config`` (off unless ``allow_propose``) opens a draft pull request
changing a workflow file: see :mod:`kelpie.connectors.github.proposals`. And
``cancel_build`` (``POST /repos/{repo}/actions/runs/{id}/cancel``):

- it runs only through the engine, so the rule's mode and the global dry-run apply;
- ``allow_cancel`` is off by default; without it the connector declares no action;
- a build is offered as ``cancellable`` only when it is superseded and active in a scan
  whose run list GitHub answered just now, in a repository in ``cancel_repos``, a workflow
  in ``cancel_workflows`` (if set) and a branch matching ``cancel_branches`` (if set; the
  default branch never). The action's ``build_id`` is looked up among those, never parsed;
- right before cancelling, both runs are fetched again, outside the cache: the run must
  still be active and on the same attempt, and the newer run must still be a newer run of
  the same group. Anything else refuses the cancel;
- results: done; nothing to do (the run finished, was re-run or is no longer superseded
  meanwhile, the newer run is gone, HTTP 409, or Kelpie already cancelled it); refused (a
  policy says no, or GitHub answers the cancel with 403 because the token lacks
  ``Actions: write``: a human is told). A transient failure (network, back-off, rate
  limit, a re-check served from the cache, 5xx, a 403 on a read) raises, so the engine
  records a failure and its ledger retries later.

Token scopes (least privilege): reading needs ``Actions: read``, ``Pull requests: read``
and ``Contents: read`` (fine-grained) or ``repo`` (classic, private repositories).
Cancelling needs ``Actions: write``. Prefer ``cancel_token_env``: a second token with
``Actions: write`` on the ``cancel_repos`` only, used for nothing but the cancel request,
so the token that reads everything stays read-only.
"""

from __future__ import annotations

import fnmatch
import itertools
import logging
import math
import os
import re
from collections import OrderedDict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.connectors.github import billing as usage
from kelpie.connectors.github.client import GitHubClient, GitHubError, valid_token
from kelpie.connectors.github.connector import valid_repo
from kelpie.connectors.github.proposals import Proposer
from kelpie.core.actions import Action, ActionKind, ActionResult
from kelpie.core.model import Build, BuildStatus, Job, Resource, ResourceKind, Snapshot
from kelpie.core.serde import parse_datetime, parse_duration

log = logging.getLogger(__name__)

SOURCE = "github"  # Build.source and executor name
PER_PAGE = 100
DEFAULT_WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(days=7)
DEFAULT_MAX_PAGES = 3
MAX_PAGES = 10
DEFAULT_REQUESTS = 30
MAX_REQUESTS = 100
DEFAULT_TICK_BUDGET = timedelta(seconds=10)
MAX_TICK_BUDGET = timedelta(seconds=60)
DEFAULT_POLL = timedelta(minutes=2)
DEFAULT_RATE_RESERVE = 1000
MAX_RESERVE_SHARE = 0.2  # of X-RateLimit-Limit: a small limit is not all reserve
DEFAULT_BRANCH_TTL = timedelta(hours=1)
_FAILED = frozenset({"failure", "timed_out", "cancelled"})
HOSTED_GROUP = "GitHub Actions"  # runner group of GitHub-hosted runners
MAX_NAME = 100  # characters kept of a job, step, runner or label name
MAX_LABELS = 5
MAX_JOB_LOOKUPS = 10  # per scan; the rest use wall-clock minutes until a later scan
MAX_REMEMBERED = 4096  # run minutes and PR files kept across scans
MAX_MESSAGE = 200  # characters of a commit message's first line
MAX_DISPLAY = 255  # characters of a branch or workflow name kept on a Build
MAX_PR_FILES = 100
SUPERSEDABLE_EVENTS = frozenset({"push", "pull_request"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_ACCOUNT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", re.ASCII)
_ACTIVE = {
    "queued": BuildStatus.QUEUED,
    "waiting": BuildStatus.QUEUED,
    "requested": BuildStatus.QUEUED,
    "pending": BuildStatus.QUEUED,
    "in_progress": BuildStatus.RUNNING,
}
_CONCLUSIONS = {
    "success": BuildStatus.SUCCESS,
    "neutral": BuildStatus.SUCCESS,
    "skipped": BuildStatus.SKIPPED,
    "cancelled": BuildStatus.CANCELLED,
    "failure": BuildStatus.FAILURE,
    "timed_out": BuildStatus.FAILURE,
    "action_required": BuildStatus.FAILURE,
    "startup_failure": BuildStatus.FAILURE,
    "stale": BuildStatus.FAILURE,
}
_KEYS = {
    "enabled",
    "repos",
    "window",
    "poll_interval",
    "rate_reserve",
    "max_pages",
    "max_requests_per_tick",
    "tick_budget",
    "included_minutes",
    "quota_reset_day",
    "allow_cancel",
    "cancel_repos",
    "cancel_workflows",
    "cancel_branches",
    "cancel_token_env",
    "billing_user",
    "billing_org",
    "billing_token_env",
    "billing_interval",
    "billing_skus",
    "allow_propose",
    "propose_repos",
    "propose_token_env",
}


@dataclass(frozen=True, slots=True)
class CiSettings:
    """``[connectors.github.ci]``."""

    repos: tuple[str, ...]
    enabled: bool = True
    window: timedelta = DEFAULT_WINDOW
    poll_interval: timedelta = DEFAULT_POLL
    rate_reserve: int = DEFAULT_RATE_RESERVE
    max_pages: int = DEFAULT_MAX_PAGES
    max_requests_per_tick: int = DEFAULT_REQUESTS
    tick_budget: timedelta = DEFAULT_TICK_BUDGET
    included_minutes: float | None = None
    quota_reset_day: int = 1
    allow_cancel: bool = False
    cancel_repos: tuple[str, ...] = ()
    cancel_workflows: tuple[str, ...] = ()
    cancel_branches: tuple[str, ...] = ()  # globs; empty: any branch but the default one
    cancel_token_env: str | None = None  # a separate token for cancelling only
    # Minutes used from GitHub's billing usage report (kelpie.connectors.github.billing):
    # the user or organization billed, with a token of its own.
    billing_account: str | None = None
    billing_organization: bool = False
    billing_token_env: str | None = None
    billing_interval: timedelta = usage.DEFAULT_INTERVAL
    billing_skus: tuple[str, ...] = usage.DEFAULT_SKUS
    # propose_ci_config (kelpie.connectors.github.proposals): off unless allowed, for these
    # repositories only, with a token of its own.
    allow_propose: bool = False
    propose_repos: tuple[str, ...] = ()
    propose_token_env: str | None = None

    def propose_token(self, environ: Mapping[str, str] = os.environ) -> str | None:
        """The token that opens pull requests, from ``$propose_token_env``; None when
        ``allow_propose`` is off. Raises ValueError when it is unset or not usable in a
        header. The message never contains the value."""
        if not self.allow_propose or self.propose_token_env is None:
            return None
        token = environ.get(self.propose_token_env, "").strip()
        if not token:
            raise ValueError(
                f"[connectors.github.ci] propose_token_env: ${self.propose_token_env} is not set"
            )
        if not valid_token(token):
            raise ValueError(
                f"[connectors.github.ci] ${self.propose_token_env} contains characters not "
                "allowed in an HTTP header"
            )
        return token

    def cancel_token(self, environ: Mapping[str, str] = os.environ) -> str | None:
        """The cancel-only token from ``$cancel_token_env``; None when not configured.

        Raises ValueError when it is configured (with ``allow_cancel``) but unset or not
        usable in a header. The message never contains the value."""
        if self.cancel_token_env is None or not self.allow_cancel:
            return None
        token = environ.get(self.cancel_token_env, "").strip()
        if not token:
            raise ValueError(
                f"[connectors.github.ci] cancel_token_env: ${self.cancel_token_env} is not set"
            )
        if not valid_token(token):
            raise ValueError(
                f"[connectors.github.ci] ${self.cancel_token_env} contains characters not "
                "allowed in an HTTP header"
            )
        return token

    def billing_token(self, environ: Mapping[str, str] = os.environ) -> str | None:
        """The billing token from ``$billing_token_env``; None when billing is not read.

        Raises ValueError when billing is configured but the variable is unset or not usable
        in a header. The message never contains the value."""
        if self.billing_account is None or self.billing_token_env is None:
            return None
        token = environ.get(self.billing_token_env, "").strip()
        if not token:
            raise ValueError(
                f"[connectors.github.ci] billing_token_env: ${self.billing_token_env} is not set"
            )
        if not valid_token(token):
            raise ValueError(
                f"[connectors.github.ci] ${self.billing_token_env} contains characters not "
                "allowed in an HTTP header"
            )
        return token

    def may_cancel(self, repo: str, workflow: str | None, branch: str, default: str) -> bool:
        """Whether policy allows cancelling a run of ``workflow`` on ``branch``."""
        if not self.allow_cancel or repo not in self.cancel_repos or branch == default:
            return False
        if self.cancel_workflows and workflow not in self.cancel_workflows:
            return False
        return not self.cancel_branches or any(
            fnmatch.fnmatchcase(branch, g) for g in self.cancel_branches
        )


def load_ci(section: Any, github_repos: Collection[str]) -> CiSettings:
    """Parse ``[connectors.github.ci]``; raises ValueError naming the offending key."""
    where = "[connectors.github.ci]"
    if not isinstance(section, Mapping):
        raise ValueError(f"{where} must be a table")
    if extra := set(section) - _KEYS:
        raise ValueError(f"unknown {where} keys: {sorted(extra)}")
    allowed = {r.lower() for r in github_repos}
    repos = _repos(section.get("repos", sorted(allowed)), allowed, f"{where} repos")
    if not repos:
        raise ValueError(f"{where} repos must not be empty")
    allow_cancel = _bool(section, "allow_cancel", False, where)
    cancel_repos = _repos(section.get("cancel_repos", []), set(repos), f"{where} cancel_repos")
    if allow_cancel and not cancel_repos:
        raise ValueError(f"{where} allow_cancel needs cancel_repos (an allow-list)")
    window = _duration(section, "window", DEFAULT_WINDOW, where)
    if not timedelta(hours=1) <= window <= MAX_WINDOW:
        raise ValueError(f"{where} window must be from 1h to 7d")
    tick_budget = _duration(section, "tick_budget", DEFAULT_TICK_BUDGET, where)
    if not timedelta(0) < tick_budget <= MAX_TICK_BUDGET:
        raise ValueError(f"{where} tick_budget must be positive and at most 60s")
    poll = _duration(section, "poll_interval", DEFAULT_POLL, where)
    if not timedelta(seconds=30) <= poll <= timedelta(hours=1):
        raise ValueError(f"{where} poll_interval must be from 30s to 1h")
    included = section.get("included_minutes")
    if included is not None and (
        isinstance(included, bool) or not isinstance(included, int | float) or included <= 0
    ):
        raise ValueError(f"{where} included_minutes must be a positive number")
    cancel_env = section.get("cancel_token_env")
    if cancel_env is not None and (
        not isinstance(cancel_env, str) or not _ENV_NAME.fullmatch(cancel_env)
    ):
        raise ValueError(f"{where} cancel_token_env must be an environment variable name")
    account, organization, billing_env, billing_interval = _billing(section, included, where)
    if billing_env is not None and billing_env == cancel_env:
        raise ValueError(f"{where} billing_token_env must not be cancel_token_env: one token each")
    skus = _strings(section, "billing_skus", where) if "billing_skus" in section else None
    if skus == ():
        raise ValueError(f"{where} billing_skus must not be empty (leave it out for the default)")
    allow_propose = _bool(section, "allow_propose", False, where)
    propose_repos = _repos(section.get("propose_repos", []), set(repos), f"{where} propose_repos")
    propose_env = section.get("propose_token_env")
    if propose_env is not None and (
        not isinstance(propose_env, str) or not _ENV_NAME.fullmatch(propose_env)
    ):
        raise ValueError(f"{where} propose_token_env must be an environment variable name")
    if allow_propose and (not propose_repos or propose_env is None):
        raise ValueError(
            f"{where} allow_propose needs propose_repos (an allow-list) and propose_token_env "
            "(a token of its own with Contents, Pull requests and Workflows: write)"
        )
    if propose_env is not None and propose_env in (cancel_env, billing_env):
        raise ValueError(
            f"{where} propose_token_env must be a token of its own, not cancel_token_env's "
            "or billing_token_env's"
        )
    reserve = section.get("rate_reserve", DEFAULT_RATE_RESERVE)
    if isinstance(reserve, bool) or not isinstance(reserve, int) or not 0 <= reserve <= 4000:
        raise ValueError(f"{where} rate_reserve must be an integer from 0 to 4000")
    return CiSettings(
        repos=repos,
        enabled=_bool(section, "enabled", True, where),
        window=window,
        poll_interval=poll,
        rate_reserve=reserve,
        max_pages=_int(section, "max_pages", DEFAULT_MAX_PAGES, MAX_PAGES, where),
        max_requests_per_tick=_int(
            section, "max_requests_per_tick", DEFAULT_REQUESTS, MAX_REQUESTS, where
        ),
        tick_budget=tick_budget,
        included_minutes=None if included is None else float(included),
        quota_reset_day=_int(section, "quota_reset_day", 1, 28, where),
        allow_cancel=allow_cancel,
        cancel_repos=cancel_repos,
        cancel_workflows=_strings(section, "cancel_workflows", where),
        cancel_branches=_strings(section, "cancel_branches", where),
        cancel_token_env=cancel_env,
        billing_account=account,
        billing_organization=organization,
        billing_token_env=billing_env,
        billing_interval=billing_interval,
        billing_skus=usage.DEFAULT_SKUS if skus is None else tuple(usage.sku_name(s) for s in skus),
        allow_propose=allow_propose,
        propose_repos=propose_repos,
        propose_token_env=propose_env,
    )


def _billing(
    section: Mapping[str, Any], included: Any, where: str
) -> tuple[str | None, bool, str | None, timedelta]:
    """``billing_user`` or ``billing_org``, ``billing_token_env``, ``billing_interval``."""
    user, org = section.get("billing_user"), section.get("billing_org")
    interval = _duration(section, "billing_interval", usage.DEFAULT_INTERVAL, where)
    if not usage.MIN_INTERVAL <= interval <= usage.MAX_INTERVAL:
        raise ValueError(f"{where} billing_interval must be from 5m to 1h")
    env = section.get("billing_token_env")
    if user is None and org is None:
        if env is not None:
            raise ValueError(f"{where} billing_token_env needs billing_user or billing_org")
        return None, False, None, interval
    if user is not None and org is not None:
        raise ValueError(f"{where}: set billing_user or billing_org, not both")
    account = user if user is not None else org
    key = "billing_user" if user is not None else "billing_org"
    if not isinstance(account, str) or not _ACCOUNT.fullmatch(account):
        raise ValueError(f"{where} {key} must be a GitHub user or organization name")
    if not isinstance(env, str) or not _ENV_NAME.fullmatch(env):
        raise ValueError(
            f"{where} {key} needs billing_token_env (the variable holding a token that may "
            "read its billing usage)"
        )
    if included is None:
        raise ValueError(f"{where} {key} needs included_minutes (the quota it counts against)")
    return account, org is not None, env, interval


@dataclass(frozen=True, slots=True)
class _Target:
    """A cancellable run as the last scan saw it: all a cancel may touch."""

    repo: str
    run_id: int
    attempt: int
    newer_id: int
    group: tuple[Any, ...]
    created_at: datetime


@dataclass(slots=True)
class _RepoScan:
    repo: str
    runs: dict[int, Mapping[str, Any]] = field(default_factory=dict)
    current: bool = True  # every page came from GitHub just now, none from a stale cache
    more: bool = False  # the last page read was full and still inside the window
    default_branch: str | None = None


@dataclass(slots=True)
class _Tick:
    jobs_left: int = MAX_JOB_LOOKUPS
    pulls: dict[tuple[str, int], Mapping[str, Any] | None] = field(default_factory=dict)


class GitHubCI:
    """Snapshot step (recent builds, quota) and :class:`~kelpie.core.ports.ActionConnector`
    for ``cancel_build`` on GitHub Actions."""

    name = SOURCE

    def __init__(
        self,
        client: GitHubClient,
        settings: CiSettings,
        cancel_client: GitHubClient | None = None,
        billing: usage.CiBilling | None = None,
        proposer: Proposer | None = None,
    ) -> None:
        self.client = client
        self.settings = settings
        self.cancel_client = cancel_client or client
        self.billing = billing
        self.proposer = proposer
        self.actions = frozenset(
            {
                *([ActionKind.CANCEL_BUILD] if settings.allow_cancel else []),
                *([ActionKind.PROPOSE_CI_CONFIG] if proposer is not None else []),
            }
        )
        self._builds: tuple[Build, ...] = ()
        self._scanned_at: datetime | None = None
        self._rotation = 0
        self._targets: dict[str, _Target] = {}
        self._cancelled: OrderedDict[str, None] = OrderedDict()
        # Minutes and jobs per run attempt: a finished attempt does not change.
        self._jobs: OrderedDict[tuple[str, int], tuple[float, tuple[Job, ...]]] = OrderedDict()
        self._files: OrderedDict[tuple[str, int, str], tuple[str, ...] | None] = OrderedDict()
        self._branches: dict[str, tuple[str, datetime]] = {}  # repo -> default branch, when
        self._failing: set[str] = set()  # repos whose run list failed (logged once)
        self._reserve_logged = False

    def __repr__(self) -> str:
        return f"GitHubCI({self.client!r}, repos={list(self.settings.repos)})"

    # --- snapshot step --------------------------------------------------------------

    def __call__(self, snapshot: Snapshot) -> Snapshot:
        now = snapshot.now
        if self._scanned_at is None or now - self._scanned_at >= self.settings.poll_interval:
            if self._reserve_low(now):
                # No new scan, and nothing may be cancelled on old evidence.
                self._targets = {}
                self._builds = tuple(replace(b, cancellable=False) for b in self._builds)
            else:
                self._reserve_logged = False
                self._builds = tuple(self._scan(now))
                self._scanned_at = now
        return replace(
            snapshot,
            builds=snapshot.builds + self._builds,
            resources=snapshot.resources + self._quota(now),
        )

    def reserve(self) -> int:
        """The reserve in effect: ``rate_reserve``, at most a fifth of the hourly limit."""
        limit = self.client.rate_limit
        if limit is None or limit <= 0:
            return self.settings.rate_reserve
        return min(self.settings.rate_reserve, int(limit * MAX_RESERVE_SHARE))

    def _reserve_low(self, now: datetime) -> bool:
        remaining, reset = self.client.rate_remaining, self.client.rate_reset
        low = (
            remaining is not None and remaining < self.reserve() and (reset is None or now < reset)
        )
        if low and not self._reserve_logged:
            log.warning(
                "[connectors.github.ci] scan skipped: %s requests left this hour, below the "
                "reserve of %s kept for conditions",
                remaining,
                self.reserve(),
            )
            self._reserve_logged = True
        return low

    def _scan(self, now: datetime) -> list[Build]:
        s = self.settings
        self._targets = {}  # only this scan's evidence counts
        self.client.begin_tick(
            s.max_requests_per_tick, s.tick_budget, min_cache_entries=4 * s.max_requests_per_tick
        )
        start = self._rotation % len(s.repos)
        self._rotation += 1
        order = s.repos[start:] + s.repos[:start]
        since = now - s.window
        scans: list[_RepoScan] = []
        for repo in order:  # page 1 of every repository first
            scan = _RepoScan(repo)
            try:
                self._page(scan, 1, since)
            except GitHubError as exc:
                if repo not in self._failing:
                    log.warning("[connectors.github.ci] %s: runs not read (%s)", repo, exc.kind)
                self._failing.add(repo)
                continue
            self._failing.discard(repo)
            scans.append(scan)
        for scan in scans:
            scan.default_branch = self._default_branch(scan.repo, now)
        for page in range(2, s.max_pages + 1):
            for scan in scans:
                if scan.more:
                    try:
                        self._page(scan, page, since)
                    except GitHubError:
                        scan.more = False  # what the earlier pages gave is still worth having
        return self._builds_of(scans, now)

    def _page(self, scan: _RepoScan, page: int, since: datetime) -> None:
        query = {
            "created": f">={since.date().isoformat()}",
            "per_page": str(PER_PAGE),
            "page": str(page),
        }
        response = self.client.get(f"/repos/{scan.repo}/actions/runs", query)
        scan.current = scan.current and not response.stale
        items = _list(_mapping(response.body).get("workflow_runs"))
        oldest: datetime | None = None
        for run in items:
            run_id, created = _id(run.get("id")), _time(run.get("created_at"))
            if run_id is None or created is None:
                continue
            oldest = created if oldest is None else min(oldest, created)
            if created >= since:
                scan.runs.setdefault(run_id, run)
        scan.more = len(items) >= PER_PAGE and (oldest is None or oldest >= since)

    def _default_branch(self, repo: str, now: datetime) -> str | None:
        known = self._branches.get(repo)
        if known is not None and now - known[1] < DEFAULT_BRANCH_TTL:
            return known[0]
        try:
            response = self.client.get(f"/repos/{repo}")
            branch = _mapping(response.body).get("default_branch")
        except GitHubError:
            return known[0] if known else None
        if not isinstance(branch, str) or not branch:
            return known[0] if known else None
        self._branches[repo] = (branch, response.fetched_at)
        return branch

    def _builds_of(self, scans: list[_RepoScan], now: datetime) -> list[Build]:
        newer: dict[tuple[str, int], Mapping[str, Any]] = {}
        for scan in scans:
            newer.update(self._successors(scan))
        # Active and newest first: they get what is left of the budget for lookups.
        work = [(scan, run) for scan in scans for run in scan.runs.values()]
        work.sort(key=lambda w: (not _status(w[1]).is_active, -_created(w[1])))
        tick = _Tick()
        builds: list[Build] = []
        for scan, run in work:
            run_id = _id(run.get("id")) or 0
            build = self._build(scan, run_id, run, now, tick)
            successor = newer.get((scan.repo, run_id))
            if successor is not None:
                newer_id = _id(successor.get("id")) or 0
                build = replace(build, superseded_by=_build_id(scan.repo, newer_id))
                target = self._target(scan, run_id, run, newer_id, build)
                if target is not None:
                    self._targets[build.id] = target
                    build = replace(build, cancellable=True)
            builds.append(build)
        return builds

    def _successors(self, scan: _RepoScan) -> dict[tuple[str, int], Mapping[str, Any]]:
        """For every run that a newer run of its group supersedes, that newer run."""
        if scan.default_branch is None:
            return {}  # without the default branch nothing is marked
        groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        for run in scan.runs.values():
            group = _group(run)
            if group is not None and run.get("head_branch") != scan.default_branch:
                groups.setdefault(group, []).append(run)
        found: dict[tuple[str, int], Mapping[str, Any]] = {}
        for members in groups.values():
            members.sort(key=lambda r: (_created(r), _id(r.get("id")) or 0))
            for older, newest in itertools.pairwise(members):
                if _created(newest) > _created(older):
                    found[(scan.repo, _id(older.get("id")) or 0)] = newest
        return found

    def _target(
        self,
        scan: _RepoScan,
        run_id: int,
        run: Mapping[str, Any],
        newer_id: int,
        build: Build,
    ) -> _Target | None:
        branch, group, path = run.get("head_branch"), _group(run), run.get("path")
        if (
            not scan.current
            or not build.status.is_active
            or group is None
            or not isinstance(branch, str)
            or scan.default_branch is None
            or not self.settings.may_cancel(
                scan.repo, path if isinstance(path, str) else None, branch, scan.default_branch
            )
        ):
            return None
        created = _time(run.get("created_at"))
        assert created is not None  # _page keeps only runs with a creation time
        return _Target(scan.repo, run_id, _attempt(run), newer_id, group, created)

    def _build(
        self, scan: _RepoScan, run_id: int, run: Mapping[str, Any], now: datetime, tick: _Tick
    ) -> Build:
        status = _status(run)
        created = _time(run.get("created_at"))
        started = _time(run.get("run_started_at")) or created
        updated = _time(run.get("updated_at"))
        finished = None if status.is_active else updated
        head = run.get("head_commit")
        message = head.get("message") if isinstance(head, Mapping) else None
        sha = _str(run.get("head_sha"))
        draft, paths = self._pull_info(scan.repo, run, sha, tick)
        # A stale run list says nothing about now: an active run is measured to its last
        # known update, never extrapolated.
        end = finished or (now if scan.current else updated or started)
        minutes, jobs = self._run_jobs(scan.repo, run_id, run, status, started, end, tick)
        return Build(
            id=_build_id(scan.repo, run_id),
            source=SOURCE,
            repo=scan.repo,
            workflow=_str(run.get("path")) or _str(run.get("name")),
            branch=_str(run.get("head_branch")),
            sha=sha,
            event=_str(run.get("event")),
            pr=_pr_number(run),
            draft=draft,
            commit_message=_first_line(message),
            changed_paths=paths,
            status=status,
            started_at=started,
            finished_at=finished,
            minutes=minutes,
            jobs=jobs,
        )

    def _run_jobs(
        self,
        repo: str,
        run_id: int,
        run: Mapping[str, Any],
        status: BuildStatus,
        started: datetime | None,
        end: datetime | None,
        tick: _Tick,
    ) -> tuple[float, tuple[Job, ...] | None]:
        """The run's minutes and, once it finished, its jobs (None when not read)."""
        if status in {BuildStatus.QUEUED, BuildStatus.SKIPPED} or started is None or end is None:
            return 0.0, None
        wall = round(max(0.0, (end - started).total_seconds() / 60), 1)
        if status.is_active or status is BuildStatus.UNKNOWN:
            return wall, None
        attempt = _attempt(run)
        key = (f"{repo}:{run_id}", attempt)
        if key in self._jobs:
            self._jobs.move_to_end(key)
            return self._jobs[key]
        if tick.jobs_left <= 0:
            return wall, None
        tick.jobs_left -= 1
        try:
            path = f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
            body = _mapping(self.client.get(path, {"per_page": str(PER_PAGE)}).body)
        except GitHubError as exc:
            if exc.kind == "budget":
                tick.jobs_left = 0
            return wall, None
        listed = _list(body.get("jobs"))
        total = body.get("total_count")
        if not isinstance(body.get("jobs"), list) or (
            isinstance(total, int) and total > len(listed)
        ):
            return wall, None  # not every job visible: the wall clock is the estimate
        billed = 0
        for job in listed:
            begun, done = _time(job.get("started_at")), _time(job.get("completed_at"))
            if begun and done and job.get("conclusion") != "skipped":
                billed += max(1, math.ceil((done - begun).total_seconds() / 60))
        found = (float(billed), tuple(j for job in listed if (j := _job(job)) is not None))
        _remember(self._jobs, key, found)
        return found

    def _pull_info(
        self, repo: str, run: Mapping[str, Any], sha: str | None, tick: _Tick
    ) -> tuple[bool, tuple[str, ...] | None]:
        number = _pr_number(run)
        if number is None:
            return False, None
        if (repo, number) not in tick.pulls:
            try:
                body = self.client.get(f"/repos/{repo}/pulls/{number}").body
                tick.pulls[(repo, number)] = _mapping(body)
            except GitHubError:
                tick.pulls[(repo, number)] = None
        pull = tick.pulls[(repo, number)]
        if pull is None:
            return False, None
        draft = pull.get("draft") is True
        head = pull.get("head")
        head_sha = head.get("sha") if isinstance(head, Mapping) else None
        changed = pull.get("changed_files")
        if sha is None or head_sha != sha or not isinstance(changed, int):
            return draft, None
        if changed > MAX_PR_FILES:
            return draft, None
        key = (repo, number, sha)
        if key in self._files:
            self._files.move_to_end(key)
            return draft, self._files[key]
        try:
            path = f"/repos/{repo}/pulls/{number}/files"
            files = _list(self.client.get(path, {"per_page": str(MAX_PR_FILES)}).body)
        except GitHubError:
            return draft, None  # not remembered: asked again on a later scan
        names = tuple(n for f in files if isinstance(n := f.get("filename"), str))
        paths = names if len(names) == changed else None
        _remember(self._files, key, paths)
        return draft, paths

    def _quota(self, now: datetime) -> tuple[Resource, ...]:
        if self.settings.included_minutes is None:
            return ()
        reading = self.billing.used(now) if self.billing is not None else None
        return (
            Resource(
                name=f"ci:{SOURCE}",
                kind=ResourceKind.CI_MINUTES,
                used=reading[0] if reading else None,
                limit=self.settings.included_minutes,
                unit="minutes",
                resets_at=next_reset(now, self.settings.quota_reset_day),
                at=reading[1] if reading else None,
            ),
        )

    # --- action -------------------------------------------------------------------

    def execute(self, action: Action) -> ActionResult:
        """Cancel a superseded run; see the module docs for every guard.

        Raises :class:`GitHubError` on a transient failure, so the engine retries later.
        ``propose_ci_config`` goes to the :class:`Proposer`, when one is configured."""
        if action.kind is ActionKind.PROPOSE_CI_CONFIG:
            return ActionResult.REFUSED if self.proposer is None else self.proposer.execute(action)
        if action.kind is not ActionKind.CANCEL_BUILD or not self.settings.allow_cancel:
            return ActionResult.REFUSED
        build_id = action.params.get("build_id")
        if not isinstance(build_id, str):
            return ActionResult.REFUSED
        if build_id in self._cancelled:
            return ActionResult.NOTHING_TO_DO  # already cancelled by Kelpie
        target = self._targets.get(build_id)
        if target is None:
            log.warning("cancel_build %s refused: not a cancellable run of the last scan", build_id)
            return ActionResult.REFUSED
        run = self._fresh(target.repo, target.run_id)
        if not _status(run).is_active:
            log.info("cancel_build %s: nothing to do, the run is no longer active", build_id)
            self._targets.pop(build_id, None)
            return ActionResult.NOTHING_TO_DO
        if _attempt(run) != target.attempt:
            log.info("cancel_build %s: nothing to do, the run was re-run since the scan", build_id)
            self._targets.pop(build_id, None)
            return ActionResult.NOTHING_TO_DO
        try:
            newer = self._fresh(target.repo, target.newer_id)
        except GitHubError as exc:
            if exc.kind != "not_found":
                raise
            log.info("cancel_build %s: nothing to do, the newer run is gone", build_id)
            self._targets.pop(build_id, None)
            return ActionResult.NOTHING_TO_DO
        if (
            _group(run) != target.group
            or _group(newer) != target.group
            or _created(newer) <= _created(run)
        ):
            log.info("cancel_build %s: nothing to do, no longer superseded", build_id)
            self._targets.pop(build_id, None)
            return ActionResult.NOTHING_TO_DO
        path = f"/repos/{target.repo}/actions/runs/{target.run_id}/cancel"
        try:
            code = self.cancel_client.post(path)
        except GitHubError as exc:
            if exc.kind != "forbidden":
                raise
            log.error(
                "cancel_build %s refused by GitHub (HTTP 403): the cancel token lacks "
                "Actions: write on %s",
                build_id,
                target.repo,
            )
            return ActionResult.REFUSED
        self._targets.pop(build_id, None)
        _remember(self._cancelled, build_id, None)
        if code == 409:
            log.info("cancel_build %s: nothing to do, the run is already finishing", build_id)
            return ActionResult.NOTHING_TO_DO
        log.info("cancel_build %s: cancel requested", build_id)
        return ActionResult.DONE

    def _fresh(self, repo: str, run_id: int) -> Mapping[str, Any]:
        response = self.client.get(f"/repos/{repo}/actions/runs/{run_id}", fresh=True)
        if response.stale:
            raise GitHubError("stale", "run state could not be re-checked")
        body = _mapping(response.body)
        if _id(body.get("id")) != run_id:
            raise GitHubError("bad_response", "run id mismatch")
        return body


def next_reset(now: datetime, day: int) -> datetime:
    """The next ``day`` of a month at 00:00 UTC strictly after ``now``."""
    now = now.astimezone(UTC)
    candidate = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if candidate > now:
        return candidate
    year, month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return candidate.replace(year=year, month=month)


def _pr_number(run: Mapping[str, Any]) -> int | None:
    """The pull request a run builds: the first GitHub lists (same-repository PRs only)."""
    return next((n for e in _list(run.get("pull_requests")) if (n := _id(e.get("number")))), None)


def _job(job: Mapping[str, Any]) -> Job | None:
    """A job of the jobs list (None without a name). A job that did not succeed keeps its
    first named step that did not succeed, its runner and labels; a passing one only its
    name, which is all rule 9 needs of it and keeps the cache small."""
    name = _str(job.get("name"))
    if name is None:
        return None
    conclusion = _status(job)
    if conclusion in {BuildStatus.SUCCESS, BuildStatus.SKIPPED}:
        return Job(name=name[:MAX_NAME], conclusion=conclusion)
    steps = (
        n
        for s in _list(job.get("steps"))
        if isinstance(c := s.get("conclusion"), str) and c in _FAILED and (n := _str(s.get("name")))
    )
    step, runner = next(steps, None), _str(job.get("runner_name"))
    if job.get("runner_group_name") == HOSTED_GROUP:
        runner = None  # a pool name GitHub reuses across machines, not a machine
    labels = job.get("labels") if isinstance(job.get("labels"), list) else []
    return Job(
        name=name[:MAX_NAME],
        conclusion=conclusion,
        step=step[:MAX_NAME] if step else None,
        runner=runner[:MAX_NAME] if runner else None,
        labels=tuple(x[:MAX_NAME] for x in labels if isinstance(x, str) and x)[:MAX_LABELS],
    )


def _build_id(repo: str, run_id: int) -> str:
    return f"{repo}:{run_id}"


def _status(run: Mapping[str, Any]) -> BuildStatus:
    status, conclusion = run.get("status"), run.get("conclusion")
    if status == "completed":
        return (
            _CONCLUSIONS.get(conclusion, BuildStatus.UNKNOWN)
            if isinstance(conclusion, str)
            else BuildStatus.UNKNOWN
        )
    return (
        _ACTIVE.get(status, BuildStatus.UNKNOWN)
        if isinstance(status, str)
        else (BuildStatus.UNKNOWN)
    )


def _attempt(run: Mapping[str, Any]) -> int:
    return _id(run.get("run_attempt")) or 1


def _created(run: Mapping[str, Any]) -> float:
    created = _time(run.get("created_at"))
    return created.timestamp() if created else 0.0


def _group(run: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Runs that supersede each other: same workflow, branch (full name), head repository
    and event, and for a pull request the same PR and base branch. None: never superseded."""
    event, branch, workflow = run.get("event"), run.get("head_branch"), _id(run.get("workflow_id"))
    if event not in SUPERSEDABLE_EVENTS or not isinstance(branch, str) or not branch:
        return None
    if workflow is None or _attempt(run) > 1:
        return None  # a re-run is a human's decision
    head_repo = run.get("head_repository")
    origin = head_repo.get("full_name") if isinstance(head_repo, Mapping) else None
    if not isinstance(origin, str):
        return None
    pr: tuple[int, str] | None = None
    if event == "pull_request":
        entries = _list(run.get("pull_requests"))
        if len(entries) != 1:
            return None  # no PR (a fork) or several: which one triggered it is unknown
        base = entries[0].get("base")
        number, ref = (
            _id(entries[0].get("number")),
            base.get("ref") if isinstance(base, Mapping) else None,
        )
        if number is None or not isinstance(ref, str):
            return None
        pr = (number, ref)
    return (workflow, branch, origin, event, pr)


def _remember[K, V](cache: OrderedDict[K, V], key: K, value: V) -> None:
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > MAX_REMEMBERED:
        cache.popitem(last=False)


def _first_line(message: Any) -> str | None:
    if not isinstance(message, str):
        return None
    lines = message.strip().splitlines()
    return lines[0][:MAX_MESSAGE] if lines else None


def _str(value: Any) -> str | None:
    """A display value: truncated, so never use it to compare or group."""
    return value[:MAX_DISPLAY] if isinstance(value, str) and value else None


def _id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GitHubError("bad_response", "unexpected response shape")
    return value


def _list(value: Any) -> list[Mapping[str, Any]]:
    return [v for v in value if isinstance(v, Mapping)] if isinstance(value, list) else []


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_datetime(value.replace("Z", "+00:00"))
    except ValueError:
        return None


# --- configuration helpers --------------------------------------------------------------


def _repos(value: Any, allowed: Collection[str], where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(valid_repo(r) for r in value):
        raise ValueError(f'{where} must be a list of "owner/name" strings')
    repos = tuple(dict.fromkeys(r.lower() for r in value))
    if outside := [r for r in repos if r not in allowed]:
        raise ValueError(f"{where}: not in the allowed repositories: {outside}")
    return repos


def _strings(section: Mapping[str, Any], key: str, where: str) -> tuple[str, ...]:
    value = section.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
        raise ValueError(f"{where} {key} must be a list of non-empty strings")
    return tuple(v.strip() for v in value)


def _bool(section: Mapping[str, Any], key: str, default: bool, where: str) -> bool:
    value = section.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{where} {key} must be true or false")
    return value


def _int(section: Mapping[str, Any], key: str, default: int, top: int, where: str) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
        raise ValueError(f"{where} {key} must be an integer from 1 to {top}")
    return value


def _duration(section: Mapping[str, Any], key: str, default: timedelta, where: str) -> timedelta:
    try:
        return parse_duration(section.get(key, default))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{where} {key}: {exc}") from None
