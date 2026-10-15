"""Budgets and spend: config objects, the price table and budget state per subject.

Pure: no I/O and no clock. The app reads token usage from the span store
(:meth:`kelpie.core.spans.SpanStore.usage_since`, root LLM spans only, so nested
instrumentations are not counted twice) and calls :func:`evaluate` while it assembles a
snapshot; rules read the result from ``Snapshot.budgets``.

Subjects. ``day``: the whole farm (subject ``farm``). ``task``: the span's task, else its
session's. ``agent``: the owner of the span's session, as an adapter reports the session;
spend of sessions no adapter knows is not attributed to an agent (it still counts for
``day``). ``project``: the model has no project field, so a project is its ``match``:
``label:<name>`` (tasks carrying that label, as adapters report them) or a glob over task
ids (``WEB-*``). Without ``match``, a task or agent budget applies to each task or agent on
its own; with ``match`` (a name or a glob), only to those it matches.

Windows are rolling: ``window`` (24h by default; task budgets default to the span
retention, i.e. the task's lifetime as far as Kelpie still stores it) and ``burn_window``
(1h by default) for the burn rate.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from fnmatch import fnmatchcase
from itertools import pairwise
from typing import Any

from kelpie.core.model import (
    BudgetScope,
    BudgetState,
    BudgetStatus,
    BudgetUnit,
    LimitHit,
    Session,
    Snapshot,
    Task,
)
from kelpie.core.serde import parse_duration
from kelpie.core.spans import TokenUsage

__all__ = [
    "Budget",
    "BudgetConfig",
    "BudgetMode",
    "BudgetScope",
    "BudgetState",
    "BudgetStatus",
    "BudgetUnit",
    "LimitHit",
    "Price",
    "PriceTable",
    "evaluate",
    "load_budgets",
    "next_limit_at",
    "threshold_forecast",
]

FARM = "farm"
LABEL = "label:"
DEFAULT_WINDOW = timedelta(hours=24)
DEFAULT_BURN_WINDOW = timedelta(hours=1)
ZERO = timedelta(0)
MAX_FORECAST = timedelta(days=3650)  # a forecast further out than this is no forecast
PER_TOKENS = 1_000_000  # prices are USD per this many tokens
BUDGET_KEYS = frozenset(
    {"name", "scope", "match", "window", "burn_window", "unit", "soft", "hard", "mode"}
)
PRICE_KEYS = frozenset({"input", "output", "cached_input"})


class BudgetMode(StrEnum):
    API = "api"  # pay per token: spend is the bill, priced from traces
    SUBSCRIPTION = "subscription"  # no bill: spend is an estimate; limits matter


def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in "*?[")


@dataclass(frozen=True, slots=True)
class Budget:
    name: str
    scope: BudgetScope
    unit: BudgetUnit
    window: timedelta
    soft: float | None = None
    hard: float | None = None
    match: str | None = None
    burn_window: timedelta = DEFAULT_BURN_WINDOW
    mode: BudgetMode = BudgetMode.API

    @property
    def limit(self) -> float:
        """The threshold ``remaining`` and forecasts are measured against."""
        limit = self.hard if self.hard is not None else self.soft
        assert limit is not None  # validated: at least one threshold
        return limit

    def state(self, spent: float) -> BudgetState:
        if self.hard is not None and spent >= self.hard:
            return BudgetState.HARD
        if self.soft is not None and spent >= self.soft:
            return BudgetState.SOFT
        return BudgetState.OK

    def fixed_subjects(self) -> tuple[str, ...]:
        """Subjects reported even when they spent nothing in the window."""
        if self.scope is BudgetScope.DAY:
            return (FARM,)
        if self.match is not None and (
            self.scope is BudgetScope.PROJECT or not _is_glob(self.match)
        ):
            return (self.match,)
        return ()


@dataclass(frozen=True, slots=True)
class Price:
    """USD per 1M tokens. Cached input tokens are part of ``input_tokens`` and priced at
    ``cached_input`` instead of ``input``."""

    input: float
    output: float
    cached_input: float

    def cost(self, usage: TokenUsage) -> float:
        cached = min(usage.cached_tokens, usage.input_tokens)
        return (
            (usage.input_tokens - cached) * self.input
            + cached * self.cached_input
            + usage.output_tokens * self.output
        ) / PER_TOKENS


@dataclass(frozen=True, slots=True)
class PriceTable:
    """``[prices]``: model name or glob -> :class:`Price`. An exact name wins; otherwise
    the first glob that matches, in config order. No match: the cost is unknown."""

    entries: tuple[tuple[str, Price], ...] = ()

    def price_for(self, model: str | None) -> Price | None:
        if not model:
            return None
        for pattern, price in self.entries:
            if pattern == model:
                return price
        for pattern, price in self.entries:
            if _is_glob(pattern) and fnmatchcase(model, pattern):
                return price
        return None


@dataclass(frozen=True, slots=True)
class BudgetConfig:
    budgets: tuple[Budget, ...] = ()
    prices: PriceTable = field(default_factory=PriceTable)

    @property
    def windows(self) -> frozenset[timedelta]:
        """Every usage window :func:`evaluate` needs (windows and burn windows)."""
        return frozenset(w for b in self.budgets for w in (b.window, b.burn_window))


# --- config ------------------------------------------------------------------------------


def load_budgets(budgets: Any, prices: Any, retention: timedelta) -> BudgetConfig:
    """Parse ``[[budgets]]`` and ``[prices]``; raises ValueError on anything unexpected.

    ``retention`` is the span retention: a window longer than it would silently count less
    than it says, so it is refused, and it is the default window of task budgets.
    """
    table = _load_prices(prices)
    if budgets is None:
        budgets = []
    if not isinstance(budgets, list) or not all(isinstance(b, Mapping) for b in budgets):
        raise ValueError("[[budgets]] must be an array of tables")
    loaded = tuple(_load_budget(b, i, retention) for i, b in enumerate(budgets))
    names = [b.name for b in loaded]
    if dupes := sorted({n for n in names if names.count(n) > 1}):
        raise ValueError(f"[[budgets]] names must be unique: {dupes}")
    if not table.entries and (usd := [b.name for b in loaded if b.unit is BudgetUnit.USD]):
        raise ValueError(f"budgets in usd need a [prices] table: {usd}")
    return BudgetConfig(loaded, table)


def _load_prices(data: Any) -> PriceTable:
    if data is None:
        return PriceTable()
    if not isinstance(data, Mapping):
        raise ValueError("[prices] must be a table of models")
    entries: list[tuple[str, Price]] = []
    for model, section in data.items():
        where = f"[prices.{model!r}]"
        if not isinstance(model, str) or not model.strip():
            raise ValueError("[prices] model names must be non-empty strings")
        if not isinstance(section, Mapping):
            raise ValueError(f"{where} must be a table with input, output, cached_input")
        if extra := set(section) - PRICE_KEYS:
            raise ValueError(f"unknown {where} keys: {sorted(extra)}")
        for key in ("input", "output"):
            if key not in section:
                raise ValueError(f"{where} needs {key} (USD per 1M tokens)")
        values = {k: _number(section[k], f"{where} {k}", allow_zero=True) for k in section}
        cached = values.get("cached_input", values["input"])
        entries.append((model, Price(values["input"], values["output"], cached)))
    return PriceTable(tuple(entries))


def _number(value: Any, what: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{what} must be a number")
    if value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{what} must be {'non-negative' if allow_zero else 'positive'}")
    return float(value)


def _duration(value: Any, what: str) -> timedelta:
    try:
        d = parse_duration(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{what}: {exc}") from exc
    if d <= timedelta(0):
        raise ValueError(f"{what} must be positive")
    return d


def _enum[E: StrEnum](cls: type[E], value: Any, what: str) -> E:
    try:
        return cls(value)
    except ValueError:
        allowed = " | ".join(str(m) for m in cls)
        raise ValueError(f"{what} must be one of {allowed}, got {value!r}") from None


def _load_budget(data: Mapping[str, Any], index: int, retention: timedelta) -> Budget:
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError(f"[[budgets]] #{index + 1} needs a non-empty name")
    where = f"budget {name!r}"
    if extra := set(data) - BUDGET_KEYS:
        raise ValueError(f"unknown {where} keys: {sorted(extra)}")
    for key in ("scope", "unit"):
        if key not in data:
            raise ValueError(f"{where} needs {key}")
    scope = _enum(BudgetScope, data["scope"], f"{where} scope")
    unit = _enum(BudgetUnit, data["unit"], f"{where} unit")
    mode = _enum(BudgetMode, data.get("mode", BudgetMode.API), f"{where} mode")
    match = data.get("match")
    if match is not None and (not isinstance(match, str) or not match.strip()):
        raise ValueError(f"{where} match must be a non-empty string")
    if scope is BudgetScope.DAY and match is not None:
        raise ValueError(f"{where}: a day budget covers the whole farm and takes no match")
    if scope is BudgetScope.PROJECT and match is None:
        raise ValueError(f"{where}: a project budget needs match (label:<name> or a task glob)")
    if match is not None and match.startswith(LABEL):
        if scope is not BudgetScope.PROJECT:
            raise ValueError(f"{where}: label: matches are for project budgets")
        if not match[len(LABEL) :].strip():
            raise ValueError(f"{where}: match label: needs a label name")
    default = retention if scope is BudgetScope.TASK else DEFAULT_WINDOW
    window = _duration(data.get("window", default), f"{where} window")
    if window > retention:
        raise ValueError(
            f"{where} window {window} is longer than [traces] retention {retention}: spans "
            "older than the retention are deleted, so the budget would count less than it says"
        )
    burn_window = _duration(
        data.get("burn_window", min(DEFAULT_BURN_WINDOW, window)), f"{where} burn_window"
    )
    if burn_window > window:
        raise ValueError(f"{where} burn_window must not be longer than window")
    soft = None if data.get("soft") is None else _number(data["soft"], f"{where} soft")
    hard = None if data.get("hard") is None else _number(data["hard"], f"{where} hard")
    if soft is None and hard is None:
        raise ValueError(f"{where} needs soft, hard or both")
    if soft is not None and hard is not None and hard <= soft:
        raise ValueError(f"{where}: hard must be greater than soft")
    return Budget(name, scope, unit, window, soft, hard, match, burn_window, mode)


# --- evaluation --------------------------------------------------------------------------


@dataclass(slots=True)
class _Spend:
    spent: float = 0.0
    cost_unknown_tokens: int = 0
    sessions: set[str] = field(default_factory=set)


def evaluate(
    config: BudgetConfig, usage: Mapping[timedelta, Sequence[TokenUsage]], snapshot: Snapshot
) -> tuple[BudgetStatus, ...]:
    """Budget state per budget and subject, in config order then by subject.

    ``usage`` maps each of ``config.windows`` to the token usage of that rolling window
    ending at ``snapshot.now``; a missing window counts as no usage. Sessions, tasks and
    limit hits come from the snapshot.
    """
    sessions = {s.id: s for s in snapshot.sessions}
    tasks = {t.id: t for t in snapshot.tasks}
    out: list[BudgetStatus] = []
    for budget in config.budgets:
        window = _spend(budget, config.prices, usage.get(budget.window, ()), sessions, tasks)
        recent = _spend(budget, config.prices, usage.get(budget.burn_window, ()), sessions, tasks)
        for subject in sorted(set(window) | set(budget.fixed_subjects())):
            spend = window.get(subject) or _Spend()
            burned = recent[subject].spent if subject in recent else 0.0
            out.append(_status(budget, subject, spend, burned, snapshot))
    return tuple(out)


def _status(
    budget: Budget, subject: str, spend: _Spend, burned: float, snapshot: Snapshot
) -> BudgetStatus:
    now = snapshot.now
    remaining = max(budget.limit - spend.spent, 0.0)
    rate = burned / (budget.burn_window / timedelta(hours=1))
    runs_out = threshold_forecast(remaining, rate, now)
    subscription = budget.mode is BudgetMode.SUBSCRIPTION
    if subscription and any(h.at <= now for h in snapshot.limit_hits):
        runs_out = next_limit_at(snapshot.limit_hits, now)
    return BudgetStatus(
        budget=budget.name,
        scope=budget.scope,
        subject=subject,
        unit=budget.unit,
        spent=spend.spent,
        state=budget.state(spend.spent),
        soft=budget.soft,
        hard=budget.hard,
        remaining=remaining,
        burn_rate=rate,
        runs_out_at=runs_out,
        window=budget.window,
        cost_unknown_tokens=spend.cost_unknown_tokens,
        estimated=subscription,
        session_ids=tuple(sorted(spend.sessions)),
    )


def _spend(
    budget: Budget,
    prices: PriceTable,
    rows: Iterable[TokenUsage],
    sessions: Mapping[str, Session],
    tasks: Mapping[str, Task],
) -> dict[str, _Spend]:
    acc: dict[str, _Spend] = {}
    for row in rows:
        subject = _subject(budget, row, sessions, tasks)
        if subject is None:
            continue
        spend = acc.setdefault(subject, _Spend())
        price = prices.price_for(row.model)
        tokens = row.input_tokens + row.output_tokens
        if price is None:
            spend.cost_unknown_tokens += tokens
        if budget.unit is BudgetUnit.TOKENS:
            spend.spent += tokens
        elif price is not None:
            spend.spent += price.cost(row)
        if row.session_id:
            spend.sessions.add(row.session_id)
    return acc


def _subject(
    budget: Budget,
    row: TokenUsage,
    sessions: Mapping[str, Session],
    tasks: Mapping[str, Task],
) -> str | None:
    session = sessions.get(row.session_id) if row.session_id else None
    match budget.scope:
        case BudgetScope.DAY:
            return FARM
        case BudgetScope.AGENT:
            subject = session.owner_id if session is not None else None
        case BudgetScope.TASK | BudgetScope.PROJECT:
            subject = row.task_id or (session.task_id if session is not None else None)
    if not subject:
        return None
    if budget.match is None:
        return subject
    if budget.scope is BudgetScope.PROJECT:
        return budget.match if _in_project(budget.match, subject, tasks) else None
    return subject if fnmatchcase(subject, budget.match) else None


def _in_project(match: str, task_id: str, tasks: Mapping[str, Task]) -> bool:
    if match.startswith(LABEL):
        task = tasks.get(task_id)
        return task is not None and match[len(LABEL) :] in task.labels
    return fnmatchcase(task_id, match)


# --- forecasts ---------------------------------------------------------------------------


def threshold_forecast(remaining: float, rate: float, now: datetime) -> datetime | None:
    """When ``remaining`` runs out at ``rate`` per hour: ``now`` if it already has, None if
    nothing is being spent (or the date is too far out to mean anything)."""
    if remaining <= 0:
        return now
    if rate <= 0:
        return None
    hours = remaining / rate
    if hours > MAX_FORECAST / timedelta(hours=1):
        return None
    return now + timedelta(hours=hours)


def next_limit_at(hits: Iterable[LimitHit], now: datetime) -> datetime | None:
    """Forecast of the next provider limit (subscription mode): the earliest over limits.

    Per limit, from the hits up to ``now``: while the last hit has not reset yet, the limit
    is in force and its time is returned (in the past). Otherwise the next hit is expected
    the typical (median) time-to-limit after the current period began, where a period
    begins at the previous hit's reset (or at the previous hit when its reset is unknown).
    None with fewer than two hits of a limit, or when the expected time has passed without
    a hit (this period is lighter than the ones before).
    """
    by_limit: dict[str, list[LimitHit]] = {}
    for hit in hits:
        if hit.at <= now:
            by_limit.setdefault(hit.limit, []).append(hit)
    forecasts = [_next_hit(sorted(h, key=lambda x: x.at), now) for h in by_limit.values()]
    return min((f for f in forecasts if f is not None), default=None)


def _period_start(hit: LimitHit, before: datetime | None = None) -> datetime:
    if hit.resets_at is not None and (before is None or hit.resets_at <= before):
        return hit.resets_at
    return hit.at


def _next_hit(past: Sequence[LimitHit], now: datetime) -> datetime | None:
    last = past[-1]
    if last.resets_at is not None and last.resets_at > now:
        return last.at
    samples = sorted(
        d for prev, cur in pairwise(past) if (d := cur.at - _period_start(prev, cur.at)) > ZERO
    )
    if not samples:
        return None
    mid = len(samples) // 2
    typical = samples[mid] if len(samples) % 2 else (samples[mid - 1] + samples[mid]) / 2
    forecast = _period_start(last) + typical
    return forecast if forecast >= now else None
