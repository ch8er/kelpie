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
  to get past Hermes' block loop guard while they waited for a merge.

**Planned waits are not waiting** (incident 20). A task that is not ready for work for its
own reasons (:func:`~kelpie.rules._tasks.planned`: not started yet, and in a pre-work
status such as Hermes triage or scheduled, or labelled with one of ``planned_labels``, on
the task or a linked PR; they default to rule 6's ``planned_labels`` and hold labels)
waits on nothing it depends on: it is not behind a human task and adds no cost, and a
planned human task is not in the inbox, as rule 6 reads it. A blocked or waiting task was
started: it waits on what it names (an approval, a human task), labels or not. On a live
farm a post-release task behind a person's planned task counted as 143 agent-hours of
waiting on that person.

**Cost of delay** of an item: the agent-hours spent waiting on it so far, the sum over the
agent tasks that wait on it directly of how long each has waited: a task parked on it since
it was parked, and a ready or backlog task that depends on it since its last status change
(platforms keep dependents in the backlog until their parents finish). Parked on it: a
condition names it (``human_task``, a dispatch gate's ``task_status``), or the task depends
on it and nothing else visibly holds it (no other unmet condition; no block reason, one
that names the human task, or one that asks a person). Everything else behind it (deeper
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
report integrates the count over the day as agent-hours waiting on people.

The state's key is the person's list of task ids, so any change to it, an item added or
one done, is announced as "changed" with the new list; an emptied inbox resolves.
``escalate_to`` names the person in the alert and the report; delivery goes to the
configured alert channels like every other alert (no per-person routing yet).

This rule owns escalating human work, per person. Rule 6 keeps the farm-level alert (free
slots and only human tasks ready) and escalates single human gates only with
``escalate_human_gates = true``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.conditions import linked_conditions
from kelpie.core.model import (
    Condition,
    ConditionKind,
    KelpieMark,
    OwnerKind,
    Snapshot,
    Status,
    Task,
)
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._tasks import labels_list
from kelpie.rules.bottlenecks import HumanGate, human_gates, link_pull_requests, with_pr_labels

NAME = "needs_you"
UNASSIGNED = "unassigned"
DEFAULT_APPROVAL_REASONS = ("waiting_human", "needs_input", "approval")
TRANSIENT = re.compile(r"waiting_human:\s*transient\b")
_SHOWN = 5
_FROZEN = frozenset({Status.BLOCKED, Status.WAITING})
MAX_REASON = 2000  # characters of a block reason searched, as [[block_conditions]] does


@dataclass(frozen=True, slots=True)
class Item:
    kind: str  # "human_task" | "approval"
    task: Task
    behind: tuple[str, ...]  # agent tasks waiting on it (the task itself for an approval)
    waiting: timedelta  # how long the longest of them has waited
    cost_hours: float  # agent-hours waited, summed over its waiting agent tasks
    waits: Mapping[str, timedelta] = field(default_factory=dict)  # waiting task -> how long

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
        snapshot = with_pr_labels(snapshot, link_pull_requests(snapshot)[0])  # as rule 6
        roles = {o.id: o.role for o in snapshot.owners}
        inbox: dict[str, list[Item]] = {}
        found, claimed = self._human_tasks(snapshot, human_gates(snapshot, self.planned_labels))
        for item in found:
            inbox.setdefault(item.task.owner_id or UNASSIGNED, []).append(item)
        for item in self._approvals(snapshot, claimed):
            inbox.setdefault(self._approver(item.task, roles) or UNASSIGNED, []).append(item)
        ranked = sorted(inbox.items(), key=lambda kv: (-_total(kv[1]), kv[0]))
        return [self._escalate(person, items, snapshot.now) for person, items in ranked]

    def _human_tasks(
        self, snapshot: Snapshot, gates: list[HumanGate]
    ) -> tuple[list[Item], set[str]]:
        """Human tasks that are the person's to move now, whose agent work has waited long
        enough: ready or in progress with nothing open before them (like rule 6), or
        blocked or waiting themselves (the person chases what blocks them). Also the agent
        tasks parked on a human task (:meth:`_parked_on`), listed or not yet: they are
        that task's, not approvals of their own."""
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
            claimed |= parked
            if not movable:
                continue  # agent work comes first: not the person's bottleneck today
            waits = {
                tid: w
                for tid in gate.behind
                if (w := _waiting(by_id[tid], snapshot.now, direct, parked)) is not None
            }
            if not waits or max(waits.values()) < self.human_wait_alert_after:
                continue  # only follow-up work behind it, or not long enough
            cost = sum(w.total_seconds() for w in waits.values()) / 3600
            items.append(
                Item("human_task", gate.task, gate.behind, max(waits.values()), cost, waits)
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
        visibly holds it (no other ``unmet`` condition linked to it, and no block, or a
        block reason that names the human task, or one that asks a person while the
        person can act on the human task). A reason about something else (CI, a
        provider) or an agent's own question behind a human task nobody can do yet is not
        the human task's."""
        if how == "condition":
            return True
        if unmet:
            return False  # it waits on something else first
        if task.block is None:
            return True
        reason = task.block.reason
        if re.search(rf"(?<![\w-]){re.escape(human_task)}(?![\w-])", reason):
            return True
        return movable and self._question_for_a_person(task, snapshot)

    def _question_for_a_person(self, task: Task, snapshot: Snapshot) -> bool:
        """Whether the task's block asks a person for input or approval: its reason starts
        with an approval reason, and it does not wait on a condition instead (a reason that
        matches ``condition_reasons``, or an unmet linked condition other than a human
        task; one that already holds is waited on no longer). A run Kelpie stopped always
        waits for a person (``Block.kelpie``, verified by the adapter)."""
        if task.block is not None and task.block.kelpie is KelpieMark.STOP:
            return True
        if task.block is None or not _asks_a_person(task.block.reason, self.approval_reasons):
            return False
        if any(p.search(task.block.reason[:MAX_REASON]) for p in self.condition_reasons):
            return False
        return not any(
            c.kind is not ConditionKind.HUMAN_TASK and c.satisfied is not True
            for c in linked_conditions(task, snapshot)[0]
        )

    def _approvals(self, snapshot: Snapshot, claimed: set[str]) -> list[Item]:
        humans = {o.id for o in snapshot.owners if o.kind is OwnerKind.HUMAN}
        items = []
        for t in snapshot.tasks:
            if t.status is not Status.BLOCKED or t.block is None or t.owner_id in humans:
                continue
            if t.id in claimed:
                continue  # it waits on a human task directly: listed under that task
            if not self._question_for_a_person(t, snapshot):
                continue
            waited = snapshot.now - t.block.since
            if waited < self.approval_wait_after:
                continue
            hours = waited.total_seconds() / 3600
            items.append(Item("approval", t, (t.id,), waited, hours, {t.id: waited}))
        return items

    def _approver(self, task: Task, roles: Mapping[str, str]) -> str | None:
        role = task.role or (roles.get(task.owner_id) if task.owner_id else None)
        return (self.approvers.get(role) if role else None) or self.default_approver

    def _escalate(self, person: str, items: list[Item], now: datetime) -> Action:
        items = sorted(items, key=lambda i: (-i.cost_hours, i.task.id))
        total = _total(items)
        blocked = sorted({tid for i in items for tid in i.behind})
        waiting = sorted({tid for i in items for tid in i.waits})
        since = now - max(i.waiting for i in items)
        severity = Severity.CRITICAL if total >= self.critical_after_hours else Severity.WARNING
        listed = "; ".join(i.describe() for i in items[:_SHOWN])
        more = f"; and {len(items) - _SHOWN} more" if len(items) > _SHOWN else ""
        who = "Nobody is assigned to approve these" if person == UNASSIGNED else person
        return Action(
            ActionKind.ESCALATE,
            NAME,
            f"Needs you ({who}): {len(items)} item(s) hold {len(blocked)} agent task(s), "
            f"{total:.1f} agent-hours waited so far. By cost of delay: {listed}{more}.",
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
                "cost_hours": round(total, 1),
                "items": [
                    {
                        "kind": i.kind,
                        "task": i.task.id,
                        "title": i.task.title,
                        "behind": list(i.behind[: _SHOWN * 2]),
                        "waiting_minutes": int(i.waiting.total_seconds() // 60),
                        "cost_hours": round(i.cost_hours, 1),
                    }
                    for i in items[: _SHOWN * 2]
                ],
            },
        )


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


def _asks_a_person(reason: str, prefixes: Sequence[str]) -> bool:
    """The block reason starts with one of ``prefixes`` as a whole word (``waiting_human:``,
    ``needs_input ...``), not as part of a longer one (``approved``, ``needs_input_x``).
    Hermes' ``transient`` blocks wait for the outside world, not for a person."""
    text = reason.strip().lower()
    if TRANSIENT.match(text):
        return False
    return any(re.match(rf"{re.escape(p)}(?![\w-])", text) for p in prefixes)


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
        reasons = config.pop("approval_reasons")
        if (
            not isinstance(reasons, list)
            or not reasons
            or not all(isinstance(r, str) and r.strip() for r in reasons)
        ):
            raise ValueError("approval_reasons must be a non-empty list of strings")
        kwargs["approval_reasons"] = tuple(r.strip().lower() for r in reasons)
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
        patterns = config.pop("condition_reasons")
        if not isinstance(patterns, list) or not all(
            isinstance(p, str) and p.strip() for p in patterns
        ):
            raise ValueError("condition_reasons must be a list of regular expressions")
        try:
            compiled = tuple(re.compile(p, re.IGNORECASE) for p in patterns)
        except re.error as exc:
            raise ValueError(f"condition_reasons: invalid pattern: {exc}") from exc
        if any(p.search("") for p in compiled):
            raise ValueError("condition_reasons: a pattern matches an empty reason (every block)")
        kwargs["condition_reasons"] = compiled
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
