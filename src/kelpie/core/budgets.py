"""Budgets and spend: config objects, the price table and budget state per subject.

Pure: no I/O and no clock. The app reads token usage from the span store
(:meth:`kelpie.core.spans.SpanStore.usage_since`: counted LLM spans only, so nested
instrumentations are not counted twice) and calls :func:`evaluate` while it assembles a
snapshot; rules read the result from ``Snapshot.budgets``.

Subjects. ``day``: the whole farm (subject ``farm``). ``task``: the span's task, else its
session's. ``agent``: the owner of the span's session. ``project``: the model has no
project field, so a project is its ``match``: ``label:<name>`` (tasks carrying that label)
or a glob over task ids (``WEB-*``). Without ``match``, a task or agent budget applies to
each task or agent on its own; with ``match`` (a name or a glob), only to those it matches.
Owners, session tasks and labels come from the snapshot and from what the span store
remembers of them (:class:`kelpie.core.spans.Attribution`), so spend of a finished session
or an archived task keeps its subject. Spend whose subject is still unknown is reported
per budget as ``unattributed`` (it always counts for ``day``).

Windows are rolling: ``window`` (24h by default; task budgets default to the span
retention, i.e. the task's lifetime as far as Kelpie still stores it) and ``burn_window``
(1h by default) for the burn rate.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
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
    Snapshot,
)
from kelpie.core.prices import DEFAULT_PRICES, Price, PriceTable
from kelpie.core.serde import parse_duration
from kelpie.core.spans import Attribution, TokenUsage

__all__ = [
    "Budget",
    "BudgetConfig",
    "BudgetEvaluation",
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
# Limit hits whose window (resets_at - at) is shorter than this are per-minute rate limits,
# not the usage limit a subscription forecast is about.
DEFAULT_MIN_LIMIT_WINDOW = timedelta(minutes=10)
ZERO = timedelta(0)
MAX_FORECAST = timedelta(days=3650)  # a forecast further out than this is no forecast
BUDGET_KEYS = frozenset(
    {
        "name",
        "scope",
        "match",
        "window",
        "burn_window",
        "unit",
        "soft",
        "hard",
        "mode",
        "providers",
        "min_limit_window",
    }
)
PRICE_KEYS = frozenset({"input", "output", "cached_input"})


def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in "*?[")


class BudgetMode(StrEnum):
    API = "api"  # pay per token: spend is the bill, priced from traces
    SUBSCRIPTION = "subscription"  # no bill: spend is an estimate; limits matter


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
    # Subscription mode: the limits (LimitHit.limit, e.g. LLM provider names) whose hits
    # drive next_limit_at (None: all), and the shortest limit window that counts.
    providers: tuple[str, ...] | None = None
    min_limit_window: timedelta = DEFAULT_MIN_LIMIT_WINDOW

    def limit_hits(self, hits: Iterable[LimitHit]) -> list[LimitHit]:
        """The hits this budget forecasts from: its providers' (all without a list), minus
        hits whose limit refills sooner than ``min_limit_window`` after the hit. A hit
        without a reset time has no known window and always counts."""
        return [
            h
            for h in hits
            if (self.providers is None or h.limit in self.providers)
            and (h.resets_at is None or h.resets_at - h.at >= self.min_limit_window)
        ]

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
class BudgetConfig:
    budgets: tuple[Budget, ...] = ()
    prices: PriceTable = field(default_factory=PriceTable)

    @property
    def windows(self) -> frozenset[timedelta]:
        """Every usage window :func:`evaluate` needs (windows and burn windows)."""
        return frozenset(w for b in self.budgets for w in (b.window, b.burn_window))


# --- config ------------------------------------------------------------------------------


def load_budgets(
    budgets: Any, prices: Any, retention: timedelta, *, default_prices: bool = True
) -> BudgetConfig:
    """Parse ``[[budgets]]`` and ``[prices]``; raises ValueError on anything unexpected.

    ``retention`` is the span retention: a window longer than it would silently count less
    than it says, so it is refused, and it is the default window of task budgets.
    ``default_prices``: put Kelpie's default table (:mod:`kelpie.core.prices`) under the
    ``[prices]`` entries.
    """
    table = _load_prices(prices, DEFAULT_PRICES if default_prices else ())
    if budgets is None:
        budgets = []
    if not isinstance(budgets, list) or not all(isinstance(b, Mapping) for b in budgets):
        raise ValueError("[[budgets]] must be an array of tables")
    loaded = tuple(_load_budget(b, i, retention) for i, b in enumerate(budgets))
    names = [b.name for b in loaded]
    if dupes := sorted({n for n in names if names.count(n) > 1}):
        raise ValueError(f"[[budgets]] names must be unique: {dupes}")
    usd = [b.name for b in loaded if b.unit is BudgetUnit.USD]
    if usd and not table.entries and not table.defaults:
        raise ValueError(f"budgets in usd need prices ([prices], or the default table): {usd}")
    return BudgetConfig(loaded, table)


def _load_prices(data: Any, defaults: tuple[Any, ...] = ()) -> PriceTable:
    if data is None:
        return PriceTable((), defaults)
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
    return PriceTable(tuple(entries), defaults)


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
    providers: tuple[str, ...] | None = None
    if "providers" in data:
        raw = data["providers"]
        if (
            not isinstance(raw, list)
            or not raw
            or not all(isinstance(p, str) and p.strip() for p in raw)
        ):
            raise ValueError(f"{where} providers must be a non-empty list of provider names")
        providers = tuple(p.strip() for p in raw)
    min_limit_window = DEFAULT_MIN_LIMIT_WINDOW
    if "min_limit_window" in data:
        min_limit_window = _duration(data["min_limit_window"], f"{where} min_limit_window")
    if mode is not BudgetMode.SUBSCRIPTION and ("providers" in data or "min_limit_window" in data):
        raise ValueError(f"{where}: providers and min_limit_window are for mode = subscription")
    return Budget(
        name,
        scope,
        unit,
        window,
        soft,
        hard,
        match,
        burn_window,
        mode,
        providers,
        min_limit_window,
    )


# --- evaluation --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BudgetEvaluation:
    """:func:`evaluate`'s result: ``statuses`` go into ``Snapshot.budgets``;
    ``unattributed`` is, per budget, the tokens in its window whose subject is unknown
    (a session no adapter ever reported an owner for, a span with no task, a task whose
    labels were never seen), so they could not be charged to any subject."""

    statuses: tuple[BudgetStatus, ...] = ()
    unattributed: Mapping[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _Spend:
    spent: float = 0.0
    cost_unknown_tokens: int = 0
    sessions: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _Known:
    """Attribution the snapshot reports now, over what the span store remembers."""

    owners: Mapping[str, str]
    session_tasks: Mapping[str, str]
    labels: Mapping[str, tuple[str, ...]]

    @classmethod
    def of(cls, snapshot: Snapshot, remembered: Attribution | None) -> _Known:
        base = remembered or Attribution()
        return cls(
            owners={**base.owners, **{s.id: s.owner_id for s in snapshot.sessions}},
            session_tasks={**base.session_tasks, **{s.id: s.task_id for s in snapshot.sessions}},
            labels={**base.labels, **{t.id: t.labels for t in snapshot.tasks}},
        )


_UNKNOWN = object()  # the row's subject cannot be told


def evaluate(
    config: BudgetConfig,
    usage: Mapping[timedelta, Sequence[TokenUsage]],
    snapshot: Snapshot,
    attribution: Attribution | None = None,
) -> BudgetEvaluation:
    """Budget state per budget and subject, in config order then by subject.

    ``usage`` maps each of ``config.windows`` to the counted token usage of that rolling
    window ending at ``snapshot.now``; a missing window counts as no usage. Owners, tasks
    and labels come from the snapshot, then from ``attribution`` (what the span store
    remembers of sessions and tasks adapters no longer report). Limit hits come from the
    snapshot.
    """
    known = _Known.of(snapshot, attribution)
    out: list[BudgetStatus] = []
    unattributed: dict[str, int] = {}
    prices = _DayPrices(config.prices, snapshot.now.date())
    for budget in config.budgets:
        window, lost = _spend(budget, prices, usage.get(budget.window, ()), known)
        recent, _ = _spend(budget, prices, usage.get(budget.burn_window, ()), known)
        if budget.scope is not BudgetScope.DAY:
            unattributed[budget.name] = lost
        for subject in sorted(set(window) | set(budget.fixed_subjects())):
            spend = window.get(subject) or _Spend()
            burned = recent[subject].spent if subject in recent else 0.0
            out.append(_status(budget, subject, spend, burned, snapshot))
    return BudgetEvaluation(tuple(out), unattributed)


@dataclass(slots=True)
class _DayPrices:
    """The price table on one day, each model looked up once."""

    table: PriceTable
    day: date
    found: dict[str | None, Price | None] = field(default_factory=dict)

    def price_for(self, model: str | None) -> Price | None:
        if model not in self.found:
            self.found[model] = self.table.price_for(model, self.day)
        return self.found[model]


def _status(
    budget: Budget, subject: str, spend: _Spend, burned: float, snapshot: Snapshot
) -> BudgetStatus:
    now = snapshot.now
    remaining = max(budget.limit - spend.spent, 0.0)
    rate = burned / (budget.burn_window / timedelta(hours=1))
    subscription = budget.mode is BudgetMode.SUBSCRIPTION
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
        runs_out_at=threshold_forecast(remaining, rate, now),
        next_limit_at=(
            next_limit_at(budget.limit_hits(snapshot.limit_hits), now) if subscription else None
        ),
        window=budget.window,
        cost_unknown_tokens=spend.cost_unknown_tokens,
        estimated=subscription,
        session_ids=tuple(sorted(spend.sessions)),
    )


def _spend(
    budget: Budget, prices: _DayPrices, rows: Iterable[TokenUsage], known: _Known
) -> tuple[dict[str, _Spend], int]:
    """Spend per subject, and the tokens whose subject is unknown."""
    acc: dict[str, _Spend] = {}
    lost = 0
    for row in rows:
        tokens = row.input_tokens + row.output_tokens
        subject = _subject(budget, row, known)
        if subject is _UNKNOWN:
            lost += tokens
            continue
        if not isinstance(subject, str):
            continue  # known, and not this budget's
        spend = acc.setdefault(subject, _Spend())
        price = prices.price_for(row.model)
        if price is None:
            spend.cost_unknown_tokens += tokens
        if budget.unit is BudgetUnit.TOKENS:
            spend.spent += tokens
        elif price is not None:
            spend.spent += price.cost(row)
        if row.session_id:
            spend.sessions.add(row.session_id)
    return acc, lost


def _subject(budget: Budget, row: TokenUsage, known: _Known) -> str | object | None:
    """The row's subject; None when it is known and not this budget's; ``_UNKNOWN``."""
    session = row.session_id or ""
    match budget.scope:
        case BudgetScope.DAY:
            return FARM
        case BudgetScope.AGENT:
            subject = known.owners.get(session)
        case BudgetScope.TASK | BudgetScope.PROJECT:
            subject = row.task_id or known.session_tasks.get(session)
    if not subject:
        return _UNKNOWN
    if budget.match is None:
        return subject
    if budget.scope is not BudgetScope.PROJECT:
        return subject if fnmatchcase(subject, budget.match) else None
    if not budget.match.startswith(LABEL):
        return budget.match if fnmatchcase(subject, budget.match) else None
    labels = known.labels.get(subject)
    if labels is None:
        return _UNKNOWN
    return budget.match if budget.match[len(LABEL) :] in labels else None


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
