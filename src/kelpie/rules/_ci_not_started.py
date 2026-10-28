"""Rule 9, CI jobs that never start: one outage (issue #229, incident 20).

On the live farm GitHub Actions at times started no jobs at all (a spending limit, a failed
payment), and a run whose jobs never start does not fail: it is refused, or sits queued.
It never counted toward an outage, and every PR waiting on CI looked like its own wait.

**Not started.** A finished build with a job the CI refused (``Job.refused``: no runner
took it, no step ran; on the live repository a required check was refused while the other
jobs had run), or a build still queued for at least ``queued_after``. A refused build stops
counting once a later build in its repository started all its jobs (CI runs again there),
and once it is older than ``window``. Builds whose jobs the connector did not read
take part only by being queued.

**Open.** At least ``min_prs`` change requests (repository and branch) with such a build →
one ``warning`` outage of kind :data:`kelpie.core.incidents.CI_NOT_STARTED` for the whole
farm (one state, :data:`STATE`): since the first of them, scoped to their repositories,
PRs and the tasks waiting on those PRs' CI or merge (as rule 9's failing-job outages), with
a cause that says which: refused (check the CI's billing and spending limits) or queued
(check that runners are up). It resolves when fewer are left; the notice names the newest
build whose jobs all started (:func:`ran_again`).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.incidents import CI_NOT_STARTED, MAX_BUILDS, Outage, incident_state_id
from kelpie.core.model import Build, BuildStatus, Snapshot
from kelpie.rules._alerts import alert_params, count
from kelpie.rules._ci_outage import _change, _listed, _newest, _pulls, _repo, _tasks

STATE = incident_state_id("ci_economy", "not_started")


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
    now = snapshot.now
    ran: dict[str, datetime] = {}  # repository -> last build whose jobs all started
    for at, b in _ran(snapshot):
        ran[_repo(b)] = max(at, ran.get(_repo(b), at))
    refused: list[tuple[datetime, Build]] = []
    queued: list[tuple[datetime, Build]] = []
    for b in snapshot.builds:
        if b.status is BuildStatus.QUEUED and b.started_at is not None:
            if now - b.started_at >= queued_after:
                queued.append((b.started_at, b))
            continue
        at = b.finished_at or b.started_at
        if at is None or now - at > window:
            continue
        last_ran = ran.get(_repo(b))
        if any(j.refused for j in b.jobs or ()) and (last_ran is None or at > last_ran):
            refused.append((at, b))
    found = refused + queued
    changes = {_change(b) for _, b in found}
    if len(changes) < min_prs:
        return []
    return [replace(_alert(snapshot, rule, refused, queued, window), observe_only=observe)]


def ran_again(snapshot: Snapshot) -> str | None:
    """For the "Resolved" notice: the newest build whose jobs all started. None when there
    is none (the outage ended another way: the engine's general notice says so)."""
    newest = max(_ran(snapshot), key=lambda found: (found[0], found[1].id), default=None)
    if newest is None:
        return None
    at, b = newest
    return f"CI started jobs again: {b.id} in {_repo(b)} ran at {at:%Y-%m-%d %H:%M} UTC."


def _ran(snapshot: Snapshot) -> list[tuple[datetime, Build]]:
    """Builds whose jobs all started, with when they finished (or started)."""
    found = []
    for b in snapshot.builds:
        at, jobs = b.finished_at or b.started_at, b.jobs or ()
        if at is not None and jobs and all(j.started for j in jobs):
            found.append((at, b))
    return found


def _alert(
    snapshot: Snapshot,
    rule: str,
    refused: list[tuple[datetime, Build]],
    queued: list[tuple[datetime, Build]],
    window: timedelta,
) -> Action:
    found = refused + queued
    since = min(at for at, _ in found)
    builds = sorted({b.id: b for _, b in found}.values(), key=lambda b: b.id)
    changes = {_change(b) for b in builds}
    pulls = _pulls(snapshot, builds)
    prs = sorted(f"{repo}#{number}" for repo, number in pulls)
    tasks = _tasks(snapshot, pulls, since - window)
    repos = sorted({_repo(b) for b in builds if b.repo})
    parts = []
    if refused:
        parts.append(
            f"{len(refused)} run(s) not started (no runner took them: check the CI's billing "
            "and spending limits)"
        )
    if queued:
        parts.append(f"{len(queued)} run(s) still queued (check that runners are up)")
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
