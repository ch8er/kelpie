"""Unanswered asks (issue #297): a question one agent puts to another is an obligation until
it is answered.

On a farm, agents ask each other things in task comments ("@architect-1 which schema
version should the migration target?") and carry on, or end their run. Nothing reminds the
other agent; the question sits until a person notices. This rule keeps such asks open and
reminds once the recipient is free to answer.

**What is an ask** (:mod:`kelpie.rules._asks`, shared with rule 13). A comment on an open
task, written by an agent (not a person, not Kelpie: adapters leave Kelpie's own comments
out), that mentions another agent of the farm as ``@<owner id>`` (case-insensitive) and
contains a question mark. Or a block reason (#461): an agent blocked its task on a question
to another agent ("@architect-1 which schema version?"), with no condition linked to the
block; the task's owner is the asker, the first agent mentioned (by name) the recipient,
and the ask opens when the block was set. The recipient must be
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
- the task reaching a terminal status (done, failed, cancelled); for a block ask, the block
  clearing.

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
  settings while it reaches people, ``needs_you``, else its defaults) is left to rule 13's
  digest. Rule 13 reads agents' comment questions to people itself (#461, decision 2a).
- A *blocked* asking task is rule 4's (``stale_blocks``) or rule 13's: one of them already
  announces the block (no condition, overdue, a dead end, or the person's digest), so its
  comment asks are not reminded here. A *waiting* one is rule 2's (``wait_condition``,
  overdue). Comment asks are reminded only on tasks that are not blocked or waiting
  (backlog, ready, in progress, review, accepted), so a wait is announced by one rule.
- A *block that asks another agent* is this rule's (#461, decision 1a): a wait on that
  agent, reminded here while the agent is idle. Rule 4 treats it as a wait, like a block
  waiting on a condition (``kelpie.app`` gives rule 4 this rule's settings while this rule
  reaches agents: it is loaded and not in ``observe`` while rule 4 is not): no "nothing
  Kelpie can check" alert and no block storm, but its overdue alert once the block has
  waited its ``max_block`` (8h), also when the agent stayed busy and was never reminded,
  or at once when the ask is past this rule's window (``stop_after`` or six days). This rule
  stops reminding about a block ask at rule 4's ``max_block`` (``block_until``, set by
  ``kelpie.app``), so one rule announces the wait at a time. Once the
  ask is answered and the block stays, rule 4 alerts as on any block. Each reminder is per
  task: five tasks blocked on the same question to one agent give five reminders per step.
  A block rule 13 lists (it asks a person, by rule 13's own settings while rule 13 reaches
  people, ``needs_you``; else by its defaults) is rule 13's, not an ask here.
- Rule 6 (``bottlenecks``) counts idle agents; it never says that one of them owes an answer.

**Actions.** One ``alert`` per ask and step, ``info`` for the first step and ``warning``
from the second on, about the asking task, with ``params``: ``asker``, ``recipient``,
``asked_at`` (ISO 8601), ``question`` (the comment, capped), ``delivery`` (``seen`` or
``unknown``), ``recipient_state`` (``idle``), ``step`` (1-based), ``open_hours`` (hours of
awake time since the ask), ``source`` (``comment`` or ``block``) and ``snooze`` when an
expired or grown snooze sent it again. The
rule only alerts; Kelpie never edits agents' instructions or writes to their sessions.

**Rate limit.** The reminders are ``own_budget`` alerts: ``[engine]
max_actions_per_task_per_hour`` counts them per rule and task, apart from the task's own
budget. A comment that mentions many agents (an honest broadcast, or an agent trying to use
up its task's budget to dodge a stop) gives many reminders, but none of them can hold back
another rule's action on the task (#390).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from itertools import pairwise
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import OwnerKind, Snapshot, Status
from kelpie.core.serde import parse_duration
from kelpie.rules import _asks, needs_you, register
from kelpie.rules._alerts import alert_params
from kelpie.rules._asks import Ask
from kelpie.rules._asks import local as _local
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

# Statuses whose wait other rules announce: rule 4 / rule 13 (blocked), rule 2 (waiting).
_LEFT_TO_OTHERS = frozenset({Status.BLOCKED, Status.WAITING})


@dataclass(frozen=True, slots=True)
class UnansweredAsks:
    steps: tuple[timedelta, ...] = DEFAULT_STEPS
    stop_after: timedelta = DEFAULT_STOP_AFTER
    session_stale_after: timedelta = DEFAULT_STALE_AFTER
    # Rule 13 as it reaches people (kelpie.app sets it from needs_you's config while it
    # does), or None: the blocks it lists are left to it, as rule 4 leaves them.
    inbox: needs_you.NeedsYou | None = None
    # How long a block that asks an agent is this rule's to remind about: rule 4's
    # ``max_block`` (kelpie.app sets it), after which rule 4's overdue alert takes it over
    # (#461), so a wait is announced by one rule at a time. None: no such limit.
    block_until: timedelta | None = None
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        snoozed = checker(snapshot)
        live = self._live_owners(snapshot)  # once per tick: asks x sessions was quadratic
        for ask in self._announced(snapshot):
            task = ask.task
            age = task_awake(task, ask.comment.at, snapshot)
            if age < self.steps[0] or not self.within(ask, snapshot):
                continue
            if ask.recipient.id in live:
                continue  # busy mid-run: not interrupted
            snooze = snoozed(task.id)
            if snooze is not None and snooze.holds:
                continue
            step = sum(1 for s in self.steps if age >= s)
            actions.append(self._remind(ask, step, age, snapshot, snooze))
        return actions

    def _announced(self, snapshot: Snapshot) -> list[Ask]:
        """The asks this rule reminds about: comment asks on tasks that are not blocked or
        waiting (and whose block, if any, does not ask a person), then block asks."""
        settings = self.inbox or needs_you.NeedsYou()
        found = [
            ask
            for ask in open_asks(snapshot)
            if ask.task.status not in _LEFT_TO_OTHERS
            and not (
                ask.task.block is not None
                and asks_a_person(
                    ask.task, snapshot, settings.approval_reasons, settings.condition_reasons
                )
            )
        ]
        return found + list(self.block_asks(snapshot).values())

    def block_asks(
        self, snapshot: Snapshot, listed: frozenset[str] | None = None
    ) -> dict[str, Ask]:
        """The unanswered asks in block reasons this rule announces, by task id
        (:func:`kelpie.rules._asks.block_ask`): every one, except a block rule 13 lists
        while it reaches people (:func:`~kelpie.rules.needs_you.inbox_task_ids`), else a
        block that asks a person by rule 13's default test. Rule 4 sends no "nothing Kelpie
        can check" alert for these (#461). ``listed``: rule 13's
        :func:`~kelpie.rules.needs_you.inbox_task_ids` with this rule's ``inbox``, when the
        caller has it already (computed here otherwise)."""
        agents = _asks.owners_by_name(snapshot, OwnerKind.AGENT)
        if not agents:
            return {}
        answered = _asks.replies(snapshot)
        if listed is None and self.inbox is not None:
            listed = needs_you.inbox_task_ids(snapshot, self.inbox)
        found: dict[str, Ask] = {}
        for task in snapshot.tasks:
            ask = _asks.block_ask(task, snapshot, agents, answered)
            if ask is None:
                continue
            if listed is not None and task.id in listed:
                continue  # rule 13's: a person is asked, or it is parked on a person's task
            if listed is None and asks_a_person(task, snapshot):
                continue  # a person is asked: rule 13's test, its defaults
            found[task.id] = ask
        return found

    def within(self, ask: Ask, snapshot: Snapshot) -> bool:
        """Whether the ask is still this rule's to remind about: not :meth:`expired`, and
        for a block ask less than ``block_until`` (rule 4's ``max_block``) since the block
        was set: rule 4's overdue alert has it after that."""
        if self.expired(ask, snapshot):
            return False
        late = self.block_until is not None and snapshot.now - ask.comment.at >= self.block_until
        return not (ask.in_block and late)

    def expired(self, ask: Ask, snapshot: Snapshot) -> bool:
        """Whether the ask is old news: ``stop_after`` of the task host's awake time, or six
        days of wall-clock time (a host that slept a lot: the ledger's once-window is
        wall-clock)."""
        if task_awake(ask.task, ask.comment.at, snapshot) >= self.stop_after:
            return True
        return snapshot.now - ask.comment.at >= MAX_STOP_AFTER

    def _live_owners(self, snapshot: Snapshot) -> set[str]:
        return {
            s.owner_id
            for s in snapshot.sessions
            if s.owner_id is not None
            and is_live(s, snapshot.now, self.session_stale_after, snapshot.host_sleeps)
        }

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
        where = (
            f"blocked task {task.id} on a question to {who}"
            if ask.in_block
            else f"asked {who} a question on task {task.id}"
        )
        reason = (
            f"{asker} {where} at {asked_at.isoformat()} "
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
            "source": "block" if ask.in_block else "comment",
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
            key=f"{NAME}:{task.id}:{_local(asker)}:{who}:{asked_at.isoformat()}:{step}"
            + (":block" if ask.in_block else ""),
            severity=Severity.INFO if step == 1 else Severity.WARNING,
            since=min(asked_at, snapshot.now),
            once=True,
            once_per_key=True,
            # One comment can mention many agents: these reminders get a budget of their
            # own, so they never use up the task's for a stop or requeue (#390).
            own_budget=True,
            params=params,
        )


def open_asks(snapshot: Snapshot) -> list[Ask]:
    """Every unanswered comment ask to an agent on the board's open tasks, in task order,
    then by when it was asked (:func:`kelpie.rules._asks.open_asks`). Pure; status filters
    and thresholds are the rule's."""
    return _asks.open_asks(snapshot, OwnerKind.AGENT)


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
    if "needs_you" in config:
        kwargs["inbox"] = needs_you.hand_off(config.pop("needs_you"))
    if "block_until" in config:
        value = config.pop("block_until")
        kwargs["block_until"] = None if value is False else _duration(value, "block_until")
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return UnansweredAsks(**kwargs)


def hand_off(value: Any) -> UnansweredAsks | None:
    """The ``unanswered_asks`` setting of rule 4: false (this rule does not reach agents:
    rule 4 keeps its alerts on blocks that ask an agent), true (this rule's defaults) or
    this rule's config section, which :func:`build` validates."""
    if value is False:
        return None
    if value is True:
        return build({})
    if isinstance(value, Mapping):
        return build(dict(value))
    raise ValueError("unanswered_asks must be false, true or the rule's settings")
