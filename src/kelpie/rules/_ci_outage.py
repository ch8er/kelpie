"""Rule 9, part 4: a CI outage is one incident (incident 20).

The farm's toolchain broke for about 12 hours; every mobile PR stayed red, Kelpie raised one
alert per blocked task and the report blamed "waiting for merges". When many change
requests fail the same way at the same time, the cause is shared: the CI (a toolchain, a
runner, a service) or the base the changes build on, not each change.

**Signature.** A failed job of a finished build (``Build.jobs``, ``Job.failed``) has the
signature *workflow, job, first failing step*. Builds whose jobs the connector did not read
take no part. No log scraping.

**Active failures.** A failure is active until a build passes that workflow's job again in
its own repository (a pass elsewhere, on other runners, fixes nothing there). Times are the
builds' finish times.

**Open.** Some ``window`` holds active failures of at least ``min_prs`` change requests
(repository and branch; retries count, so a slow farm whose PRs stay red still qualifies),
and there is evidence that the environment broke rather than each change:

- two repositories fail on the same runner (same machine, different code; a CI's shared
  hosted pool names no machine), or
- a commit that passed the job before fails it now in one of their repositories (a re-run:
  the code did not change), since the group began failing; that re-run's change request
  does not count toward ``min_prs``.

Without such evidence it takes twice ``min_prs`` change requests: three unrelated PRs red at
a generic step ("Run tests") within two hours happen on a busy farm; six with nothing
passing in between rarely do. With evidence, only change requests in the repositories it
ties together count (those on a shared runner or of a re-run, and any repository failing the
same way on their runners), so a PR red at the same step elsewhere stays its own business;
when they are too few, the group still opens at twice ``min_prs`` (evidence never holds back
what would open without it). Runners are matched by name: a pool of self-hosted machines
with different names counts as different runners. ``since`` is the first failure of the
outage's repositories in the window, walked back while their failures follow each other
within ``window``: one change red since yesterday does not move it.

**Resolve.** One long-lived alert state (:func:`kelpie.core.incidents.outage_state_id`); the
rule stops reporting it when a build passes the job again in one of its repositories, when
its failures leave the CI connector's window, or when too few failing builds stay readable
(re-runs in progress, the connector's job-list budget): then it resolves after the engine's
``resolve_after`` and may open again as a new announcement.

**Scope** (the alert's params, :class:`kelpie.core.incidents.CiOutage`): the repositories,
workflows, runners (names and labels) and PRs of the active failures since ``since`` in
those repositories, and the tasks the outage explains:

- a PR's own task when it is blocked or waiting on its CI or merge: an unmet CI or merge
  condition that names no other PR, or no condition at all, waiting since the outage began
  (``since`` minus ``window``) and on no unfinished task; a heuristic: a block reason that
  asks a person without registering a condition looks the same;
- any task waiting on an unmet CI or merge condition of one of those PRs.

When the code host listed its PRs in full, PRs that are no longer open are left out.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.conditions import linked_conditions
from kelpie.core.incidents import MAX_BUILDS, CiOutage, outage_signature, outage_state_id
from kelpie.core.model import (
    Build,
    BuildStatus,
    ConditionKind,
    Job,
    PullRequest,
    Snapshot,
    Status,
    Task,
)

MAX_LISTED = 10  # PRs and tasks named in the alert text
UNSURE_FACTOR = 2  # change requests needed, in multiples of min_prs, without evidence
_NOTICE_RUN = timedelta(hours=2)  # failures this close are one run, for the resolved notice
_WAITING = frozenset({Status.BLOCKED, Status.WAITING})
_PR_CONDITIONS = frozenset({ConditionKind.CI_STATUS, ConditionKind.PR_MERGED})

type Signature = tuple[str | None, str, str | None]  # workflow, job, failing step
type _PassKey = tuple[str, str | None, str]  # repository, workflow, job
type _Passes = dict[_PassKey, list[tuple[datetime, Build]]]


@dataclass(frozen=True, slots=True)
class _Failure:
    build: Build
    job: Job
    at: datetime


def outages(snapshot: Snapshot, rule: str, min_prs: int, window: timedelta) -> list[Action]:
    """One alert per open outage (see the module docs); none with ``min_prs`` = 0."""
    if min_prs <= 0:
        return []
    passes = _passes(snapshot)
    actions = []
    for sig, found in sorted(_failures(snapshot).items(), key=lambda kv: str(kv[0])):
        active = _active(found, passes, sig)
        if (opened := _opened(active, passes, sig, min_prs, window)) is not None:
            since, repos = opened
            scope = [f for f in active if f.at >= since and _repo(f.build) in repos]
            actions.append(_alert(snapshot, rule, sig, scope, since, window))
    return actions


def passed_again(state_id: str, snapshot: Snapshot) -> str | None:
    """For the "Resolved" notice: the first build that passed the outage's job again in one
    of the repositories of its latest run of failures (those following each other within
    two hours of the last one), after that repository's last failure; None when there is
    none (the outage stopped being reported for another reason: the engine's general
    notice says so)."""
    found = next(
        (fs for sig, fs in _failures(snapshot).items() if outage_state_id(*sig) == state_id), []
    )
    if not found:
        return None
    times = sorted(f.at for f in found)
    began = _began(times, times[-1], _NOTICE_RUN)
    workflow, job = found[0].build.workflow, found[0].job.name
    passes = _passes(snapshot)
    last: dict[str, datetime] = {}
    for f in found:
        if f.at >= began:
            last[_repo(f.build)] = max(f.at, last.get(_repo(f.build), f.at))
    after = [
        (at, b.id, b)
        for repo, failed in last.items()
        for at, b in passes.get((repo, workflow, job), ())
        if at > failed
    ]
    if not after:
        return None
    at, _, build = min(after, key=lambda p: (p[0], p[1]))
    where = f" in {build.repo}" if build.repo else ""
    return (
        f"CI outage over: {outage_signature(workflow, job, None)} passed again{where} at "
        f"{at:%Y-%m-%d %H:%M} UTC ({build.id})."
    )


def _failures(snapshot: Snapshot) -> dict[Signature, list[_Failure]]:
    found: dict[Signature, list[_Failure]] = {}
    for build in snapshot.builds:
        at = build.finished_at or build.started_at
        for job in build.jobs or ():
            if job.failed and at is not None:
                found.setdefault((build.workflow, job.name, job.step), []).append(
                    _Failure(build, job, at)
                )
    return found


def _passes(snapshot: Snapshot) -> _Passes:
    """Builds in which a job passed, by repository, workflow and job name."""
    found: _Passes = {}
    for build in snapshot.builds:
        at = build.finished_at or build.started_at
        for job in build.jobs or ():
            if job.conclusion is BuildStatus.SUCCESS and at is not None:
                found.setdefault((_repo(build), build.workflow, job.name), []).append((at, build))
    return found


def _active(found: list[_Failure], passes: _Passes, sig: Signature) -> list[_Failure]:
    """The failures newer than the job's last pass in their own repository."""
    workflow, job, _ = sig

    def last_pass(repo: str) -> datetime | None:
        return max((at for at, _ in passes.get((repo, workflow, job), ())), default=None)

    last = {repo: last_pass(repo) for repo in {_repo(f.build) for f in found}}
    return [f for f in found if (p := last[_repo(f.build)]) is None or f.at > p]


def _opened(
    active: list[_Failure], passes: _Passes, sig: Signature, min_prs: int, window: timedelta
) -> tuple[datetime, set[str]] | None:
    """When the outage began and its repositories, for the first window that qualifies, or
    None (see "Open" in the module docs)."""
    by_change: dict[tuple[str, str], list[_Failure]] = {}
    for f in active:
        by_change.setdefault(_change(f.build), []).append(f)
    reruns = [f for f in active if _rerun(f, passes, sig)]
    for start in sorted({f.at for f in active}):
        group = {
            c for c, fs in by_change.items() if any(start <= f.at <= start + window for f in fs)
        }
        if len(group) < min_prs:
            continue
        failures = [f for c in group for f in by_change[c]]
        began = _began(sorted(f.at for f in failures), start, window)
        group_repos = {c[0] for c in group}
        # A failed re-run in one of these repositories since the group began failing.
        evidence = {
            _change(f.build)
            for f in reruns
            if _repo(f.build) in group_repos and f.at >= began - window
        }
        shared = _shared_runners(failures)
        # The repositories the evidence ties together, and those failing on their runners.
        core = {_repo(f.build) for f in failures if f.job.runner in shared}
        core |= {c[0] for c in evidence}
        tied = core | _runner_mates([f for f in failures if _repo(f.build) in core], active)
        if core and len({c for c in group if c[0] in tied} - evidence) >= min_prs:
            repos = tied
        elif len(group - evidence) >= min_prs * UNSURE_FACTOR:
            repos = group_repos | tied  # evidence never holds back what opens without it
        else:
            continue
        mine = [f for c in group if c[0] in repos for f in by_change[c]]
        first = min(f.at for f in mine if start <= f.at <= start + window)
        return _began(sorted(f.at for f in mine), first, window), repos
    return None


def _began(times: list[datetime], start: datetime, window: timedelta) -> datetime:
    """Walk back from the window's start while the group's failures follow each other
    within ``window``: one change red since yesterday does not move the outage's start."""
    began = start
    for at in reversed([t for t in times if t < start]):
        if began - at > window:
            break
        began = at
    return began


def _shared_runners(failures: list[_Failure]) -> set[str]:
    """Runners two or more repositories fail on."""
    repos: dict[str, set[str]] = {}
    for f in failures:
        if f.job.runner and f.build.repo:
            repos.setdefault(f.job.runner, set()).add(_repo(f.build))
    return {runner for runner, r in repos.items() if len(r) >= 2}


def _runner_mates(group: list[_Failure], active: list[_Failure]) -> set[str]:
    """Other repositories failing the same way on a runner the group failed on."""
    runners = {f.job.runner for f in group if f.job.runner}
    return {_repo(f.build) for f in active if f.build.repo and f.job.runner in runners}


def _rerun(f: _Failure, passes: _Passes, sig: Signature) -> bool:
    """The failing build's commit passed this job before in the same repository."""
    workflow, job, _ = sig
    passed = passes.get((_repo(f.build), workflow, job), ())
    return bool(f.build.sha) and any(b.sha == f.build.sha and at < f.at for at, b in passed)


def _alert(
    snapshot: Snapshot,
    rule: str,
    sig: Signature,
    active: list[_Failure],
    since: datetime,
    window: timedelta,
) -> Action:
    workflow, job, step = sig
    builds = sorted({f.build.id: f.build for f in active}.values(), key=lambda b: b.id)
    changes = {_change(b) for b in builds}
    pulls = _pulls(snapshot, builds)
    prs = sorted(f"{repo}#{number}" for repo, number in pulls)
    tasks = _tasks(snapshot, pulls, since - window)
    runners = sorted({x for f in active for x in (f.job.runner, *f.job.labels) if x})
    repos = sorted({_repo(b) for b in builds if b.repo})
    state = outage_state_id(workflow, job, step)
    outage = CiOutage(
        state=state,
        signature=outage_signature(workflow, job, step),
        job=job,
        workflow=workflow,
        step=step,
        repos=tuple(repos),
        workflows=tuple(sorted({b.workflow for b in builds if b.workflow})),
        prs=tuple(prs),
        tasks=tuple(tasks),
        runners=tuple(runners),
        builds=tuple(b.id for b in sorted(builds, key=_newest)[:MAX_BUILDS]),
    )
    where = f" in {', '.join(repos)}" if repos else ""
    on = f" on {', '.join(runners)}" if runners else ""
    named = f": {_listed(prs)}" if prs else ""
    waiting = f" {len(tasks)} task(s) wait on it: {_listed(tasks)}." if tasks else ""
    reason = (
        f"CI outage since {since:%Y-%m-%d %H:%M} UTC: {len(changes)} change(s){where} fail at "
        f"{outage.signature}{on}, and nothing has passed that job since{named}. The cause is "
        "shared, not each change's own: the CI (toolchain, runner, service) or the base they "
        f"build on. Check one failing log and fix it there.{waiting}"
    )
    return Action(
        ActionKind.ALERT,
        rule,
        reason,
        severity=Severity.WARNING,
        key=state,
        state=state,
        since=since,
        params=outage.to_params(),
    )


def _pulls(snapshot: Snapshot, builds: Sequence[Build]) -> dict[tuple[str, int], PullRequest]:
    """The PRs of the failing builds, by ``(repo, number)``: the build's own PR number, else
    an open PR of the same repository whose head is the build's branch or commit. When the
    code host listed its PRs in full, only open ones."""
    found: dict[tuple[str, int], PullRequest] = {}
    by_number = {(p.repo.lower(), p.number): p for p in snapshot.pull_requests}
    for b in builds:
        repo = _repo(b)
        if b.pr is not None:
            pull = by_number.get((repo, b.pr))
            if pull is not None or not snapshot.pull_requests_complete:
                found[(repo, b.pr)] = pull or PullRequest(repo, b.pr)
            continue
        for p in snapshot.pull_requests:
            if (
                p.repo.lower() == repo
                and not p.fork
                and ((b.branch and p.branch == b.branch) or (b.sha and p.head_sha == b.sha))
            ):
                found[(repo, p.number)] = p
    return found


def _tasks(
    snapshot: Snapshot, pulls: dict[tuple[str, int], PullRequest], began: datetime
) -> list[str]:
    """The tasks the outage explains (see "Scope" in the module docs)."""
    own: dict[str, set[str]] = {}  # task -> repositories of its PRs in scope
    for (repo, _), pull in pulls.items():
        for task_id in pull.task_ids:
            own.setdefault(task_id, set()).add(repo)
    return sorted(
        task.id
        for task in snapshot.tasks
        if task.status in _WAITING
        and _explained(task, snapshot, pulls, own.get(task.id, set()), began)
    )


def _explained(
    task: Task,
    snapshot: Snapshot,
    pulls: dict[tuple[str, int], PullRequest],
    own_repos: set[str],
    began: datetime,
) -> bool:
    """See "Scope" in the module docs; ``own_repos``: the repositories of the task's own PRs
    in scope (empty: none of them is the task's)."""
    conditions, missing = linked_conditions(task, snapshot)
    unmet = [c for c in conditions if c.satisfied is not True]
    if any(c.kind in _PR_CONDITIONS and _pr_of(c.params) in pulls for c in unmet):
        return True
    if not own_repos:
        return False
    if any(
        c.kind in _PR_CONDITIONS
        and _pr_of(c.params) is None
        and _repo_of(c.params) in (None, *own_repos)
        for c in unmet
    ):
        return True  # its own PR's CI or merge, named another way
    return (
        not conditions
        and missing is None
        and task.status_since >= began
        and not any(
            (other := snapshot.task(d.depends_on)) is not None and not other.status.is_terminal
            for d in snapshot.dependencies
            if d.task_id == task.id
        )
    )


def _repo_of(params: dict[str, object]) -> str | None:
    repo = params.get("repo")
    return repo.lower() if isinstance(repo, str) else None


def _pr_of(params: dict[str, object]) -> tuple[str, int] | None:
    repo, number = params.get("repo"), params.get("pr")
    if isinstance(number, str) and number.isdigit():
        number = int(number)
    if not isinstance(repo, str) or isinstance(number, bool) or not isinstance(number, int):
        return None
    return repo.lower(), number


def _repo(build: Build) -> str:
    return (build.repo or "").lower()


def _change(build: Build) -> tuple[str, str]:
    return (_repo(build), build.branch or build.sha or build.id)


def _newest(build: Build) -> float:
    at = build.finished_at or build.started_at
    return -at.timestamp() if at is not None else 0.0


def _listed(items: Sequence[str]) -> str:
    more = f" (+{len(items) - MAX_LISTED} more)" if len(items) > MAX_LISTED else ""
    return ", ".join(items[:MAX_LISTED]) + more
