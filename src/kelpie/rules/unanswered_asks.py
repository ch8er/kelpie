"""Unanswered asks (issue #297): a question one agent puts to another is an obligation until
it is answered.

On a farm, agents ask each other things in task comments ("@architect-1 which schema
version should the migration target?") and carry on, or end their run. Nothing reminds the
other agent; the question sits until a person notices. This rule keeps such asks open and
reminds once the recipient is free to answer.

**What is an ask.** A comment on an open task, written by an agent (not a person, not
Kelpie: adapters leave Kelpie's own comments out), that mentions another agent of the farm
as ``@<owner id>`` (case-insensitive) and contains a question mark. The recipient must be
an agent owner in the snapshot (:class:`~kelpie.core.model.OwnerKind.AGENT`) that serves
the task's machine; a mention of anyone else is no ask. On a multi-host farm owner ids are
``<host>:<id>`` and comment authors are not: ``@dev-1`` names the ``dev-1`` of the task's
own machine, ``@host-b:dev-1`` names that host's (and is no ask unless it serves the
task), and answers count only from the same machine. One obligation per task, asker and
recipient: it opens at the first ask after the last answer, and later asks of the same pair
add nothing.

**What counts as an answer** (the most conservative reading, so a reminder is never sent
for a question that was answered somewhere Kelpie can see):

- any later comment by the recipient on the same task;
- any later comment by the recipient on any task of the same machine that mentions the
  asker (``@<asker>``);
- any later comment by a person on the same task (a person settled it, or took it over);
- the task reaching a terminal status (done, failed, cancelled).

**When it reminds.** Only while the recipient is idle: no live session of the recipient
(:func:`~kelpie.rules._tasks.is_live`, ``session_stale_after``). A recipient that is busy
mid-run is not interrupted; the reminder waits until it is between runs. An adapter that
reports no sessions for the recipient makes it look idle: the reminder then goes out on
the step's schedule alone. The ask's age is
awake time of the task's host (a host that slept does not age it). Each of ``steps``
(``1h``, ``4h``, ``12h``, ``24h`` by default) gives at most one reminder: when the
recipient is idle, the reminder of the latest step the ask has passed is sent once
(``once_per_key``, keyed by task, asker, recipient, when the ask opened and the step), so a
recipient busy across two steps gets one reminder, not two. Past ``stop_after`` (``3d``) of
awake time, or six days of wall-clock time, the rule stops reminding about the ask: it is
old news by then, and the ledger's once-window (``[engine] once_window``, 7 days) must not
run out under a reminder that repeats.

**Delivery** is ``seen`` only when the recipient had a session on the asking task that
started after the ask; otherwise ``unknown``: Kelpie cannot tell whether the recipient ever
read the comment, and never assumes the ask was lost or resends it.

**Snoozes.** A snooze on the asking task (``kelpie snooze``, a chat reply, an ``until:``
marker) holds its reminders while active; once it expired (or the work behind the task grew
since it was snoozed) the next reminder due goes out and mentions the snooze (a step
already reminded before the snooze is not reminded again).

**Boundaries with other rules (no double alerts).**

- Questions to *people* are rule 13's (``needs_you``): a person is never a recipient here,
  and an asking task with a block that asks a person (:func:`asks_a_person` with rule 13's
  default ``approval_reasons``, not a customised ``[engine.rules.needs_you]``) is left to
  rule 13's digest.
- A *blocked* asking task is rule 4's (``stale_blocks``) or rule 13's: one of them already
  announces the block (no condition, overdue, a dead end, or the person's digest). A
  *waiting* one is rule 2's (``wait_condition``, overdue). This rule only reminds about asks
  on tasks that are not blocked or waiting (backlog, ready, in progress, review, accepted),
  so a wait is announced by one rule, never by two.
- Rule 6 (``bottlenecks``) counts idle agents; it never says that one of them owes an answer.

**Actions.** One ``alert`` per ask and step, ``info`` for the first step and ``warning``
from the second on, about the asking task, with ``params``: ``asker``, ``recipient``,
``asked_at`` (ISO 8601), ``question`` (the comment, capped), ``delivery`` (``seen`` or
``unknown``), ``recipient_state`` (``idle``), ``step`` (1-based), ``open_hours`` (hours of
awake time since the ask) and ``snooze`` when an expired or grown snooze sent it again. The
rule only alerts; Kelpie never edits agents' instructions or writes to their sessions.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import pairwise
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Comment, Owner, OwnerKind, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._alerts import alert_params
from kelpie.rules._snoozes import SnoozeCheck, checker
from kelpie.rules._tasks import DEFAULT_STALE_AFTER, asks_a_person, is_live, task_awake

NAME = "unanswered_asks"

DEFAULT_STEPS = (
    timedelta(hours=1),
    timedelta(hours=4),
    timedelta(hours=12),
    timedelta(hours=24),
)
DEFAULT_STOP_AFTER = timedelta(days=3)
# The ledger keeps once-actions for 7 days (``[engine] once_window``, wall-clock); reminders
# stop well within that, also when a sleeping host keeps the ask's awake age low.
MAX_STOP_AFTER = timedelta(days=6)
MAX_QUESTION = 300  # characters of the question quoted in the alert

_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9][\w.:-]*)")
# Statuses whose wait other rules announce: rule 4 / rule 13 (blocked), rule 2 (waiting).
_LEFT_TO_OTHERS = frozenset({Status.BLOCKED, Status.WAITING})


def _local(name: str) -> str:
    """A name without a host prefix (``host-a:dev-1`` -> ``dev-1``), case-folded."""
    return name.rsplit(":", 1)[-1].casefold()


def _mentions(text: str) -> set[str]:
    """The names mentioned as ``@name`` or ``@host:name`` (case-folded, without a trailing
    sentence dot or colon)."""
    return {m.rstrip(".:-").casefold() for m in _MENTION.findall(text)}


def _resolve(name: str, task: Task, agents: dict[str, list[Owner]]) -> Owner | None:
    """The agent a mention on ``task`` names: among the agents with that id (without its
    host prefix), the first by id that serves the task's machine; ``@host:name`` must name
    that host too (on a multi-host farm, owner ids are ``<host>:<id>``)."""
    host, _, local = name.rpartition(":")
    for owner in agents.get(local, ()):
        if host and owner.id.casefold() != name and (owner.host or "").casefold() != host:
            continue
        if owner.serves(task):
            return owner
    return None


Replies = dict[tuple[str | None, str], list[tuple[datetime, set[str]]]]


@dataclass(frozen=True, slots=True)
class Ask:
    task: Task
    asker: str
    recipient: Owner
    comment: Comment  # the first ask of the pair since the last answer


@dataclass(frozen=True, slots=True)
class UnansweredAsks:
    steps: tuple[timedelta, ...] = DEFAULT_STEPS
    stop_after: timedelta = DEFAULT_STOP_AFTER
    session_stale_after: timedelta = DEFAULT_STALE_AFTER
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        snoozed = checker(snapshot)
        for ask in open_asks(snapshot):
            task = ask.task
            if task.status in _LEFT_TO_OTHERS:
                continue
            if task.block is not None and asks_a_person(task, snapshot):
                continue  # waits on a person: rule 13's digest
            age = task_awake(task, ask.comment.at, snapshot)
            if age < self.steps[0] or age >= self.stop_after:
                continue
            if snapshot.now - ask.comment.at >= MAX_STOP_AFTER:
                continue  # a host that slept a lot: the ledger's once-window is wall-clock
            if self._busy(ask.recipient, snapshot):
                continue
            snooze = snoozed(task.id)
            if snooze is not None and snooze.holds:
                continue
            step = sum(1 for s in self.steps if age >= s)
            actions.append(self._remind(ask, step, age, snapshot, snooze))
        return actions

    def _busy(self, recipient: Owner, snapshot: Snapshot) -> bool:
        return any(
            s.owner_id == recipient.id
            and is_live(s, snapshot.now, self.session_stale_after, snapshot.host_sleeps)
            for s in snapshot.sessions
        )

    def _remind(
        self, ask: Ask, step: int, age: timedelta, snapshot: Snapshot, snooze: SnoozeCheck | None
    ) -> Action:
        task, who, asker = ask.task, ask.recipient.id, ask.asker
        asked_at = ask.comment.at
        delivery = _delivery(ask, snapshot)
        hours = round(age.total_seconds() / 3600, 1)
        question = " ".join(ask.comment.text.split())
        if len(question) > MAX_QUESTION:
            question = question[: MAX_QUESTION - 1] + "…"
        seen = (
            f"{who} has had a run on the task since the question"
            if delivery == "seen"
            else f"Kelpie cannot tell whether {who} has seen it (delivery unknown)"
        )
        again = f" Reminding again: {snooze.text()}." if snooze is not None else ""
        reason = (
            f"{asker} asked {who} a question on task {task.id} at {asked_at.isoformat()} "
            f"({hours:g}h of awake time ago) and has no answer yet; {seen}. {who} is idle now "
            f"(no live session), so this is a good moment to answer. Question: "
            f'"{question}".{again}'
        )
        params: dict[str, Any] = {
            "asker": asker,
            "recipient": who,
            "asked_at": asked_at.isoformat(),
            "question": question,
            "delivery": delivery,
            "recipient_state": "idle",
            "step": step,
            "open_hours": hours,
            **alert_params(
                f"{who} has not answered {asker}'s question on {task.id} ({hours:g}h)",
                f"{who} to answer {asker}'s question on {task.id}, or say who should",
            ),
        }
        if snooze is not None:
            params["snooze"] = snooze.params()
        return Action(
            ActionKind.ALERT,
            NAME,
            reason,
            task_id=task.id,
            key=f"{NAME}:{task.id}:{_local(asker)}:{who}:{asked_at.isoformat()}:{step}",
            severity=Severity.INFO if step == 1 else Severity.WARNING,
            since=min(asked_at, snapshot.now),
            once=True,
            once_per_key=True,
            params=params,
        )


def open_asks(snapshot: Snapshot) -> list[Ask]:
    """Every unanswered ask on the board's open tasks, in task order, then by when it was
    asked. Pure; status filters and thresholds are the rule's."""
    agents: dict[str, list[Owner]] = {}
    for o in sorted(snapshot.owners, key=lambda o: o.id):
        if o.kind is OwnerKind.AGENT:
            agents.setdefault(_local(o.id), []).append(o)
    if not agents:
        return []
    # Comments by (machine, author) with the names they mention: an answer to the asker
    # anywhere on the same machine (agents of two hosts may share a name).
    replies: Replies = {}
    for t in snapshot.tasks:
        for c in t.comments:
            if not c.human:
                names = {_local(m) for m in _mentions(c.text)}
                replies.setdefault((t.host, _local(c.author)), []).append((c.at, names))
    found: list[Ask] = []
    for task in snapshot.tasks:
        if task.status.is_terminal or not task.comments:
            continue
        found.extend(_task_asks(task, agents, replies))
    return found


def _task_asks(
    task: Task,
    agents: dict[str, list[Owner]],
    replies: Replies,
) -> list[Ask]:
    comments = sorted(task.comments, key=lambda c: c.at)
    pending: dict[tuple[str, str], Ask] = {}
    for c in comments:
        author = _local(c.author)
        if c.human:
            pending.clear()  # a person spoke on the task: what was asked is theirs now
            continue
        for pair in [p for p in pending if _local(p[1]) == author]:
            del pending[pair]  # the recipient spoke on the task after the ask
        if "?" not in c.text:
            continue
        for name in sorted(_mentions(c.text)):
            owner = _resolve(name, task, agents)
            if owner is None or _local(owner.id) == author:
                continue
            pending.setdefault((author, owner.id), Ask(task, c.author.strip(), owner, c))
    return [
        ask
        for pair, ask in sorted(pending.items(), key=lambda x: (x[1].comment.at, x[0]))
        if not _answered_elsewhere(task.host, pair, ask.comment.at, replies)
    ]


def _answered_elsewhere(
    host: str | None, pair: tuple[str, str], asked_at: datetime, replies: Replies
) -> bool:
    asker, recipient = pair
    return any(
        at > asked_at and asker in names for at, names in replies.get((host, _local(recipient)), ())
    )


def _delivery(ask: Ask, snapshot: Snapshot) -> str:
    """``seen`` when the recipient had a session on the asking task after the ask, else
    ``unknown``: never assumed lost."""
    for s in snapshot.sessions_for(ask.task.id):
        if s.owner_id == ask.recipient.id and s.started_at > ask.comment.at:
            return "seen"
    return "unknown"


def _duration(value: Any, what: str) -> timedelta:
    try:
        found = parse_duration(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{what}: {exc}") from exc
    if found <= timedelta(0):
        raise ValueError(f"{what} must be positive")
    return found


@register(NAME)
def build(config: dict[str, Any]) -> UnansweredAsks:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    steps = DEFAULT_STEPS
    if "steps" in config:
        raw = config.pop("steps")
        if not isinstance(raw, list | tuple) or not raw:
            raise ValueError("steps must be a non-empty list of durations")
        steps = tuple(_duration(s, "steps") for s in raw)
        if any(b <= a for a, b in pairwise(steps)):
            raise ValueError("steps must be increasing")
        kwargs["steps"] = steps
    stop_after = DEFAULT_STOP_AFTER
    if "stop_after" in config:
        stop_after = _duration(config.pop("stop_after"), "stop_after")
        kwargs["stop_after"] = stop_after
    if stop_after <= steps[-1]:
        raise ValueError("stop_after must be longer than the last step")
    if stop_after > MAX_STOP_AFTER:
        raise ValueError("stop_after must be at most 6d (the ledger's once-window is 7d)")
    if "session_stale_after" in config:
        kwargs["session_stale_after"] = _duration(
            config.pop("session_stale_after"), "session_stale_after"
        )
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return UnansweredAsks(**kwargs)
