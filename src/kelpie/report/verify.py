"""Before/after verification of fixes: did the farm get better after a change?

Pure and deterministic, like :mod:`kelpie.report.daily`, which calls it for the fixes of
the report's day and the day before. A fix (:class:`~kelpie.report.fixes.FixEvent`) comes
from ``kelpie fix`` (manual), a config change seen by ``kelpie serve``, a rule whose mode
changed (read from the action ledger: the mode of a rule's ledger entries switched, e.g.
observe to act, or dry-run turned off), or an output filter that appeared in traces
(:func:`kelpie.traces.outputs.filter_introductions`).

Each fix is measured over ``window`` before it and the same length after it (shorter when
the report ends sooner): hours lost per day (chaos states counted exactly like the report's
cost of chaos: task-hours, each once, and for forgotten holds idle agent-hours; only the
fix's rule when it names one, its own task-hours whatever another rule also saw), tokens
per day (the fix's
role when it names one), returns per day, review time
(mean session length of ``review_roles``), sessions that failed or ended without a final
status, and for a command fix the output tokens per call of that command. A side with fewer
than ``min_sample`` observations, or an after-window shorter than a quarter of the window,
is a small sample, labelled *direction only*: it shows a direction, not a result. Metrics
with no data on either side are left out.

What the report cannot separate, it does not credit (#193):

- **Overlapping fixes.** A fix less than ``window`` after another joins its group
  (:func:`overlapping`): the group is measured once, from ``window`` before its first fix
  to ``window`` after its last, over the rule and role its fixes share (else all), and
  credited to none of them alone. Only what one fix alone can explain is shown per fix:
  its role's tokens, failures (and review time for a review role) when no other fix of
  the group touches that role or the whole farm, and its command's output per call when
  no other fix names the same command for the same role. Fixes are read from
  ``Evidence.known_from`` on: a group that starts less than a window after it says an
  older fix may belong to it (``Verification.truncated``), and its before-window starts
  at ``known_from`` at the earliest (a side under a quarter of the window is *direction
  only*).
- **Newly enabled signals.** A per-day rate whose data starts after the first quarter of
  the before-window has no "before" to compare with: tokens per day when traces began
  that late (after a whole loaded window without a span, when ``known_from`` is set),
  and with a config change in the group, a cause of hours lost seen first after the
  change with nothing before (a connector or rule the change may have turned on). They
  are listed with their value after instead (:class:`Excluded`), so a new problem is
  not hidden. Means (per call, per session) still compare.

A side whose window starts before what the board still reports under-counts review time,
failures and returns; ``kelpie report`` widens the board's lookback to two windows before
the day before by itself (:func:`kelpie.report.cli.report_settings`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from kelpie.core.ledger import OBSERVE_ONLY, LedgerEntry, Outcome
from kelpie.core.model import Session, Span, Status, Task
from kelpie.core.spans import SpanKey, last_at, span_key
from kelpie.core.states import OpenState
from kelpie.report import md
from kelpie.report.fixes import CONFIG, FILTER, MODE, FixEvent
from kelpie.report.store import StateRow
from kelpie.traces.outputs import Call, OutputSettings, filter_introductions

DEFAULT_WINDOW = timedelta(days=1)
DEFAULT_MIN_SAMPLE = 10
DEFAULT_REVIEW_ROLES = ("reviewer",)
DAY = timedelta(days=1)


@dataclass(frozen=True, slots=True)
class Metric:
    name: str
    unit: str
    before: float | None
    after: float | None
    n_before: int
    n_after: int
    small: bool
    lower_is_better: bool = True

    @property
    def change(self) -> float | None:  # None from or to nothing (see ``improved``)
        if self.before is None or self.after is None or not self.before:
            return None
        return self.after / self.before - 1

    @property
    def improved(self) -> bool | None:
        if self.before is None or self.after is None or self.before == self.after:
            return None
        return (self.after < self.before) is self.lower_is_better


@dataclass(frozen=True, slots=True)
class Excluded:
    """A metric left out of a comparison: its data starts after the before-window began
    (``first``), so there is no full "before" to compare with. ``after`` is its value after
    the fix(es), shown so that a new problem is not hidden by the exclusion."""

    name: str
    first: datetime
    after: float | None = None
    unit: str = ""


@dataclass(frozen=True, slots=True)
class Credited:
    """A metric of one fix of a group that no other fix of the group can explain: the fix's
    own role (no other fix touches it or the whole farm) or its own command's output."""

    event: FixEvent
    metric: Metric


@dataclass(frozen=True, slots=True)
class Verification:
    """One fix, or fixes whose windows overlap measured together (``events``, in time
    order): ``metrics`` are measured from ``window`` before the first to ``window`` after
    the last and credited to none of them alone; ``own`` holds what one fix alone can
    explain."""

    events: tuple[FixEvent, ...]
    metrics: tuple[Metric, ...]
    after: timedelta  # how much of the after-window there is data for
    own: tuple[Credited, ...] = ()
    excluded: tuple[Excluded, ...] = ()
    min_sample: int = DEFAULT_MIN_SAMPLE
    # an older fix may belong to the group: fixes are known only from this time on
    truncated: datetime | None = None

    @property
    def event(self) -> FixEvent:
        return self.events[0]

    @property
    def together(self) -> bool:
        return len(self.events) > 1

    @property
    def span(self) -> timedelta:
        """From the first fix of the group to the last."""
        return self.events[-1].at - self.events[0].at

    @property
    def small_sample(self) -> bool:
        return any(m.small for m in self.metrics) or any(c.metric.small for c in self.own)


@dataclass(frozen=True, slots=True)
class Evidence:
    """What fixes are measured against; the report assembles it from its inputs."""

    end: datetime  # nothing after this is known (the report day's end)
    states: Sequence[StateRow] = ()
    chaos_rules: frozenset[str] = frozenset()
    spans: Sequence[Span] = ()
    counted: Mapping[SpanKey, str | None] | None = None
    sessions: Sequence[Session] = ()
    tasks: Sequence[Task] = ()
    roles: Mapping[str, str] | None = None  # session id -> role
    calls: Sequence[Call] = ()
    window: timedelta = DEFAULT_WINDOW
    min_sample: int = DEFAULT_MIN_SAMPLE
    # The earliest moment the loaded fixes, spans and states cover (None: unknown). A group
    # that starts less than a window after it may have lost an older fix to the cut.
    known_from: datetime | None = None
    review_roles: tuple[str, ...] = DEFAULT_REVIEW_ROLES
    # How the report counts a chaos state: its cause (a rule, or "bottlenecks:hold") and
    # its hours lost in a window (task-hours, idle agent-hours for holds).
    cause: Callable[[OpenState], str] = lambda s: s.rule
    # When a state starts counting (a person's inbox: when it opened, see the report).
    start: Callable[[OpenState], datetime] = lambda s: max(s.since, s.opened_at)
    hours: Callable[[OpenState, datetime, datetime], float] = lambda s, lo, hi: (
        (hi - lo).total_seconds() / 3600
    )
    # The hours lost of several states in a window, each task-hour once, as the report
    # counts them (#228); None: the sum of ``hours`` per state.
    lost: Callable[[Sequence[OpenState], datetime, datetime], float] | None = None


def mode_events(entries: Iterable[LedgerEntry]) -> list[FixEvent]:
    """A fix event wherever a rule's ledger entries switch mode (first entry per rule is
    the baseline). Suppressed entries are skipped: they repeat an earlier decision; so are
    an observe-only check's (``OBSERVE_ONLY``): its mode is not the rule's."""
    last: dict[str, str] = {}
    out: list[FixEvent] = []
    for e in sorted(entries, key=lambda e: (e.at, e.rule)):
        if e.outcome is Outcome.SUPPRESSED or e.params.get(OBSERVE_ONLY) is True:
            continue
        before = last.get(e.rule)
        last[e.rule] = e.mode.value
        if before is not None and before != e.mode.value:
            out.append(FixEvent(e.at, MODE, f"{e.rule}: {before} -> {e.mode.value}", e.rule))
    return out


def filter_events(calls: Sequence[Call], settings: OutputSettings) -> list[FixEvent]:
    return [
        FixEvent(b.at, FILTER, f"{b.key} now runs through {b.filter}", None, b.key, b.role)
        for b in filter_introductions(calls, settings)
    ]


def overlapping(events: Iterable[FixEvent], window: timedelta) -> list[tuple[FixEvent, ...]]:
    """Fixes in time order, grouped where their windows overlap: a fix less than ``window``
    after the previous one joins its group. A group's effects cannot be told apart."""
    out: list[list[FixEvent]] = []
    for e in sorted(events, key=lambda e: (e.at, e.kind, e.note)):
        if out and e.at - out[-1][-1].at < window:
            out[-1].append(e)
        else:
            out.append([e])
    return [tuple(g) for g in out]


def verify_all(
    events: Iterable[FixEvent], ev: Evidence, since: datetime
) -> tuple[Verification, ...]:
    """Every group of overlapping fixes (:func:`overlapping`) with a fix at or after
    ``since``; ``events`` should reach back far enough to see the fixes a recent one
    overlaps."""
    groups = overlapping((e for e in events if e.at < ev.end), ev.window)
    return tuple(verify_group(g, ev) for g in groups if g[-1].at >= since)


def verify(event: FixEvent, ev: Evidence) -> Verification:
    """One fix alone (no other fix within its window)."""
    return verify_group((event,), ev)


def verify_group(events: Sequence[FixEvent], ev: Evidence) -> Verification:
    """A fix, or a group of overlapping fixes measured together: from ``window`` before the
    first to ``window`` after the last; the time between them is left out. Within a group,
    only a fix's own role (no other fix of the group touches it or the whole farm) and its
    own command's output are credited to it alone."""
    events = tuple(sorted(events, key=lambda e: (e.at, e.kind, e.note)))
    first, last = events[0], events[-1]
    before, after = _windows(first.at, last.at, ev)
    short = _short(before, after, ev)
    scope = _scope(events)
    excluded: list[Excluded] = []
    metrics = [
        _stuck(ev, scope, before, after, short, excluded, _config_at(events)),
        _tokens(ev, scope, before, after, short, excluded),
        _returns(ev, before, after, short),
        _review_time(ev, scope, before, after),
        _failures(ev, scope, before, after),
    ]
    own: list[Credited] = []
    shared: set[tuple[str, str | None]] = set()
    for e in events:
        others = [o for o in events if o is not e]
        if others and e.role and all(o.role is not None and o.role != e.role for o in others):
            b, a = _windows(e.at, e.at, ev)
            mine = [
                _tokens(ev, e, b, a, _short(b, a, ev), excluded),
                _failures(ev, e, b, a),
            ]
            if e.role in ev.review_roles:
                mine.append(_review_time(ev, e, b, a))
            own += [Credited(e, m) for m in mine if m is not None and _has_data(m)]
        if not e.command:
            continue
        clash = [o for o in others if o.command == e.command and _roles_meet(o.role, e.role)]
        if not clash:
            b, a = _windows(e.at, e.at, ev)
            m = _output(ev, e.command, e.role, b, a, _short(b, a, ev))
            if not others:
                metrics.append(m)
            elif _has_data(m):
                own.append(Credited(e, m))
        elif (e.command, scope.role) not in shared:  # the same command, measured once
            shared.add((e.command, scope.role))
            metrics.append(_output(ev, e.command, scope.role, before, after, short))
    shown = tuple(m for m in metrics if m is not None and _has_data(m))  # no data: no row
    cut = ev.known_from is not None and first.at - ev.known_from < ev.window
    return Verification(
        events,
        shown,
        after[1] - after[0],
        tuple(own),
        tuple(dict.fromkeys(excluded)),
        ev.min_sample,
        ev.known_from if cut else None,
    )


Window = tuple[datetime, datetime]


def _windows(first: datetime, last: datetime, ev: Evidence) -> tuple[Window, Window]:
    """Before and after; the before-window starts no earlier than the loaded data
    (``known_from``): per-day rates over a part never loaded would read as zero."""
    start = first - ev.window
    if ev.known_from is not None:
        start = min(max(start, ev.known_from), first)
    return (start, first), (last, min(last + ev.window, ev.end))


def _short(b: Window, a: Window, ev: Evidence) -> bool:
    """A side under a quarter of the window: a direction, not a result."""
    return min(b[1] - b[0], a[1] - a[0]) < ev.window / 4


def _has_data(m: Metric) -> bool:
    return bool(m.n_before or m.n_after)


def _roles_meet(a: str | None, b: str | None) -> bool:
    """Whether two scopes touch the same agents (no role: the whole farm)."""
    return a is None or b is None or a == b


def _scope(events: Sequence[FixEvent]) -> FixEvent:
    """What a group is measured over: the rule and role all its fixes share, else all."""
    first = events[0]
    if len(events) == 1:
        return first
    rules = {e.rule for e in events}
    roles = {e.role for e in events}
    return FixEvent(
        first.at,
        first.kind,
        first.note,
        rules.pop() if len(rules) == 1 else None,
        None,
        roles.pop() if len(roles) == 1 else None,
    )


def _config_at(events: Sequence[FixEvent]) -> datetime | None:
    """The first config change of the group: what may have turned a signal on."""
    return next((e.at for e in events if e.kind == CONFIG), None)


def _traces_from(ev: Evidence) -> datetime | None:
    """The first span the evidence holds: when traces (or what feeds them) began, as far
    as the loaded history shows."""
    found = [last_at(s) for s in ev.spans]
    return min(found) if found else None


def _days(w: Window) -> float:
    return max((w[1] - w[0]) / DAY, 1e-9)


def _stuck(
    ev: Evidence,
    event: FixEvent,
    b: Window,
    a: Window,
    short: bool,
    excluded: list[Excluded],
    config_at: datetime | None = None,
) -> Metric:
    """Hours lost per day. With a config change among the fixes, a cause with nothing in
    the before-window that first shows up after the change may be a signal the change
    turned on (a connector, a rule): it is left out and listed instead."""
    # A rule covers its causes (bottlenecks covers bottlenecks:hold); a cause is exact.
    exact = event.rule in ev.chaos_rules and ":" in (event.rule or "")
    scoped = exact or event.rule in {c.split(":")[0] for c in ev.chaos_rules}

    def counts(s: OpenState) -> bool:
        cause = ev.cause(s)
        if exact:
            return cause == event.rule
        return not scoped or cause.split(":")[0] == event.rule

    def by_cause(w: Window) -> dict[str, tuple[float, int]]:
        out: dict[str, tuple[float, int]] = {}
        for row in ev.states:
            s = row.state
            if not counts(s):
                continue
            lo, hi = max(ev.start(s), w[0]), min(s.last_seen, w[1])
            if hi > lo:
                h, n = out.get(ev.cause(s), (0.0, 0))
                out[ev.cause(s)] = (h + ev.hours(s, lo, hi), n + 1)
        return out

    cb, ca = by_cause(b), by_cause(a)
    new: set[str] = set()
    if config_at is not None:
        firsts: dict[str, datetime] = {}
        for row in ev.states:
            cause = ev.cause(row.state)
            at = ev.start(row.state)
            if counts(row.state) and (cause not in firsts or at < firsts[cause]):
                firsts[cause] = at
        for cause in sorted(set(ca) - set(cb)):
            if firsts[cause] >= config_at:
                new.add(cause)
                excluded.append(
                    Excluded(
                        f"hours lost per day ({md.text(cause)})",
                        firsts[cause],
                        ca[cause][0] / _days(a),
                        "h/day",
                    )
                )
    hb = sum(h for c, (h, _) in cb.items() if c not in new)
    nb = sum(n for c, (_, n) in cb.items() if c not in new)
    ha = sum(h for c, (h, _) in ca.items() if c not in new)
    na = sum(n for c, (_, n) in ca.items() if c not in new)
    if ev.lost is not None:  # a task-hour once, as the report counts it (#228)
        kept = [r.state for r in ev.states if counts(r.state) and ev.cause(r.state) not in new]
        hb, ha = ev.lost(kept, *b), ev.lost(kept, *a)
    if scoped:
        name = f"hours lost per day ({md.text(event.rule or '')})"
    elif event.rule:  # a rule without chaos states: all causes, and the name says so
        name = f"hours lost per day (all causes; {md.text(event.rule)} has none)"
    else:
        name = "hours lost per day"
    small = short or min(nb, na) < ev.min_sample
    return Metric(name, "h/day", hb / _days(b), ha / _days(a), nb, na, small)


def _tokens(
    ev: Evidence, event: FixEvent, b: Window, a: Window, short: bool, excluded: list[Excluded]
) -> Metric | None:
    """Tokens per day; None (and listed in ``excluded``) when traces began after the first
    quarter of the before-window: a per-day rate over a window seen only in part."""
    counted = ev.counted or {}
    roles = ev.roles or {}

    def per_day(w: Window) -> tuple[float, int]:
        total, n = 0, 0
        for s in ev.spans:
            if span_key(s) not in counted or not w[0] <= last_at(s) < w[1]:
                continue
            if event.role and roles.get(s.session_id or "") != event.role:
                continue
            total += s.input_tokens + s.output_tokens
            n += 1
        return total / _days(w), n

    (tb, nb), (ta, na) = per_day(b), per_day(a)
    who = f" ({md.text(event.role)})" if event.role else ""
    name = f"tokens per day{who}"
    began = _traces_from(ev)
    # Traces began late in the before-window, and (when known) after a whole loaded window
    # with no span at all: an idle night at the start of the history is no signal turning on.
    if (
        began is not None
        and (nb or na)
        and began > b[0] + (b[1] - b[0]) / 4
        and (ev.known_from is None or began - ev.known_from >= ev.window)
    ):
        excluded.append(Excluded(name, began, ta, "tokens/day"))
        return None
    small = short or min(nb, na) < ev.min_sample
    return Metric(name, "tokens/day", tb, ta, nb, na, small)


def _output(ev: Evidence, key: str, role: str | None, b: Window, a: Window, short: bool) -> Metric:
    """Output tokens per call of a command (one role, or all): a mean per call, so a
    window seen only in part still compares."""
    mine = [c for c in ev.calls if c.key == key and (role is None or c.role == role)]
    before = [c.tokens for c in mine if b[0] <= c.at < b[1]]
    after = [c.tokens for c in mine if a[0] <= c.at < a[1]]
    who = f" ({md.text(role)})" if role else ""
    return Metric(
        f"output tokens per {md.code(key, cell=True)} call{who}",
        "tokens",
        sum(before) / len(before) if before else None,
        sum(after) / len(after) if after else None,
        len(before),
        len(after),
        short or min(len(before), len(after)) < ev.min_sample,
    )


def _returns(ev: Evidence, b: Window, a: Window, short: bool) -> Metric:
    def per_day(w: Window) -> tuple[float, int]:
        n = sum(1 for t in ev.tasks for r in t.returns if w[0] <= r.at < w[1])
        return n / _days(w), n

    (rb, nb), (ra, na) = per_day(b), per_day(a)
    small = short or min(nb, na) < ev.min_sample
    return Metric("task returns per day", "/day", rb, ra, nb, na, small)


def _ended(ev: Evidence, w: Window, roles: Iterable[str] | None) -> list[Session]:
    wanted = None if roles is None else set(roles)
    by_role = ev.roles or {}
    return [
        s
        for s in ev.sessions
        if s.ended_at is not None
        and w[0] <= s.ended_at < w[1]
        and (wanted is None or by_role.get(s.id) in wanted)
    ]


def _review_time(ev: Evidence, event: FixEvent, b: Window, a: Window) -> Metric:
    roles = (event.role,) if event.role in ev.review_roles else ev.review_roles

    def mean(w: Window) -> tuple[float | None, int]:
        found = _ended(ev, w, roles)
        if not found:
            return None, 0
        minutes = [
            ((s.ended_at or s.started_at) - s.started_at).total_seconds() / 60 for s in found
        ]
        return sum(minutes) / len(minutes), len(found)

    (mb, nb), (ma, na) = mean(b), mean(a)
    small = min(nb, na) < ev.min_sample
    return Metric("review time", "min", mb, ma, nb, na, small)


def _failures(ev: Evidence, event: FixEvent, b: Window, a: Window) -> Metric:
    roles = (event.role,) if event.role else None

    def share(w: Window) -> tuple[float | None, int, int]:
        found = _ended(ev, w, roles)
        bad = sum(1 for s in found if s.final_status in (None, Status.FAILED))
        return (bad / len(found) if found else None), bad, len(found)

    (fb, kb, nb), (fa, ka, na) = share(b), share(a)
    small = min(nb, na) < ev.min_sample
    return Metric(
        f"sessions failed or without status ({kb} of {nb} -> {ka} of {na})",
        "share",
        fb,
        fa,
        nb,
        na,
        small,
    )
