"""Rule 4, block storms: many tasks blocked on the same reason at once are one outage
(issue #229, incident 20).

On the live farm the Xcode installation broke; seven agents blocked their tasks within the
hour on "waiting for CEO (Xcode first launch)". From Kelpie's side that looked like seven
independent waits on a person. It is one cause, and one alert should say so.

**Reasons.** A block's reason is normalised: case and spacing folded, the task's own id,
dates, times, durations, fractions ("3/3") and hex hashes stripped. What a reason names
stays apart: PRs, issues, URLs and other tasks' ids are its references, and two blocks share
a reason only when their normalised texts are equal and they name the same references
("waiting for CI on #12" is not "waiting for CI on #13", "waiting for T-100" not "waiting
for T-101"); other numbers and versions stay in the text ("Xcode 15" is not "Xcode 16",
"host-1" not "host-2"). A reason must say something beyond a kind of wait: one with fewer
than two words that are not generic ("waiting for review", "blocked: dependency", "label
blocked", an approval kind alone) groups nothing. Only blocks without a condition Kelpie
can check count (a checkable wait is rule 2's and rule 9's), never Kelpie's own stops, and
not tasks whose snooze holds (a person already decided to wait). Matching is exact after
normalising: paraphrases of one ask ("waiting on CEO", "waiting for CEO") stay apart.

**Open.** At least ``min_tasks`` tasks blocked on the same reason within ``window`` of each
other (by when their blocks began) → one ``warning`` outage of kind
:data:`kelpie.core.incidents.BLOCK_STORM`: since the first of them, scoped to every task
still blocked on that reason since then, with the reason as its cause and, when the reason
names a person on the board, that person as the one who can end it: a human owner's id as
a word, or a role one person alone holds (no agent either), as whom the reason waits for
("waiting for CEO", "ask the lead", "CEO:"). On a multi-host hub ids are matched without
their host and board prefixes (``Task.local_id``), as the agents wrote them.

**Resolves.** Once open (its state id in ``Snapshot.open_states``) it stays open while
:func:`keep_open` tasks (half of ``min_tasks``, at least two) are still blocked on the
reason, whenever they blocked: a storm half answered is still one cause, and a storm at the
threshold does not flap. It resolves when fewer are left.

**Rule 13.** Its "needs you" inbox lists the person's blocks one by one; a storm does not
fold them (they are not per-task alerts), so with the storm switched on the person also
gets the storm. The per-task alerts this rule folds are its own no-condition and overdue
ones, and only once the storm is switched on (``storm_mode = "rule"``).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.conditions import linked_conditions
from kelpie.core.incidents import BLOCK_STORM, Outage, incident_state_id
from kelpie.core.model import KelpieMark, OwnerKind, Snapshot, Status, Task
from kelpie.rules._alerts import alert_params, count
from kelpie.rules._snoozes import SnoozeCheck

MAX_REASON = 300  # characters of a block reason shown and normalised
MAX_LISTED = 10  # tasks named in the alert text
MIN_WORDS = 2  # words beyond a kind of wait a shared reason must have

_URL = re.compile(r"https?://\S+", re.IGNORECASE)
_PR_REF = re.compile(r"(?:[\w.-]+/[\w.-]+)?#\d+|/pull/\d+")
# A task id holds a digit: "t_28268216", "T-201" (not "re-add"); hosts and models
# ("host-1", "gpt-4") look the same and stay apart as references too.
_TASK_ID = re.compile(r"\b[a-z]{1,4}[_-](?=[0-9a-f]*\d)[0-9a-f]{3,}\b|\b[a-z]{1,4}-\d+\b", re.I)
_HEX = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,}\b", re.IGNORECASE)
_DATE = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:[t ]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?)?\b"
    r"|\b\d{1,2}:\d{2}(?::\d{2})?\b",
    re.IGNORECASE,
)
_DURATION = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?)\b", re.I
)
_FRACTION = re.compile(r"\b\d+/\d+\b")
_NOISE = re.compile(r"[^\w()./-]+|(?<!\w)[./-]+|[./-]+(?!\w)")
# Words that say only what kind of wait it is, not on what.
_GENERIC_WORDS = """
    a an the to for on of in at by from with and or is are be it this that its
    waiting wait waits waited waiting_human needs need needs_input input blocked block blocks
    blocker label labeled review reviews reviewer ci approve approval approved dependency
    dependencies depends depend merge merged task tasks pr issue human answer reply response
    waiting_ci waiting_pr_merge conflict conflicts rebase
    since until still please guard circuit breaker parked park kind reason none unknown
    question decision confirm confirmation help
"""
_GENERIC = frozenset(_GENERIC_WORDS.split())


@dataclass(frozen=True, slots=True)
class _Blocked:
    task: Task
    since: datetime


def normalise(reason: str, *own: str | None) -> str:
    """A block reason with what differs per task stripped: the task's own id, dates, times,
    durations, hashes (see the module docs)."""
    text = _own_stripped(reason, *own).lower()
    for pattern in (_URL, _PR_REF, _TASK_ID, _DATE, _DURATION, _FRACTION, _HEX):
        text = pattern.sub(" ", text)
    return " ".join(_NOISE.sub(" ", text).split())


def informative(text: str) -> bool:
    """Whether a normalised reason says more than a kind of wait."""
    words = text.replace("(", " ").replace(")", " ").split()
    return sum(1 for w in words if w not in _GENERIC and not w.isdigit()) >= MIN_WORDS


def _own_stripped(reason: str, *own: str | None) -> str:
    """The reason without the task's own ids: as the board names it, and as the agent's
    platform wrote it, without a multi-host hub's or a board's prefix (``Task.local_id``)."""
    text = reason[:MAX_REASON]
    for item in {o for o in own if o}:
        text = re.sub(rf"(?<![\w.-]){re.escape(item)}(?![\w-])", " ", text, flags=re.I)
    return text


def _local(item_id: str | None, host: str | None) -> str | None:
    """An id without its multi-host namespace (``<host>:``)."""
    if item_id is None or host is None:
        return item_id
    return item_id.removeprefix(f"{host}:")


def _refs(reason: str, *own: str | None) -> tuple[str, ...]:
    """What a reason names: PRs, URLs, other tasks' ids. Blocks naming different ones wait
    on different things, whatever the rest of their text says."""
    text = _own_stripped(reason, *own).lower()
    found = {*_URL.findall(text), *_PR_REF.findall(_URL.sub(" ", text))}
    found.update(_TASK_ID.findall(_PR_REF.sub(" ", _URL.sub(" ", text))))
    return tuple(sorted(found))


def keep_open(min_tasks: int) -> int:
    """How many tasks still blocked on its reason keep an open storm open."""
    return max(2, min_tasks // 2)


def storms(
    snapshot: Snapshot,
    rule: str,
    min_tasks: int,
    window: timedelta,
    observe: bool = True,
    snoozed: Callable[[str], SnoozeCheck | None] | None = None,
) -> list[Action]:
    """One alert per block storm (see the module docs); none with ``min_tasks`` = 0. With
    ``observe`` (the default for a new check) the alerts are only logged, whatever the
    rule's mode. ``snoozed`` tells a task's snooze (none: no task is snoozed)."""
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
        if snoozed is not None and (snooze := snoozed(task.id)) is not None and snooze.holds:
            continue  # a person decided to wait
        text = normalise(block.reason, task.id, task.local_id)
        if informative(text):
            refs = _refs(block.reason, task.id, task.local_id)
            # what a reason names ("t_5", "#12") is its own machine's and board's
            where = f"{task.host or ''}:{task.board or ''}"
            key = (text, (where, *refs) if refs and where != ":" else refs)
            groups.setdefault(key, []).append(_Blocked(task, block.since))
    open_states = set(snapshot.open_states)
    actions = []
    for (text, refs), blocked in sorted(groups.items()):
        state = incident_state_id(rule, text, *refs)
        since = _since(blocked, min_tasks, window)
        if since is None and state in open_states and len(blocked) >= keep_open(min_tasks):
            since = min(b.since for b in blocked)  # open: kept while enough still wait
        if since is not None:
            scope = sorted((b for b in blocked if b.since >= since), key=lambda b: b.task.id)
            alert = _alert(snapshot, rule, state, text, scope, since)
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
    state: str,
    text: str,
    scope: list[_Blocked],
    since: datetime,
) -> Action:
    first = min(scope, key=lambda b: (b.since, b.task.id))
    shown = _shown(first.task) or text
    span = max(b.since for b in scope) - since
    person = _person(snapshot, first.task.block.reason if first.task.block else text, first.task)
    tasks = [b.task.id for b in scope]
    cause = f'{len(tasks)} tasks blocked within {_minutes(span)} on "{shown}"'
    outage = Outage(
        state=state,
        signature=shown,
        kind=BLOCK_STORM,
        cause=cause,
        person=person,
        tasks=tuple(tasks),
    )
    # No one named: the cause may still need a fix per task (each one rebasing), so no
    # promise that one fix ends it.
    who = f" Only {person} can end it: one answer unblocks all of them." if person else ""
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


def _shown(task: Task) -> str:
    """The reason as people read it: the earliest task's, without its own id."""
    if task.block is None:
        return ""
    text = _own_stripped(task.block.reason, task.id, task.local_id)
    text = re.sub(r"[\[(]\s*[\])]", " ", text)  # "[t_1]" left as "[]"
    return " ".join(text.split()).strip(" -,;.")


def _headline(n: int, person: str | None) -> str:
    """The alert's own line; the block's words stay in its "Why" (as for every rule 4 alert)."""
    asks = f", waiting for {person}" if person else ""
    return f"Block storm: {count(n, 'task')} blocked on the same reason{asks}"


def _person(snapshot: Snapshot, reason: str, task: Task) -> str | None:
    """The human owner a reason names (see the module docs): by id as a word, else by a
    role one person alone holds (no agent either), as whom it waits for. On a multi-host
    hub a person is named by their id on their own machine, and may be on several boards:
    the one on the task's machine is named."""
    text = " ".join(reason[:MAX_REASON].lower().split())
    humans = sorted(
        (o for o in snapshot.owners if o.kind is OwnerKind.HUMAN),
        key=lambda o: (o.host != task.host, o.id),  # the task's own machine first
    )
    for owner in humans:
        for own in {owner.id, _local(owner.id, owner.host)} - {None, ""}:
            assert own is not None
            if re.search(rf"(?<![\w.-]){re.escape(own.lower())}(?![\w-])", text):
                return owner.id
    for owner in humans:
        role = owner.role.lower().strip()
        holders = {
            (o.kind, _local(o.id, o.host))
            for o in snapshot.owners
            if o.role.lower().strip() == role and o.serves(task)
        }
        if not role or len(holders) > 1:
            continue  # several people, or agents too: whom it means is unclear
        r = re.escape(role)
        asked = (
            rf"\b(?:waiting (?:for|on)|wait (?:for|on)|ask|asks|asking)"
            rf"(?: the)? {r}(?![\w-])|(?:^|[:(]\s*){r}\s*:"
        )
        if re.search(asked, text):
            return owner.id
    return None


def _minutes(span: timedelta) -> str:
    minutes = int(span.total_seconds() // 60)
    return f"{minutes} min" if minutes < 120 else f"{minutes / 60:.1f} h"
