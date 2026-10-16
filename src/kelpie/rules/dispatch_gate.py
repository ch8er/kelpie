"""Dispatch gate — do not start work whose prerequisite is not met (week 3, field evidence).

A ready task that depends on something not yet true wastes a whole session if an agent picks
it up. Field case: an agent was about to redo a migration while the PR it depended on was
still red. The gate parks such a task in ``waiting`` on its prerequisites instead, and rule 2
(``wait_condition``, which must run in act mode too) wakes it when they hold.

Prerequisites of a ready task nobody is working on yet (no live session):

- **Parent tasks** (``snapshot.dependencies``) that are not finished, only with
  ``gate_on_parents`` (default off). Dispatchers normally do not hand out a task with open
  parents, and rule 6 treats such a task as not runnable yet; parking it would only freeze
  the chain behind it and hide rule 6's critical-path alert. Turn it on for a dispatcher
  that ignores dependencies. Each open parent gets a ``task_status`` condition waiting for
  ``done``; a parent on a dependency cycle with the task never gates it. When every parent
  ended without all being done, left the board, or is no longer a parent, this rule wakes
  the task itself (the conditions carry ``terminal_ok``, so rule 2 does not call that a
  dead end). Known limit: a task parked on a parent and a PR condition together is not
  released this way when the parent ends without being done; rule 2 alerts that its wait
  can never be met, and a human moves it back to ready.
- **Conditions linked to the task** by ``task_id`` for its current status (as
  ``linked_conditions`` reads them): those the platform registered, and those the GitHub
  step attaches from PR references in the task's title and body (``[[block_conditions]]`` with
  ``applies_to = ["ready_task"]``), bound to the PR's repo, number and head like block
  conditions (``pr_merged`` when the task needs the PR landed, ``ci_green`` when it needs
  its head green). Only an unmet condition checked within ``max_condition_age`` counts.

A task whose prerequisites cannot be resolved is left alone: a condition that is unknown
(``satisfied`` None), stale, or can never be met (``params.terminal``, rules 2 and 4 alert
on those) never gates it, and neither does a parent that is not on the board. PR conditions
derived from text are passed on without the head ``sha`` they were checked at, so the
condition follows the PR's current head and a fixing push wakes the task. Once woken, a
task is not gated again on the same PR reference (its condition id is kept by the adapter):
a human who moves a gated task back to ready overrides the gate. The ``wait``
action carries every unmet prerequisite and is sent once per stay in ready; it needs an
adapter with ``SupportsWait``, and the engine tells a human otherwise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import Condition, ConditionKind, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._tasks import DEFAULT_MAX_CONDITION_AGE, is_live, linked_conditions

NAME = "dispatch_gate"
_SHOWN = 5


@dataclass(frozen=True, slots=True)
class DispatchGate:
    max_condition_age: timedelta = DEFAULT_MAX_CONDITION_AGE
    gate_on_parents: bool = False  # for dispatchers that hand out tasks with open parents
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        by_id = {t.id: t for t in snapshot.tasks}
        parents: dict[str, set[str]] = {}
        for d in snapshot.dependencies:
            parents.setdefault(d.task_id, set()).add(d.depends_on)
        live = {s.task_id for s in snapshot.sessions if is_live(s, snapshot.now)}
        actions: list[Action] = []
        for task in snapshot.tasks:
            if task.status is not Status.READY or task.id in live:
                continue
            open_parents = (
                sorted(
                    p
                    for p in parents.get(task.id, ())
                    if p in by_id
                    and not by_id[p].status.is_terminal
                    and task.id not in _ancestors(p, parents)  # a cycle: never parks
                )
                if self.gate_on_parents
                else []
            )
            unmet = [c for c in linked_conditions(task, snapshot)[0] if self._unmet(c, snapshot)]
            if open_parents or unmet:
                actions.append(self._park(task, open_parents, unmet, by_id))
        actions.extend(self._release(snapshot, by_id, parents))
        return actions

    def _release(
        self, snapshot: Snapshot, by_id: dict[str, Task], parents: dict[str, set[str]]
    ) -> list[Action]:
        """Wake a task parked only on parents once none of them still gates it (ended,
        gone from the board, or no longer a parent) without all being done; rule 2 wakes
        it when all are done."""
        actions: list[Action] = []
        for task in snapshot.tasks:
            if task.status is not Status.WAITING:
                continue
            conditions, _ = linked_conditions(task, snapshot)
            prefix = f"{NAME}:{task.id}:"
            if not conditions or not all(c.id.startswith(prefix) for c in conditions):
                continue
            targets = [str(c.params.get("task")) for c in conditions]
            still = [
                t
                for t in targets
                if t in by_id and not by_id[t].status.is_terminal and t in parents.get(task.id, ())
            ]
            if still:
                continue
            if all(t in by_id and by_id[t].status is Status.DONE for t in targets):
                continue  # rule 2's: every condition holds
            actions.append(
                Action(
                    ActionKind.WAKE,
                    NAME,
                    f"Task {task.id} no longer waits on a parent: {_names(targets)} ended, left "
                    "the board or are no longer its parents. Waking it.",
                    task_id=task.id,
                    key=f"{NAME}:release:{task.id}:{task.status_since.isoformat()}",
                    params={"waited_for": targets},
                )
            )
        return actions

    def _unmet(self, condition: Condition, snapshot: Snapshot) -> bool:
        checked = condition.checked_at
        return (
            condition.satisfied is False
            and condition.params.get("terminal") is not True
            and checked is not None
            and snapshot.now - checked <= self.max_condition_age
        )

    def _park(
        self,
        task: Task,
        open_parents: list[str],
        unmet: list[Condition],
        by_id: dict[str, Task],
    ) -> Action:
        conditions: list[dict[str, Any]] = [
            {"id": c.id, "kind": c.kind.value, "params": _clean(c)} for c in unmet
        ]
        conditions += [
            {
                "id": f"{NAME}:{task.id}:{p}",
                "kind": ConditionKind.TASK_STATUS.value,
                "params": {
                    "task": p,
                    "status": Status.DONE.value,
                    "source": NAME,
                    "terminal_ok": True,  # this rule releases the task if p ends otherwise
                },
            }
            for p in open_parents
        ]
        why = [_describe(c) for c in unmet]
        why += [f"parent {p} is {by_id[p].status}" for p in open_parents]
        more = len(why) - _SHOWN
        text = "; ".join(why[:_SHOWN]) + (f"; +{more} more" if more > 0 else "")
        return Action(
            ActionKind.WAIT,
            NAME,
            f"Task {task.id} is ready but its prerequisites are not met: {text}. Parking it in "
            "waiting so no agent session is spent on it; it is woken when they hold.",
            task_id=task.id,
            key=f"{NAME}:{task.id}:{task.status_since.isoformat()}",
            once=True,
            params={
                "conditions": conditions,
                "open_parents": open_parents,
                "unmet_conditions": [c.id for c in unmet],
            },
        )


# Keys connectors write into ``Condition.params`` as results of a check; a condition the
# adapter registers again starts without them. ``sha`` stays on conditions the platform
# registered (their own head binding) and is dropped from those derived from text (below).
_RESULTS = frozenset({"error", "terminal", "observed_status", "pr_state", "ci_state"})
# Conditions the GitHub step derives from text are rebuilt every tick for the PR's current
# head; re-registered with ``sha`` they would stay pinned to the commit seen when parked,
# and a fixing push would never wake the task.
_DERIVED = frozenset({"task_text", "block_reason"})
_HUMAN_RESULTS = frozenset({"status", "owner", "task_left_board"})  # human_tasks connector


def _clean(c: Condition) -> dict[str, Any]:
    drop = _RESULTS | (_HUMAN_RESULTS if c.kind is ConditionKind.HUMAN_TASK else frozenset())
    if c.params.get("source") in _DERIVED:
        drop |= {"sha"}
    return {k: v for k, v in c.params.items() if k not in drop}


def _ancestors(task_id: str, parents: dict[str, set[str]]) -> set[str]:
    """Every task ``task_id`` waits for, transitively."""
    seen: set[str] = set()
    todo = [task_id]
    while todo:
        for p in parents.get(todo.pop(), ()):
            if p not in seen:
                seen.add(p)
                todo.append(p)
    return seen


def _names(names: Sequence[str]) -> str:
    more = len(names) - _SHOWN
    return ", ".join(names[:_SHOWN]) + (f" +{more} more" if more > 0 else "")


def _describe(c: Condition) -> str:
    repo, pr = c.params.get("repo"), c.params.get("pr")
    ref = f"{repo}#{pr}" if repo and pr else c.id
    if c.kind is ConditionKind.PR_MERGED:
        return f"PR {ref} is not merged"
    if c.kind is ConditionKind.CI_STATUS:
        return f"CI on PR {ref} is not green"
    if c.kind is ConditionKind.HUMAN_TASK:
        return f"human task {c.params.get('task')} is not done"
    if c.kind is ConditionKind.TASK_STATUS:
        return f"task {c.params.get('task')} is not {c.params.get('status', 'done')}"
    return f"{c.id} ({c.kind}) is not met"


@register(NAME)
def build(config: dict[str, Any]) -> DispatchGate:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "max_condition_age" in config:
        value = config.pop("max_condition_age")
        try:
            age = parse_duration(value)
        except (AttributeError, TypeError) as exc:
            raise ValueError(f"max_condition_age: invalid duration {value!r}") from exc
        if age <= timedelta(0):
            raise ValueError("max_condition_age must be positive")
        kwargs["max_condition_age"] = age
    if "gate_on_parents" in config:
        value = config.pop("gate_on_parents")
        if not isinstance(value, bool):
            raise ValueError("gate_on_parents must be true or false")
        kwargs["gate_on_parents"] = value
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return DispatchGate(**kwargs)
