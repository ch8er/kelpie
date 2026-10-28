"""Rule 9, CI jobs that never start: one outage (issue #229, incident 20).

On the live farm GitHub Actions at times started no jobs at all (a spending limit, a failed
payment), and a run whose jobs never start does not fail: it is refused, or sits queued.
It never counted toward an outage, and every PR waiting on CI looked like its own wait.

**Refused.** A finished build with a job the CI refused (``Job.refused``: no runner took
it, no step ran; on the live repository a required check was refused while the other jobs
had run), where that job used to run: an earlier build of the same repository, workflow
and trigger started a job of that name. A job that never runs for that trigger (a deploy
job an environment's protection rules refuse on PRs) is no sign. A refusal counts until a
later build of that repository and workflow starts a job of that name (CI runs it again),
however long that takes; builds are dated by when they started, never when they finished.

**Queued.** A build still queued for at least ``queued_after``, unless the CI holds it on
purpose (``Build.held``: an approval, a concurrency group) or runners take work in its
repository and workflow: a build there is running, or one started after it was queued and
ran all its jobs (a busy runner is a queue, not an outage). Builds whose jobs the
connector did not read take part only by being queued.

**Open.** Signs of at least ``min_prs`` change requests (repository and branch) within
``window`` of each other → one ``warning`` outage of kind
:data:`kelpie.core.incidents.CI_NOT_STARTED` for the whole farm (one state,
:data:`~kelpie.core.incidents.CI_NOT_STARTED_STATE`): since the first of them, scoped to
the signs since then, their repositories, PRs and the tasks waiting on those PRs' CI or
merge (as rule 9's failing-job outages), with a cause that says which: refused (check the
CI's billing and spending limits, or a protection rule) or queued (check that runners are
up). It resolves when fewer are left; the notice names the first build that ran a refused
job again (:func:`ran_again`).
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
_Started = dict[_Key, list[tuple[datetime, Build]]]  # builds that started such a job


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


def ran_again(snapshot: Snapshot) -> str | None:
    """For the "Resolved" notice: the first build that started a refused job again after
    its repository's last refusal of it. None when there is none (the outage ended another
    way: the engine's general notice says so)."""
    started = _started(snapshot)
    last: dict[_Key, datetime] = {}
    for b in snapshot.builds:
        for job in b.jobs or ():
            key = (_repo(b), b.workflow, job.name)
            if job.refused and b.started_at is not None and _used_to_run(started, key, b):
                last[key] = max(b.started_at, last.get(key, b.started_at))
    again = [
        (at, b.id, b, key[2])
        for key, refused in last.items()
        for at, b in started.get(key, ())
        if at > refused
    ]
    if not again:
        return None
    at, _, b, job = min(again, key=lambda found: (found[0], found[1]))
    return (
        f"CI started jobs again: {b.id} in {_repo(b)} ran {job} (started {at:%Y-%m-%d %H:%M} UTC)."
    )


def _started(snapshot: Snapshot) -> _Started:
    found: _Started = {}
    for b in snapshot.builds:
        if b.started_at is None:
            continue
        for job in b.jobs or ():
            if job.started:
                found.setdefault((_repo(b), b.workflow, job.name), []).append((b.started_at, b))
    return found


def _used_to_run(started: _Started, key: _Key, build: Build) -> bool:
    """Whether an earlier build of the same trigger started this job (it can run here)."""
    assert build.started_at is not None
    return any(
        at < build.started_at and (b.event == build.event or None in (b.event, build.event))
        for at, b in started.get(key, ())
    )


def _signs(snapshot: Snapshot, queued_after: timedelta) -> list[_Sign]:
    started = _started(snapshot)
    signs = []
    for b in snapshot.builds:
        if b.started_at is None:
            continue
        if b.status is BuildStatus.QUEUED:
            old = snapshot.now - b.started_at >= queued_after
            if old and not b.held and not _moving(snapshot, b):
                signs.append(_Sign(b.started_at, b, queued=True))
            continue
        for job in b.jobs or ():
            key = (_repo(b), b.workflow, job.name)
            again = any(at > b.started_at for at, _ in started.get(key, ()))
            if job.refused and not again and _used_to_run(started, key, b):
                signs.append(_Sign(b.started_at, b, queued=False))
                break
    return sorted(signs, key=lambda s: (s.at, s.build.id))


def _moving(snapshot: Snapshot, queued: Build) -> bool:
    """Whether runners take work in the queued build's repository and workflow."""
    assert queued.started_at is not None
    for b in snapshot.builds:
        if _repo(b) != _repo(queued) or b.workflow != queued.workflow or b.id == queued.id:
            continue
        if b.status is BuildStatus.RUNNING:
            return True
        later = b.started_at is not None and b.started_at > queued.started_at
        if later and b.jobs and all(j.started for j in b.jobs):
            return True
    return False


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
