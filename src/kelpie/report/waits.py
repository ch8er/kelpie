"""What the farm's tasks waited for: the cause of each task's time in the ``waiting`` stage
(:mod:`kelpie.report.throughput`), and the day's biggest wait with a concrete suggestion.

**Cause of a task's wait**, the first that applies:

1. ``hold``: the task carries a hold label (rule 6's ``hold_labels`` and ``holds``), or a
   rule 6 hold alert of the day listed it as held;
2. ``people``: a rule 13 alert of the day listed it as waiting on a person (an approval, a
   human task it depends on), or it waits on a ``human_task`` condition;
3. the kind of the condition it waits on: ``ci`` (``ci_status``), ``merge``
   (``pr_merged``), ``quota`` (``quota_reset``), ``tasks`` (``task_status``), ``timer``
   (``time``), ``other`` (``custom``). The condition its block names wins, then its
   unsatisfied conditions, then the latest registered;
4. else ``unknown``: the task waited without telling Kelpie what for.

The board only says what a task waits for now (or last), so the cause is the task's latest
known one and all its waiting time of the day counts under it. Pure and deterministic.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass

from kelpie.core.ledger import LedgerEntry
from kelpie.core.model import Condition, ConditionKind, Task
from kelpie.report import md
from kelpie.report.throughput import RoleThroughput

UNKNOWN = "unknown"
# What a cause is called in "waiting for ...".
WAIT_CAUSES: Mapping[str, str] = {
    "ci": "CI",
    "merge": "merges",
    "people": "people (approvals, human tasks)",
    "hold": "holds",
    "quota": "LLM quotas",
    "tasks": "other tasks",
    "timer": "timers",
    "other": "custom conditions",
    UNKNOWN: "an unknown cause (no condition registered)",
}
SUGGESTIONS: Mapping[str, str] = {
    "ci": "add CI capacity where the tasks queue (another runner for that pipeline), run only "
    "the checks a change needs, or let fewer tasks of the role wait on CI at once",
    "merge": "merge sooner: auto-merge once checks and approvals are in, or batch merges",
    "people": "answer the inboxes under Needs a human first, name approvers for agent roles "
    "([engine.rules.needs_you] approvers), and keep human tasks off the critical path",
    "hold": "give every hold an owner and an expiry ([engine.rules.bottlenecks] holds), and "
    "lift a hold when its reason is gone",
    "quota": "spread the role over more providers or models, or raise the quota",
    "tasks": "unblock the tasks they depend on first, or drop the dependency",
    "timer": "shorten the scheduled waits",
    "other": "check what the custom conditions wait for and whether a connector can resolve "
    "them sooner",
    UNKNOWN: "have agents register a condition when they wait (ci_status, human_task, ...), "
    "so Kelpie can tell what they wait for and wake them when it holds",
}
_KINDS: Mapping[ConditionKind, str] = {
    ConditionKind.HUMAN_TASK: "people",
    ConditionKind.CI_STATUS: "ci",
    ConditionKind.PR_MERGED: "merge",
    ConditionKind.QUOTA_RESET: "quota",
    ConditionKind.TASK_STATUS: "tasks",
    ConditionKind.TIME: "timer",
    ConditionKind.CUSTOM: "other",
}
MIN_HOURS = 1.0  # a smaller wait is no headline


@dataclass(frozen=True, slots=True)
class BiggestWait:
    role: str
    cause: str
    hours: float  # task-hours of the role's tasks waiting for the cause
    work_hours: float  # the role's working and review task-hours, for scale
    tasks: tuple[str, ...]
    repo: str | None = None  # for CI and merges: the repository most of them waited on

    @property
    def label(self) -> str:
        return WAIT_CAUSES[self.cause]

    @property
    def suggestion(self) -> str:
        return SUGGESTIONS[self.cause]

    def headline(self) -> str:
        """One line of Markdown-safe text."""
        where = f", mostly on {md.code(self.repo)}" if self.repo else ""
        return (
            f"Biggest wait: {md.text(self.role)} tasks spent {self.hours:.1f} task-hours "
            f"waiting for {self.label} ({len(self.tasks)} task(s){where}), against "
            f"{self.work_hours:.1f} h of work. Suggestion: {self.suggestion}."
        )


class WaitCauses:
    """The cause of each task's wait (see the module docs)."""

    def __init__(
        self,
        conditions: Iterable[Condition],
        entries: Iterable[LedgerEntry],
        hold_labels: Collection[str] = (),
    ) -> None:
        self._labels = frozenset(hold_labels)
        self._by_id: dict[str, Condition] = {}
        self._by_task: dict[str, list[Condition]] = {}
        for c in conditions:
            self._by_id[c.id] = c
            if c.task_id:
                self._by_task.setdefault(c.task_id, []).append(c)
        self._held: set[str] = set()
        self._people: set[str] = set()
        for e in entries:
            if e.rule == "bottlenecks" and e.params.get("hold"):
                self._held.update(_ids(e.params.get("held_tasks")))
            elif e.rule == "needs_you":
                self._people.update(_ids(e.params.get("waiting")))
                for item in _list(e.params.get("items")):
                    if not isinstance(item, Mapping):
                        continue
                    self._people.update(_ids(item.get("behind")))
                    if item.get("kind") == "approval":
                        self._people.update(_ids([item.get("task")]))

    def __call__(self, task: Task) -> str:
        if task.id in self._held or self._labels.intersection(task.labels):
            return "hold"
        if task.id in self._people:
            return "people"
        found = self.condition(task)
        return _KINDS.get(found.kind, "other") if found is not None else UNKNOWN

    def condition(self, task: Task) -> Condition | None:
        """The condition the task waits (or last waited) on."""
        if task.block is not None and task.block.condition_id in self._by_id:
            return self._by_id[task.block.condition_id]
        mine = self._by_task.get(task.id)
        if not mine:
            return None
        return max(
            mine,
            key=lambda c: (c.satisfied is not True, c.registered_at is not None, c.registered_at
                           or task.status_since, c.id),
        )  # fmt: skip


def biggest_wait(
    roles: Sequence[RoleThroughput], tasks: Sequence[Task], causes: WaitCauses
) -> BiggestWait | None:
    """The role and cause with the most waiting task-hours (at least :data:`MIN_HOURS`)."""
    found = [
        (-hours, r.role, cause, r)
        for r in roles
        for cause, hours in r.waits.items()
        if hours >= MIN_HOURS
    ]
    if not found:
        return None
    neg, role, cause, r = min(found, key=lambda x: x[:3])
    hours = -neg
    waited = tuple(sorted(r.waited.get(cause, ())))
    repos: Counter[str] = Counter()
    if cause in ("ci", "merge"):
        by_id = {t.id: t for t in tasks}
        for tid in waited:
            c = causes.condition(by_id[tid]) if tid in by_id else None
            repo = c.params.get("repo") if c is not None else None
            if isinstance(repo, str) and repo:
                repos[repo] += 1
    repo = min(repos, key=lambda x: (-repos[x], x)) if repos else None
    work = r.stages.get("working", 0.0) + r.stages.get("review", 0.0)
    return BiggestWait(role, cause, hours, work, waited, repo)


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _ids(value: object) -> list[str]:
    return [x for x in _list(value) if isinstance(x, str) and x]
