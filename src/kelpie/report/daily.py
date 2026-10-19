"""Daily "cost of chaos" report (rule 8): what the farm lost, what Kelpie fixed by itself,
what needs a human, and the top 3 recommendations.

Pure and deterministic: :func:`build_report` reads only its :class:`ReportInputs` (loaded
from storage by :mod:`kelpie.report.run`), never the clock, and the same inputs give the
same report. No LLM: an optional root-cause section is a later, separate step.

**Cost of chaos, hours.** Long-lived alert states (``kelpie.core.states``) of the rules in
:data:`CHAOS_CAUSES` describe work that is stuck: an ownerless task, a block that outlived
its cause, a runaway loop (``loop_breaker``'s budget and forecast states are spend, not
chaos, and are left out). Each counts from when Kelpie first reported it (``opened_at``:
a grace period or an allowed wait before the threshold is not lost) to its ``last_seen``
(the last tick that still saw it), clipped to the day; a lower bound, since a rule going
live re-opens its states. Two conditions on one task both count: the unit is
*condition-hours*, not wall-clock hours. A forgotten hold (rule 6, ``bottlenecks:hold:*``)
is its own cause and counts idle agent-hours instead (:func:`hold_agent_hours`); so is a
person's inbox (rule 13, ``needs_you:*``): the agent-hours of the agent tasks waiting on
them (:func:`people_agent_hours`). A card stuck in a pre-work status with work behind it
(rule 3, ``orphan_status:stuck-before-work:*``) is its own cause too, in card-hours. All
are "hours lost"; ``[report] hour_usd`` turns them into dollars. A card in a pre-work status
that rule 3 finds stuck (past ``prework_after``, not held, ignored or queued behind its own
parent) but that no open task depends on, so rule 3 never reported it, is listed under
"Stuck, blocking nothing" with its age, and counts nowhere: it delays no work.

**Cost of chaos, dollars.** Tokens burned by sessions that were going nowhere: a session in
a ``loop_breaker`` (identical calls, error streak) or ``no_progress`` state while it lasted,
and every session that ``exit_contract`` requeued (it ended without a status, so its work
is redone: a restart; a requeue that found nothing to do is not one). Each model call is
burned at most once: by the first state that covers it (in :data:`CHAOS_CAUSES` order),
else by a restart, so the chaos never exceeds the day's spend; nested instrumentations are
not double counted either (:mod:`kelpie.core.spans`). Tokens are priced by ``[prices]``;
tokens of models without a price are reported as unpriced. In subscription mode there is
no bill: dollars are an estimate and marked so.

**Cost per completed task.** The day's priced spend divided by the tasks that reached
``done`` during the day, as the board reports them now.

**Planned waits.** Agent tasks planned for later (rule 13's ``planned``, behind a person's
open inbox) are listed under "Planned waits" in agent-hours and never counted: they have not
started, so they do not wait.

**Fixed by Kelpie.** Ledger entries of the day that an executor carried out, per rule and
action; entries logged in observe mode or dry-run, or suggested, are listed as what Kelpie
*would* have done in act mode. **Needs a human.** Long-lived states still open at the end
of the day (since when, escalation level, last seen), actions Kelpie could not carry out, and
repeated huge-output offenders. **Top 3 recommendations.** Filters for huge tool outputs
(:mod:`kelpie.traces.outputs`) with their estimated savings, rules that would have fixed
part of the chaos in act mode, and a fixed remedy for the largest remaining causes; ranked
by dollars, with hours converted by ``hour_usd`` when it is set, else alternating the best
by dollars and the best by hours.

**Biggest wait.** Under the headline: the role and cause whose tasks spent the most
task-hours waiting for conditions, with a suggestion (:mod:`kelpie.report.waits`); a known
cause also competes for the top 3.

**Sections.** Analyses registered in :mod:`kelpie.report.sections` (token optimization,
...) add their own section after the top 3, and their advice competes for it; they follow
the same rules: pure, deterministic, no clock and no LLM.
"""

from __future__ import annotations

import logging
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from itertools import zip_longest
from types import MappingProxyType
from typing import Any

from kelpie.core.actions import ActionKind
from kelpie.core.budgets import PriceTable
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome, Suppression
from kelpie.core.model import (
    Condition,
    Dependency,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.core.spans import (
    Attribution,
    SpanKey,
    TokenUsage,
    counted_llm,
    last_at,
    span_key,
    usage,
)
from kelpie.core.states import OpenState
from kelpie.report import farm, md
from kelpie.report.farm import UNKNOWN_FARM, FarmFacts
from kelpie.report.fixes import FixEvent
from kelpie.report.sections import (
    SECTIONS,
    Advice,
    Section,
    SectionBuilder,
    SectionContext,
    Spend,
    load_sections,
    run_sections,
)
from kelpie.report.store import StateRow
from kelpie.report.throughput import throughput
from kelpie.report.verify import (
    DEFAULT_MIN_SAMPLE,
    DEFAULT_REVIEW_ROLES,
    Evidence,
    Metric,
    Verification,
    filter_events,
    mode_events,
    verify,
)
from kelpie.report.verify import DEFAULT_WINDOW as DEFAULT_VERIFY_WINDOW
from kelpie.report.waits import (
    CI_OUTAGE,
    BiggestWait,
    BlockKind,
    Outage,
    WaitCauses,
    biggest_wait,
    outage_day,
    outages,
)
from kelpie.report.waits import UNKNOWN as UNKNOWN_WAIT
from kelpie.rules.needs_you import PLANNED_AGENT_TASKS, PLANNED_TASKS
from kelpie.rules.orphan_status import OrphanStatus
from kelpie.traces.outputs import (
    OutputGroup,
    OutputSettings,
    Recommendation,
    calls,
    rank,
    recommend,
    session_context,
)

log = logging.getLogger(__name__)

_SHOWN_STUCK = 10
# Rule 3's pattern f test: open tasks behind a card stuck before work, None if not stuck.
type StuckCheck = Callable[[Task, Snapshot], list[str] | None]

# Rule 6's forgotten holds: a cause of their own, counted in idle agent-hours.
HOLDS = "bottlenecks:hold"
HOLD_STATE = HOLDS + ":"
# Rule 13's inboxes: agent-hours spent waiting on people.
PEOPLE = "needs_you"
PEOPLE_STATE = PEOPLE + ":"
# Rule 3's cards stuck in a pre-work status (triage, scheduled) with work behind them.
PREWORK = "orphan_status:prework"
PREWORK_STATE = "orphan_status:stuck-before-work:"

# Rules whose long-lived states are work the farm lost, and what to call that loss.
CHAOS_CAUSES: Mapping[str, str] = {
    "orphan_status": "ownerless tasks",
    "stale_blocks": "blocks that outlived their cause",
    "wait_condition": "waits past their limit",
    "exit_contract": "exits without a final status",
    "ping_pong": "review ping-pong",
    "bottlenecks": "idle capacity (bottlenecks, human gates)",
    HOLDS: "idle agents behind forgotten holds",
    PEOPLE: "agents waiting on people (approvals, human tasks)",
    PREWORK: "cards stuck before work (triage, scheduled)",
    "loop_breaker": "runaway loops",
    "no_progress": "activity without progress",
}
# What a human can do about a cause when no rule could have fixed it by itself.
REMEDIES: Mapping[str, str] = {
    "orphan_status": "give ownerless statuses an owner: route them to a role or let "
    "orphan_status reassign them",
    "wait_condition": "check the conditions agents wait for: a connector that stopped "
    "checking or a limit that is too short",
    "exit_contract": "make agents set a final status before they exit (role instructions)",
    "ping_pong": "escalate review ping-pong earlier or tighten the acceptance criteria",
    PREWORK: "move cards out of triage or scheduled when other tasks depend on them, or "
    "drop the dependency",
    "bottlenecks": "rebalance roles and move human tasks off the critical path",
    "loop_breaker": "let loop_breaker stop runaway sessions (act mode)",
    "no_progress": "longer heartbeat interval, a cheaper model for maintenance calls, "
    "bounded sessions with a handoff file",
}
# The remedy where the rule cannot act on this farm: never advise switching act mode on.
REMEDIES_WITHOUT_ACT: Mapping[str, str] = {
    "orphan_status": "give ownerless statuses an owner: route them to a role",
}


def remedy(
    rule: str,
    facts: FarmFacts,
    can_act: bool,
    acts: bool = False,
    roles: Collection[str] | None = None,
) -> str:
    """What to do about a chaos cause, never advising what the farm already has
    (:mod:`kelpie.report.farm`): act mode where the rule cannot act (``can_act``) or
    already acts (``acts``), or config it already has. ``roles``: the farm's agent roles."""
    if rule == HOLDS:
        return farm.holds(facts)
    if rule == PEOPLE:
        return farm.people(facts, roles)
    if rule == "stale_blocks":
        return farm.stale_blocks(facts, can_act)
    if rule == "loop_breaker":
        if not can_act:  # in act mode or not, nothing stops the sessions here
            return farm.runaway_by_hand(facts)
        if acts:
            return farm.runaway_acting(facts)
    if (acts or not can_act) and rule in REMEDIES_WITHOUT_ACT:
        return REMEDIES_WITHOUT_ACT[rule]
    return REMEDIES[rule]


# States whose id ends with the session they are about.
SESSION_STATES = ("no_progress:", "loop_breaker:identical_calls:", "loop_breaker:error_streak:")
FIX_KINDS = frozenset(
    {
        ActionKind.REQUEUE,
        ActionKind.UNBLOCK,
        ActionKind.WAKE,
        ActionKind.WAIT,
        ActionKind.REASSIGN,
        ActionKind.CLOSE,
        ActionKind.STOP,
        ActionKind.CANCEL_BUILD,
        ActionKind.WIP_COMMIT,
        ActionKind.COMMENT,
    }
)
LOGGED_ONLY = frozenset({Outcome.OBSERVED, Outcome.DRY_RUN, Outcome.SUGGESTED})
NOT_DONE = frozenset({Outcome.FAILED, Outcome.NOT_EXECUTED, Outcome.DROPPED})
MAX_TASKS = 5  # task ids listed per line before "+N more"
TOP = 3


@dataclass(frozen=True, slots=True)
class ReportInputs:
    """Everything one day's report is computed from. ``spans`` may include spans before
    the day: they only count for repeated huge-output offenders. ``tasks``, ``owners`` and
    ``sessions`` are the board as the adapters report it now. ``modes`` is each rule's
    effective mode (observe under dry-run)."""

    day: date
    tz: tzinfo
    actions: Sequence[LedgerEntry] = ()
    states: Sequence[StateRow] = ()
    spans: Sequence[Span] = ()
    tasks: Sequence[Task] = ()
    owners: Sequence[Owner] = ()
    sessions: Sequence[Session] = ()
    prices: PriceTable = field(default_factory=PriceTable)
    estimated: bool = False  # subscription mode: dollars are an estimate
    outputs: OutputSettings = field(default_factory=OutputSettings)
    attribution: Attribution | None = None
    modes: Mapping[str, Mode] = field(default_factory=dict)
    hour_usd: float | None = None
    fixes: Sequence[FixEvent] = ()  # recorded fixes (manual, config) to verify
    verify_window: timedelta = DEFAULT_VERIFY_WINDOW
    review_roles: tuple[str, ...] = DEFAULT_REVIEW_ROLES
    min_sample: int = DEFAULT_MIN_SAMPLE
    section_config: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    conditions: Sequence[Condition] = ()  # the board's conditions, as reported now
    hold_labels: tuple[str, ...] = ()  # rule 6's hold labels: a held task waits for a hold
    block_kinds: tuple[BlockKind, ...] = ()  # [[block_conditions]]: waits a block declares
    dependencies: Sequence[Dependency] = ()  # the board's, as reported now
    # rule 3's test for a card stuck before work, as configured (None: rule 3's defaults)
    stuck_check: StuckCheck | None = None
    # Rules that cannot act on this farm, with what is missing (``kelpie.app.act_blockers``).
    act_blockers: Mapping[str, str] = field(default_factory=dict)
    farm: FarmFacts = UNKNOWN_FARM  # what the farm already has: advice never repeats it


@dataclass(frozen=True, slots=True)
class OutageLine:
    """A CI outage of the day (rule 9), for the report: what failed, since when (it may have
    begun on an earlier day), until when, the PRs in its scope that day and the alerts
    folded under it that day (a lower bound; :func:`kelpie.report.waits.outage_day`)."""

    signature: str
    since: datetime
    until: datetime  # its last sighting by the end of the day
    open: bool  # still open at the end of the day
    prs: int
    folded: int


@dataclass(frozen=True, slots=True)
class PlannedWaits:
    """Agent tasks planned for later behind a person's open inbox (rule 13's ``planned``):
    not waiting, so listed and never counted as hours lost. Planned work behind a human task
    rule 13 does not escalate (nothing real waits on it) is not seen here."""

    agent_hours: float
    tasks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StuckCard:
    """A card in a pre-work status (triage, scheduled) past rule 3's ``prework_after`` that no
    open task depends on: it delays no work, so it is listed, not counted."""

    task_id: str
    status: str  # the platform's status name, else Kelpie's
    since: datetime
    age_hours: float  # at the end of the day


@dataclass(frozen=True, slots=True)
class ChaosLine:
    rule: str
    cause: str
    hours: float
    spend: Spend
    tasks: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FixLine:
    rule: str
    kind: ActionKind
    count: int  # distinct subjects (tasks, else sessions, else entries)
    tasks: tuple[str, ...]
    outcome: Outcome | None = None  # for actions Kelpie could not carry out


@dataclass(frozen=True, slots=True)
class InboxLine:
    """One person's "needs you" inbox (rule 13), as its latest escalation describes it."""

    person: str | None  # None: nobody assigned
    cost_hours: float
    tasks: tuple[str, ...]  # human tasks and approvals, by cost of delay
    blocked_agent_tasks: int


@dataclass(frozen=True, slots=True)
class OpenLine:
    state: OpenState
    open_hours: float  # from ``since`` to the end of the day (or last seen, if earlier)


@dataclass(frozen=True, slots=True)
class DailyReport:
    day: date
    start: datetime
    end: datetime
    chaos: tuple[ChaosLine, ...]
    spend: Spend  # the farm's whole day
    completed: tuple[str, ...]
    fixed: tuple[FixLine, ...]
    would_fix: tuple[FixLine, ...]
    not_done: tuple[FixLine, ...]
    open_states: tuple[OpenLine, ...]
    offenders: tuple[Recommendation, ...]  # repeated huge-output offenders
    outputs: tuple[OutputGroup, ...]  # the largest output groups of the day
    recommendations: tuple[Advice, ...]
    estimated: bool = False
    priced: bool = True  # False: no [prices] table, dollars unknown
    hour_usd: float | None = None
    tz: tzinfo = UTC  # times are shown in the report's time zone
    verifications: tuple[Verification, ...] = ()  # fixes of the day and the day before
    sections: tuple[Section, ...] = ()  # from kelpie.report.sections builders
    inbox: tuple[InboxLine, ...] = ()  # rule 13, per person, by cost of delay
    biggest_wait: BiggestWait | None = None  # the role and cause tasks waited longest for
    # cards stuck before work that nothing waits on: listed, never counted as hours lost
    stuck_idle: tuple[StuckCard, ...] = ()
    outages: tuple[OutageLine, ...] = ()  # rule 9's CI outages of the day
    outage_wait_hours: float = 0.0  # the day's waiting time all of them explained
    planned: PlannedWaits | None = None  # behind work planned for later: never counted
    act_blockers: Mapping[str, str] = field(default_factory=dict)  # as in ReportInputs

    @property
    def chaos_hours(self) -> float:
        return sum(c.hours for c in self.chaos)

    @property
    def chaos_spend(self) -> Spend:
        total = Spend()
        for line in self.chaos:
            total = total + line.spend
        return total

    @property
    def chaos_usd(self) -> float:
        """Tokens burned plus, with ``hour_usd``, the stuck hours."""
        return self.chaos_spend.usd + self.chaos_hours * (self.hour_usd or 0.0)

    @property
    def per_completed_task(self) -> float | None:
        return self.spend.usd / len(self.completed) if self.completed else None


def day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """The day's start and end (exclusive) in UTC; a DST day is 23 or 25 hours."""
    start = datetime.combine(day, time(0), tz)
    end = datetime.combine(day + timedelta(days=1), time(0), tz)
    return start.astimezone(UTC), end.astimezone(UTC)


def build_report(
    inputs: ReportInputs,
    sections: Mapping[str, SectionBuilder] | None = None,
    section_keys: Mapping[str, frozenset[str]] | None = None,
) -> DailyReport:
    """The day's report. ``sections`` are the extra section builders (default: every one
    registered in :mod:`kelpie.report.sections`), ``section_keys`` the config keys each
    accepts (default: as registered)."""
    start, end = day_bounds(inputs.day, inputs.tz)
    spans = [s for s in inputs.spans if start <= last_at(s) < end]
    index = {span_key(s): s for s in inputs.spans}
    counted = counted_llm(index, spans)
    pricer = _Pricer(inputs.prices)
    spend = pricer.price(usage(spans, counted))
    actions = [e for e in inputs.actions if start <= e.at < end]
    chaos = _chaos(inputs, actions, spans, counted, pricer, start, end)
    fixed, would_fix, not_done = _actions(actions)
    open_states = _open_states(inputs.states, end)
    inbox = _inbox(inputs.actions, open_states, end)
    filters, groups = _outputs(inputs, end, spans)
    completed = tuple(
        sorted(
            t.id for t in inputs.tasks if t.status is Status.DONE and start <= t.status_since < end
        )
    )
    failed: list[str] = []
    if sections is None:
        failed = load_sections()
        sections = SECTIONS
    context = SectionContext(
        inputs=inputs,
        start=start,
        end=end,
        spans=tuple(spans),
        counted=MappingProxyType(dict(counted)),
        price=pricer.price,
        spend=spend,
        roles=MappingProxyType(_roles(inputs, end)),
    )
    extra = run_sections(sections, context, failed, inputs.section_config, section_keys)
    wait, outage_lines, outage_hours = _biggest_wait(inputs, start, end, context.roles)
    advice = _recommendations(
        inputs,
        chaos,
        would_fix,
        filters,
        pricer,
        _session_models(spans, counted),
        [a for s in extra for a in s.advice],
        wait,
        frozenset((f, g.platform) for g in groups for f in g.filters),
    )
    return DailyReport(
        day=inputs.day,
        start=start,
        end=end,
        chaos=chaos,
        spend=spend,
        completed=completed,
        fixed=fixed,
        would_fix=would_fix,
        not_done=not_done,
        open_states=open_states,
        inbox=inbox,
        offenders=tuple(r for r in filters if r.repeated),
        outputs=tuple(groups[:5]),
        recommendations=advice,
        estimated=inputs.estimated,
        priced=bool(inputs.prices.entries),
        hour_usd=inputs.hour_usd,
        tz=inputs.tz,
        verifications=_verifications(inputs, index, start, end),
        sections=extra,
        biggest_wait=wait,
        outages=outage_lines,
        outage_wait_hours=outage_hours,
        stuck_idle=_stuck_idle(inputs, start, end),
        planned=_planned_waits(inputs, start, end),
        act_blockers=inputs.act_blockers,
    )


def _planned_waits(inputs: ReportInputs, start: datetime, end: datetime) -> PlannedWaits | None:
    """The agent-hours of planned agent tasks behind people's open inboxes over the day, from
    rule 13 (counted like the people line, :func:`people_agent_hours`: per person, so a task
    behind two people's human tasks counts for each, as in rule 13's counts). Held tasks are
    planned to rule 13 too, but the hold line already names them: they are not listed."""
    hours, tasks = 0.0, set()
    for row in inputs.states:
        state = row.state
        if not state.id.startswith(PEOPLE_STATE):
            continue
        a, b = max(counts_from(state), start), min(state.last_seen, end)
        if b <= a:
            continue
        h, ids = people_agent_hours(inputs.actions, state, a, b, planned=True)
        hours += h
        tasks.update(ids)
    held = {t.id for t in inputs.tasks if set(t.labels) & set(inputs.hold_labels)}
    return PlannedWaits(hours, tuple(sorted(tasks - held))) if hours > 0 else None


def _stuck_idle(inputs: ReportInputs, start: datetime, end: datetime) -> tuple[StuckCard, ...]:
    """Cards stuck before work, by rule 3's own test at the end of the day (pattern f, as
    configured: past ``prework_after``, not held, ignored or queued behind a parent), that
    nothing waits on, and that no chaos state of the day names (those count in the cost of
    chaos, e.g. rule 3's stuck-before-work or no-next-action). Statuses and dependencies
    are the board's as reported now. Oldest first."""
    check = inputs.stuck_check or OrphanStatus().stuck_before_work
    alerted = {
        row.state.task_id
        for row in inputs.states
        if is_chaos(row.state)  # counted in the cost of chaos under any cause
        and max(counts_from(row.state), start) < min(row.state.last_seen, end)
    }
    board = Snapshot(now=end, tasks=tuple(inputs.tasks), dependencies=tuple(inputs.dependencies))
    out = [
        StuckCard(
            t.id,
            t.platform_status or t.status.value,
            t.status_since,
            (end - t.status_since).total_seconds() / 3600,
        )
        for t in inputs.tasks
        if t.id not in alerted and t.status_since < end and check(t, board) == []
    ]
    out.sort(key=lambda c: (c.since, c.task_id))
    return tuple(out)


def wait_causes(inputs: ReportInputs, start: datetime, end: datetime) -> WaitCauses:
    """What each task of the board waited for (:mod:`kelpie.report.waits`), from the day's
    ledger entries (rule 13 re-records an open inbox about hourly)."""
    entries = [e for e in inputs.actions if start <= e.at < end]
    # Rule 9 writes an outage's scope at most hourly: an outage open since yesterday may
    # have its only sample before midnight, so read every entry loaded before the end.
    found = outages([row.state for row in inputs.states], [e for e in inputs.actions if e.at < end])
    return WaitCauses(inputs.conditions, entries, inputs.hold_labels, inputs.block_kinds, found)


def _biggest_wait(
    inputs: ReportInputs, start: datetime, end: datetime, roles: Mapping[str, str]
) -> tuple[BiggestWait | None, tuple[OutageLine, ...], float]:
    """The biggest wait, the day's CI outages and the waiting time they explain."""
    causes = wait_causes(inputs, start, end)
    per_role = throughput(
        inputs.tasks,
        inputs.sessions,
        inputs.owners,
        [row.state for row in inputs.states],
        start,
        end,
        inputs.review_roles,
        roles,
        causes.split,
    )
    explained = sum(r.waits.get(CI_OUTAGE, 0.0) for r in per_role)
    found = biggest_wait(per_role, inputs.tasks, causes, inputs.farm)
    return found, _outage_lines(inputs, causes.outages, start, end), explained


def _outage_lines(
    inputs: ReportInputs, found: Sequence[Outage], start: datetime, end: datetime
) -> tuple[OutageLine, ...]:
    closed = {(row.state.id, row.state.since): row.closed_at for row in inputs.states}
    entries = [e for e in inputs.actions if e.at < end]
    out: list[OutageLine] = []
    for o in sorted(found, key=lambda o: (o.since, o.state)):
        # last seen before the day: not listed, even if it closed after midnight (folds in
        # between, at most resolve_after long, count on neither day)
        if o.since >= end or o.until <= start:
            continue
        closed_at = closed.get((o.state, o.since))
        prs, folded = outage_day(o, entries, start, end, closed_at)
        out.append(
            OutageLine(
                o.signature or o.state,
                o.since,
                min(o.until, end),
                closed_at is None or closed_at >= end,
                prs,
                folded,
            )
        )
    return tuple(out)


def _roles(inputs: ReportInputs, end: datetime) -> dict[str, str]:
    board = Snapshot(
        now=end,
        tasks=tuple(inputs.tasks),
        owners=tuple(inputs.owners),
        sessions=tuple(inputs.sessions),
    )
    return session_context(board, inputs.attribution)[0]


def _verifications(
    inputs: ReportInputs, index: Mapping[SpanKey, Span], start: datetime, end: datetime
) -> tuple[Verification, ...]:
    """Before/after of every fix made on the report's day or the day before."""
    known = [s for s in inputs.spans if last_at(s) < end]
    board = Snapshot(
        now=end,
        tasks=tuple(inputs.tasks),
        owners=tuple(inputs.owners),
        sessions=tuple(inputs.sessions),
    )
    roles, platforms = session_context(board, inputs.attribution)
    found = calls(known, roles, platforms)
    since, _ = day_bounds(inputs.day - timedelta(days=1), inputs.tz)
    events = [
        *inputs.fixes,
        *mode_events(e for e in inputs.actions if e.at < end),
        *filter_events(found, replace(inputs.outputs, window=inputs.verify_window)),
    ]
    events = sorted(
        (e for e in events if since <= e.at < end), key=lambda e: (e.at, e.kind, e.note)
    )
    if not events:
        return ()
    evidence = Evidence(
        end=end,
        states=[r for r in inputs.states if is_chaos(r.state)],  # counted like the chaos
        cause=cause_of,
        start=counts_from,
        hours=lambda st, lo, hi: state_hours(inputs.actions, st, lo, hi, _humans(inputs)),
        chaos_rules=frozenset(CHAOS_CAUSES),
        spans=known,
        counted=counted_llm(index, known),
        sessions=inputs.sessions,
        tasks=inputs.tasks,
        roles=roles,
        calls=found,
        window=inputs.verify_window,
        min_sample=inputs.min_sample,
        review_roles=inputs.review_roles,
    )
    return tuple(verify(e, evidence) for e in events)


class _Pricer:
    def __init__(self, prices: PriceTable) -> None:
        self.prices = prices

    def price(self, rows: Iterable[TokenUsage]) -> Spend:
        total = Spend()
        for row in rows:
            tokens = row.input_tokens + row.output_tokens
            found = self.prices.price_for(row.model)
            if found is None:
                total = total + Spend(tokens, 0.0, tokens)
            else:
                total = total + Spend(tokens, found.cost(row), 0)
        return total

    def input_usd(self, tokens: int, model: str | None) -> float:
        found = self.prices.price_for(model)
        return 0.0 if found is None else tokens * found.input / 1_000_000


def _outputs(
    inputs: ReportInputs, end: datetime, spans: Sequence[Span]
) -> tuple[list[Recommendation], list[OutputGroup]]:
    """Huge-output groups and filters of the day; repeats are judged over every span the
    inputs carry up to the end of the day (``repeat_days`` needs earlier days)."""
    board = Snapshot(
        now=end,
        tasks=tuple(inputs.tasks),
        owners=tuple(inputs.owners),
        sessions=tuple(inputs.sessions),
    )
    roles, platforms = session_context(board, inputs.attribution)
    settings = inputs.outputs
    groups = rank(calls(spans, roles, platforms), settings, inputs.tz)
    first, _ = day_bounds(inputs.day - timedelta(days=settings.repeat_days - 1), inputs.tz)
    earlier = [s for s in inputs.spans if first <= last_at(s) < end]
    history = {
        (g.role, g.key): g.hot_days
        for g in rank(calls(earlier, roles, platforms), settings, inputs.tz)
    }
    filters = []
    for rec in recommend(groups, settings):
        days = history.get((rec.group.role, rec.group.key), rec.group.hot_days)
        filters.append(
            replace(
                rec,
                group=replace(rec.group, hot_days=days),
                repeated=len(days) >= settings.repeat_days,
            )
        )
    return filters, groups


def _chaos(
    inputs: ReportInputs,
    actions: Sequence[LedgerEntry],
    spans: Sequence[Span],
    counted: Mapping[SpanKey, str | None],
    pricer: _Pricer,
    start: datetime,
    end: datetime,
) -> tuple[ChaosLine, ...]:
    by_session: dict[str, list[Span]] = defaultdict(list)
    for span in spans:
        if span.session_id and span_key(span) in counted:
            by_session[span.session_id].append(span)
    burned: dict[SpanKey, str] = {}  # each model call is burned once, by its first cause

    def burn(rule: str, session: str, a: datetime, b: datetime) -> None:
        for span in by_session.get(session, ()):
            if a <= last_at(span) < b:
                burned.setdefault(span_key(span), rule)

    hours: dict[str, float] = defaultdict(float)
    tasks: dict[str, set[str]] = defaultdict(set)
    rank_ = {rule: i for i, rule in enumerate(CHAOS_CAUSES)}
    chaos = sorted(
        (r.state for r in inputs.states if is_chaos(r.state)),
        key=lambda s: (rank_[cause_of(s)], s.opened_at, s.id),
    )
    for state in chaos:
        # From when Kelpie first reported it: a grace period or an allowed wait before the
        # threshold is not lost time. A lower bound (a rule going live re-opens a state).
        a, b = max(counts_from(state), start), min(state.last_seen, end)
        if b <= a:
            continue
        cause = cause_of(state)
        if cause == HOLDS:
            agent_hours, held = hold_agent_hours(inputs.actions, state, a, b, _humans(inputs))
            hours[HOLDS] += agent_hours
            tasks[HOLDS].update(held)
            continue
        if cause == PEOPLE:
            agent_hours, waiting = people_agent_hours(inputs.actions, state, a, b)
            hours[PEOPLE] += agent_hours
            tasks[PEOPLE].update(waiting)
            continue
        hours[cause] += (b - a).total_seconds() / 3600
        if state.task_id:
            tasks[cause].add(state.task_id)
        if (session := session_of(state)) is not None:
            burn(state.rule, session, a, b)
    for entry in actions:
        if entry.rule != "exit_contract" or entry.kind is not ActionKind.REQUEUE:
            continue
        if entry.outcome in (Outcome.SUPPRESSED, Outcome.NOTHING_TO_DO):
            continue  # nothing was restarted
        session = entry.params.get("session_id")
        if isinstance(session, str):
            burn(entry.rule, session, start, end)
            if entry.task_id:
                tasks[entry.rule].add(entry.task_id)
    by_rule: dict[str, list[Span]] = defaultdict(list)
    index = {span_key(s): s for spans_ in by_session.values() for s in spans_}
    for key, rule in burned.items():
        by_rule[rule].append(index[key])
    money = {
        rule: pricer.price(usage(found, {span_key(s): counted[span_key(s)] for s in found}))
        for rule, found in by_rule.items()
    }
    lines = [
        ChaosLine(
            rule,
            CHAOS_CAUSES[rule],
            hours[rule],
            money.get(rule, Spend()),
            tuple(sorted(tasks[rule])),
        )
        for rule in CHAOS_CAUSES
        if hours[rule] > 0 or money.get(rule, Spend()).tokens > 0
    ]
    lines.sort(key=lambda c: (-c.spend.usd, -c.hours, c.rule))
    return tuple(lines)


def counts_from(state: OpenState) -> datetime:
    """When a chaos state starts counting: when Kelpie first reported it, not before its
    condition began. A person's inbox (rule 13) counts from when it opened: its ``since``
    is its longest current wait, which moves forward as items are answered."""
    if state.id.startswith(PEOPLE_STATE):
        return state.opened_at
    return max(state.since, state.opened_at)


def cause_of(state: OpenState) -> str:
    """The :data:`CHAOS_CAUSES` key a chaos state counts under."""
    if state.id.startswith(HOLD_STATE):
        return HOLDS
    if state.id.startswith(PREWORK_STATE):
        return PREWORK
    return state.rule


def hold_agent_hours(
    entries: Sequence[LedgerEntry],
    state: OpenState,
    a: datetime,
    b: datetime,
    humans: frozenset[str] = frozenset(),
) -> tuple[float, tuple[str, ...]]:
    """Idle agent-hours a forgotten hold caused in ``[a, b)``, and the tasks it held.

    Rule 6 reports the agents a hold keeps idle (``idle_agents``) whenever its alert is
    handled: on transitions, and as suppressed repeats about once an hour while it stays
    open; see :func:`_sampled_hours`. ``humans`` (owners of kind human) never count as
    idle agents, even if an older alert listed them."""
    label = state.id[len(HOLD_STATE) :]

    def sample(e: LedgerEntry) -> tuple[int, list[object]] | None:
        idle = e.params.get("idle_agents")
        if e.rule != "bottlenecks" or e.params.get("hold") != label or not isinstance(idle, list):
            return None
        return len([x for x in idle if x not in humans]), _held(e.params)

    return _sampled_hours(entries, state.since, a, b, sample)


def people_agent_hours(
    entries: Sequence[LedgerEntry],
    state: OpenState,
    a: datetime,
    b: datetime,
    planned: bool = False,
) -> tuple[float, tuple[str, ...]]:
    """Agent-hours spent waiting on a person in ``[a, b)``, and the agent tasks that waited.

    Rule 13 reports how many agent tasks wait on the person (``waiting_agent_tasks``) with
    every handling of their inbox's alert, including the hourly suppressed repeats; see
    :func:`_sampled_hours`. Alerts recorded before that parameter existed count as 0. Tasks
    planned for later are not waiting (incident 20): rule 13 leaves them out of that count
    and reports them apart (``planned_agent_tasks``, ``planned``), which ``planned`` reads
    instead; they never count as hours lost."""
    person = state.id[len(PEOPLE_STATE) :]
    count_key, ids_key = (
        (PLANNED_AGENT_TASKS, PLANNED_TASKS) if planned else ("waiting_agent_tasks", "waiting")
    )

    def sample(e: LedgerEntry) -> tuple[int, list[object]] | None:
        who = e.params.get("escalate_to")
        if e.rule != PEOPLE or "escalate_to" not in e.params:
            return None  # a resolution notice or another rule's entry: no view of the inbox
        if (who if isinstance(who, str) else "unassigned") != person:
            return None
        count = e.params.get(count_key)
        waiting = e.params.get(ids_key)
        n = count if isinstance(count, int) and not isinstance(count, bool) else 0
        return max(n, 0), waiting if isinstance(waiting, list) else []

    return _sampled_hours(entries, state.opened_at, a, b, sample)


def _sampled_hours(
    entries: Sequence[LedgerEntry],
    first: datetime,
    a: datetime,
    b: datetime,
    sample: Callable[[LedgerEntry], tuple[int, list[object]] | None],
) -> tuple[float, tuple[str, ...]]:
    """Integrate a count a state's alerts report (idle agents, waiting tasks) over
    ``[a, b)``: each sample of this episode of the state (at or after ``first``) holds
    until the next one, a step function. Before the first sample in the window, the latest
    earlier sample applies, else the first one. No sample at all: 0. Also the task ids the
    samples name."""
    samples = sorted(
        (
            (e.at, found[0], found[1])
            for e in entries
            if first <= e.at < b and (found := sample(e)) is not None
        ),
        key=lambda x: (x[0], x[1]),
    )
    if not samples:
        return 0.0, ()
    before = [x for x in samples if x[0] <= a]
    level = (before[-1] if before else samples[0])[1]
    total, at = 0.0, a
    for when, count, _ in samples:
        if when <= a:
            continue
        total += level * (when - at).total_seconds() / 3600
        at, level = when, count
    total += level * (b - at).total_seconds() / 3600
    named = sorted({t for _, _, ids in samples for t in ids if isinstance(t, str)})
    return total, tuple(named)


def _held(params: Mapping[str, object]) -> list[object]:
    """The held work a hold alert lists: its tasks, then its PRs (PR labels as holds)."""
    return [
        *(params.get("held_tasks") if isinstance(params.get("held_tasks"), list) else []),
        *(params.get("held_prs") if isinstance(params.get("held_prs"), list) else []),
    ]


def state_hours(
    entries: Sequence[LedgerEntry],
    state: OpenState,
    a: datetime,
    b: datetime,
    humans: frozenset[str] = frozenset(),
) -> float:
    """What a chaos state costs in ``[a, b)``: its condition-hours, for a forgotten hold
    the idle agent-hours behind it (:func:`hold_agent_hours`), for a person's inbox the
    agent-hours waiting on them (:func:`people_agent_hours`)."""
    if state.id.startswith(HOLD_STATE):
        return hold_agent_hours(entries, state, a, b, humans)[0]
    if state.id.startswith(PEOPLE_STATE):
        return people_agent_hours(entries, state, a, b)[0]
    return max((b - a).total_seconds() / 3600, 0.0)


def _humans(inputs: ReportInputs) -> frozenset[str]:
    return frozenset(o.id for o in inputs.owners if o.kind is OwnerKind.HUMAN)


def is_chaos(state: OpenState) -> bool:
    """A state that is lost work: a chaos rule's, except loop_breaker's budget and
    forecast states (spend against a budget, not a loop)."""
    if state.rule == "loop_breaker":
        return session_of(state) is not None
    return state.rule in CHAOS_CAUSES


def session_of(state: OpenState) -> str | None:
    """The session a ``no_progress`` or ``loop_breaker`` (calls, errors) state is about."""
    for prefix in SESSION_STATES:
        if state.id.startswith(prefix) and len(state.id) > len(prefix):
            return state.id[len(prefix) :]
    return None


def _actions(
    entries: Sequence[LedgerEntry],
) -> tuple[tuple[FixLine, ...], tuple[FixLine, ...], tuple[FixLine, ...]]:
    done: dict[tuple[str, ActionKind], list[LedgerEntry]] = defaultdict(list)
    logged: dict[tuple[str, ActionKind], list[LedgerEntry]] = defaultdict(list)
    failed: dict[tuple[str, ActionKind, Outcome], list[LedgerEntry]] = defaultdict(list)
    for e in entries:
        if e.outcome is Outcome.EXECUTED and e.kind in FIX_KINDS:
            done[(e.rule, e.kind)].append(e)
        elif e.outcome in LOGGED_ONLY and e.kind in FIX_KINDS:
            logged[(e.rule, e.kind)].append(e)
        elif e.outcome in NOT_DONE:
            failed[(e.rule, e.kind, e.outcome)].append(e)

    def lines(groups: Mapping[tuple[str, ActionKind], list[LedgerEntry]]) -> tuple[FixLine, ...]:
        out = [FixLine(r, k, _distinct(es), _task_ids(es)) for (r, k), es in groups.items()]
        return tuple(sorted(out, key=lambda f: (-f.count, f.rule, f.kind)))

    not_done = [FixLine(r, k, _distinct(es), _task_ids(es), o) for (r, k, o), es in failed.items()]
    not_done.sort(key=lambda f: (-f.count, f.rule, f.kind))
    return lines(done), lines(logged), tuple(not_done)


def _distinct(entries: Sequence[LedgerEntry]) -> int:
    return len({e.subject for e in entries})


def _task_ids(entries: Iterable[LedgerEntry]) -> tuple[str, ...]:
    return tuple(sorted({e.task_id for e in entries if e.task_id}))


def _open_states(rows: Sequence[StateRow], end: datetime) -> tuple[OpenLine, ...]:
    out = []
    for row in rows:
        if row.closed_at is not None and row.closed_at < end:
            continue  # resolved within the day
        if row.state.since >= end:
            continue
        upto = min(end, max(row.state.last_seen, row.state.since))
        out.append(OpenLine(row.state, (upto - row.state.since).total_seconds() / 3600))
    rank_ = {"critical": 0, "warning": 1, "info": 2}
    out.sort(key=lambda o: (rank_[o.state.severity], o.state.since, o.state.id))
    return tuple(out)


def _session_models(
    spans: Sequence[Span], counted: Mapping[SpanKey, str | None]
) -> dict[str, str | None]:
    """The model that read the most input tokens in each session (prices tool output)."""
    tally: dict[str, Counter[str]] = defaultdict(Counter)
    for span in spans:
        model = counted.get(span_key(span))
        if span.kind is SpanKind.LLM and span.session_id and model:
            tally[span.session_id][model] += span.input_tokens
    return {s: max(sorted(c), key=lambda m: c[m]) for s, c in tally.items() if c}


def _recommendations(
    inputs: ReportInputs,
    chaos: Sequence[ChaosLine],
    would_fix: Sequence[FixLine],
    filters: Sequence[Recommendation],
    pricer: _Pricer,
    models: Mapping[str, str | None],
    extra: Sequence[Advice] = (),
    wait: BiggestWait | None = None,
    filters_in_use: frozenset[tuple[str, str | None]] = frozenset(),
) -> tuple[Advice, ...]:
    candidates: list[Advice] = list(extra)
    if wait is not None and wait.cause != UNKNOWN_WAIT:  # the headline covers that one
        candidates.append(
            Advice(
                title=f"Cut the {md.text(wait.role)} wait for {wait.label}",
                detail=(
                    f"{len(wait.tasks)} task(s) of the role waited {wait.hours:.1f} task-hours "
                    f"for {wait.label} (time the work stood still, valued like hours lost), "
                    f"against {wait.work_hours:.1f} agent-hours of work; {wait.suggestion}"
                ),
                hours=wait.hours,
                tasks=wait.tasks,
            )
        )
    for rec in filters:
        group = rec.group
        model = next((models[s] for s in group.sessions if models.get(s)), None)
        role = f" ({md.text(group.role)})" if group.role else ""
        # other commands of the same platform already run through it
        in_use = (rec.filter.name, group.platform) in filters_in_use
        maybe = "" if rec.platform_known or in_use else " if RTK supports your agent platform"
        how = (
            f"route it through {rec.filter.name} like the farm's other commands that already run "
            "through it"
            if in_use
            else rec.filter.how
        )
        candidates.append(
            Advice(
                title=f"Filter {md.code(group.key)} output{role} with {rec.filter.name}{maybe}",
                detail=(
                    f"{group.calls} calls put {group.tokens:,} tokens into the context, "
                    f"{round(group.mean):,} per call; {how}. Expected saving: about "
                    f"{rec.saved_tokens:,} tokens a day (assumed {rec.filter.reduction:.0%}; "
                    "verified before/after once it is in place)"
                ),
                usd=pricer.input_usd(rec.saved_tokens, model),
                tasks=group.tasks,
            )
        )
    roles = {o.role for o in inputs.owners if o.kind is OwnerKind.AGENT} or None
    acting: dict[str, FixLine] = {}
    for f in would_fix:  # most subjects first: describe a rule by its main action
        acting.setdefault(f.rule, f)
    for line in chaos:
        mode = inputs.modes.get(line.rule, Mode.OBSERVE)
        fix = acting.get(line.rule)
        blocked = inputs.act_blockers.get(line.rule)
        if fix is not None and mode is not Mode.ACT and blocked is None:
            # The tasks it would have acted on, not every task of the cause.
            title = f"Let {line.rule} act"
            detail = (
                f"in {mode} mode it would have done {fix.kind} on {fix.count} "
                f"subject(s); {line.cause} lasted {line.hours:.1f} condition-hours"
            )
            if inputs.farm.dry_run:
                detail += " (Kelpie runs in dry-run: no rule acts until [engine] dry_run = false)"
            tasks = fix.tasks or line.tasks
        else:
            title = f"Reduce {line.cause}"
            detail = remedy(line.rule, inputs.farm, blocked is None, mode is Mode.ACT, roles)
            if blocked is not None and fix is not None:
                detail += f" (act mode is not available here: {md.text(blocked)})"
            tasks = line.tasks
        candidates.append(Advice(title, detail, line.spend.usd, line.hours, tasks))
    return tuple(_top(candidates, inputs.hour_usd))


def _top(candidates: Sequence[Advice], hour_usd: float | None) -> list[Advice]:
    def worth(a: Advice) -> tuple[float, float, str]:
        return (a.usd + a.hours * (hour_usd or 0.0), a.hours, a.detail)

    best: dict[str, Advice] = {}
    for a in candidates:  # one advice per title: the one worth most
        if a.title not in best or worth(a) > worth(best[a.title]):
            best[a.title] = a
    candidates = list(best.values())
    if hour_usd is not None:
        return sorted(candidates, key=lambda a: (-(a.usd + a.hours * hour_usd), a.title))[:TOP]
    by_usd = sorted((a for a in candidates if a.usd > 0), key=lambda a: (-a.usd, a.title))
    by_hours = sorted(
        (a for a in candidates if a.hours > 0 and a.usd <= 0), key=lambda a: (-a.hours, a.title)
    )
    rest = sorted(
        (a for a in candidates if a.usd <= 0 and a.hours <= 0), key=lambda a: a.title
    )  # e.g. output filters without a price table
    out: list[Advice] = []
    for pair in zip_longest(by_usd, by_hours):
        out.extend(a for a in pair if a is not None)
    return (out + rest)[:TOP]


# --- rendering ---------------------------------------------------------------------------


def render_markdown(report: DailyReport, task_url: str | None = None) -> str:
    """The full report as Markdown; ``task_url`` (with ``{id}``) links task ids."""
    tasks = _TaskLinks(task_url)
    est = " (estimate)" if report.estimated else ""
    lines = [f"# Kelpie daily report: {report.day.isoformat()}", ""]
    lines += [_headline(report), ""]
    if report.biggest_wait is not None:
        lines += [report.biggest_wait.headline(), ""]
    lines += ["## Cost of chaos", ""]
    if report.chaos:
        lines += [
            f"| Cause | Hours lost | Tokens burned | USD{est} | Tasks |",
            "|---|---:|---:|---:|---|",
        ]
        for c in report.chaos:
            lines.append(
                f"| {c.cause} (`{c.rule}`) | {c.hours:.1f} | {c.spend.tokens:,} | "
                f"{_usd(c.spend, report)} | {tasks(c.tasks)} |"
            )
        if report.hour_usd is not None:
            lines += [
                "",
                f"Stuck hours at ${report.hour_usd:,.2f}/hour: "
                f"${report.chaos_hours * report.hour_usd:,.2f}.",
            ]
        lines += [
            "",
            "Hours lost: condition-hours, how long each condition lasted after Kelpie first "
            "reported it, summed over conditions (two on one task both count; a lower bound); "
            "for forgotten holds, idle agent-hours: the agents rule 6 saw idle behind the hold "
            "after its expiry, over time; for agents waiting on people, agent-hours: the agent "
            "tasks rule 13 saw waiting on a person's approval or human task, over time (a "
            "human gate rule 6 also reports counts in both lines).",
        ]
    else:
        lines.append("No hours lost and no tokens burned on loops, idle sessions or restarts.")
    if report.outages:
        lines += [
            "",
            f"**CI outages** (rule 9; {report.outage_wait_hours:.1f} task-hours of waiting fell "
            'inside them and count under "a shared CI failure", not CI or merges; PRs in scope and '
            "alerts folded under each are the day's):",
            "",
        ]
        for o in report.outages:
            until = "still open" if o.open else f"until {_local(o.until, report)}"
            lines.append(
                f"- {md.code(o.signature)} since {_local(o.since, report)}, {until}: "
                f"{o.prs} PR(s), {o.folded} alert(s) folded under it"
            )
    if report.stuck_idle:
        lines += [
            "",
            "**Stuck, blocking nothing** (in a pre-work status past rule 3's threshold, no open "
            "task depends on them; not counted in the hours lost):",
            "",
        ]
        for c in report.stuck_idle[:_SHOWN_STUCK]:
            lines.append(
                f"- {tasks((c.task_id,))} ({md.text(c.status)}) for {c.age_hours:.1f} h, "
                f"since {_local(c.since, report)}"
            )
        if len(report.stuck_idle) > _SHOWN_STUCK:
            lines.append(f"- and {len(report.stuck_idle) - _SHOWN_STUCK} more")
    if report.planned is not None:
        listed = f": {tasks(report.planned.tasks)}" if report.planned.tasks else ""
        lines += [
            "",
            f"**Planned waits**: {report.planned.agent_hours:.1f} agent-hours of agent tasks "
            "planned for later (not started: a pre-work status or a planned label) behind a "
            "person's open inbox; not waiting, so not counted in the hours lost (rule 13 lists "
            f"at most 20 ids per alert){listed}",
        ]
    lines += ["", "## Spend", ""]
    note = " (as the board reports them now; adapters only look back so far)"
    lines.append(
        f"{report.spend.tokens:,} tokens, {_usd(report.spend, report)}{est}; "
        f"{len(report.completed)} tasks completed{note}"
        + (f": {tasks(report.completed)}." if report.completed else ".")
    )
    if report.spend.unpriced_tokens:
        lines.append(
            f"{report.spend.unpriced_tokens:,} tokens are of models without a price in "
            "[prices] and are not in the dollar figures."
        )
    lines += ["", "## Fixed by Kelpie", ""]
    lines += _fix_lines(report.fixed, tasks) or ["Nothing this day."]
    if report.would_fix:
        lines += ["", "Would have done in act mode (observe, suggest or dry-run):", ""]
        lines += _fix_lines(report.would_fix, tasks, report.act_blockers)
    lines += ["", "## Needs a human", ""]
    needs = _needs_lines(report, tasks)
    lines += needs or ["Nothing open."]
    lines += ["", "## Top 3 recommendations", ""]
    if report.recommendations:
        for i, a in enumerate(report.recommendations, 1):
            value = []
            if a.usd > 0:
                value.append(f"about ${a.usd:,.2f}/day{est}")
            if a.hours > 0:
                value.append(f"{a.hours:.1f} hours lost")
            tail = f" ({', '.join(value)})" if value else ""
            linked = f" Tasks: {tasks(a.tasks)}." if a.tasks else ""
            lines.append(f"{i}. **{a.title}**{tail}: {a.detail}.{linked}")
    else:
        lines.append("None: nothing above the thresholds.")
    for section in report.sections:
        if section.lines:
            lines += ["", f"## {section.title}", "", *section.lines]
    lines += ["", "## Before/after of recent fixes", ""]
    lines += _verification_lines(report) or [
        "No fixes on this day or the day before (note one with `kelpie fix`)."
    ]
    if report.outputs:
        lines += ["", "## Largest tool outputs", ""]
        lines += ["| Role | Command or tool | Calls | Tokens | Per call | Filtered calls |"]
        lines += ["|---|---|---:|---:|---:|---:|"]
        for g in report.outputs:
            approx = "~" if g.estimated else ""
            filters = md.text(", ".join(g.filters))
            used = f"{g.filtered_calls} ({filters})" if g.filtered_calls else "0"
            lines.append(
                f"| {md.text(g.role or '?')} | {md.code(g.key, cell=True)} | {g.calls} | "
                f"{approx}{g.tokens:,} | "
                f"{approx}{round(g.mean):,} | {used} |"
            )
    return "\n".join(lines) + "\n"


def render_summary(report: DailyReport) -> str:
    """A short plain-text summary for a chat channel (the full report is the Markdown)."""
    lines = [f"Kelpie daily report {report.day.isoformat()}", _headline(report)]
    if report.biggest_wait is not None:
        lines.append(report.biggest_wait.headline())
    if report.open_states:
        lines.append(f"Needs a human: {len(report.open_states)} open condition(s).")
    for i, a in enumerate(report.recommendations, 1):
        lines.append(f"{i}. {a.title}")
    return "\n".join(lines)


def _headline(report: DailyReport) -> str:
    est = " (estimate)" if report.estimated else ""
    burned = report.chaos_spend
    chaos = f"Cost of chaos: {report.chaos_hours:.1f} hours lost"
    parts = []
    if report.priced:
        parts.append(f"${burned.usd:,.2f}{est} in {burned.tokens:,} tokens burned")
    else:
        parts.append(f"{burned.tokens:,} tokens burned")
    if report.hour_usd is not None:
        parts.append(f"${report.chaos_hours * report.hour_usd:,.2f} in stuck hours")
    chaos += ", " + " + ".join(parts) + "."
    if report.priced and report.spend.unpriced_tokens:
        chaos += f" {report.spend.unpriced_tokens:,} tokens of unpriced models not in dollars."
    per = report.per_completed_task
    if per is not None and report.priced:
        done = f" {len(report.completed)} tasks completed, ${per:,.2f}{est} per completed task."
    else:
        done = f" {len(report.completed)} tasks completed."
    return chaos + done


def _local(at: datetime, report: DailyReport) -> str:
    return at.astimezone(report.tz).strftime("%Y-%m-%d %H:%M %Z")


def _verification_lines(report: DailyReport) -> list[str]:
    out: list[str] = []
    for v in report.verifications:
        e = v.event
        scope = ", ".join(
            x
            for x in (e.rule and f"rule {md.text(e.rule)}", e.role and f"role {md.text(e.role)}")
            if x
        )
        hours = v.after.total_seconds() / 3600
        title = f"**{_local(e.at, report)}, {e.kind}**: {_inline(e.note)}" + (
            f" ({scope})" if scope else ""
        )
        if not v.metrics:
            out += [f"{title}. No data on either side yet (after-window: {hours:.1f}h).", ""]
            continue
        out += [
            f"{title}. After-window: {hours:.1f}h"
            + (", small sample: a direction, not a result." if v.small_sample else "."),
            "",
            "| Metric | Before | After | Change | n before / after |",
            "|---|---:|---:|---:|---:|",
        ]
        for m in v.metrics:
            small = " (small sample)" if m.small else ""
            out.append(
                f"| {m.name}{small} | {_value(m.before, m.unit)} | {_value(m.after, m.unit)} "
                f"| {_change(m)} | {m.n_before} / {m.n_after} |"
            )
        out.append("")
    return out[:-1] if out else out


def _inline(text: str) -> str:
    """Free text (a fix note) as one Markdown-safe line."""
    return md.text(text) or "(no note)"  # after "**time, kind**:", never a block


def _value(value: float | None, unit: str) -> str:
    if value is None:
        return "n/a"
    if unit == "share":
        return f"{value:.0%}"
    number = f"{value:,.1f}" if value < 100 else f"{value:,.0f}"
    return f"{number} {unit}" if unit in ("min", "h/day") else number


def _change(m: Metric) -> str:
    change = m.change
    if change is None:
        return "new, worse" if m.improved is False else "n/a"
    mark = {True: " better", False: " worse", None: ""}[m.improved]
    return f"{change:+.0%}{mark}"


def _usd(spend: Spend, report: DailyReport) -> str:
    return f"${spend.usd:,.2f}" if report.priced else "n/a"


def _fix_lines(
    lines: Sequence[FixLine], tasks: _TaskLinks, blockers: Mapping[str, str] | None = None
) -> list[str]:
    """One line per rule and action; with ``blockers``, a rule that cannot act on this farm
    says what is missing."""
    out = []
    for f in lines:
        text = f"- `{f.rule}`: {f.kind} x{f.count} {tasks(f.tasks)}".rstrip()
        if blockers and (why := blockers.get(f.rule)):
            text += f" (act mode is not available here: {md.text(why)})"
        out.append(text)
    return out


def _inbox(
    entries: Sequence[LedgerEntry], open_states: Sequence[OpenLine], end: datetime
) -> tuple[InboxLine, ...]:
    """Rule 13's open inboxes at the end of the day, the most costly person first, from
    the latest escalation of each (its params carry the sorted items). An inbox that stays
    unchanged is not announced again, but the engine still records it about hourly as a
    suppressed ``state_open`` entry with fresh params: those count too."""
    open_ids = {o.state.id for o in open_states if o.state.rule == "needs_you"}
    latest: dict[str, LedgerEntry] = {}
    for e in entries:
        if e.rule != "needs_you" or e.at >= end:
            continue
        if e.outcome is Outcome.SUPPRESSED and e.suppression is not Suppression.STATE_OPEN:
            continue  # a cooldown or rate-limit repeat, not the state's latest view
        if "escalate_to" not in e.params:
            continue  # a resolution notice: no view of an inbox
        person = e.params.get("escalate_to")
        state = f"needs_you:{person if isinstance(person, str) else 'unassigned'}"
        if state in open_ids and (state not in latest or e.at >= latest[state].at):
            latest[state] = e
    out = []
    for e in latest.values():
        items = e.params.get("items")
        ids = (
            tuple(str(i["task"]) for i in items if isinstance(i, dict) and "task" in i)
            if isinstance(items, list)
            else ()
        )
        cost = e.params.get("cost_hours")
        blocked = e.params.get("blocked_agent_tasks")
        person = e.params.get("escalate_to")
        out.append(
            InboxLine(
                person if isinstance(person, str) else None,
                float(cost) if isinstance(cost, int | float) else 0.0,
                ids,
                int(blocked) if isinstance(blocked, int) else 0,
            )
        )
    out.sort(key=lambda i: (-i.cost_hours, i.person or ""))
    return tuple(out)


def _needs_lines(report: DailyReport, tasks: _TaskLinks) -> list[str]:
    out = []
    for i in report.inbox:
        who = md.text(i.person) if i.person else "nobody assigned"
        out.append(
            f"- **Needs you, {who}**: {i.cost_hours:.1f} agent-hours waited, "
            f"{i.blocked_agent_tasks} agent task(s) held; by cost of delay: {tasks(i.tasks)}"
        )
    listed = {f"needs_you:{i.person or 'unassigned'}" for i in report.inbox}
    for o in report.open_states:
        s = o.state
        if s.id in listed:
            continue  # shown above as the person's inbox
        task = f" {tasks((s.task_id,))}" if s.task_id else ""
        level = f", escalation {s.level}" if s.level else ""
        out.append(
            f"- **{s.severity}** `{s.rule}`{task}: open {o.open_hours:.1f}h since "
            f"{_local(s.since, report)}{level}, "
            f"last seen {_local(s.last_seen, report)}: {md.text(s.summary)}"
        )
    for f in report.not_done:
        out.append(
            f"- Kelpie could not carry out `{f.rule}` {f.kind} ({f.outcome}) "
            f"x{f.count} {tasks(f.tasks)}".rstrip()
        )
    for r in report.offenders:
        owner = f"{md.text(r.owner)}: " if r.owner else ""
        g = r.group
        role = md.text(g.role or "agents")
        out.append(
            f"- {owner}add {r.filter.name} for {md.code(g.key)} to the {role}' "
            f"instructions: above the threshold on {len(g.hot_days)} days "
            f"{tasks(g.tasks)}".rstrip()
        )
    return out


class _TaskLinks:
    def __init__(self, url: str | None) -> None:
        self.url = url

    def __call__(self, ids: Sequence[str]) -> str:
        if not ids:
            return ""
        shown = [self._one(i) for i in ids[:MAX_TASKS]]
        more = f" +{len(ids) - MAX_TASKS} more" if len(ids) > MAX_TASKS else ""
        return ", ".join(shown) + more

    def _one(self, task_id: str) -> str:
        if self.url is None:
            return md.code(task_id, cell=True)  # task lists also fill table cells
        target = self.url.replace("{id}", urllib.parse.quote(task_id, safe=""))
        return f"[{md.link_text(task_id)}]({target})"
