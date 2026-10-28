"""Rule 13 — the "needs you" inbox (incident 13).

Everything on the farm that waits for a person, gathered per person and sorted by cost of
delay, so the human sees one list instead of scattered blocks:

- **Human tasks with agent work behind them.** An open task owned by a human
  (:attr:`~kelpie.core.model.OwnerKind.HUMAN`) that agent tasks wait on, through
  dependencies or the ``human_task`` conditions of blocked and waiting tasks, transitively
  (the same graph as rule 6's :func:`~kelpie.rules.bottlenecks.human_gates`), and that is
  the person's to move now: ready or in progress with nothing open before it, or blocked
  or waiting itself (the person chases what blocks it). It enters the inbox once agent
  work waiting on it has waited ``human_wait_alert_after`` (1h). Human tasks with nothing
  behind them never do.
- **Agents waiting for a person's input or approval.** An agent task blocked with a reason
  that starts with one of ``approval_reasons`` (``waiting_human``, Hermes' typed blocks
  ``needs_input``, ``approval``, ...), unless it is parked on a human task (see below;
  then it is listed under that task), once it has waited ``approval_wait_after`` (30m).
  Its human is ``approvers[role]``, else ``default_approver``; without either it is listed
  under ``unassigned``. A block that waits on a condition is not one, whatever its kind: it
  has a linked condition other than a human task (rule 4 owns it), or its reason matches
  ``condition_reasons`` (by default the ``[[block_conditions]]`` block patterns, such as
  ``needs_input: automerge PR #12``). On a live farm reviewers blocked with ``needs_input``
  to get past Hermes' block loop guard while they waited for a merge. Nor is a block
  whose condition can never be met (the human task it waited on was cancelled) or that
  names a condition Kelpie cannot see: rule 4 alerts on those, with what is stuck behind
  them. The test is :func:`~kelpie.rules._tasks.asks_a_person`.

**Rule 4 leaves what this rule lists to it.** While this rule reaches people, rule 4 gets
its settings and leaves it every task :func:`inbox_task_ids` returns (what this rule lists,
its time thresholds aside: the items, approvals and the person's own blocked tasks, and the
agent tasks parked on a listed human task), with no no-condition, overdue or dead-end alert;
every other block keeps rule 4's alerts. A block is announced by one of the two, never by
both and never by neither: a task parked on a human task the person cannot move yet is
listed here nowhere, so rule 4 alerts on it; one that waits on something else too (a PR),
or that has a dead end or a condition Kelpie cannot see, is not parked here, so rule 4
alerts on it. Only stale evidence (a connector that stopped checking) alerts either way.
Rule 2 (``wait_condition``) does the same for waiting tasks (#213): it sends no overdue alert
for a waiting task :func:`inbox_task_ids` returns, and keeps its dead-end, unseen-condition,
no-condition and wake outputs.

**Planned waits are not waiting** (incident 20). A task that is not ready for work for its
own reasons (:func:`~kelpie.rules._tasks.planned`: not started yet, and in a pre-work
status such as Hermes triage or scheduled, or labelled with one of ``planned_labels``, on
the task or a linked PR; they default to rule 6's ``planned_labels`` and hold labels)
waits on nothing it depends on: it is not behind a human task and adds no cost, and a
planned human task is in the inbox only for the tasks a condition parks on it, as rule 6
reads it. A blocked or waiting task was started: it waits on what it names (an approval, a
human task), labels or not. On a live farm a post-release task behind a person's planned
task counted as 143 agent-hours of waiting on that person.

**Cost of delay** of an item: the agent-hours spent waiting on it so far, the sum over the
agent tasks that wait on it directly of how long each has waited: a task parked on it since
it was parked, and a ready or backlog task that depends on it since its last status change
(platforms keep dependents in the backlog until their parents finish). Parked on it: a
condition names it (``human_task``, a dispatch gate's ``task_status``), or the task depends
on it and nothing else visibly holds it (no block reason, one that names the human task, or
one that asks a person); either way no other unmet condition holds it, and none of its
conditions is a dead end or one Kelpie cannot see. Everything else behind it (deeper
in the chain, in progress, or parked for another reason such as CI) is held but waits on
something else first: it counts in the number of agent tasks behind the item, not in its
cost. Items are sorted by cost; so are the people, each waiting task counted once with its
longest wait.

**Actions.** One ``escalate`` per person, addressed to them (``escalate_to``), listing their
items in order, as a long-lived state (``needs_you:<person>``): announced when it opens,
again when the list changes or at the escalation thresholds, resolved when it is empty.
Severity is ``critical`` once the person's total cost reaches ``critical_after_hours``
(24), else ``warning``. Every escalation also carries ``waiting_agent_tasks``, how many agent
tasks wait on the person right now, and ``waiting``, their ids (at most 20): the daily
report integrates the count over the day as agent-hours waiting on people. Planned work is
apart (incident 20): ``planned_agent_tasks`` and ``planned`` (at most 20 ids) name the first
planned agent task on each dependency path behind the listed human tasks (held tasks
included: hold labels are planned labels), not what waits behind them, and nothing for a
human task that is not in the inbox. Like ``waiting``, a task behind two people's tasks is
in both escalations: sum across people by id.

``items`` lists the person's items in order (all of them, at most 200; ``items_total``
counts them), for the alert's digest ("Waiting for you: 2 tasks (oldest 46 h)", which shows
10 and counts the rest) and the daily report: ``task_id``, ``title``, ``kind``
(``human_task`` or ``approval``), ``needed`` (what the person is asked, in the agent's
words: an approval's block reason, or why a human task is blocked or waiting; for a
person's blocked task that can never clear on its own, why, in rule 4's words, since rule 4
sends nothing on it: "the PR was closed without merging (pr:x): the block will never clear
on its own", "the block names condition pr:gone, which Kelpie cannot see"; none for a
human task that is just to do, whose title says it), ``since`` (when its longest wait
began, ISO 8601), ``blocking`` (how many tasks wait behind it, all of them: the agent tasks
behind a human task; for an approval, the tasks waiting on the blocked one, as rule 4's
``stuck_ids`` counts them) and ``cost_hours``. The older keys stay as they are, since the
ledger holds them and the report reads ``task``: ``task`` (the id again), ``behind`` (at
most 10 of the tasks behind it; the approval itself for one) and ``waiting_minutes``.

The state's key is the person's list of task ids, so any change to it, an item added or
one done, is announced as "changed" with the new list; an emptied inbox resolves.
``escalate_to`` names the person in the alert and the report; delivery goes to the
configured alert channels like every other alert (no per-person routing yet).

**Snoozes** (issue #176, :mod:`kelpie.rules._snoozes`). An item whose task a person snoozed
(``kelpie snooze``, a chat reply, or an ``until:`` marker where agents record people's
decisions) leaves the escalation while the snooze holds: it is not in the state's key, the
items, ``human_tasks``, ``approvals``, the counts, ``waiting`` or the cost, so the digest
does not announce or escalate because of it, and a person whose items are all snoozed gets
no escalation (their open state resolves, and the notice lists the snoozes). It is listed
in ``snoozed`` instead (``task_id``, ``title``, ``kind``, ``since``, ``blocking``,
``waiting``: the agent tasks parked on it as ``waiting`` counts them, at most 10, and
``snooze``: ``state``, ``until``, ``reason``, ``by``, ``source``; at most 200;
``snoozed_total`` counts them), which the alert shows as a footer ("Snoozed: <title> until
<date> (<reason>, by <who>)", "from marker" for a marker). Setting a snooze changes the
key, so the digest says "changed" once, with the item in the footer. The item comes back
when its snooze expired (within the last 7 days: its ``snooze.state`` is ``expired``) or
when more tasks are blocked behind its task than when it was snoozed (``grown``, with
``grown``: how many more; a snooze that does not know that count, such as a marker, never
comes back this way; the count is :func:`~kelpie.rules._snoozes.behind_count`, and growth
``kelpie serve`` saw once is latched with the snooze). An item
whose block can never clear (``broken``: a dead end, a condition Kelpie cannot see) is never
held: that is news, said at once. A snooze is on the item's own task: the human task, or
the blocked agent task of an approval; one on a task merely parked behind a human task
holds nothing (``kelpie snooze`` warns, :func:`parked_on`).

This rule owns escalating human work, per person. Rule 6 keeps the farm-level alert (free
slots and only human tasks ready) and escalates single human gates only with
``escalate_human_gates = true``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.conditions import linked_conditions
from kelpie.core.model import Condition, KelpieMark, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._chains import behind_lookup
from kelpie.rules._snoozes import SnoozeCheck, checker, describe
from kelpie.rules._tasks import (
    DEFAULT_APPROVAL_REASONS,
    approval_reasons_list,
    asks_a_person,
    condition_patterns,
    dead_end_text,
    dead_ends,
    labels_list,
)
from kelpie.rules.bottlenecks import (
    PLANNED_AGENT_TASKS,
    HumanGate,
    human_gates,
    link_pull_requests,
    with_pr_labels,
)

NAME = "needs_you"
# Params every escalation carries for planned work behind the person's listed human tasks
# (incident 20): not waiting on them, so outside ``waiting``; for the daily report to import.
# (Not ``PLANNED``: ``kelpie.rules.PLANNED`` is the list of planned rules.)
PLANNED_TASKS = "planned"
UNASSIGNED = "unassigned"
_SHOWN = 5
MAX_ITEMS = 200  # items sent per person (the alert shows 10 and counts the rest)
_FROZEN = frozenset({Status.BLOCKED, Status.WAITING})


@dataclass(frozen=True, slots=True)
class Item:
    kind: str  # "human_task" | "approval"
    task: Task
    behind: tuple[str, ...]  # agent tasks waiting on it (the task itself for an approval)
    waiting: timedelta  # how long the longest of them has waited
    cost_hours: float  # agent-hours waited, summed over its waiting agent tasks
    waits: Mapping[str, timedelta] = field(default_factory=dict)  # waiting task -> how long
    planned: tuple[str, ...] = ()  # planned agent tasks behind it, not waiting (incident 20)
    held: tuple[str, ...] = ()  # an approval's: the tasks waiting behind it (rule 4's stuck_ids)
    # A person's blocked task whose block can never clear on its own: what rule 4 would say
    # (a dead end, a condition Kelpie cannot see), said here instead, once.
    broken: str | None = None
    # Its task's snooze: holding it (listed in the footer), or why it is back (#176).
    snooze: SnoozeCheck | None = None

    @property
    def blocking(self) -> int:
        """How many tasks wait behind it, all of them (``behind`` is cut in params): the
        agent tasks behind a human task; for an approval, the tasks that wait on the
        blocked task, through dependencies and task waits."""
        return len(self.held) if self.kind == "approval" else len(self.behind)

    @property
    def needed(self) -> str | None:
        """What the person is asked, in the agent's own words: an approval's block reason,
        or the reason a human task is blocked or waiting (the person chases that). For a
        human task whose block can never clear on its own, why (``broken``: rule 4 leaves
        it to this digest). None for a human task that is just to do: its title says it."""
        if self.broken:
            return self.broken
        if self.kind == "approval" or self.task.status in _FROZEN:
            reason = self.task.block.reason.strip() if self.task.block is not None else ""
            return reason or None
        return None

    def describe(self) -> str:
        hours = f"{self.cost_hours:.1f} agent-hours"
        if self.kind == "approval":
            what = (
                "was stopped by Kelpie and needs unblocking"
                if _stopped(self.task)
                else ("waits for your input")
            )
            return f"{self.task.id} ('{self.task.title}') {what}: {hours}"
        state = ""
        if self.task.status in _FROZEN:
            why = f": {self.task.block.reason}" if self.task.block is not None else ""
            state = f"is {self.task.status}{why}, and "
        return (
            f"{self.task.id} ('{self.task.title}') {state}holds {len(self.behind)} agent task(s) "
            f"({', '.join(self.behind[:_SHOWN])}{'...' if len(self.behind) > _SHOWN else ''})"
            f": {hours}"
        )


@dataclass(frozen=True, slots=True)
class NeedsYou:
    human_wait_alert_after: timedelta = timedelta(hours=1)
    approval_wait_after: timedelta = timedelta(minutes=30)
    approval_reasons: tuple[str, ...] = DEFAULT_APPROVAL_REASONS
    approvers: Mapping[str, str] = field(default_factory=dict)  # agent role -> human
    default_approver: str | None = None
    critical_after_hours: float = 24.0
    # Block reasons that name something a condition checks (inherited from
    # [[block_conditions]]): such a block waits on the condition, not on a person.
    condition_reasons: tuple[re.Pattern[str], ...] = ()
    # Labels marking a task as later (inherited from rule 6's planned and hold labels).
    planned_labels: frozenset[str] = frozenset()
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        split = self._split(snapshot, self._inbox(snapshot, waited=True))
        ranked = sorted(
            ((person, live, held) for person, (live, held) in split.items() if live),
            key=lambda x: (-_total(x[1]), x[0]),
        )
        return [self._escalate(person, live, snapshot.now, held) for person, live, held in ranked]

    def resolved(self, state_id: str, snapshot: Snapshot) -> str | None:
        """The resolve notice of a person whose items are all snoozed: what is snoozed, and
        when it comes back. None otherwise: the engine's general notice."""
        person = state_id.removeprefix(f"{NAME}:")
        if person == state_id:
            return None
        inbox = self._split(snapshot, self._inbox(snapshot, waited=True))
        live, held = inbox.get(person, ([], []))
        if live or not held:
            return None
        who = "nobody assigned" if person == UNASSIGNED else person
        listed = "; ".join(_snoozed_text(i) for i in held[:_SHOWN])
        more = f"; and {len(held) - _SHOWN} more" if len(held) > _SHOWN else ""
        return (
            f"Everything waiting for {who} is snoozed: {listed}{more}. Kelpie alerts again "
            "when a snooze ends or more work is blocked behind its task."
        )

    def _split(
        self, snapshot: Snapshot, inbox: Mapping[str, list[Item]]
    ) -> dict[str, tuple[list[Item], list[Item]]]:
        """Every person's items, split into those to escalate (a snooze that expired or
        whose work grew is marked on its item) and those a snooze holds (the footer). An
        item whose block can never clear (``broken``) is never held: that is news, not a
        reminder."""
        snoozed = checker(snapshot)
        out: dict[str, tuple[list[Item], list[Item]]] = {}
        for person, items in inbox.items():
            live: list[Item] = []
            held: list[Item] = []
            for item in items:
                found = snoozed(item.task.id)
                if found is not None and found.holds and item.broken:
                    found = None  # a dead end on a snoozed task is said at once, unmarked
                if found is not None:
                    item = replace(item, snooze=found)
                (held if found is not None and found.holds else live).append(item)
            out[person] = (live, held)
        return out

    def _inbox(self, snapshot: Snapshot, *, waited: bool) -> dict[str, list[Item]]:
        """Every person's items. ``waited``: only those past ``human_wait_alert_after`` and
        ``approval_wait_after``; without it, everything the inbox lists once it has waited
        long enough (:func:`inbox_task_ids`)."""
        snapshot = with_pr_labels(snapshot, link_pull_requests(snapshot)[0])  # as rule 6
        roles = {o.id: o.role for o in snapshot.owners}
        inbox: dict[str, list[Item]] = {}
        gates = human_gates(snapshot, self.planned_labels)
        found, parked = self._human_tasks(snapshot, gates, waited)
        for item in found:
            inbox.setdefault(item.task.owner_id or UNASSIGNED, []).append(item)
        for item in self._approvals(snapshot, parked, waited):
            inbox.setdefault(self._approver(item.task, roles) or UNASSIGNED, []).append(item)
        return inbox

    def _human_tasks(
        self, snapshot: Snapshot, gates: list[HumanGate], waited: bool = True
    ) -> tuple[list[Item], set[str]]:
        """Human tasks that are the person's to move now, whose agent work has waited long
        enough (``waited``): ready or in progress with nothing open before them (like rule
        6), or blocked or waiting themselves (the person chases what blocks them). Also the
        agent tasks parked on a human task (:meth:`_parked_on`), whether it is listed or not
        the person's to move yet: they are that task's, not approvals of their own. Parked
        on one the person cannot move yet, a task is listed nowhere here (agent work comes
        first), so rule 4 keeps its alerts on it (:func:`inbox_task_ids`)."""
        by_id = {t.id: t for t in snapshot.tasks}
        open_ids = {t.id for t in snapshot.tasks if not t.status.is_terminal}
        unmet = {
            t.id: [c for c in linked_conditions(t, snapshot)[0] if c.satisfied is not True]
            for t in snapshot.tasks
            if t.status in _FROZEN
        }  # once per task: the loop below runs per human task
        waiters = _direct_waiters(snapshot, unmet)
        items = []
        claimed: set[str] = set()
        for gate in gates:
            direct = waiters.get(gate.task.id, {})
            movable = gate.actionable or _stuck_on_its_own(gate.task, snapshot, open_ids)
            parked = {
                tid
                for tid in gate.behind
                if tid in direct
                and by_id[tid].status in _FROZEN
                and self._parked_on(
                    by_id[tid], gate.task.id, direct[tid], unmet[tid], movable, snapshot
                )
            }
            claimed |= parked  # not approvals of their own, listed under it or not
            if not movable:
                continue  # agent work comes first: not the person's bottleneck today
            waits = {
                tid: w
                for tid in gate.behind
                if (w := _waiting(by_id[tid], snapshot.now, direct, parked)) is not None
            }
            if not waits:
                continue  # only follow-up work behind it
            if waited and max(waits.values()) < self.human_wait_alert_after:
                continue  # not long enough
            cost = sum(w.total_seconds() for w in waits.values()) / 3600
            items.append(
                Item(
                    "human_task",
                    gate.task,
                    gate.behind,
                    max(waits.values()),
                    cost,
                    waits,
                    gate.planned,
                    broken=_broken(gate.task, unmet.get(gate.task.id, ()), snapshot),
                )
            )
        return items, claimed

    def _parked_on(
        self,
        task: Task,
        human_task: str,
        how: str,
        unmet: Sequence[Condition],
        movable: bool,
        snapshot: Snapshot,
    ) -> bool:
        """Whether a parked agent task that waits directly on ``human_task`` is parked
        because of it: a condition names it; or, linked by a dependency only, nothing else
        visibly holds it (no block, or a block reason that names the human task, or one
        that asks a person while the person can act on the human task). Either way no other
        ``unmet`` condition holds it (it waits on that first: a PR, CI), and none of its
        conditions is a dead end or one Kelpie cannot see (rule 4 alerts on those). A
        reason about something else (CI, a provider) or an agent's own question behind a
        human task nobody can do yet is not the human task's."""
        if any(c.params.get("task") != human_task for c in unmet):
            return False  # it waits on something else first
        if task.block is not None:
            missing = linked_conditions(task, snapshot)[1]
            if missing is not None or dead_ends(unmet, task.block.since):
                return False  # rule 4's dead end or broken link
        if how == "condition":
            return True
        if task.block is None:
            return True
        reason = task.block.reason
        if re.search(rf"(?<![\w-]){re.escape(human_task)}(?![\w-])", reason):
            return True
        return movable and self._asks_a_person(task, snapshot)

    def _asks_a_person(self, task: Task, snapshot: Snapshot) -> bool:
        """:func:`~kelpie.rules._tasks.asks_a_person` with this rule's settings: the test
        rule 4 runs too (with the same settings), to leave these blocks to this rule."""
        return asks_a_person(task, snapshot, self.approval_reasons, self.condition_reasons)

    def _approvals(self, snapshot: Snapshot, claimed: set[str], waited: bool = True) -> list[Item]:
        items = []
        behind = behind_lookup(snapshot)
        for t in snapshot.tasks:
            if t.status is not Status.BLOCKED or t.block is None:
                continue
            if t.id in claimed:
                continue  # it waits on a human task directly: listed under that task
            if not self._asks_a_person(t, snapshot):  # a person's own task is not one
                continue
            age = max(snapshot.now - t.block.since, timedelta(0))
            if waited and age < self.approval_wait_after:
                continue
            hours = age.total_seconds() / 3600
            held = tuple(behind(t.id))
            items.append(Item("approval", t, (t.id,), age, hours, {t.id: age}, held=held))
        return items

    def _approver(self, task: Task, roles: Mapping[str, str]) -> str | None:
        role = task.role or (roles.get(task.owner_id) if task.owner_id else None)
        return (self.approvers.get(role) if role else None) or self.default_approver

    def _escalate(
        self, person: str, items: list[Item], now: datetime, snoozed: Sequence[Item] = ()
    ) -> Action:
        items = sorted(items, key=lambda i: (-i.cost_hours, i.task.id))
        snoozed = sorted(snoozed, key=lambda i: (_until(i, now), i.task.id))
        total = _total(items)
        # held: the tasks waiting on the person and those behind them, as the items count
        blocked = sorted({tid for i in items for tid in (*i.behind, *i.held)})
        waiting = sorted({tid for i in items for tid in i.waits})
        planned = sorted({tid for i in items for tid in i.planned} - set(waiting))
        since = now - max(i.waiting for i in items)
        severity = Severity.CRITICAL if total >= self.critical_after_hours else Severity.WARNING
        listed = "; ".join(_described(i) for i in items[:_SHOWN])
        more = f"; and {len(items) - _SHOWN} more" if len(items) > _SHOWN else ""
        who = "Nobody is assigned to approve these" if person == UNASSIGNED else person
        aside = f" Snoozed, not counted: {len(snoozed)} item(s)." if snoozed else ""
        return Action(
            ActionKind.ESCALATE,
            NAME,
            f"Needs you ({who}): {len(items)} item(s) hold {len(blocked)} agent task(s), "
            f"{total:.1f} agent-hours waited so far. By cost of delay: {listed}{more}.{aside}",
            severity=severity,
            key=f"{NAME}:{person}:{','.join(sorted(i.task.id for i in items))}",
            state=f"{NAME}:{person}",
            since=since,
            params={
                "escalate_to": None if person == UNASSIGNED else person,
                "human_tasks": [i.task.id for i in items if i.kind == "human_task"],
                "approvals": [i.task.id for i in items if i.kind == "approval"],
                "blocked_agent_tasks": len(blocked),
                # agent tasks waiting on the person right now: the report integrates their
                # count over the day as agent-hours lost waiting on people
                "waiting_agent_tasks": len(waiting),
                "waiting": waiting[: _SHOWN * 4],
                # planned work behind the person's tasks: not waiting, reported apart
                PLANNED_AGENT_TASKS: len(planned),
                PLANNED_TASKS: planned[: _SHOWN * 4],
                "cost_hours": round(total, 1),
                # The alert's layout reads task_id, title, needed, since and blocking; the
                # daily report reads task (and the ledger holds the rest as it always has).
                "items_total": len(items),
                "items": [
                    {
                        "kind": i.kind,
                        "task_id": i.task.id,
                        "task": i.task.id,
                        "title": i.task.title,
                        "needed": i.needed,
                        "since": (now - i.waiting).isoformat(),
                        "blocking": i.blocking,
                        "behind": list(i.behind[: _SHOWN * 2]),
                        "waiting_minutes": int(i.waiting.total_seconds() // 60),
                        "cost_hours": round(i.cost_hours, 1),
                        **({"snooze": i.snooze.params()} if i.snooze is not None else {}),
                        # what a chat snooze button would hold (#176): not a broken block
                        **({} if i.broken else {"snooze_task": i.task.id}),
                    }
                    for i in items[:MAX_ITEMS]
                ],
                # Items a snooze holds: out of everything above, listed in the footer (#176)
                "snoozed_total": len(snoozed),
                "snoozed": [
                    {
                        "kind": i.kind,
                        "task_id": i.task.id,
                        "title": i.task.title,
                        "since": (now - i.waiting).isoformat(),
                        "blocking": i.blocking,
                        # the agent tasks parked on it, as ``waiting`` counts them (the report
                        # reads them as waiting on purpose)
                        "waiting": sorted(i.waits)[: _SHOWN * 2],
                        "snooze": i.snooze.params(),
                    }
                    for i in snoozed[:MAX_ITEMS]
                    if i.snooze is not None
                ],
            },
        )


def _described(item: Item) -> str:
    """An item for the reason, with why it is back when its snooze no longer holds it."""
    return item.describe() + (f" ({item.snooze.text()})" if item.snooze is not None else "")


def _snoozed_text(item: Item) -> str:
    return describe(item.snooze.snooze, item.task.title) if item.snooze else item.task.id


def _until(item: Item, now: datetime) -> datetime:
    return item.snooze.snooze.until if item.snooze is not None else now


def parked_on(snapshot: Snapshot, settings: NeedsYou, task_id: str) -> tuple[str, ...]:
    """The human tasks the inbox lists ``task_id`` under, when the task is only parked
    behind them and is not an item itself (time thresholds ignored); else empty. A snooze
    on such a task holds nothing: ``kelpie snooze`` says to snooze the human task."""
    inbox = settings._inbox(snapshot, waited=False)
    items = [i for found in inbox.values() for i in found]
    if any(i.task.id == task_id for i in items):
        return ()
    return tuple(sorted({i.task.id for i in items if task_id in i.waits}))


def inbox_task_ids(snapshot: Snapshot, settings: NeedsYou) -> frozenset[str]:
    """The tasks the inbox lists, with ``settings``' reasons and labels and its time
    thresholds ignored: its items (approvals and human tasks) and the tasks waiting on a
    listed human task. Rules 4 and 2 leave exactly these to this rule (with their own
    thresholds), so every block and wait reaches a person through one rule and none through two.
    Pure: the same code path as :meth:`NeedsYou.evaluate`."""
    inbox = settings._inbox(snapshot, waited=False)
    return frozenset(
        tid for items in inbox.values() for i in items for tid in (i.task.id, *i.waits)
    )


def digest_notice(snapshot: Snapshot, settings: NeedsYou, task_id: str, noun: str) -> str:
    """The resolve notice of a block or wait (``noun``) that rule 13 lists now: where, and,
    when this rule holds what it is listed under (the human task, or the task itself when it
    is an item) in the digest's footer, that the digest does not announce it yet. Held is
    decided by the digest's own split (:meth:`NeedsYou._split`): an item whose block can never
    clear is live even when snoozed."""
    under = parked_on(snapshot, settings, task_id)
    where = f", under {', '.join(under)}" if under else ""
    split = settings._split(snapshot, settings._inbox(snapshot, waited=False))

    def about(item: Item) -> bool:
        return item.task.id == task_id or task_id in item.waits

    live = any(about(i) for items, _ in split.values() for i in items)
    held = any(about(i) for _, items in split.values() for i in items)
    if held and not live:
        return (
            f"Task {task_id} is snoozed in the person's needs-you digest{where}: its footer "
            f"lists it, and the digest announces the {noun} again when the snooze ends or "
            "more work is blocked behind it."
        )
    return (
        f"Task {task_id} is in the person's needs-you digest now{where}: that digest "
        f"announces the {noun} from here on."
    )


def hand_off(value: Any) -> NeedsYou | None:
    """The ``needs_you`` setting of a rule that leaves tasks to this one (rules 2 and 4):
    false (this rule does not reach people: the rule keeps all its alerts), true (this
    rule's defaults) or this rule's config section, which :func:`build` validates."""
    if value is False:
        return None
    if value is True:
        return build({})
    if isinstance(value, Mapping):
        return build(dict(value))
    raise ValueError("needs_you must be false, true or rule 13's settings")


def _broken(task: Task, unmet: Sequence[Condition], snapshot: Snapshot) -> str | None:
    """Why a person's blocked task can never clear on its own, in rule 4's words: a
    condition that can never be met, or one Kelpie cannot see. Rule 4 sends neither for a
    task in the digest, so the digest says it."""
    if task.block is None or task.status not in _FROZEN:
        return None
    missing = linked_conditions(task, snapshot)[1]
    if missing is not None:
        return f"the block names condition {missing}, which Kelpie cannot see"
    ended = dead_ends(unmet, task.block.since)
    return f"{dead_end_text(ended)}: the block will never clear on its own" if ended else None


def _direct_waiters(
    snapshot: Snapshot, unmet: Mapping[str, Sequence[Condition]]
) -> dict[str, dict[str, str]]:
    """For each task, the tasks that wait on it directly, by how: ``"condition"`` for a
    parked task whose current wait (``unmet``, by parked task) is a condition naming it
    (``human_task``, or the ``task_status`` of a dispatch gate on its parent),
    ``"dependency"`` for a task that depends on it. One pass over the snapshot."""
    found: dict[str, dict[str, str]] = {}
    for d in snapshot.dependencies:
        found.setdefault(d.depends_on, {})[d.task_id] = "dependency"
    for tid, conditions in unmet.items():
        for c in conditions:
            target = c.params.get("task")
            if isinstance(target, str):
                found.setdefault(target, {})[tid] = "condition"
    return found


def _waiting(
    task: Task, now: datetime, direct: Mapping[str, str], parked: set[str]
) -> timedelta | None:
    """How long an agent task has waited on the human task: a task ``parked`` on it since
    it was parked; a ready or backlog task that depends on it directly since its last
    status change (platforms keep dependents in the backlog until their parents finish).
    None for everything else behind it, deeper in the chain, in progress, or parked for
    another reason (CI, a provider): it is held, but its wait is not this human task's
    cost."""
    if task.id in parked:
        since = task.block.since if task.block is not None else task.status_since
    elif direct.get(task.id) == "dependency" and task.status in {Status.READY, Status.BACKLOG}:
        since = task.status_since
    else:
        return None
    return max(now - since, timedelta(0))


def _stuck_on_its_own(task: Task, snapshot: Snapshot, open_ids: set[str]) -> bool:
    """A blocked or waiting human task with nothing open before it."""
    return task.status in _FROZEN and not any(
        d.task_id == task.id and d.depends_on in open_ids for d in snapshot.dependencies
    )


def _total(items: Sequence[Item]) -> float:
    """A person's cost of delay: each waiting agent task once, with its longest wait (a
    task behind two of their human tasks is not counted twice)."""
    longest: dict[str, timedelta] = {}
    for item in items:
        for tid, w in item.waits.items():
            longest[tid] = max(w, longest.get(tid, w))
    return sum(w.total_seconds() for w in longest.values()) / 3600


def _stopped(task: Task) -> bool:
    return task.block is not None and task.block.kelpie is KelpieMark.STOP


def _duration(value: Any, what: str) -> timedelta:
    try:
        found = parse_duration(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{what}: {exc}") from exc
    if found <= timedelta(0):
        raise ValueError(f"{what} must be positive")
    return found


@register(NAME)
def build(config: dict[str, Any]) -> NeedsYou:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    for key in ("human_wait_alert_after", "approval_wait_after"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    if "approval_reasons" in config:
        kwargs["approval_reasons"] = approval_reasons_list(config.pop("approval_reasons"))
    if "approvers" in config:
        approvers = config.pop("approvers")
        if not isinstance(approvers, Mapping) or not all(
            isinstance(k, str) and k and isinstance(v, str) and v.strip()
            for k, v in approvers.items()
        ):
            raise ValueError("approvers must map agent roles to human owner ids")
        kwargs["approvers"] = {k: v.strip() for k, v in approvers.items()}
    if "default_approver" in config:
        default = config.pop("default_approver")
        if not isinstance(default, str) or not default.strip():
            raise ValueError("default_approver must be a human owner id")
        kwargs["default_approver"] = default.strip()
    if "condition_reasons" in config:
        kwargs["condition_reasons"] = condition_patterns(config.pop("condition_reasons"))
    if "planned_labels" in config:
        kwargs["planned_labels"] = frozenset(
            labels_list(config.pop("planned_labels"), "planned_labels")
        )
    if "critical_after_hours" in config:
        hours = config.pop("critical_after_hours")
        if isinstance(hours, bool) or not isinstance(hours, int | float) or hours <= 0:
            raise ValueError("critical_after_hours must be a positive number")
        kwargs["critical_after_hours"] = float(hours)
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return NeedsYou(**kwargs)
