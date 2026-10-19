"""What the farm's tasks waited for: the cause of each task's time in the ``waiting`` stage
(:mod:`kelpie.report.throughput`), and the day's biggest wait with a concrete suggestion.

**Cause of a task's wait**, the first that applies:

1. ``hold``: the task carries a hold label (rule 6's ``hold_labels`` and ``holds``);
2. the kind of an unmet condition it waits on: ``people`` (``human_task``), ``ci``
   (``ci_status``), ``merge`` (``pr_merged``), ``quota`` (``quota_reset``), ``tasks``
   (``task_status``), ``timer`` (``time``), ``other`` (``custom``). The condition its block
   names wins, then its unmet conditions (a CI found red comes before the merge of the
   same PR it holds up; an unchecked CI is not known red, so the merge stays); else a
   ``[[block_conditions]]`` pattern its block reason matches (CI or a merge, by the
   pattern's kind: the report checks no PR). A merge wait is "PRs to pass checks and
   merge", never "merges": offline the report cannot tell a red CI from a slow merge;
3. ``people``: a rule 13 alert of the day listed it as an approval or as parked on a
   person's human task;
4. the kind of its last condition, met by now;
5. else ``unknown``: the task waited without telling Kelpie what for.

**CI outage.** While rule 9's CI outage incident is open (:mod:`kelpie.core.incidents`), the
waiting time it explains counts under ``ci_outage`` instead: tasks its alerts name
(``tasks``), and tasks waiting on CI or a merge of a PR in its scope (``prs``) or of a
repository it covers (``repos``). Only the part of a wait inside the outage moves; the rest
keeps the task's own cause.

The board only says what a task waits for now (or last), so outside an outage the cause is
the task's latest known one. Pure and deterministic.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from kelpie.core.incidents import CiOutage, is_outage_state
from kelpie.core.ledger import LedgerEntry
from kelpie.core.model import Condition, ConditionKind, Task
from kelpie.core.states import OpenState
from kelpie.report import farm, md
from kelpie.report.farm import UNKNOWN_FARM, FarmFacts
from kelpie.report.throughput import RoleThroughput

UNKNOWN = "unknown"
# What a cause is called in "waiting for ...".
WAIT_CAUSES: Mapping[str, str] = {
    "ci": "CI",
    "merge": "PRs to pass checks and merge",
    "ci_outage": "a shared CI failure (an outage of the CI or of the base branch)",
    "people": "people (approvals, human tasks)",
    "hold": "holds",
    "quota": "LLM quotas",
    "tasks": "other tasks",
    "timer": "timers",
    "other": "custom conditions",
    UNKNOWN: "an unknown cause (no condition registered)",
}
_SUGGESTIONS: Mapping[str, str] = {
    "ci": "add CI capacity where the tasks queue (another runner for that pipeline), run only "
    "the checks a change needs, or let fewer tasks of the role wait on CI at once",
    "ci_outage": "fix the shared failure first (see its CI outage alert: the CI, or the base "
    "branch the changes build on): no PR it covers can go green until then",
    "quota": "spread the role over more providers or models, or raise the quota",
    "tasks": "unblock the tasks they depend on first, or drop the dependency",
    "timer": "shorten the scheduled waits",
    "other": "check what the custom conditions wait for and whether a connector can resolve "
    "them sooner",
}


def suggestion(cause: str, facts: FarmFacts, role: str | None = None) -> str:
    """What to do about a wait, never advising what the farm already has
    (:mod:`kelpie.report.farm`)."""
    if cause == "people":
        return farm.people(facts, None if role is None else (role,))
    if cause == "hold":
        return farm.holds(facts)
    if cause == "merge":
        return farm.merges()
    if cause == UNKNOWN:
        return farm.unregistered_waits(facts)
    return _SUGGESTIONS[cause]


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
CI_OUTAGE = "ci_outage"
MAX_REASON = 2000  # characters of a block reason matched, as the GitHub step does
# "owner/name#12" or a pull request URL in a pattern's ``pr`` group: the repository
_REPO_IN_REF = re.compile(r"([\w.-]+/[\w.-]+)(?:#|/pull/)\d+")


@dataclass(frozen=True, slots=True)
class BlockKind:
    """A ``[[block_conditions]]`` pattern: a block reason it matches waits for ``kind``."""

    pattern: re.Pattern[str]
    kind: ConditionKind
    repo: str | None = None


@dataclass(frozen=True, slots=True)
class BiggestWait:
    role: str
    cause: str
    hours: float  # task-hours of the role's tasks waiting for the cause
    work_hours: float  # agent-hours the role's agents had a session running, for scale
    tasks: tuple[str, ...]
    repo: str | None = None  # for CI and merges: the repository most of them waited on
    facts: FarmFacts = UNKNOWN_FARM

    @property
    def label(self) -> str:
        return WAIT_CAUSES[self.cause]

    @property
    def suggestion(self) -> str:
        return suggestion(self.cause, self.facts, self.role)

    def headline(self) -> str:
        """One line of Markdown-safe text."""
        where = f", mostly on {md.code(self.repo)}" if self.repo else ""
        return (
            f"Biggest wait: {md.text(self.role)} tasks spent {self.hours:.1f} task-hours "
            f"waiting for {self.label} ({len(self.tasks)} task(s){where}), against "
            f"{self.work_hours:.1f} agent-hours of work. Suggestion: {self.suggestion}."
        )


@dataclass(frozen=True, slots=True)
class Outage:
    """One occurrence of a CI outage (rule 9's state, :mod:`kelpie.core.incidents`), with
    what its alerts said about it: while it is open, the waits it explains count under
    ``ci_outage`` instead of CI or merges."""

    since: datetime
    until: datetime  # its last_seen
    repos: frozenset[str] = frozenset()  # lower-case owner/name
    prs: frozenset[str] = frozenset()  # lower-case owner/name#N
    tasks: frozenset[str] = frozenset()


def outages(states: Iterable[OpenState], entries: Iterable[LedgerEntry]) -> list[Outage]:
    """Rule 9's CI outages, each with the scope its alerts reported: every entry that names
    the state (``params.state``) within that occurrence's time span (the same signature
    failing again later reuses the state id). Entries are as of their time (written at most
    hourly), so the repository clause in :meth:`WaitCauses.split` covers later joiners."""
    found = [s for s in states if is_outage_state(s.id)]
    samples = [
        (e.at, o)
        for e in entries
        if e.rule == "ci_economy" and (o := CiOutage.from_params(e.params)) is not None
    ]
    out = []
    for s in found:
        mine = [o for at, o in samples if o.state == s.id and s.since <= at <= s.last_seen]
        out.append(
            Outage(
                since=s.since,
                until=s.last_seen,
                repos=frozenset(r.lower() for o in mine for r in o.repos),
                prs=frozenset(p.lower() for o in mine for p in o.prs),
                tasks=frozenset(t for o in mine for t in o.tasks),
            )
        )
    return out


class WaitCauses:
    """The cause of each task's wait (see the module docs). ``entries`` are the day's
    ledger entries: an older inbox may have been answered since."""

    def __init__(
        self,
        conditions: Iterable[Condition],
        entries: Iterable[LedgerEntry],
        hold_labels: Collection[str] = (),
        block_kinds: Sequence[BlockKind] = (),
        outages: Sequence[Outage] = (),
    ) -> None:
        self._labels = frozenset(hold_labels)
        self._outages = tuple(outages)
        self._block_kinds = tuple(block_kinds)
        self._by_id: dict[str, Condition] = {}
        self._by_task: dict[str, list[Condition]] = {}
        for c in conditions:
            self._by_id[c.id] = c
            if c.task_id:
                self._by_task.setdefault(c.task_id, []).append(c)
        # Rule 13: approvals (the agent task waits for a person's answer) and the agent
        # tasks parked on a person's human task (``waiting``); not ``items[].behind``,
        # which reaches tasks further down the chain that may wait on something else.
        self._people: set[str] = set()
        for e in entries:
            if e.rule == "needs_you" and "escalate_to" in e.params:
                self._people.update(_ids(e.params.get("approvals")))
                self._people.update(_ids(e.params.get("waiting")))

    def __call__(self, task: Task) -> str:
        if self._labels.intersection(task.labels):
            return "hold"
        found = self.condition(task)
        if found is not None and found.satisfied is not True:
            return _KINDS.get(found.kind, "other")  # what it waits on now
        if task.id in self._people:
            return "people"
        return _KINDS.get(found.kind, "other") if found is not None else UNKNOWN

    def split(self, task: Task, a: datetime, b: datetime) -> dict[str, float]:
        """Hours of the task's wait in ``[a, b)`` by cause: the part an open CI outage
        explains counts under ``ci_outage``, the rest under the task's own cause. A hold is
        deliberate: a held task's wait stays the hold's, outage or not."""
        total = (b - a).total_seconds() / 3600
        if self._labels.intersection(task.labels):
            return {"hold": total}
        spans = sorted(
            (max(a, o.since), min(b, o.until))
            for o in self._outages
            if o.since < b and o.until > a and self._explains(o, task)
        )
        covered, reach = 0.0, a
        for lo, hi in spans:
            lo = max(lo, reach)
            if hi > lo:
                covered += (hi - lo).total_seconds() / 3600
                reach = hi
        out: dict[str, float] = {}
        if covered > 0:
            out[CI_OUTAGE] = covered
        if total - covered > 1e-9:
            out[self(task)] = total - covered
        return out

    def _explains(self, outage: Outage, task: Task) -> bool:
        """The outage names the task, or the task waits on CI or a merge of a PR in its
        scope or of a repository it covers."""
        if task.id in outage.tasks:
            return True
        found = self.condition(task)
        if found is None or found.kind not in (ConditionKind.CI_STATUS, ConditionKind.PR_MERGED):
            return False
        repo, pr = found.params.get("repo"), found.params.get("pr")
        if not isinstance(repo, str):
            return False
        return repo.lower() in outage.repos or f"{repo}#{pr}".lower() in outage.prs

    def condition(self, task: Task) -> Condition | None:
        """The condition the task waits (or last waited) on."""
        if task.block is not None and task.block.condition_id in self._by_id:
            return self._by_id[task.block.condition_id]
        mine = self._by_task.get(task.id, [])

        def rank(c: Condition) -> tuple[bool, datetime, str]:
            return (c.satisfied is not True, c.registered_at or task.status_since, c.id)

        last = max(mine, key=rank) if mine else None
        if last is not None and last.satisfied is not True:
            if last.kind is ConditionKind.PR_MERGED:  # a red CI of that PR comes first
                ref = (last.params.get("repo"), last.params.get("pr"))
                red = [
                    c
                    for c in mine
                    if c.kind is ConditionKind.CI_STATUS
                    and c.satisfied is False
                    and (c.params.get("repo"), c.params.get("pr")) == ref
                ]
                if red:
                    return max(red, key=rank)
            return last
        return self._declared(task) or last

    def _declared(self, task: Task) -> Condition | None:
        """A wait the block reason declares (``[[block_conditions]]``), unchecked."""
        if task.block is None:
            return None
        reason = task.block.reason[:MAX_REASON]
        for b in self._block_kinds:
            m = b.pattern.search(reason)
            if m is None or not m.groupdict().get("pr"):
                continue  # as the GitHub step: a match must name a PR
            ref = _REPO_IN_REF.search(m["pr"])
            repo = ref.group(1).lower() if ref else b.repo
            number = re.search(r"(\d+)$", m["pr"].strip().rstrip(".,;:)]"))
            params: dict[str, object] = {"repo": repo} if repo else {}
            if number:
                params["pr"] = int(number.group(1))
            return Condition(f"block:{task.id}:{b.kind}", b.kind, task.id, params)
        return None


def biggest_wait(
    roles: Sequence[RoleThroughput],
    tasks: Sequence[Task],
    causes: WaitCauses,
    facts: FarmFacts = UNKNOWN_FARM,
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
    if cause in ("ci", "merge", CI_OUTAGE):
        by_id = {t.id: t for t in tasks}
        for tid in waited:
            c = causes.condition(by_id[tid]) if tid in by_id else None
            repo = c.params.get("repo") if c is not None else None
            if isinstance(repo, str) and repo:
                repos[repo] += 1
    repo = min(repos, key=lambda x: (-repos[x], x)) if repos else None
    return BiggestWait(role, cause, hours, r.busy_hours, waited, repo, facts)


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _ids(value: object) -> list[str]:
    return [x for x in _list(value) if isinstance(x, str) and x]
