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
on its own Macs. The latest change at most ``window`` before the outage began (and at most
:data:`SLACK` after: a change is first seen at the source's next sample) wins. It is a
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


def likely_change(
    snapshot: Snapshot,
    since: datetime,
    window: timedelta,
    hosts: Collection[str] | None = None,
) -> ToolchainChange | None:
    """The change most likely behind an outage that began at ``since``, or None."""
    if window <= timedelta(0):
        return None
    found = [c for c in snapshot.toolchain_changes if since - window <= c.at <= since + SLACK]
    if hosts:
        found = [c for c in found if c.host in hosts] or found
    return max(found, key=lambda c: (c.at, c.host, c.tool), default=None)


def describe(change: ToolchainChange, since: datetime) -> str:
    """One line: what changed, where and when, relative to the outage's start."""
    was = f" from {change.before}" if change.before else ""
    gap = since - change.at
    minutes = int(abs(gap).total_seconds() // 60)
    span = f"{minutes} min" if minutes < 120 else f"{minutes / 60:.1f} h"
    rel = f"{span} before it began" if gap >= timedelta(0) else f"{span} after it began"
    return (
        f"{change.tool} changed{was} to {change.after} on {change.host} at "
        f"{change.at:%Y-%m-%d %H:%M} UTC, {rel}"
    )


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
