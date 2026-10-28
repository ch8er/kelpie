"""Rule 9, CI jobs that never start: one outage (issue #229, incident 20).

On the live farm GitHub Actions at times started no jobs at all (a spending limit, a failed
payment), and a run whose jobs never start does not fail: it is refused, or sits queued.
It never counted toward an outage, and every PR waiting on CI looked like its own wait.

**Refused.** A finished build with a job the CI refused (``Job.refused``: no runner took
it, no step ran; on the live repository a required check was refused while the other jobs
had run). It is a sign when the CI refused the whole run (every job: protection rules
refuse single jobs), or when that job used to run there: an earlier build of the same
repository and workflow started a job of that name for the same trigger (for a push, the
same branch). A deploy job that an environment's branch rules refuse on PRs or on feature
branches is no sign. A refusal counts until a later build of that repository and workflow
starts a job of that name (CI runs it again), however long that takes; builds are dated by
when they started, never when they finished, and a skipped job never ran.

**Queued.** A build still queued for at least ``queued_after``, unless the CI holds it on
purpose (``Build.held``: an approval, a concurrency group) or runners take work anywhere on
the farm: a build is running, or one started after it was queued and started a job (a busy
runner is a queue, not an outage; a stopped CI or dead runners start nothing). Builds whose
jobs the connector did not read take part only by being queued.

**Open.** Signs of at least ``min_prs`` change requests (repository and branch) within
``window`` of each other → one ``warning`` outage of kind
:data:`kelpie.core.incidents.CI_NOT_STARTED` for the whole farm (one state,
:data:`~kelpie.core.incidents.CI_NOT_STARTED_STATE`): since the first of them, scoped to
the signs since then, their repositories, PRs and the tasks waiting on those PRs' CI or
merge (as rule 9's failing-job outages), with a cause that says which: refused (check the
CI's billing and spending limits, or a protection rule) or queued (check that runners are
up). It resolves when fewer are left; the notice names the first build that ran a job
refused since the outage began again (:func:`ran_again`).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.incidents import CI_NOT_STARTED, CI_NOT_STARTED_STATE, MAX_BUILDS, Outage
from kelpie.core.model import Build, BuildStatus, Snapshot
from kelpie.rules._alerts import alert_params, count
from kelpie.rules._ci_outage import _change, _listed, _newest, _pulls, _repo, _tasks

STATE = CI_NOT_STARTED_STATE

_Key = tuple[str, str | None, str]  # repository, workflow, job name


@dataclass(frozen=True, slots=True)
class _Sign:
    at: datetime  # when the build started (was queued)
    build: Build
    queued: bool


def not_started(
    snapshot: Snapshot,
    rule: str,
    min_prs: int,
    window: timedelta,
    queued_after: timedelta,
    observe: bool = True,
) -> list[Action]:
    """The outage of CI jobs that never start, if open (see the module docs); none with
    ``min_prs`` = 0. With ``observe`` (a new check) its alert is only logged."""
    if min_prs <= 0:
        return []
    signs = _signs(snapshot, queued_after)
    since = _since(signs, min_prs, window)
    if since is None:
        return []
    scope = [s for s in signs if s.at >= since]
    alert = _alert(snapshot, rule, scope, since, window, queued_after)
    return [replace(alert, observe_only=observe)]


def ran_again(snapshot: Snapshot, since: datetime | None = None) -> str | None:
    """For the "Resolved" notice: the first build that started a job again after its last
    refusal there, of those refused since the outage began (``since``). None when there is
    none (a queue that cleared, or the outage ended another way: the engine's general
    notice says so)."""
    started = _Started.of(snapshot)
    last: dict[_Key, datetime] = {}
    for _, at, key in _refusals(snapshot, started):
        if since is None or at >= since:
            last[key] = max(at, last.get(key, at))
    again = [
        (at, b.id, b, key[2])
        for key, refused in last.items()
        for at, b in started.runs.get(key, ())
        if at > refused
    ]
    if not again:
        return None
    at, _, b, job = min(again, key=lambda found: (found[0], found[1]))
    return (
        f"CI started jobs again: {b.id} in {_repo(b)} ran {job} (started {at:%Y-%m-%d %H:%M} UTC)."
    )


@dataclass(frozen=True, slots=True)
class _Started:
    """Builds that started each job (skipped ones never ran), by when they started."""

    runs: dict[_Key, list[tuple[datetime, Build]]]
    last: dict[_Key, datetime]  # the latest start of each job
    # the earliest start of each job by trigger and push branch; ``any`` whatever the trigger
    first: dict[tuple[_Key, str | None, str | None], datetime]
    any: dict[_Key, datetime]

    @classmethod
    def of(cls, snapshot: Snapshot) -> _Started:
        runs: dict[_Key, list[tuple[datetime, Build]]] = {}
        last: dict[_Key, datetime] = {}
        first: dict[tuple[_Key, str | None, str | None], datetime] = {}
        any_: dict[_Key, datetime] = {}
        for b in snapshot.builds:
            if b.started_at is None:
                continue
            for job in b.jobs or ():
                if not job.started or job.conclusion is BuildStatus.SKIPPED:
                    continue
                key = (_repo(b), b.workflow, job.name)
                runs.setdefault(key, []).append((b.started_at, b))
                last[key] = max(b.started_at, last.get(key, b.started_at))
                trigger = (key, b.event, _branch(b))
                first[trigger] = min(b.started_at, first.get(trigger, b.started_at))
                any_[key] = min(b.started_at, any_.get(key, b.started_at))
        return cls(runs, last, first, any_)

    def used_to_run(self, key: _Key, build: Build) -> bool:
        """Whether an earlier build of the same trigger started this job (it can run here);
        where a trigger is unknown (the refused build's or the earlier one's), any counts."""
        assert build.started_at is not None
        if build.event is None:
            starts = [self.any.get(key)]
        else:
            starts = [
                self.first.get((key, build.event, _branch(build))),
                self.first.get((key, None, None)),
            ]
        return any(at is not None and at < build.started_at for at in starts)


def _branch(build: Build) -> str | None:
    """The branch a push's evidence must share (branch rules); other triggers: none."""
    return build.branch if build.event == "push" else None


def _refusals(snapshot: Snapshot, started: _Started) -> list[tuple[Build, datetime, _Key]]:
    """Refused jobs that are signs (see the module docs), whether or not they ran again."""
    found = []
    for b in snapshot.builds:
        jobs = b.jobs or ()
        if b.started_at is None or not any(j.refused for j in jobs):
            continue
        whole = all(j.refused for j in jobs)
        for job in jobs:
            key = (_repo(b), b.workflow, job.name)
            if job.refused and (whole or started.used_to_run(key, b)):
                found.append((b, b.started_at, key))
    return found


def _signs(snapshot: Snapshot, queued_after: timedelta) -> list[_Sign]:
    started = _Started.of(snapshot)
    signs = {
        b.id: _Sign(at, b, queued=False)
        for b, at, key in _refusals(snapshot, started)
        if started.last.get(key, at) <= at  # not run again since
    }
    moving = _moving(snapshot)
    for b in snapshot.builds:
        if b.status is not BuildStatus.QUEUED or b.started_at is None or b.held:
            continue
        old = snapshot.now - b.started_at >= queued_after
        if old and (moving is None or moving <= b.started_at):
            signs[b.id] = _Sign(b.started_at, b, queued=True)
    return sorted(signs.values(), key=lambda s: (s.at, s.build.id))


def _moving(snapshot: Snapshot) -> datetime | None:
    """The latest sign that runners take work anywhere on the farm: now while a build
    runs, else the latest start of a build that started a job. None: no sign."""
    if any(b.status is BuildStatus.RUNNING for b in snapshot.builds):
        return snapshot.now
    starts = [
        b.started_at
        for b in snapshot.builds
        if b.started_at is not None and any(j.started for j in b.jobs or ())
    ]
    return max(starts, default=None)


def _since(signs: list[_Sign], min_prs: int, window: timedelta) -> datetime | None:
    """When the first group of signs of ``min_prs`` changes within ``window`` began."""
    for i, first in enumerate(signs):
        changes = {_change(s.build) for s in signs[i:] if s.at - first.at <= window}
        if len(changes) >= min_prs:
            return first.at
    return None


def _alert(
    snapshot: Snapshot,
    rule: str,
    scope: list[_Sign],
    since: datetime,
    window: timedelta,
    queued_after: timedelta,
) -> Action:
    builds = sorted({s.build.id: s.build for s in scope}.values(), key=lambda b: b.id)
    changes = {_change(b) for b in builds}
    pulls = _pulls(snapshot, builds)
    prs = sorted(f"{repo}#{number}" for repo, number in pulls)
    tasks = _tasks(snapshot, pulls, since - window)
    repos = sorted({_repo(b) for b in builds if b.repo})
    refused = sum(1 for s in scope if not s.queued)
    queued = len(scope) - refused
    parts = []
    if refused:
        parts.append(
            f"{refused} run(s) with jobs the CI refused to start (no runner took them, no "
            "step ran: check the CI's billing and spending limits, or a protection rule)"
        )
    if queued:
        minutes = int(queued_after.total_seconds() // 60)
        parts.append(
            f"{queued} run(s) queued over {minutes} min while nothing else started there "
            "(check that runners are up)"
        )
    cause = "; ".join(parts)
    outage = Outage(
        state=STATE,
        signature="CI jobs not starting",
        kind=CI_NOT_STARTED,
        cause=cause,
        repos=tuple(repos),
        workflows=tuple(sorted({b.workflow for b in builds if b.workflow})),
        prs=tuple(prs),
        tasks=tuple(tasks),
        builds=tuple(b.id for b in sorted(builds, key=_newest)[:MAX_BUILDS]),
    )
    where = f" in {', '.join(repos)}" if repos else ""
    named = f": {_listed(prs)}" if prs else ""
    waiting = f" {len(tasks)} task(s) wait on it: {_listed(tasks)}." if tasks else ""
    reason = (
        f"CI jobs not starting since {since:%Y-%m-%d %H:%M} UTC: {len(changes)} change(s)"
        f"{where}{named}. {cause[0].upper()}{cause[1:]}. Nothing in the changes is wrong: "
        f"CI did not run them.{waiting}"
    )
    headline = f"CI jobs not starting: {count(len(changes), 'change')} waiting"
    return Action(
        ActionKind.ALERT,
        rule,
        reason,
        severity=Severity.WARNING,
        key=STATE,
        state=STATE,
        since=since,
        params={**outage.to_params(), **alert_params(headline)},
    )
