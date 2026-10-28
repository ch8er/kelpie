"""Rule 4, block storms: many tasks blocked on the same reason at once are one outage
(issue #229, incident 20).

On the live farm the Xcode installation broke; seven agents blocked their tasks within the
hour on "waiting for CEO (Xcode first launch)". From Kelpie's side that looked like seven
independent waits on a person. It is one cause, and one alert should say so.

**Reasons.** A block's reason is normalised: case and spacing folded, URLs, PR and issue
references, task ids, hex hashes and numbers (so times and dates) stripped. Two blocks share a
reason when their normalised texts are equal and they name the same PRs (a reason that
names its own PR, "waiting for CI on #12", is not shared with "waiting for CI on #13").
Only blocks without a condition Kelpie can check count (a checkable wait is rule 2's and
rule 9's), and never Kelpie's own stops.

**Open.** At least ``min_tasks`` tasks blocked on the same reason within ``window`` of each
other (by when their blocks began) → one ``warning`` outage of kind
:data:`kelpie.core.incidents.BLOCK_STORM`: since the first of them, scoped to every task
still blocked on that reason since then, with the reason as its cause and, when the reason
names a person on the board (a human owner's id or role as a word), that person as the one
who can end it. It stays open while that many are still blocked on it, and resolves when
enough are unblocked (or reblocked on something else).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.conditions import linked_conditions
from kelpie.core.incidents import BLOCK_STORM, Outage, incident_state_id
from kelpie.core.model import KelpieMark, OwnerKind, Snapshot, Status, Task
from kelpie.rules._alerts import alert_params, count

MAX_REASON = 300  # characters of a block reason shown and normalised
MAX_LISTED = 10  # tasks named in the alert text

_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_PR_REF = re.compile(r"(?:[\w.-]+/[\w.-]+)?#\d+|/pull/\d+")
# An id or hash holds a digit: "t_28268216", "T-201", "a1b2c3d" (not "re-add", "deadbeef").
_TASK_ID = re.compile(r"\b[a-z]{1,4}[_-](?=[0-9a-f]*\d)[0-9a-f]{3,}\b|\b[a-z]{1,4}-\d+\b", re.I)
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{7,}\b", re.IGNORECASE)
_NUMBER = re.compile(r"\d+")
_NOISE = re.compile(r"[^\w()]+")


@dataclass(frozen=True, slots=True)
class _Blocked:
    task: Task
    since: datetime


def normalise(reason: str) -> str:
    """A block reason with what differs per task stripped: ids, references, times, numbers."""
    text = reason[:MAX_REASON].lower()
    for pattern in (_URL, _PR_REF, _TASK_ID, _HEX, _NUMBER):  # times and dates: numbers
        text = pattern.sub(" ", text)
    return " ".join(_NOISE.sub(" ", text).split())


def _refs(reason: str) -> tuple[str, ...]:
    """The PRs and URLs a reason names: blocks naming different ones wait on different
    things, whatever the rest of their text says."""
    text = reason[:MAX_REASON].lower()
    return tuple(sorted({*_URL.findall(text), *_PR_REF.findall(text)}))


def storms(
    snapshot: Snapshot, rule: str, min_tasks: int, window: timedelta, observe: bool = True
) -> list[Action]:
    """One alert per block storm (see the module docs); none with ``min_tasks`` = 0. With
    ``observe`` (the default for a new check) the alerts are only logged, whatever the
    rule's mode."""
    if min_tasks <= 0:
        return []
    groups: dict[tuple[str, tuple[str, ...]], list[_Blocked]] = {}
    for task in snapshot.tasks:
        block = task.block
        if task.status is not Status.BLOCKED or block is None or block.kelpie is KelpieMark.STOP:
            continue
        conditions, missing = linked_conditions(task, snapshot)
        if conditions or missing is not None:
            continue  # a wait Kelpie can check, or one it is told about but cannot see
        if text := normalise(block.reason):
            groups.setdefault((text, _refs(block.reason)), []).append(_Blocked(task, block.since))
    actions = []
    for (text, refs), blocked in sorted(groups.items()):
        if (since := _since(blocked, min_tasks, window)) is not None:
            scope = sorted((b for b in blocked if b.since >= since), key=lambda b: b.task.id)
            alert = _alert(snapshot, rule, text, refs, scope, since)
            actions.append(replace(alert, observe_only=observe))
    return actions


def _since(blocked: list[_Blocked], min_tasks: int, window: timedelta) -> datetime | None:
    """When the first group of ``min_tasks`` blocks within ``window`` of each other began."""
    times = sorted(b.since for b in blocked)
    for i in range(len(times) - min_tasks + 1):
        if times[i + min_tasks - 1] - times[i] <= window:
            return times[i]
    return None


def _alert(
    snapshot: Snapshot,
    rule: str,
    text: str,
    refs: tuple[str, ...],
    scope: list[_Blocked],
    since: datetime,
) -> Action:
    first = min(scope, key=lambda b: (b.since, b.task.id))
    shown = " ".join(first.task.block.reason[:MAX_REASON].split()) if first.task.block else text
    span = max(b.since for b in scope) - since
    person = _person(snapshot, text)
    tasks = [b.task.id for b in scope]
    state = incident_state_id(rule, text, *refs)
    cause = f'{len(tasks)} tasks blocked within {_minutes(span)} on "{shown}"'
    outage = Outage(
        state=state,
        signature=shown,
        kind=BLOCK_STORM,
        cause=cause,
        person=person,
        tasks=tuple(tasks),
    )
    who = (
        f" Only {person} can end it: one answer unblocks all of them."
        if person
        else " One fix unblocks all of them."
    )
    listed = ", ".join(tasks[:MAX_LISTED]) + (
        f" (+{len(tasks) - MAX_LISTED} more)" if len(tasks) > MAX_LISTED else ""
    )
    reason = (
        f"Block storm since {since:%Y-%m-%d %H:%M} UTC: {cause}. One cause, not "
        f"{len(tasks)} separate waits: {listed}.{who}"
    )
    return Action(
        ActionKind.ALERT,
        rule,
        reason,
        severity=Severity.WARNING,
        key=state,
        state=state,
        since=since,
        params={**outage.to_params(), **alert_params(_headline(len(tasks), person))},
    )


def _headline(n: int, person: str | None) -> str:
    """The alert's own line; the block's words stay in its "Why" (as for every rule 4 alert)."""
    asks = f", waiting for {person}" if person else ""
    return f"Block storm: {count(n, 'task')} blocked on the same reason{asks}"


def _person(snapshot: Snapshot, text: str) -> str | None:
    """A human owner the normalised reason names by id or role, as a whole word."""
    words = set(text.replace("(", " ").replace(")", " ").split())
    for owner in sorted(snapshot.owners, key=lambda o: o.id):
        if owner.kind is OwnerKind.HUMAN and ({owner.id.lower(), owner.role.lower()} & words):
            return owner.id
    return None


def _minutes(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes / 60:.1f} h"
