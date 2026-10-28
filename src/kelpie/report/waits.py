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

**Outages** (:mod:`kelpie.core.incidents`, #229). While an outage incident is open, the
waiting time it explains counts under it instead of the task's own cause. A CI outage
(rule 9: failing CI jobs, or jobs that never start) explains, under ``ci_outage``, the
tasks its alerts name (``tasks``) and the tasks waiting on CI or a merge of a PR in its
scope (``prs``) or of a repository it covers (``repos``). A block storm (rule 4: many tasks
blocked on the same reason at once) explains, under ``shared_blocker``, only the tasks it
names, even when its reason names a person: the person can end it, but the hours are the
outage's, not a people wait. Only the part of a wait inside the outage moves; the rest
keeps the task's own cause.

**Snoozed** (issue #176). A wait a person chose is not one the farm lost. The part of a
task's wait in ``[a, b)`` counts under ``snoozed`` only where all of these hold:

- its own cause is ``people`` or ``unknown`` (never a hold, CI, a merge or another
  condition, nor an outage: those are the farm's waits whatever a person snoozed);
- a snooze holds there (from when it was set to its end, or to when ``kelpie serve``
  latched growth behind it): the task's own, or that of a task it waits on on the board
  (a dependency on it, or an unmet ``human_task`` condition naming it; also the
  ``waiting`` ids rule 13 lists under a snoozed item). On purpose this covers an
  unknown-cause task that depends on any snoozed task, a person's or not: the snooze says
  the wait in front of it is deliberate;
- rule 13 did not list the task as live in a person's ``waiting`` then (a step function
  over its escalations, as :func:`kelpie.report.daily.people_agent_hours` reads them, ended
  by the inbox's resolve notice): a live item, one that grew or broke included, is people
  chaos.

:func:`biggest_wait` never names it.

The board only says what a task waits for now (or last), so outside an outage the cause is
the task's latest known one. Pure and deterministic.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from kelpie.core.incidents import BLOCK_STORM, CI_FAILURES, FOLDED_UNDER, is_outage_state
from kelpie.core.incidents import Outage as OutageParams
from kelpie.core.ledger import OBSERVE_ONLY, LedgerEntry, Suppression
from kelpie.core.model import Condition, ConditionKind, Dependency, Snooze, Task
from kelpie.core.states import OpenState
from kelpie.report import farm, intervals, md
from kelpie.report.farm import UNKNOWN_FARM, FarmFacts
from kelpie.report.throughput import RoleThroughput

UNKNOWN = "unknown"
# What a cause is called in "waiting for ...".
WAIT_CAUSES: Mapping[str, str] = {
    "ci": "CI",
    "merge": "PRs to pass checks and merge",
    "ci_outage": "a shared CI failure (an outage of the CI or of the base branch)",
    "shared_blocker": "a shared blocker (many tasks blocked on the same reason)",
    "people": "people (approvals, human tasks)",
    "snoozed": "snoozed (deliberate)",
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
    "shared_blocker": "end the shared blocker the Outages list names: one fix or one "
    "person's answer unblocks every task behind it",
    "snoozed": "nothing to fix: a person deferred these waits on purpose (kelpie snoozes "
    "lists them; kelpie unsnooze ends one early)",
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
SHARED_BLOCKER = "shared_blocker"  # a block storm's waits (incident 20)
SNOOZED = "snoozed"  # waits a person snoozed: deliberate, never the biggest wait
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
        """One line of Markdown-safe text. The role and repository take at most
        :data:`kelpie.report.md.MAX_NAME` characters each once escaped, so the suggestion at
        the end survives a chat line's cut (:data:`kelpie.alerts.summary.MAX_LINE`) even with
        names from a misconfigured board."""
        where = f", mostly on {md.short_code(self.repo)}" if self.repo else ""
        return (
            f"Biggest wait: {md.short_text(self.role)} tasks spent {self.hours:.1f} task-hours "
            f"waiting for {self.label} ({len(self.tasks)} task(s){where}), against "
            f"{self.work_hours:.1f} agent-hours of work. Suggestion: {self.suggestion}."
        )


@dataclass(frozen=True, slots=True)
class OutageSpan:
    """One occurrence of an outage incident (:mod:`kelpie.core.incidents`), with what its
    alerts said about it: while it is open, the waits it explains count under it
    (:attr:`wait_cause`) instead of their own cause. ``observe_only`` is its last alert's (a
    check switched on during an outage moves its waits from the outage's start)."""

    since: datetime
    until: datetime  # its last_seen
    repos: frozenset[str] = frozenset()  # lower-case owner/name
    prs: frozenset[str] = frozenset()  # lower-case owner/name#N
    tasks: frozenset[str] = frozenset()
    state: str = ""
    signature: str | None = None  # how people read it, e.g. "ci.yml > build > Select toolchain"
    kind: str = CI_FAILURES  # what found it (kelpie.core.incidents.KINDS)
    reason: str | None = None  # what happened, in one line (the incident's ``cause``)
    person: str | None = None  # who can end it, when its reason names one
    # a check still in observe (``params.observe_only``): listed, its waits not moved
    observe_only: bool = False

    @property
    def wait_cause(self) -> str:
        """The wait cause its waits count under: a CI outage's or a shared blocker's."""
        return SHARED_BLOCKER if self.kind == BLOCK_STORM else CI_OUTAGE


def outage_samples(
    entries: Iterable[LedgerEntry],
) -> list[tuple[datetime, OutageParams, bool]]:
    """What every outage alert said, any rule's and any kind's: ``(at, params, observe
    only)`` of each ledger entry whose params are an outage's (``params.state`` an outage
    state); observe only: written by a check still in observe (``params.observe_only``)."""
    return [
        (e.at, o, e.params.get(OBSERVE_ONLY) is True)
        for e in entries
        if is_outage_state(str(e.params.get("state", "")))
        and (o := OutageParams.from_params(e.params)) is not None
    ]


def outages(states: Iterable[OpenState], entries: Iterable[LedgerEntry]) -> list[OutageSpan]:
    """The outages, each with the scope its alerts reported: every entry that names the
    state (``params.state``) within that occurrence's time span (the same signature failing
    again later reuses the state id). Entries are as of their time (written at most hourly),
    so the repository clause in :meth:`WaitCauses.split` covers a CI outage's later
    joiners. An outage none of whose entries is loaded is read as failing CI jobs with no
    scope (its kind is in the entries)."""
    samples = outage_samples(entries)
    out = []
    for s in (s for s in states if is_outage_state(s.id)):
        found = [
            (o, obs) for at, o, obs in samples if o.state == s.id and s.since <= at <= s.last_seen
        ]
        mine = [o for o, _ in found]
        last = mine[-1] if mine else None
        out.append(
            OutageSpan(
                since=s.since,
                until=s.last_seen,
                repos=frozenset(r.lower() for o in mine for r in o.repos),
                prs=frozenset(p.lower() for o in mine for p in o.prs),
                tasks=frozenset(t for o in mine for t in o.tasks),
                state=s.id,
                signature=last.signature if last else None,
                kind=last.kind if last else CI_FAILURES,
                reason=last.cause if last else None,
                person=last.person if last else None,
                observe_only=bool(found) and found[-1][1],
            )
        )
    return out


def outage_day(
    outage: OutageSpan,
    entries: Iterable[LedgerEntry],
    start: datetime,
    end: datetime,
    closed_at: datetime | None = None,
) -> tuple[int, int]:
    """The PRs (for a block storm: the tasks) in an outage's scope and the alerts folded under
    it between ``start`` and ``end``: those in its scope during that span, from its
    samples in it and the last one before it (its scope as the span began; samples are
    hourly, so a PR that went green just before may count); and the alerts folded under
    it with an entry in that span until it closed (``closed_at``; the engine keeps
    folding for a while after its last sighting), counted once each (one entry when a
    fold starts, then at most one an hour), a lower bound."""
    a, b = max(outage.since, start), min(outage.until, end)
    folds_until = end if closed_at is None else min(closed_at, end)
    storm = outage.kind == BLOCK_STORM
    prs: set[str] = set()
    before: OutageParams | None = None
    folded: set[tuple[str, str]] = set()

    def scope(o: OutageParams) -> Iterable[str]:
        return o.tasks if storm else (p.lower() for p in o.prs)

    for e in sorted(entries, key=lambda e: e.at):
        o = (
            OutageParams.from_params(e.params)
            if is_outage_state(str(e.params.get("state", "")))
            else None
        )
        if o is not None:
            if o.state != outage.state or not outage.since <= e.at <= b:
                continue
            if e.at < a:
                before = o
            else:
                prs.update(scope(o))
        elif (
            a <= e.at <= folds_until
            and e.suppression is Suppression.INCIDENT
            and e.params.get(FOLDED_UNDER) == outage.state
        ):
            folded.add((e.rule, e.fingerprint))
    if before is not None:
        prs.update(scope(before))
    return len(prs), len(folded)


class WaitCauses:
    """The cause of each task's wait (see the module docs). ``entries`` are the day's
    ledger entries: an older inbox may have been answered since."""

    def __init__(
        self,
        conditions: Iterable[Condition],
        entries: Iterable[LedgerEntry],
        hold_labels: Collection[str] = (),
        block_kinds: Sequence[BlockKind] = (),
        outages: Sequence[OutageSpan] = (),
        snoozes: Iterable[Snooze] = (),
        dependencies: Iterable[Dependency] = (),
        inboxes: Iterable[OpenState] = (),
        history: Iterable[LedgerEntry] | None = None,
    ) -> None:
        self._snoozes = {s.task_id: s for s in snoozes}
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
        # The tasks a task waits on that a snooze may be on (#176), from the board: its
        # dependencies and its unmet human_task conditions; and the ids rule 13 lists as
        # parked on a snoozed item (``snoozed[].waiting``).
        self._humans: dict[str, set[str]] = {}
        for d in dependencies:
            if d.task_id != d.depends_on:
                self._humans.setdefault(d.task_id, set()).add(d.depends_on)
        for c in conditions:
            ref = c.params.get("task")
            unmet = c.kind is ConditionKind.HUMAN_TASK and c.satisfied is not True
            if unmet and c.task_id and isinstance(ref, str) and ref != c.task_id:
                self._humans.setdefault(c.task_id, set()).add(ref)
        entries = sorted(entries, key=lambda e: e.at)
        for e in entries:
            if e.rule == "needs_you" and "escalate_to" in e.params:
                self._people.update(_ids(e.params.get("approvals")))
                self._people.update(_ids(e.params.get("waiting")))
                for item in _list(e.params.get("snoozed")):
                    human = item.get("task_id") if isinstance(item, Mapping) else None
                    if isinstance(human, str) and human and isinstance(item, Mapping):
                        for tid in _ids(item.get("waiting")):
                            if tid != human:
                                self._humans.setdefault(tid, set()).add(human)
        # rule 13's escalations before the day too: the latest earlier one applies at its
        # start, as the people line reads them (kelpie.report.daily._sampled_hours)
        before = entries if history is None else sorted(history, key=lambda e: e.at)
        self._live = _live_waits(before, inboxes)

    @property
    def outages(self) -> tuple[OutageSpan, ...]:
        return self._outages

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
        """Hours of the task's wait in ``[a, b)`` by cause: the part an open outage
        explains under its cause (a block storm's ``shared_blocker`` first, then
        ``ci_outage``), the part a snooze holds under ``snoozed`` (see the
        module docs: only a people or unknown wait, never while rule 13 listed
        the task as live), the rest under the task's own cause. A hold is
        deliberate: a held task's wait stays the hold's, outage or not."""
        total = (b - a).total_seconds() / 3600
        if self._labels.intersection(task.labels):
            return {"hold": total}
        # a block storm first (it names the task), then a CI outage over the rest
        by_cause = {
            cause: intervals.merged(
                (max(a, o.since), min(b, o.until))
                for o in self._outages
                if o.wait_cause == cause
                and not o.observe_only
                and o.since < b
                and o.until > a
                and self._explains(o, task)
            )
            for cause in (SHARED_BLOCKER, CI_OUTAGE)
        }
        by_cause[CI_OUTAGE] = intervals.minus(by_cause[CI_OUTAGE], by_cause[SHARED_BLOCKER])
        outage = intervals.merged([*by_cause[SHARED_BLOCKER], *by_cause[CI_OUTAGE]])
        own = self(task)
        snoozed: intervals.Spans = []
        if own in ("people", UNKNOWN):
            windows = intervals.merged(
                (max(a, s.created_at), min(b, _holds_until(s))) for s in self.snooze_sources(task)
            )
            live = intervals.merged(
                (max(a, lo or a), min(b, hi or b)) for lo, hi in self._live.get(task.id, ())
            )
            snoozed = intervals.minus(intervals.minus(windows, live), outage)
        held, covered = intervals.hours(snoozed), intervals.hours(outage)
        out: dict[str, float] = {}
        if held > 1e-9:
            out[SNOOZED] = held
        for cause, spans in by_cause.items():
            if (hours := intervals.hours(spans)) > 1e-9:
                out[cause] = hours
        if total - held - covered > 1e-9:
            out[own] = total - held - covered
        return out

    def snooze_sources(self, task: Task) -> list[Snooze]:
        """The snoozes that may hold the task's wait: its own, and those of the tasks it
        waits on on the board (or that rule 13 listed it under while snoozed)."""
        ids = {task.id} | self._humans.get(task.id, set())
        return [self._snoozes[i] for i in sorted(ids) if i in self._snoozes]

    def _explains(self, outage: OutageSpan, task: Task) -> bool:
        """The outage names the task, or (a CI outage) the task waits on CI or a merge of a
        PR in its scope or of a repository it covers."""
        if task.id in outage.tasks:
            return True
        if outage.kind == BLOCK_STORM:
            return False  # a block storm explains only the tasks it names
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
                ref = _pr_of(last)
                red = [
                    c
                    for c in mine
                    if c.kind is ConditionKind.CI_STATUS
                    and c.satisfied is False
                    and _pr_of(c) == ref
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
        if hours >= MIN_HOURS and cause != SNOOZED  # chosen by a person: nothing to fix
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


def _holds_until(s: Snooze) -> datetime:
    """When a snooze stopped holding: its end, or when growth behind it was latched."""
    return s.until if s.grown_at is None else min(s.until, s.grown_at)


type _Live = list[tuple[datetime | None, datetime | None]]  # None: open-ended


def _live_waits(
    entries: Sequence[LedgerEntry], inboxes: Iterable[OpenState] = ()
) -> dict[str, _Live]:
    """When rule 13 listed each task as live in a person's ``waiting``: every escalation of
    a person holds until their next one or the inbox's resolve notice (``params.state``
    ``needs_you:<person>``). ``entries`` reach back before the day, so at its start the
    latest earlier escalation applies, as the people line reads rule 13's samples; before a
    person's first escalation of all, the first one applies, back to when that inbox opened
    (``inboxes``: rule 13's states, ``needs_you:<person>``; without one, from the start).
    ``entries`` sorted by time."""
    opened: dict[str, list[OpenState]] = {}
    for state in inboxes:
        if state.id.startswith("needs_you:"):
            opened.setdefault(state.id.removeprefix("needs_you:"), []).append(state)
    samples: dict[str, list[tuple[datetime, list[str]]]] = {}
    ends: dict[str, list[datetime]] = {}
    for e in entries:
        if e.rule != "needs_you":
            continue
        if "escalate_to" in e.params:
            who = e.params.get("escalate_to")
            person = who if isinstance(who, str) else "unassigned"
            samples.setdefault(person, []).append((e.at, _ids(e.params.get("waiting"))))
        elif isinstance(state := e.params.get("state"), str) and state.startswith("needs_you:"):
            ends.setdefault(state.removeprefix("needs_you:"), []).append(e.at)
    out: dict[str, _Live] = {}
    for person, found in samples.items():
        for i, (at, ids) in enumerate(found):
            nxt = found[i + 1][0] if i + 1 < len(found) else None
            closed = [t for t in ends.get(person, ()) if t >= at and (nxt is None or t < nxt)]
            hi = closed[0] if closed else nxt
            lo: datetime | None = at
            if i == 0:  # back to when this inbox opened, if known
                mine = [s for s in opened.get(person, ()) if s.opened_at <= at <= s.last_seen]
                lo = mine[0].opened_at if mine else None
            for tid in ids:
                out.setdefault(tid, []).append((lo, hi))
    return out


def _pr_of(c: Condition) -> tuple[str, str] | None:
    """The condition's PR as (lower-case repo, number as text), however it was written."""
    repo, pr = c.params.get("repo"), c.params.get("pr")
    if not isinstance(repo, str) or pr is None or isinstance(pr, bool):
        return None
    return repo.lower(), str(pr).strip().lstrip("#")


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _ids(value: object) -> list[str]:
    return [x for x in _list(value) if isinstance(x, str) and x]
