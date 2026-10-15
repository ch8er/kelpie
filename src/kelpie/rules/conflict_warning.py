"""Rule 12 — early conflict warning (incident 7).

46 of 63 task returns on the farm were merge conflicts in a few shared files: two live tasks
edited the same files and only found out at merge, so one of them redid its work. This rule
warns while both tasks are still in flight.

A task is *live* while its change is unmerged: in progress, in review, accepted, blocked or
waiting. Its files are ``Task.files`` as the adapter reports them. When two live tasks
share files (after dropping files matching ``expected_shared``, glob patterns of files that
are meant to be edited by many tasks, such as a changelog), each gets one ``comment`` naming
the other tasks and the files they share. The comment is sent once per set of overlaps
(``once``); a new overlap sends a new one.

With ``serialize`` (default off) the later task is also held until the earlier ones are
done: a ``wait`` action parks it in ``waiting`` on a ``task_status`` condition per earlier
task, and rule 2 (``wait_condition``) wakes it when they are. Only a task in progress is
parked (a task in review or later is past editing), and only behind tasks that started
earlier. A task's start is its first session, else when it entered its current status.
``wait`` needs an adapter with ``SupportsWait``; without one the engine tells a human.
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

NAME = "conflict_warning"

LIVE = frozenset(
    {Status.IN_PROGRESS, Status.REVIEW, Status.ACCEPTED, Status.BLOCKED, Status.WAITING}
)
_SHOWN = 5  # files and tasks named in one message


@dataclass(frozen=True, slots=True)
class ConflictWarning:
    expected_shared: tuple[str, ...] = ()  # glob patterns; ``*`` also matches ``/``
    serialize: bool = False
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        overlaps = self.overlaps(snapshot)
        if not overlaps:
            return []
        by_id = {t.id: t for t in snapshot.tasks}
        started = _starts(snapshot)
        actions: list[Action] = []
        for tid in sorted(overlaps):
            others = overlaps[tid]
            actions.append(self._comment(by_id[tid], others, by_id))
            if self.serialize:
                actions.extend(self._park(by_id[tid], others, by_id, started))
        return actions

    def overlaps(self, snapshot: Snapshot) -> dict[str, dict[str, tuple[str, ...]]]:
        """For each live task: the other live tasks it shares files with, and those files."""
        by_file: dict[str, set[str]] = {}
        for t in snapshot.tasks:
            if t.status not in LIVE:
                continue
            for f in set(t.files):
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
        self, task: Task, others: dict[str, tuple[str, ...]], by_id: dict[str, Task]
    ) -> Action:
        listed = "; ".join(
            f"{other} ('{by_id[other].title}', {by_id[other].status}) changes {_names(files)}"
            for other, files in list(others.items())[:_SHOWN]
        )
        more = len(others) - _SHOWN
        listed += f"; +{more} more task(s)" if more > 0 else ""
        files = sorted({f for fs in others.values() for f in fs})
        signature = ";".join(f"{o}={','.join(fs)}" for o, fs in others.items())
        return Action(
            ActionKind.COMMENT,
            NAME,
            f"Heads-up: other live tasks change the same files as this one: {listed}. "
            "Expect a merge conflict: coordinate with them, keep your edits to these files "
            "small, and rebase as soon as one of them merges.",
            task_id=task.id,
            key=f"{NAME}:{task.id}:{signature}",
            once=True,
            params={"conflicting_tasks": list(others), "files": files},
        )

    def _park(
        self,
        task: Task,
        others: dict[str, tuple[str, ...]],
        by_id: dict[str, Task],
        started: dict[str, datetime],
    ) -> list[Action]:
        if task.status is not Status.IN_PROGRESS:
            return []
        mine = started.get(task.id, task.status_since)
        earlier = sorted(
            o for o in others if (started.get(o, by_id[o].status_since), o) < (mine, task.id)
        )
        if not earlier:
            return []
        conditions: list[dict[str, Any]] = [
            {
                "id": f"{NAME}:{task.id}:{o}",
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
                f"change the same files ({_names(files)}). Rule 2 wakes it when they are.",
                task_id=task.id,
                key=f"{NAME}:wait:{task.id}:{','.join(earlier)}",
                params={"conditions": conditions, "files": files, "waits_for": earlier},
            )
        ]


def _starts(snapshot: Snapshot) -> dict[str, datetime]:
    """When work on each task began: its first session."""
    first: dict[str, datetime] = {}
    for s in snapshot.sessions:
        if s.task_id not in first or s.started_at < first[s.task_id]:
            first[s.task_id] = s.started_at
    return first


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
