"""A toolchain change as the likely cause of an outage (incident 20, #284).

On the live farm an automatic Xcode update broke every iOS build for ~12 h. Kelpie saw the
outage (failing runs, a block storm) but not why. With toolchain versions per host in the
snapshot (``Snapshot.toolchain_changes``: config data read from node_exporter's textfile
collector, e.g. ``scripts/kelpie-toolchain.sh`` writing the active Xcode and CoreSimulator),
an outage that begins within ``window`` after a change names that change as the likely
cause: in its alert's reason and in its params (``toolchain``, one line the report shows).

Every outage kind uses it: rule 9's failing CI jobs and jobs that never start, rule 4's
block storm and rule 10's correlated tool failures. A change on the outage's own host is
preferred (a tool outage knows its host); otherwise any host's, since the farm's CI may run
on its own Macs. A change spans from the old version's last sighting to the new one's first
(a tool that reported nothing while it was replaced leaves a gap); one whose span meets
``window`` before the outage began up to :data:`SLACK` after it (a change is first seen at
the source's next sample) counts, and the latest wins. It is a
hint, never an action: the alert says "likely", and nothing is done about the change.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from datetime import datetime, timedelta

from kelpie.core.actions import Action
from kelpie.core.model import Snapshot, ToolchainChange

DEFAULT_WINDOW = timedelta(hours=6)
# A change is seen when the textfile script next runs and Prometheus next scrapes it.
SLACK = timedelta(minutes=15)
PARAM = "toolchain"
# Longer than this between the old version's last sighting and the new one's first: the
# line names the span (the tool could not report a version while it was being replaced).
GAP = timedelta(minutes=15)
MAX_TEXT = 80


def likely_change(
    snapshot: Snapshot,
    since: datetime,
    window: timedelta,
    hosts: Collection[str] | None = None,
) -> ToolchainChange | None:
    """The change most likely behind an outage that began at ``since``, or None."""
    if window <= timedelta(0):
        return None
    # The change happened between the old version's last sighting and the new one's first:
    # that span must meet [since - window, since + SLACK].
    found = [
        c
        for c in snapshot.toolchain_changes
        if since - window <= c.at and _from(c) <= since + SLACK
    ]
    if hosts:
        found = [c for c in found if c.host in hosts] or found
    return max(found, key=lambda c: (c.at, c.host, c.tool), default=None)


def _from(change: ToolchainChange) -> datetime:
    return min(change.last_before or change.at, change.at)


def describe(change: ToolchainChange, since: datetime) -> str:
    """One line: what changed, where and when, relative to the outage's start."""
    was = f" from {_short(change.before)}" if change.before else ""
    begun = _from(change)
    if change.at - begun > GAP:  # nothing reported for a while: the change is in between
        when = f"between {begun:%Y-%m-%d %H:%M} and {change.at:%Y-%m-%d %H:%M} UTC"
        ref = begun
    else:
        when = f"at {change.at:%Y-%m-%d %H:%M} UTC"
        ref = change.at
    gap = since - ref
    minutes = int(abs(gap).total_seconds() // 60)
    span = f"{minutes} min" if minutes < 120 else f"{minutes / 60:.1f} h"
    rel = f"{span} before it began" if gap >= timedelta(0) else f"{span} after it began"
    return (
        f"{_short(change.tool)} changed{was} to {_short(change.after)} on "
        f"{_short(change.host)} {when}, {rel}"
    )


def _short(text: str) -> str:
    """A version or name as one bounded line (a script may print anything)."""
    one = " ".join(text.split())
    return one if len(one) <= MAX_TEXT else one[: MAX_TEXT - 1] + "…"


def with_change(
    action: Action,
    snapshot: Snapshot,
    since: datetime,
    window: timedelta,
    hosts: Collection[str] | None = None,
) -> Action:
    """``action`` (an outage alert) naming the likely toolchain change, if there is one."""
    change = likely_change(snapshot, since, window, hosts)
    if change is None:
        return action
    line = describe(change, since)
    return replace(
        action,
        reason=f"{action.reason} Likely cause: {line}.",
        params={**action.params, PARAM: line},
    )
