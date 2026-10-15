"""Rule 12 — early conflict warning (incident 7).

46 of 63 task returns on the farm were merge conflicts in a few shared files: two live tasks
edited the same files and only found out at merge, so one of them redid its work. This rule
warns while both tasks are still in flight.

A task is *live* while its change is unmerged: in progress, in review, accepted, blocked or
waiting. Its files are ``Task.files`` as the adapter reports them (paths are compared after
dropping a leading ``./``). No adapter reports them yet (Hermes gives only a count of
changed files), so on a live farm the rule stays silent until one does. When two live tasks
share files (after dropping files matching ``expected_shared``, glob patterns of files that
are meant to be edited by many tasks, such as a changelog), each gets one ``comment`` per
other task, naming the files they share then. It is sent once per pair (``once``): files
added later, or a third task leaving, send nothing new; a new pair does.

With ``serialize`` (default off) the later task is also held until the earlier ones are
done ("done" stands for merged, as on the board): a ``wait`` action parks it in ``waiting``
on a ``task_status`` condition per earlier task, and rule 2 (``wait_condition``, which must
then run in act mode too) wakes it when they are all done. To rule out mutual waits, only
a task in progress is parked, and only behind tasks that are moving (in progress, review or
accepted, not blocked or waiting) and that do not depend on it. A task's start is its first
session, else when it entered its current status; ties go by task id. When every task a
parked task waits for has left the live set without all of them being done (cancelled,
failed, requeued, or gone from the board), the conflict is gone but rule 2 cannot wake it:
this rule wakes it instead. ``wait`` needs an adapter with ``SupportsWait``; without one the
engine tells a human.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from fnmatch import fnmatchcase
from typing import Any

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import ConditionKind, Snapshot, Status, Task
from kelpie.rules import register
from kelpie.rules._tasks import linked_conditions

NAME = "conflict_warning"

LIVE = frozenset(
    {Status.IN_PROGRESS, Status.REVIEW, Status.ACCEPTED, Status.BLOCKED, Status.WAITING}
)
MOVING = frozenset({Status.IN_PROGRESS, Status.REVIEW, Status.ACCEPTED})
_SHOWN = 5  # files named in one message
_PREFIX = f"{NAME}:"  # condition ids this rule creates


@dataclass(frozen=True, slots=True)
class ConflictWarning:
    expected_shared: tuple[str, ...] = ()  # glob patterns; ``*`` also matches ``/``
    serialize: bool = False
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        by_id = {t.id: t for t in snapshot.tasks}
        overlaps = self.overlaps(snapshot)
        started = _starts(snapshot)
        actions: list[Action] = []
        for tid in sorted(overlaps):
            others = overlaps[tid]
            actions.extend(
                self._comment(by_id[tid], o, files, by_id) for o, files in others.items()
            )
            if self.serialize:
                actions.extend(self._park(by_id[tid], others, by_id, started, snapshot))
        actions.extend(self._release(snapshot, by_id))
        return actions

    def overlaps(self, snapshot: Snapshot) -> dict[str, dict[str, tuple[str, ...]]]:
        """For each live task: the other live tasks it shares files with, and those files."""
        by_file: dict[str, set[str]] = {}
        for t in snapshot.tasks:
            if t.status not in LIVE:
                continue
            for f in {_normalized(f) for f in t.files}:
                if f and not self._shared(f):
                    by_file.setdefault(f, set()).add(t.id)
        pairs: dict[str, dict[str, set[str]]] = {}
        for f, ids in by_file.items():
            if len(ids) < 2:
                continue
            for a in ids:
                for b in ids:
                    if a != b:
                        pairs.setdefault(a, {}).setdefault(b, set()).add(f)
        return {
            a: {b: tuple(sorted(files)) for b, files in sorted(others.items())}
            for a, others in pairs.items()
        }

    def _shared(self, path: str) -> bool:
        return any(fnmatchcase(path, pattern) for pattern in self.expected_shared)

    def _comment(
        self, task: Task, other: str, files: tuple[str, ...], by_id: dict[str, Task]
    ) -> Action:
        o = by_id[other]
        return Action(
            ActionKind.COMMENT,
            NAME,
            f"Heads-up: live task {other} ('{o.title}', {o.status}) changes the same files "
            f"as this one: {_names(files)}. Expect a merge conflict: coordinate with it, "
            "keep your edits to these files small, and rebase as soon as one of you merges.",
            task_id=task.id,
            key=f"{NAME}:{task.id}:{other}",  # once per pair, whatever the files become
            once=True,
            params={"conflicting_task": other, "files": list(files)},
        )

    def _park(
        self,
        task: Task,
        others: dict[str, tuple[str, ...]],
        by_id: dict[str, Task],
        started: dict[str, datetime],
        snapshot: Snapshot,
    ) -> list[Action]:
        if task.status is not Status.IN_PROGRESS:
            return []
        mine = (started.get(task.id, task.status_since), task.id)
        needs_me = _depends_on(snapshot, task.id)
        earlier = sorted(
            o
            for o in others
            if by_id[o].status in MOVING
            and o not in needs_me
            and (started.get(o, by_id[o].status_since), o) < mine
        )
        if not earlier:
            return []
        conditions: list[dict[str, Any]] = [
            {
                "id": f"{_PREFIX}{task.id}:{o}",
                "kind": ConditionKind.TASK_STATUS.value,
                "params": {"task": o, "status": Status.DONE.value, "source": NAME},
            }
            for o in earlier
        ]
        files = sorted({f for o in earlier for f in others[o]})
        return [
            Action(
                ActionKind.WAIT,
                NAME,
                f"Holding {task.id} until {_names(earlier)} (started earlier) are done: they "
                f"change the same files ({_names(files)}).",
                task_id=task.id,
                key=f"{NAME}:wait:{task.id}:{','.join(earlier)}:{task.status_since.isoformat()}",
                once=True,
                params={"conditions": conditions, "files": files, "waits_for": earlier},
            )
        ]

    def _release(self, snapshot: Snapshot, by_id: dict[str, Task]) -> list[Action]:
        """Wake tasks this rule parked once every task they wait for has left the live set
        without all being done (rule 2 wakes them when all are done)."""
        actions: list[Action] = []
        for task in snapshot.tasks:
            if task.status is not Status.WAITING:
                continue
            conditions, _ = linked_conditions(task, snapshot)
            if not conditions or not all(c.id.startswith(_PREFIX) for c in conditions):
                continue  # not (only) our wait: rule 2's business
            targets = [c.params.get("task") for c in conditions]
            ended = [by_id.get(t) if isinstance(t, str) else None for t in targets]
            if any(t is not None and t.status in LIVE for t in ended):
                continue  # still a conflict
            if all(t is not None and t.status is Status.DONE for t in ended):
                continue  # all done: rule 2 wakes it on fresh evidence
            gone = ", ".join(
                f"{tid} ({t.status if t is not None else 'gone'})"
                for tid, t in zip(targets, ended, strict=True)
            )
            actions.append(
                Action(
                    ActionKind.WAKE,
                    NAME,
                    f"The tasks {task.id} was held behind are no longer in flight: {gone}. "
                    "The conflict is gone; waking it.",
                    task_id=task.id,
                    key=f"{NAME}:release:{task.id}:{task.status_since.isoformat()}",
                    params={"waited_for": [str(t) for t in targets]},
                )
            )
        return actions


def _depends_on(snapshot: Snapshot, task_id: str) -> set[str]:
    """Open tasks that wait for ``task_id``, transitively, through dependencies."""
    successors: dict[str, set[str]] = {}
    for d in snapshot.dependencies:
        successors.setdefault(d.depends_on, set()).add(d.task_id)
    seen: set[str] = set()
    todo = [task_id]
    while todo:
        for nxt in successors.get(todo.pop(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    return seen


def _starts(snapshot: Snapshot) -> dict[str, datetime]:
    """When work on each task began: its first session."""
    first: dict[str, datetime] = {}
    for s in snapshot.sessions:
        if s.task_id not in first or s.started_at < first[s.task_id]:
            first[s.task_id] = s.started_at
    return first


def _normalized(path: str) -> str:
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def _names(names: Sequence[str], shown: int = _SHOWN) -> str:
    more = len(names) - shown
    return ", ".join(names[:shown]) + (f" +{more} more" if more > 0 else "")


@register(NAME)
def build(config: dict[str, Any]) -> ConflictWarning:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "expected_shared" in config:
        patterns = config.pop("expected_shared")
        if not isinstance(patterns, list | tuple) or not all(
            isinstance(p, str) and p for p in patterns
        ):
            raise ValueError("expected_shared must be a list of non-empty glob patterns")
        kwargs["expected_shared"] = tuple(patterns)
    if "serialize" in config:
        serialize = config.pop("serialize")
        if not isinstance(serialize, bool):
            raise ValueError("serialize must be true or false")
        kwargs["serialize"] = serialize
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return ConflictWarning(**kwargs)
