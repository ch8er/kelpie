"""Rule 9, part 4: a CI outage is one incident (incident 20).

The farm's toolchain broke for about 12 hours; every mobile PR stayed red, Kelpie raised one
alert per blocked task and the report blamed "waiting for merges". When many change
requests fail the same way at the same time, the cause is shared: the CI (a toolchain, a
runner, a service) or the base the changes build on, not each change.

**Signature.** A failed job of a finished build (``Build.jobs``, ``Job.failed``) has the
signature *workflow, job, first failing step*. Builds whose jobs the connector did not read
take no part. No log scraping.

**Active failures.** For each signature, the failures newer than the last build that passed
that workflow's job *in one of the failing repositories* (a pass elsewhere, on other runners,
fixes nothing here). Each change request (repository and branch) counts once, at its first
active failure. Times are the builds' finish times.

**Open.** At least ``min_prs`` change requests started failing within ``window`` of each
other, and there is evidence that the environment broke rather than each change:

- the failures span two or more repositories (different code, same failure), or
- a commit that passed the job before fails it now (a re-run: the code did not change).

Without such evidence it takes twice ``min_prs`` change requests: three unrelated PRs red at
a generic step ("Run tests") within two hours happen on a busy farm; six with nothing
passing in between rarely do. ``since`` is the first failure of the earliest such group.

**Resolve.** One long-lived alert state (:func:`kelpie.core.incidents.outage_state_id`); the
rule stops reporting it when a build passes the job again in one of its repositories, when
its failures leave the CI connector's window, or when too few failing builds stay readable
(re-runs in progress, the connector's job-list budget): then it resolves after the engine's
``resolve_after`` and may open again as a new announcement.

**Scope** (the alert's params, :class:`kelpie.core.incidents.CiOutage`): the repositories,
workflows, runners (names and labels) and PRs of the active failures, and the tasks the
outage explains:

- a PR's own task when it is blocked or waiting on its CI or merge: an unmet CI or merge
  condition, or no condition at all and waiting since the outage began (``since`` minus
  ``window``); a task waiting on something else (a person, another task) is not explained;
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
        if (since := _since(active, passes, sig, min_prs, window)) is not None:
            actions.append(_alert(snapshot, rule, sig, active, since, window))
    return actions


def passed_again(state_id: str, snapshot: Snapshot) -> str | None:
    """For the "Resolved" notice: the build that passed the outage's job again after its
    last failure, in one of its repositories; None when there is none (the outage stopped
    being reported for another reason: the engine's general notice says so)."""
    found = next(
        (fs for sig, fs in _failures(snapshot).items() if outage_state_id(*sig) == state_id), []
    )
    if not found:
        return None
    workflow, job = found[0].build.workflow, found[0].job.name
    last, repos = max(f.at for f in found), {_repo(f.build) for f in found}
    after = [
        (at, b.id, b)
        for repo in repos
        for at, b in _passes(snapshot).get((repo, workflow, job), ())
        if at > last
    ]
    if not after:
        return None
    at, _, build = max(after, key=lambda p: (p[0], p[1]))
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
    """The failures newer than the job's last pass in any of the failing repositories."""
    workflow, job, _ = sig
    repos = {_repo(f.build) for f in found}
    last = max(
        (at for repo in repos for at, _ in passes.get((repo, workflow, job), ())), default=None
    )
    return [f for f in found if last is None or f.at > last]


def _since(
    active: list[_Failure], passes: _Passes, sig: Signature, min_prs: int, window: timedelta
) -> datetime | None:
    """The start of the earliest group of enough change requests that started failing
    within ``window`` of each other, or None (see "Open" in the module docs)."""
    first: dict[tuple[str, str], datetime] = {}
    for f in active:
        change = _change(f.build)
        first[change] = min(f.at, first.get(change, f.at))
    needed = min_prs if _evidence(active, passes, sig) else min_prs * UNSURE_FACTOR
    times = sorted(first.values())
    for i in range(len(times) - needed + 1):
        if times[i + needed - 1] - times[i] <= window:
            return times[i]
    return None


def _evidence(active: list[_Failure], passes: _Passes, sig: Signature) -> bool:
    """The environment broke, not the changes: two or more repositories fail the same way,
    or a commit that passed the job before fails it now."""
    if len({_repo(f.build) for f in active}) >= 2:
        return True
    workflow, job, _ = sig
    return any(
        f.build.sha and b.sha == f.build.sha and at < f.at
        for f in active
        for at, b in passes.get((_repo(f.build), workflow, job), ())
    )


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
    own = {t for p in pulls.values() for t in p.task_ids}
    return sorted(
        task.id
        for task in snapshot.tasks
        if task.status in _WAITING and _explained(task, snapshot, pulls, task.id in own, began)
    )


def _explained(
    task: Task,
    snapshot: Snapshot,
    pulls: dict[tuple[str, int], PullRequest],
    own: bool,
    began: datetime,
) -> bool:
    conditions, missing = linked_conditions(task, snapshot)
    unmet = [c for c in conditions if c.satisfied is not True]
    if any(c.kind in _PR_CONDITIONS and _pr_of(c.params) in pulls for c in unmet):
        return True
    if not own:
        return False
    if any(c.kind in _PR_CONDITIONS for c in unmet):
        return True  # its own PR's CI or merge, named another way
    return not conditions and missing is None and task.status_since >= began


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
