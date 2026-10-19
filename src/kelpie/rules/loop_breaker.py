"""Rule 10 — stop runaway runs: identical calls, error streaks and budgets.

Built on traces and budgets only, so it works for any platform that sends them. Three
checks, all ending in ``stop`` (carrying ``session_id``) plus an ``alert``:

- **identical calls**: ``identical_calls`` consecutive tool calls with the same tool and
  arguments, the last ones within ``identical_window`` (default 8 in 10 minutes, the
  AgentWatchdog thresholds);
- **error streak**: ``consecutive_errors`` failed tool calls in a row (default 5), the last
  one at most ``error_window`` ago and the streak spanning at least ``min_error_span``
  (so one parallel batch of failing reads is not a streak);
- **budgets**: read from ``Snapshot.budgets`` (see :mod:`kelpie.core.budgets`), never
  priced here. A soft state is a warning. A hard task or agent budget stops every live
  session that spent within its window; a hard day or project budget is shared by many
  sessions, so it only alerts unless ``stop_on_shared_budgets`` is true. A budget that will
  run out within ``forecast_horizon`` (or a subscription's next provider limit) is an info
  alert. While ``budgets_ok`` is false the statuses are stale: nothing budget-related is
  emitted;
- **the bill**: when a provider's cost report disagrees with the spend priced from traces
  for its newest reconciled day (``Snapshot.reconciliations``, ``large`` past the
  provider's ``warn_gap`` and ``warn_min_usd``), a warning naming the likely causes, so
  dollar budgets of that provider can be trusted again. Traces lower than the bill point
  at usage the farm's traces do not show (agents that do not export spans, other users
  of the same key, models without a price, and for a bill of everything rather than
  tokens: web search, tools and other non-token items); higher, at prices above the
  bill's (a discount, batch, credits or a ``[prices]`` entry) or calls the provider did
  not bill (another platform's under the provider's ``models``).

Precedence with rule 2 (``wait_condition``): a run of identical calls that matches
``poll_patterns`` is polling, which rule 2 coaches with a ``comment`` once the same check
ran ``poll_comment_after`` times. This rule leaves it alone, and stops it only if it goes
on for ``poll_grace`` after that point (the coaching was ignored), or reaches
``poll_hard_limit`` identical calls (never below ``identical_calls``). ``poll_patterns``
has the same shape as rule 2's (condition kind -> command substrings). Unless this rule
sets its own, ``kelpie.app.load_rules`` gives it rule 2's, so the two cannot disagree by
accident (a warning is logged if both are set and differ); defaults come from rule 2's
own. A failing poll is the same case: the error streak of one polled check is left to the
identical-calls precedence above.

Thresholds are overridable per agent (owner id) or role: ``agents`` and ``roles`` map a
name to a table with any of the threshold keys. An agent entry wins over a role entry.

Only live sessions are stopped. Alerts about a loop or a budget are long-lived states, so
a human is told on transitions, not on every tick.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import ANNOUNCES
from kelpie.core.model import (
    BudgetScope,
    BudgetState,
    BudgetStatus,
    BudgetUnit,
    Reconciliation,
    Session,
    Snapshot,
    Span,
)
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._calls import (
    PollPatterns,
    call_key,
    error_streak,
    identical_streak,
    parse_poll_patterns,
    poll_kind,
    tool_calls,
)
from kelpie.rules._tasks import DEFAULT_STALE_AFTER, is_live
from kelpie.rules._tokens import SpanIndex, span_index, tokens_since
from kelpie.rules.wait_condition import WaitCondition

NAME = "loop_breaker"

# Rule 2's defaults are the AgentWatchdog thresholds too: one source of truth.
_RULE2 = WaitCondition()


@dataclass(frozen=True, slots=True)
class Thresholds:
    identical_calls: int = _RULE2.poll_min_repeats
    identical_window: timedelta = _RULE2.poll_window
    consecutive_errors: int = _RULE2.poll_error_streak
    # An error streak counts only while it is recent (its last error at most error_window
    # ago) and spans at least min_error_span from first to last error, so one parallel
    # batch of failing calls is not a streak.
    error_window: timedelta = timedelta(minutes=10)
    min_error_span: timedelta = timedelta(seconds=60)
    # Polling (calls matching poll_patterns): rule 2 comments at poll_comment_after identical
    # calls; stop only poll_grace after that, or at poll_hard_limit identical calls.
    poll_comment_after: int = _RULE2.poll_min_repeats
    poll_grace: timedelta = timedelta(minutes=15)
    poll_hard_limit: int = 30


_DURATION_KEYS = ("identical_window", "poll_grace", "error_window", "min_error_span")
_COUNT_KEYS = ("identical_calls", "consecutive_errors", "poll_comment_after", "poll_hard_limit")
_THRESHOLD_KEYS = frozenset(f.name for f in fields(Thresholds))


@dataclass(frozen=True, slots=True)
class LoopBreaker:
    thresholds: Thresholds = field(default_factory=Thresholds)
    poll_patterns: PollPatterns = field(default_factory=dict)
    agents: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # owner id -> overrides
    roles: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)  # role -> overrides
    forecast_horizon: timedelta = timedelta(hours=2)
    # A hard day or project budget is shared by many sessions: alert only, unless set.
    stop_on_shared_budgets: bool = False
    session_stale_after: timedelta = DEFAULT_STALE_AFTER
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        index: SpanIndex | None = None
        for session in snapshot.sessions:
            if not is_live(session, snapshot.now, self.session_stale_after):
                continue
            calls = tool_calls(snapshot.spans_for_session(session.id))
            if not calls:
                continue
            limits = self._thresholds(session, snapshot)
            trips = [
                trip
                for trip in (
                    self._loop(calls, limits, snapshot.now),
                    self._errors(calls, limits, snapshot.now),
                )
                if trip is not None
            ]
            if not trips:
                continue
            if index is None:
                index = span_index(snapshot.spans)
            actions.extend(self._stop_session(session, snapshot, trips, index))
        if snapshot.budgets_ok:
            for status in snapshot.budgets:
                actions.extend(self._budget(status, snapshot))
        actions.extend(self._bill(snapshot))
        return actions

    # --- the bill -----------------------------------------------------------------------

    def _bill(self, snapshot: Snapshot) -> Iterable[Action]:
        newest: dict[str, Reconciliation] = {}
        for r in snapshot.reconciliations:
            if r.provider not in newest or r.day > newest[r.provider].day:
                newest[r.provider] = r
        for provider, r in sorted(newest.items()):
            if not r.large:
                continue
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"Spend from traces and the {provider} bill disagree for {r.day:%Y-%m-%d}: "
                f"${r.estimated_usd:,.2f} priced from traces, ${r.reported_usd:,.2f} in the "
                f"cost report{_share(r)}; {_causes(r)}. Dollar budgets of {provider} follow "
                "the traces, not the bill.",
                severity=Severity.WARNING,
                key=f"bill:{provider}",
                state=f"{NAME}:bill:{provider}",
                params={
                    "provider": provider,
                    "day": r.day.date().isoformat(),
                    "estimated_usd": r.estimated_usd,
                    "reported_usd": r.reported_usd,
                    "gap_usd": r.gap_usd,
                    "gap_ratio": r.gap_ratio,
                },
            )

    def resolved(self, state_id: str, snapshot: Snapshot) -> str | None:
        """What the newest reconciled day of a ``bill:`` state's provider shows now."""
        prefix = f"{NAME}:bill:"
        if not state_id.startswith(prefix):
            return None
        provider = state_id.removeprefix(prefix)
        days = [r for r in snapshot.reconciliations if r.provider == provider]
        if not days:
            return f"the {provider} bill is no longer reconciled with the traces."
        r = max(days, key=lambda d: d.day)
        if r.reported_usd < 0:
            return f"the {provider} bill for {r.day:%Y-%m-%d} is a credit (${r.reported_usd:,.2f})."
        return (
            f"the {provider} bill and the spend from traces agree within the thresholds for "
            f"{r.day:%Y-%m-%d}: ${r.estimated_usd:,.2f} priced from traces, "
            f"${r.reported_usd:,.2f} in the cost report{_share(r)}."
        )

    # --- thresholds ---------------------------------------------------------------------

    def _thresholds(self, session: Session, snapshot: Snapshot) -> Thresholds:
        owner = snapshot.owner(session.owner_id)
        task = snapshot.task(session.task_id)
        role = (owner.role if owner else None) or (task.role if task else None)
        found = self.roles.get(role, {}) if role else {}
        found = {**found, **self.agents.get(session.owner_id, {})}
        return replace(self.thresholds, **found) if found else self.thresholds

    # --- loops and error streaks --------------------------------------------------------

    def _loop(self, calls: Sequence[Span], limits: Thresholds, now: datetime) -> _Trip | None:
        streak = identical_streak(calls)
        n = limits.identical_calls
        if streak is None or streak.length < n:
            return None
        last = streak.calls[-1]
        if now - last.start > limits.identical_window:
            return None  # history: the run moved on, or the agent went quiet
        if last.start - streak.calls[-n].start > limits.identical_window:
            return None  # slow repetition, e.g. a legitimate periodic check
        tool = streak.key[0]
        what = f"{streak.length} identical consecutive `{tool}` calls"
        detail = f"(limit {n} within {_minutes(limits.identical_window)})"
        if poll_kind(self.poll_patterns, last) is not None:
            # Polling: rule 2's job first. Stop if the coaching was ignored for the grace
            # period, or the polling is far beyond anything coachable.
            comment_at = streak.calls[min(limits.poll_comment_after, streak.length) - 1].start
            ignored = last.start - comment_at >= limits.poll_grace
            if not ignored and streak.length < limits.poll_hard_limit:
                return None
            why = (
                f"polling went on {_minutes(last.start - comment_at)} after the "
                f"wait_condition coaching point"
                if ignored
                else f"polling reached the hard limit of {limits.poll_hard_limit}"
            )
            detail = f"({why})"
        return _Trip(
            kind="identical_calls",
            what=what,
            detail=detail,
            since=streak.calls[0].start,
        )

    def _errors(self, calls: Sequence[Span], limits: Thresholds, now: datetime) -> _Trip | None:
        errors = error_streak(calls)
        if len(errors) < limits.consecutive_errors:
            return None
        if now - errors[-1].start > limits.error_window:
            return None  # history
        if errors[-1].start - errors[0].start < limits.min_error_span:
            return None  # one parallel batch of failing calls, not a retry streak
        keys = {call_key(s) for s in errors}
        if len(keys) == 1 and None not in keys and poll_kind(self.poll_patterns, errors[-1]):
            return None  # one polled check failing: handled with the identical calls above
        return _Trip(
            kind="error_streak",
            what=f"{len(errors)} consecutive tool errors",
            detail=f"(limit {limits.consecutive_errors})",
            since=errors[0].start,
        )

    def _stop_session(
        self, session: Session, snapshot: Snapshot, trips: list[_Trip], index: SpanIndex
    ) -> Iterable[Action]:
        subject = f"session {session.id} (agent {session.owner_id}, task {session.task_id})"
        tokens = tokens_since(
            snapshot.spans_for_session(session.id), min(t.since for t in trips), index
        )
        spent = f" About {_tokens(tokens)} tokens were spent since it began." if tokens else ""
        for trip in trips:
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"Runaway run: {subject} made {trip.what} {trip.detail}.{spent} "
                "Check the task before resuming it.",
                task_id=session.task_id,
                severity=Severity.CRITICAL,
                key=f"{trip.kind}:{session.id}",
                state=f"{NAME}:{trip.kind}:{session.id}",
                since=trip.since,
                params={
                    "session_id": session.id,
                    "owner_id": session.owner_id,
                    "trigger": trip.kind,
                    "tokens_since": tokens,
                    ANNOUNCES: f"stop:{session.id}",
                },
            )
        yield Action(
            ActionKind.STOP,
            NAME,
            f"Runaway run: {subject} made "
            + "; ".join(f"{t.what} {t.detail}" for t in trips)
            + ".",
            task_id=session.task_id,
            key=f"stop:{session.id}",
            params={"session_id": session.id, "trigger": trips[0].kind},
        )

    # --- budgets ------------------------------------------------------------------------

    def _budget(self, b: BudgetStatus, snapshot: Snapshot) -> Iterable[Action]:
        who = f"{b.scope.value} {b.subject}"
        label = f"Budget `{b.budget}` ({who})"
        estimate = "Estimate, not a bill: " if b.estimated else ""
        limit = b.hard if b.hard is not None else b.soft
        task_id = b.subject if b.scope is BudgetScope.TASK else None
        params: dict[str, Any] = {
            "budget": b.budget,
            "scope": b.scope.value,
            "subject": b.subject,
            "unit": b.unit.value,
            "spent": b.spent,
            "soft": b.soft,
            "hard": b.hard,
            "estimated": b.estimated,
        }
        if b.scope is BudgetScope.AGENT:
            params["owner_id"] = b.subject
        base = f"{b.budget}:{b.subject}"
        if b.state is BudgetState.SOFT:
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"{estimate}{label} passed its soft threshold: {_amount(b.spent, b.unit)} spent "
                f"of {_amount(b.soft, b.unit)} (hard {_amount(b.hard, b.unit)})"
                f"{_runs_out(b, snapshot.now)}.",
                task_id=task_id,
                severity=Severity.WARNING,
                key=f"budget:{base}:soft",
                state=f"{NAME}:budget:{base}",
                params={**params, "runs_out_at": _iso(b.runs_out_at)},
            )
        elif b.state is BudgetState.HARD:
            # A task or agent budget is that subject's own: stop its sessions. A day or
            # project budget is shared by many unrelated sessions: alert, and stop only
            # when asked to.
            may_stop = (
                b.scope in (BudgetScope.TASK, BudgetScope.AGENT) or self.stop_on_shared_budgets
            )
            live = [
                s
                for s in snapshot.sessions
                if may_stop
                and s.id in b.session_ids
                and is_live(s, snapshot.now, self.session_stale_after)
            ]
            names = ", ".join(sorted(s.id for s in live))
            if live:
                tail = f" Live session(s) to stop: {names}."
            elif may_stop:
                tail = " No live session to stop."
            else:
                tail = " Shared budget: no session is stopped automatically, a human decides."
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"{estimate}{label} passed its hard threshold: {_amount(b.spent, b.unit)} spent, "
                f"hard limit {_amount(b.hard, b.unit)}{_runs_out(b, snapshot.now)}.{tail}",
                task_id=task_id,
                severity=Severity.CRITICAL,
                # The sessions to stop are part of the key: one caught later is announced.
                key=f"budget:{base}:hard:{names}",
                state=f"{NAME}:budget:{base}",
                params={
                    **params,
                    "runs_out_at": _iso(b.runs_out_at),
                    "session_ids": sorted(s.id for s in live),
                    ANNOUNCES: [f"stop:{s.id}:budget:{base}" for s in live],
                },
            )
            for s in live:
                yield Action(
                    ActionKind.STOP,
                    NAME,
                    f"Session {s.id}: budget `{b.budget}` for {who} is over its hard limit "
                    f"({_amount(b.spent, b.unit)} of {_amount(b.hard, b.unit)}).",
                    task_id=s.task_id,
                    key=f"stop:{s.id}:budget:{base}",
                    params={"session_id": s.id, "trigger": "budget", "budget": b.budget},
                )
        elif (
            limit is not None
            and b.runs_out_at is not None
            and snapshot.now <= b.runs_out_at <= snapshot.now + self.forecast_horizon
        ):
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"{estimate}{label} runs out at {_at(b.runs_out_at)} "
                f"({_in(b.runs_out_at - snapshot.now)}) at the current burn rate of "
                f"{_amount(b.burn_rate, b.unit)}/h: {_amount(b.spent, b.unit)} of "
                f"{_amount(limit, b.unit)} spent.",
                task_id=task_id,
                severity=Severity.INFO,
                key=f"forecast:{base}",
                state=f"{NAME}:forecast:{base}",
                params={**params, "runs_out_at": _iso(b.runs_out_at)},
            )
        if (
            b.estimated
            and b.next_limit_at is not None
            and snapshot.now <= b.next_limit_at <= snapshot.now + self.forecast_horizon
        ):
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"Estimate: the next provider limit for {label} is expected at "
                f"{_at(b.next_limit_at)} ({_in(b.next_limit_at - snapshot.now)}), judged from "
                "the limits hit so far.",
                task_id=task_id,
                severity=Severity.INFO,
                key=f"limit:{base}",
                state=f"{NAME}:limit:{base}",
                params={**params, "next_limit_at": _iso(b.next_limit_at)},
            )


@dataclass(frozen=True, slots=True)
class _Trip:
    kind: str  # identical_calls | error_streak
    what: str
    detail: str
    since: datetime


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _at(value: datetime) -> str:
    return f"{value.astimezone(UTC):%Y-%m-%d %H:%M} UTC"


def _in(delta: timedelta) -> str:
    minutes = max(0, round(delta.total_seconds() / 60))
    return f"in {minutes} min" if minutes < 120 else f"in {minutes / 60:.1f} h"


def _minutes(d: timedelta) -> str:
    return f"{d.total_seconds() / 60:.0f} minutes"


def _tokens(count: float) -> str:
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}k"
    return f"{count:.0f}"


def _amount(value: float | None, unit: BudgetUnit) -> str:
    if value is None:
        return "no limit"
    return f"${value:,.2f}" if unit is BudgetUnit.USD else f"{_tokens(value)} tokens"


def _runs_out(b: BudgetStatus, now: datetime) -> str:
    if b.runs_out_at is None:
        return ""
    if b.runs_out_at <= now:
        return "; the budget has run out"
    return f"; at the current burn rate it runs out at {_at(b.runs_out_at)}"


# --- config -------------------------------------------------------------------------------


def _duration(value: Any, name: str) -> timedelta:
    try:
        result = parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if result <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return result


def _count(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _threshold_values(raw: Mapping[str, Any], where: str) -> dict[str, Any]:
    """Validated threshold keys (durations parsed); unknown keys fail."""
    unknown = set(raw) - _THRESHOLD_KEYS
    if unknown:
        raise ValueError(f"{where}: unknown threshold keys {sorted(unknown)}")
    out: dict[str, Any] = {}
    for key, value in raw.items():
        if key in _DURATION_KEYS:
            out[key] = _duration(value, f"{where}.{key}")
        else:
            assert key in _COUNT_KEYS
            out[key] = _count(value, f"{where}.{key}")
    return out


def _overrides(raw: Any, name: str) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, Mapping) or not all(isinstance(v, Mapping) for v in raw.values()):
        raise ValueError(f"{name} must map names to tables of thresholds")
    return {str(k): _threshold_values(v, f"{name}.{k}") for k, v in raw.items()}


@register(NAME)
def build(config: dict[str, Any]) -> LoopBreaker:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    own = {k: config.pop(k) for k in list(config) if k in _THRESHOLD_KEYS}
    if own:
        kwargs["thresholds"] = Thresholds(**_threshold_values(own, "thresholds"))
    for key in ("forecast_horizon", "session_stale_after"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    if "poll_patterns" in config:
        kwargs["poll_patterns"] = parse_poll_patterns(config.pop("poll_patterns"))
    if "stop_on_shared_budgets" in config:
        flag = config.pop("stop_on_shared_budgets")
        if not isinstance(flag, bool):
            raise ValueError("stop_on_shared_budgets must be true or false")
        kwargs["stop_on_shared_budgets"] = flag
    for key in ("agents", "roles"):
        if key in config:
            kwargs[key] = _overrides(config.pop(key), key)
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    rule = LoopBreaker(**kwargs)
    _check_limits(rule.thresholds, "thresholds")
    for table, entries in (("agents", rule.agents), ("roles", rule.roles)):
        for name, found in entries.items():
            _check_limits(replace(rule.thresholds, **found), f"{table}.{name}")
    return rule


def _check_limits(limits: Thresholds, where: str) -> None:
    if limits.poll_hard_limit < limits.identical_calls:
        raise ValueError(
            f"{where}: poll_hard_limit ({limits.poll_hard_limit}) must not be below "
            f"identical_calls ({limits.identical_calls})"
        )


def _share(r: Reconciliation) -> str:
    return f" ({r.gap_ratio:+.0%})" if r.gap_ratio is not None else ""


def _causes(r: Reconciliation) -> str:
    """The likely causes of a day's gap, most likely first."""
    if r.gap_usd < 0:
        causes = ["agents that do not export spans", "other users of the same key"]
        if r.unpriced_tokens:
            causes.insert(0, f"models without a price ({r.unpriced_tokens:,} tokens that day)")
        if r.unknown_tokens:
            causes.append(f"calls whose provider is not known ({r.unknown_tokens:,} tokens)")
        if r.bill_scope == "total":
            causes.insert(0, "non-token items the bill includes (web search, tools, storage)")
        return "traces show less than was billed: " + ", ".join(causes)
    return (
        "traces show more than was billed: prices above the bill's (a discount, batch, "
        "credits or a [prices] entry), or calls the provider did not bill (another "
        "platform's, matched by [spend.providers] models)"
    )
