"""Before/after verification of fixes: did the farm get better after a change?

Pure and deterministic, like :mod:`kelpie.report.daily`, which calls it for the fixes of
the report's day and the day before. A fix (:class:`~kelpie.report.fixes.FixEvent`) comes
from ``kelpie fix`` (manual), a config change seen by ``kelpie serve``, a rule whose mode
changed (read from the action ledger: the mode of a rule's ledger entries switched, e.g.
observe to act, or dry-run turned off), or an output filter that appeared in traces
(:func:`kelpie.traces.outputs.filter_introductions`).

Each fix is measured over ``window`` before it and the same length after it (shorter when
the report ends sooner): hours lost per day (chaos states counted exactly like the report's
cost of chaos, condition-hours and for forgotten holds idle agent-hours; only the fix's
rule when it names one), tokens per day (the fix's
role when it names one), returns per day, review time
(mean session length of ``review_roles``), sessions that failed or ended without a final
status, and for a command fix the output tokens per call of that command. A side with fewer
than ``min_sample`` observations, or an after-window shorter than a quarter of the window,
is a small sample and labelled as such: it shows a direction, not a result. Metrics with no
data on either side are left out.

A side whose window starts before what the board still reports under-counts review time,
failures and returns; ``kelpie report`` widens the board's lookback to the before-window of
the day before's fixes by itself (:func:`kelpie.report.cli.report_settings`).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.model import Session, Span, Status, Task
from kelpie.core.spans import SpanKey, last_at, span_key
from kelpie.core.states import OpenState
from kelpie.report import md
from kelpie.report.fixes import FILTER, MODE, FixEvent
from kelpie.report.store import StateRow
from kelpie.traces.outputs import Call, OutputSettings, before_after, filter_introductions

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
class Verification:
    event: FixEvent
    metrics: tuple[Metric, ...]
    after: timedelta  # how much of the after-window there is data for

    @property
    def small_sample(self) -> bool:
        return any(m.small for m in self.metrics)


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
    review_roles: tuple[str, ...] = DEFAULT_REVIEW_ROLES
    # How the report counts a chaos state: its cause (a rule, or "bottlenecks:hold") and
    # its hours lost in a window (condition-hours, idle agent-hours for holds).
    cause: Callable[[OpenState], str] = lambda s: s.rule
    hours: Callable[[OpenState, datetime, datetime], float] = lambda s, lo, hi: (
        (hi - lo).total_seconds() / 3600
    )


def mode_events(entries: Iterable[LedgerEntry]) -> list[FixEvent]:
    """A fix event wherever a rule's ledger entries switch mode (first entry per rule is
    the baseline). Suppressed entries are skipped: they repeat an earlier decision."""
    last: dict[str, str] = {}
    out: list[FixEvent] = []
    for e in sorted(entries, key=lambda e: (e.at, e.rule)):
        if e.outcome is Outcome.SUPPRESSED:
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


def verify(event: FixEvent, ev: Evidence) -> Verification:
    at = event.at
    before = (at - ev.window, at)
    after_end = min(at + ev.window, ev.end)
    after = (at, after_end)
    short = after_end - at < ev.window / 4
    metrics = [
        _stuck(ev, event, before, after, short),
        _tokens(ev, event, before, after, short),
        _returns(ev, before, after, short),
        _review_time(ev, event, before, after),
        _failures(ev, event, before, after),
    ]
    if event.command:
        settings = OutputSettings(window=ev.window, min_sample=ev.min_sample)
        found = [c for c in ev.calls if c.at < ev.end]
        ba = before_after(found, event.command, at, settings, event.role)
        metrics.append(
            Metric(
                f"output tokens per {md.code(event.command, cell=True)} call",
                "tokens",
                ba.before.mean if ba.before.calls else None,
                ba.after.mean if ba.after.calls else None,
                ba.before.calls,
                ba.after.calls,
                ba.small_sample or short,
            )
        )
    shown = tuple(m for m in metrics if m.n_before or m.n_after)  # no data: no row
    return Verification(event, shown, after_end - at)


Window = tuple[datetime, datetime]


def _days(w: Window) -> float:
    return max((w[1] - w[0]) / DAY, 1e-9)


def _stuck(ev: Evidence, event: FixEvent, b: Window, a: Window, short: bool) -> Metric:
    # A rule covers its causes (bottlenecks covers bottlenecks:hold); a cause is exact.
    exact = event.rule in ev.chaos_rules and ":" in (event.rule or "")
    scoped = exact or event.rule in {c.split(":")[0] for c in ev.chaos_rules}

    def counts(s: OpenState) -> bool:
        cause = ev.cause(s)
        if exact:
            return cause == event.rule
        return not scoped or cause.split(":")[0] == event.rule

    def hours(w: Window) -> tuple[float, int]:
        total, n = 0.0, 0
        for row in ev.states:
            s = row.state
            if not counts(s):
                continue
            lo, hi = max(s.since, s.opened_at, w[0]), min(s.last_seen, w[1])
            if hi > lo:
                total += ev.hours(s, lo, hi)
                n += 1
        return total / _days(w), n

    (hb, nb), (ha, na) = hours(b), hours(a)
    if scoped:
        name = f"hours lost per day ({event.rule})"
    elif event.rule:  # a rule without chaos states: all causes, and the name says so
        name = f"hours lost per day (all causes; {event.rule} has none)"
    else:
        name = "hours lost per day"
    return Metric(name, "h/day", hb, ha, nb, na, short or min(nb, na) < ev.min_sample)


def _tokens(ev: Evidence, event: FixEvent, b: Window, a: Window, short: bool) -> Metric:
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
    who = f" ({event.role})" if event.role else ""
    small = short or min(nb, na) < ev.min_sample
    return Metric(f"tokens per day{who}", "tokens/day", tb, ta, nb, na, small)


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
