"""GitHub Actions as the CI source of rule 9 (``ci_economy``).

A snapshot step reads recent workflow runs of the configured repositories and maps them
onto the CI-agnostic :class:`~kelpie.core.model.Build` records:

- Only runs created within ``window`` (default 24 h) are read: the run list is paged
  newest first, at most ``max_pages`` pages of 100 per repository, within the step's own
  per-tick request and time budget. The query names a date, not a time, so the URL stays
  the same for a day and unchanged pages come back as 304 (free of the GitHub rate limit).
- ``minutes`` of a finished run are the billable minutes of its jobs (each job rounded up
  to a minute, as GitHub bills), read once per run attempt and remembered; while a run is
  active, or when the job lookup does not fit in the budget, the run's wall-clock time.
- ``superseded_by``: a newer run of the same workflow, branch, head repository and event
  (``push`` or ``pull_request`` only; deploys, schedules and manual runs are never marked)
  was created after it.
- ``draft`` and ``changed_paths`` come from the pull request the run belongs to (the
  PR's current draft state; its changed files, only while the PR head is the run's commit
  and it touches at most 100 files, else unknown). Files are remembered per PR head.
- With ``included_minutes`` set, a ``ci_minutes`` resource carries the monthly quota and
  its reset (``quota_reset_day``, UTC). GitHub's billing API needs an administrative
  permission, so minutes used are not read: the rule projects the burn of the builds it
  sees against the quota.

The one write is ``cancel_build`` (``POST /repos/{repo}/actions/runs/{id}/cancel``), and
it is guarded on every side:

- it runs only through the engine, so the rule's mode and the global dry-run apply;
- ``allow_cancel`` is off by default; without it the connector declares no action;
- the run must be one this connector reported as superseded and active in its last scan
  (the action's ``build_id`` is looked up there, never parsed), in a repository listed in
  ``cancel_repos`` and, if ``cancel_workflows`` is set, a workflow listed there;
- right before cancelling, both runs are fetched again, outside the cache: the run must
  still be queued or running and the newer run must still be a newer run of the same
  workflow, branch and event. A stale or failed re-check refuses the cancel;
- it is idempotent: a run already cancelled by Kelpie, or one that finished in the
  meantime (HTTP 409 or a completed status), is not touched again.

Token scopes (least privilege): reading needs ``Actions: read``, ``Pull requests: read``
and ``Contents: read`` (fine-grained token) or ``repo`` for a classic token on private
repositories. Cancelling needs ``Actions: write``; grant it only together with
``allow_cancel`` and only on the ``cancel_repos``.
"""

from __future__ import annotations

import itertools
import logging
import math
from collections import OrderedDict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.connectors.github.connector import valid_repo
from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import Build, BuildStatus, Resource, ResourceKind, Snapshot
from kelpie.core.serde import parse_datetime, parse_duration

log = logging.getLogger(__name__)

SOURCE = "github"  # Build.source and executor name
PER_PAGE = 100
DEFAULT_WINDOW = timedelta(hours=24)
MAX_WINDOW = timedelta(days=7)
DEFAULT_MAX_PAGES = 3
MAX_PAGES = 10
DEFAULT_REQUESTS = 30
MAX_REQUESTS = 1000
DEFAULT_TICK_BUDGET = timedelta(seconds=10)
MAX_TICK_BUDGET = timedelta(seconds=60)
MAX_JOB_LOOKUPS = 10  # per tick; the rest use wall-clock minutes until a later tick
MAX_REMEMBERED = 4096  # run minutes and PR files kept across ticks
MAX_MESSAGE = 200  # characters of a commit message's first line
MAX_PR_FILES = 100
SUPERSEDABLE_EVENTS = frozenset({"push", "pull_request"})
_ACTIVE = {"queued": BuildStatus.QUEUED, "waiting": BuildStatus.QUEUED}
_ACTIVE |= {"requested": BuildStatus.QUEUED, "pending": BuildStatus.QUEUED}
_ACTIVE |= {"in_progress": BuildStatus.RUNNING}
_CONCLUSIONS = {
    "success": BuildStatus.SUCCESS,
    "neutral": BuildStatus.SUCCESS,
    "skipped": BuildStatus.SKIPPED,
    "cancelled": BuildStatus.CANCELLED,
}
_KEYS = {
    "enabled",
    "repos",
    "window",
    "max_pages",
    "max_requests_per_tick",
    "tick_budget",
    "included_minutes",
    "quota_reset_day",
    "allow_cancel",
    "cancel_repos",
    "cancel_workflows",
}


@dataclass(frozen=True, slots=True)
class CiSettings:
    """``[connectors.github.ci]``."""

    repos: tuple[str, ...]
    enabled: bool = True
    window: timedelta = DEFAULT_WINDOW
    max_pages: int = DEFAULT_MAX_PAGES
    max_requests_per_tick: int = DEFAULT_REQUESTS
    tick_budget: timedelta = DEFAULT_TICK_BUDGET
    included_minutes: float | None = None
    quota_reset_day: int = 1
    allow_cancel: bool = False
    cancel_repos: tuple[str, ...] = ()
    cancel_workflows: tuple[str, ...] = ()


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
    enabled = _bool(section, "enabled", True, where)
    allow_cancel = _bool(section, "allow_cancel", False, where)
    cancel_repos = _repos(section.get("cancel_repos", []), set(repos), f"{where} cancel_repos")
    if allow_cancel and not cancel_repos:
        raise ValueError(f"{where} allow_cancel needs cancel_repos (an allow-list)")
    workflows = section.get("cancel_workflows", [])
    if not isinstance(workflows, list) or not all(
        isinstance(w, str) and w.strip() for w in workflows
    ):
        raise ValueError(f"{where} cancel_workflows must be a list of workflow paths")
    window = _duration(section, "window", DEFAULT_WINDOW, where)
    if not timedelta(hours=1) <= window <= MAX_WINDOW:
        raise ValueError(f"{where} window must be from 1h to 7d")
    tick_budget = _duration(section, "tick_budget", DEFAULT_TICK_BUDGET, where)
    if not timedelta(0) < tick_budget <= MAX_TICK_BUDGET:
        raise ValueError(f"{where} tick_budget must be positive and at most 60s")
    included = section.get("included_minutes")
    if included is not None and (
        isinstance(included, bool) or not isinstance(included, int | float) or included <= 0
    ):
        raise ValueError(f"{where} included_minutes must be a positive number")
    return CiSettings(
        repos=repos,
        enabled=enabled,
        window=window,
        max_pages=_int(section, "max_pages", DEFAULT_MAX_PAGES, MAX_PAGES, where),
        max_requests_per_tick=_int(
            section, "max_requests_per_tick", DEFAULT_REQUESTS, MAX_REQUESTS, where
        ),
        tick_budget=tick_budget,
        included_minutes=None if included is None else float(included),
        quota_reset_day=_int(section, "quota_reset_day", 1, 28, where),
        allow_cancel=allow_cancel,
        cancel_repos=cancel_repos,
        cancel_workflows=tuple(w.strip() for w in workflows),
    )


@dataclass(frozen=True, slots=True)
class _Target:
    """A superseded active run as the last scan saw it: what a cancel may touch."""

    repo: str
    run_id: int
    newer_id: int
    group: tuple[Any, ...]  # workflow id, branch, head repository, event
    created_at: datetime
    workflow: str | None


@dataclass(slots=True)
class _Tick:
    jobs_left: int = MAX_JOB_LOOKUPS
    pulls: dict[tuple[str, int], Mapping[str, Any] | None] = field(default_factory=dict)


class GitHubCI:
    """Snapshot step (recent builds, quota) and :class:`~kelpie.core.ports.ActionConnector`
    for ``cancel_build`` on GitHub Actions."""

    name = SOURCE

    def __init__(self, client: GitHubClient, settings: CiSettings) -> None:
        self.client = client
        self.settings = settings
        self.actions = (
            frozenset({ActionKind.CANCEL_BUILD}) if settings.allow_cancel else frozenset()
        )
        self._targets: dict[str, _Target] = {}
        self._cancelled: OrderedDict[str, None] = OrderedDict()
        self._minutes: OrderedDict[tuple[str, int], float] = OrderedDict()
        self._files: OrderedDict[tuple[str, int, str], tuple[str, ...] | None] = OrderedDict()
        self._failing: set[str] = set()  # repos whose run list failed last tick (logged once)

    def __repr__(self) -> str:
        return f"GitHubCI({self.client!r}, repos={list(self.settings.repos)})"

    # --- snapshot step --------------------------------------------------------------

    def __call__(self, snapshot: Snapshot) -> Snapshot:
        now = snapshot.now
        s = self.settings
        self.client.begin_tick(
            s.max_requests_per_tick, s.tick_budget, min_cache_entries=4 * s.max_requests_per_tick
        )
        tick = _Tick()
        builds: list[Build] = []
        targets: dict[str, _Target] = {}
        for repo in s.repos:
            try:
                runs, current = self._runs(repo, now)
            except GitHubError as exc:
                if repo not in self._failing:
                    log.warning("[connectors.github.ci] %s: runs not read (%s)", repo, exc.kind)
                self._failing.add(repo)
                continue
            self._failing.discard(repo)
            found, superseded = self._builds(repo, runs, now, tick)
            builds.extend(found)
            if current:
                targets.update(superseded)
        # Only what this scan saw from GitHub may be cancelled: a repo whose run list failed,
        # or came from the cache because GitHub could not be asked, has no targets.
        self._targets = targets
        return replace(
            snapshot,
            builds=snapshot.builds + tuple(builds),
            resources=snapshot.resources + self._quota(now),
        )

    def _runs(self, repo: str, now: datetime) -> tuple[list[Mapping[str, Any]], bool]:
        """Runs created within the window, and whether every page read was current."""
        since = now - self.settings.window
        runs: dict[int, Mapping[str, Any]] = {}
        current = True
        for page in range(1, self.settings.max_pages + 1):
            query = {
                "created": f">={since.date().isoformat()}",
                "per_page": str(PER_PAGE),
                "page": str(page),
            }
            try:
                response = self.client.get(f"/repos/{repo}/actions/runs", query)
                body = _mapping(response.body)
                current = current and not response.stale
            except GitHubError:
                if page == 1:
                    raise
                break  # what the earlier pages gave is still worth having
            items = _list(body.get("workflow_runs"))
            oldest: datetime | None = None
            for run in items:
                run_id, created = _id(run.get("id")), _time(run.get("created_at"))
                if run_id is None or created is None:
                    continue
                oldest = created if oldest is None else min(oldest, created)
                if created >= since:
                    runs.setdefault(run_id, run)
            if len(items) < PER_PAGE or (oldest is not None and oldest < since):
                break
        return list(runs.values()), current

    def _builds(
        self, repo: str, runs: list[Mapping[str, Any]], now: datetime, tick: _Tick
    ) -> tuple[list[Build], dict[str, _Target]]:
        # Active and newest first: they get the budget for PR and job lookups first.
        def order(run: Mapping[str, Any]) -> tuple[bool, float]:
            created = _time(run.get("created_at"))
            return (_status(run).is_active is False, -(created.timestamp() if created else 0))

        runs = sorted(runs, key=order)
        groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        for run in runs:
            if (group := _group(run)) is not None:
                groups.setdefault(group, []).append(run)
        newer: dict[int, Mapping[str, Any]] = {}
        for members in groups.values():
            members.sort(key=lambda r: (_time(r.get("created_at")), _id(r.get("id"))))
            for older, newest in itertools.pairwise(members):
                newer[_id(older.get("id")) or 0] = newest
        builds: list[Build] = []
        targets: dict[str, _Target] = {}
        for run in runs:
            run_id = _id(run.get("id"))
            assert run_id is not None  # _runs keeps only runs with an id
            build = self._build(repo, run_id, run, now, tick)
            successor = newer.get(run_id)
            if successor is not None:
                build = replace(build, superseded_by=_build_id(repo, _id(successor.get("id"))))
                group = _group(run)
                created = _time(run.get("created_at"))
                if build.status.is_active and group is not None and created is not None:
                    targets[build.id] = _Target(
                        repo,
                        run_id,
                        _id(successor.get("id")) or 0,
                        group,
                        created,
                        build.workflow,
                    )
            builds.append(build)
        return builds, targets

    def _build(
        self, repo: str, run_id: int, run: Mapping[str, Any], now: datetime, tick: _Tick
    ) -> Build:
        status = _status(run)
        created = _time(run.get("created_at"))
        started = _time(run.get("run_started_at")) or created
        finished = None if status.is_active else _time(run.get("updated_at"))
        head = run.get("head_commit")
        message = head.get("message") if isinstance(head, Mapping) else None
        sha = _str(run.get("head_sha"))
        draft, paths = self._pull_info(repo, run, sha, tick)
        return Build(
            id=_build_id(repo, run_id),
            source=SOURCE,
            repo=repo,
            workflow=_str(run.get("path")) or _str(run.get("name")),
            branch=_str(run.get("head_branch")),
            sha=sha,
            event=_str(run.get("event")),
            draft=draft,
            commit_message=_first_line(message),
            changed_paths=paths,
            status=status,
            started_at=started,
            finished_at=finished,
            minutes=self._run_minutes(repo, run_id, run, status, started, finished, now, tick),
        )

    def _run_minutes(
        self,
        repo: str,
        run_id: int,
        run: Mapping[str, Any],
        status: BuildStatus,
        started: datetime | None,
        finished: datetime | None,
        now: datetime,
        tick: _Tick,
    ) -> float:
        if status is BuildStatus.QUEUED or status is BuildStatus.SKIPPED or started is None:
            return 0.0
        wall = max(0.0, ((finished or now) - started).total_seconds() / 60)
        if status.is_active:
            return round(wall, 1)
        attempt = _id(run.get("run_attempt")) or 1
        key = (f"{repo}:{run_id}", attempt)
        if key in self._minutes:
            self._minutes.move_to_end(key)
            return self._minutes[key]
        if tick.jobs_left <= 0:
            return round(wall, 1)
        tick.jobs_left -= 1
        try:
            path = f"/repos/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs"
            body = _mapping(self.client.get(path, {"per_page": str(PER_PAGE)}).body)
        except GitHubError as exc:
            if exc.kind == "budget":
                tick.jobs_left = 0
            return round(wall, 1)
        jobs = _list(body.get("jobs"))
        total = body.get("total_count")
        if isinstance(total, int) and total > len(jobs):
            return round(wall, 1)  # not every job visible: the wall clock is the estimate
        billed = 0
        for job in jobs:
            begun, done = _time(job.get("started_at")), _time(job.get("completed_at"))
            if begun and done and job.get("conclusion") != "skipped":
                billed += max(1, math.ceil((done - begun).total_seconds() / 60))
        _remember(self._minutes, key, float(billed))
        return float(billed)

    def _pull_info(
        self, repo: str, run: Mapping[str, Any], sha: str | None, tick: _Tick
    ) -> tuple[bool, tuple[str, ...] | None]:
        number = None
        for entry in _list(run.get("pull_requests")):
            number = _id(entry.get("number"))
            if number is not None:
                break
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
            return draft, None  # not remembered: asked again on a later tick
        names = tuple(n for f in files if isinstance(n := f.get("filename"), str))
        paths = names if len(names) == changed else None
        _remember(self._files, key, paths)
        return draft, paths

    def _quota(self, now: datetime) -> tuple[Resource, ...]:
        if self.settings.included_minutes is None:
            return ()
        return (
            Resource(
                name=f"ci:{SOURCE}",
                kind=ResourceKind.CI_MINUTES,
                limit=self.settings.included_minutes,
                unit="minutes",
                resets_at=next_reset(now, self.settings.quota_reset_day),
            ),
        )

    # --- action -------------------------------------------------------------------

    def execute(self, action: Action) -> bool:
        """Cancel a superseded run; see the module docs for every guard. False: refused."""
        if action.kind is not ActionKind.CANCEL_BUILD or not self.settings.allow_cancel:
            return False
        build_id = action.params.get("build_id")
        if not isinstance(build_id, str):
            return False
        if build_id in self._cancelled:
            return True  # idempotent: already cancelled by Kelpie
        target = self._targets.get(build_id)
        if target is None:
            log.warning("cancel_build %s refused: not a superseded run of the last scan", build_id)
            return False
        if target.repo not in self.settings.cancel_repos or (
            self.settings.cancel_workflows and target.workflow not in self.settings.cancel_workflows
        ):
            log.warning("cancel_build %s refused: not in cancel_repos/cancel_workflows", build_id)
            return False
        try:
            run = self._fresh(target.repo, target.run_id)
            if not _status(run).is_active:
                log.info("cancel_build %s: the run already finished", build_id)
                self._targets.pop(build_id, None)
                return True
            newer = self._fresh(target.repo, target.newer_id)
            newer_created = _time(newer.get("created_at"))
            if (
                _group(run) != target.group
                or _group(newer) != target.group
                or newer_created is None
                or newer_created <= target.created_at
            ):
                log.warning("cancel_build %s refused: no longer superseded", build_id)
                return False
            code = self.client.post(f"/repos/{target.repo}/actions/runs/{target.run_id}/cancel")
        except GitHubError as exc:
            log.warning("cancel_build %s not done: %s", build_id, exc.kind)
            return False
        self._targets.pop(build_id, None)
        _remember(self._cancelled, build_id, None)
        log.info(
            "cancel_build %s: %s",
            build_id,
            "already finishing" if code == 409 else "cancel requested",
        )
        return True

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


def _build_id(repo: str, run_id: int | None) -> str:
    return f"{repo}:{run_id}"


def _status(run: Mapping[str, Any]) -> BuildStatus:
    status, conclusion = run.get("status"), run.get("conclusion")
    if status != "completed":
        return (
            _ACTIVE.get(status, BuildStatus.QUEUED)
            if isinstance(status, str)
            else (BuildStatus.QUEUED)
        )
    if isinstance(conclusion, str) and conclusion in _CONCLUSIONS:
        return _CONCLUSIONS[conclusion]
    return BuildStatus.FAILURE


def _group(run: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Runs that supersede each other: same workflow, branch, head repository, event."""
    event = run.get("event")
    branch = _str(run.get("head_branch"))
    workflow = _id(run.get("workflow_id"))
    if event not in SUPERSEDABLE_EVENTS or branch is None or workflow is None:
        return None
    head_repo = run.get("head_repository")
    origin = head_repo.get("full_name") if isinstance(head_repo, Mapping) else None
    return (workflow, branch, origin if isinstance(origin, str) else None, event)


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
    return value[:255] if isinstance(value, str) and value else None


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
