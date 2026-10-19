"""``[spend]``: how Kelpie turns tokens into spend, and in which unit, per provider.

Pure: config and lookups, no I/O and no clock.

``default_prices`` (true by default) puts Kelpie's dated default price table
(:mod:`kelpie.core.prices`) under the ``[prices]`` entries; false leaves only ``[prices]``.

**Spend mode per provider** (:class:`SpendMode`). With an API key every token is billed,
so spend is dollars; on a subscription there is no bill, only a usage limit, so spend is
tokens and the share of that limit, and dollars would only estimate what the same tokens
would have cost. ``[spend.providers.<name>] mode`` sets it; ``auto`` (the default) decides
from the config alone, so every command that reads modes gives the same answer offline
(:meth:`SpendConfig.mode_for`). First match wins:

1. ``mode = "api"`` or ``"subscription"`` in ``[spend.providers.<name>]``;
2. a provider cost report configured for it (an admin key: there is a bill to read): API;
3. a ``[[budgets]]`` entry with ``mode = "subscription"`` whose ``providers`` (names of
   ``[connectors.llm_quotas]`` probes) name the provider itself or one of its ``quotas``,
   or that names no providers (it covers them all): subscription;
4. ``limit_tokens`` set for it (a usage limit to measure against): subscription;
5. a ``day`` budget (the whole farm) in ``usd`` with mode ``api`` (the budget default):
   API, as weak evidence;
6. otherwise unknown, and ``kelpie doctor`` says how to set it.

A model of no known provider (``mode_for(None)``) is subscription only under a subscription
budget that names no providers; otherwise unknown.

**Provider of a model** (:meth:`SpendConfig.provider_for`): the first
``[spend.providers.<name>] models`` pattern (a name or a glob, on the model id as written
or, case-insensitively, its canonical id) that matches, then the provider the default
price table knows the model under, then the provider its name gives away
(:data:`PROVIDER_PATTERNS`: ``claude-*`` is Anthropic, ``gpt-*``, ``chatgpt-*``,
``codex-*`` and ``o<digit>*`` OpenAI, ``gemini-*`` Google); else none.

**Limit share** (:meth:`SpendConfig.limit_for`): ``limit_tokens`` per ``limit_window``
(``"5h"``, ``"7d"``) as the user knows them for their plan. Providers describe
subscription limits as usage windows, not token counts, so Kelpie never guesses one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Any

from kelpie.core.budgets import Budget, BudgetMode, BudgetScope, BudgetUnit
from kelpie.core.model import Reconciliation, ReportedCost
from kelpie.core.prices import PriceTable, canonical_model, model_id
from kelpie.core.serde import parse_duration

__all__ = [
    "PROVIDER_PATTERNS",
    "Estimate",
    "GapThreshold",
    "ProviderMode",
    "ProviderSpend",
    "SpendConfig",
    "SpendMode",
    "SpendSettings",
    "load_spend",
    "reconcile",
]

SPEND_KEYS = frozenset({"default_prices", "providers"})
PROVIDER_KEYS = frozenset({"mode", "models", "quotas", "limit_tokens", "limit_window"})
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}")  # a bare TOML key
_QUOTA_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}")  # as [connectors.llm_quotas]
SETTINGS = ("auto", "api", "subscription")  # [spend.providers.<name>] mode
# Canonical model ids (kelpie.core.prices.canonical_model) whose provider the name tells.
PROVIDER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("claude-*", "anthropic"),
    ("gpt-*", "openai"),
    ("chatgpt-*", "openai"),
    ("o[0-9]*", "openai"),
    ("codex-*", "openai"),
    ("gemini-*", "google"),
)


class SpendMode(StrEnum):
    API = "api"  # pay per token: spend is dollars
    SUBSCRIPTION = "subscription"  # a plan with a usage limit: tokens and limit share
    UNKNOWN = "unknown"  # not set, and nothing in the config tells


@dataclass(frozen=True, slots=True)
class ProviderSpend:
    """One ``[spend.providers.<name>]`` table."""

    name: str
    mode: str = "auto"  # one of SETTINGS
    models: tuple[str, ...] = ()
    quotas: tuple[str, ...] = ()  # [connectors.llm_quotas] probes of this provider
    limit_tokens: int | None = None
    limit_window: timedelta | None = None


@dataclass(frozen=True, slots=True)
class SpendSettings:
    """``[spend]`` as written."""

    default_prices: bool = True
    providers: tuple[ProviderSpend, ...] = ()


@dataclass(frozen=True, slots=True)
class ProviderMode:
    """A provider's spend mode and why (``kelpie status``, ``kelpie doctor``, the report)."""

    provider: str | None
    mode: SpendMode
    reason: str


@dataclass(frozen=True, slots=True)
class SpendConfig:
    """``[spend]`` with what decides each provider's mode: the budgets, the price table and
    the providers that have a cost report configured."""

    settings: SpendSettings = field(default_factory=SpendSettings)
    prices: PriceTable = field(default_factory=PriceTable)
    budgets: tuple[Budget, ...] = ()
    cost_reports: frozenset[str] = frozenset()

    @property
    def providers(self) -> tuple[str, ...]:
        """The providers ``[spend.providers]`` names, in config order."""
        return tuple(p.name for p in self.settings.providers)

    def _own(self, name: str | None) -> ProviderSpend | None:
        return next((p for p in self.settings.providers if p.name == name), None)

    def configured_provider(self, model: str | None, *, as_written: bool = False) -> str | None:
        """The ``[spend.providers.<name>]`` whose ``models`` match ``model``, if any.
        ``as_written``: only a pattern that matches the id as written and not the id
        without its wrapping (:func:`~kelpie.core.prices.model_id`), i.e. one that names
        the platform (``*.anthropic.*``, ``*@*``, ``openrouter/*``); so ``claude-*`` claims
        neither ``us.anthropic.claude-...`` nor Vertex's ``claude-...@date``."""
        if not model:
            return None
        canonical = canonical_model(model)
        for p in self.settings.providers:
            for m in p.models:
                if as_written:
                    if fnmatchcase(model, m) and not fnmatchcase(model_id(model), m):
                        return p.name
                elif fnmatchcase(model, m) or fnmatchcase(canonical, m.lower()):
                    return p.name
        return None

    def provider_for(self, model: str | None) -> str | None:
        """The provider ``model`` belongs to, if anything tells (module docstring)."""
        if not model:
            return None
        if (found := self.configured_provider(model)) is not None:
            return found
        if (found := self.prices.provider_for(model)) is not None:
            return found
        canonical = canonical_model(model)
        return next((v for pat, v in PROVIDER_PATTERNS if fnmatchcase(canonical, pat)), None)

    def mode_for(self, provider: str | None) -> ProviderMode:
        """The spend mode of ``provider`` and why (module docstring, first match wins)."""
        if provider is None:  # only a budget that covers every provider tells
            for b in self.budgets:
                if b.mode is BudgetMode.SUBSCRIPTION and b.providers is None:
                    reason = f"budget {b.name!r} has mode = subscription and covers every provider"
                    return ProviderMode(None, SpendMode.SUBSCRIPTION, reason)
            return ProviderMode(None, SpendMode.UNKNOWN, "the model's provider is not known")
        where = f"[spend.providers.{provider}]"
        own = self._own(provider)
        if own is not None and own.mode != "auto":
            return ProviderMode(provider, SpendMode(own.mode), f"mode set in {where}")
        if provider in self.cost_reports:
            return ProviderMode(provider, SpendMode.API, "its cost report is configured")
        names = {provider, *(own.quotas if own is not None else ())}
        for b in self.budgets:
            if b.mode is BudgetMode.SUBSCRIPTION and (
                b.providers is None or names & set(b.providers)
            ):
                whom = "names it" if b.providers is not None else "covers every provider"
                reason = f"budget {b.name!r} has mode = subscription and {whom}"
                return ProviderMode(provider, SpendMode.SUBSCRIPTION, reason)
        if own is not None and own.limit_tokens is not None:
            return ProviderMode(provider, SpendMode.SUBSCRIPTION, f"limit_tokens set in {where}")
        for b in self.budgets:
            if b.mode is BudgetMode.API and b.unit is BudgetUnit.USD and b.scope is BudgetScope.DAY:
                reason = f"day budget {b.name!r} counts the farm's spend in usd, budget mode api"
                return ProviderMode(provider, SpendMode.API, reason)
        reason = f'nothing in the config tells; set {where} mode = "api" or "subscription"'
        return ProviderMode(provider, SpendMode.UNKNOWN, reason)

    def mode_for_model(self, model: str | None) -> ProviderMode:
        """:meth:`mode_for` the provider of ``model``."""
        return self.mode_for(self.provider_for(model))

    def limit_for(self, provider: str | None) -> tuple[int, timedelta] | None:
        """``(limit_tokens, limit_window)`` of ``provider`` when configured."""
        own = self._own(provider)
        if own is None or own.limit_tokens is None or own.limit_window is None:
            return None
        return own.limit_tokens, own.limit_window

    def unlinked(self) -> tuple[tuple[str, str], ...]:
        """(budget, name) for each name a subscription budget's ``providers`` lists that no
        provider answers to (:meth:`mode_for`'s test): not a ``[spend.providers]`` name,
        not in one's ``quotas``, not a provider Kelpie knows (``anthropic``, ``openai``,
        ``google``): that budget sets no provider's spend mode."""
        known = {p.name for p in self.settings.providers}
        known |= {q for p in self.settings.providers for q in p.quotas}
        known |= {r.provider for r in self.prices.defaults}
        known |= {provider for _, provider in PROVIDER_PATTERNS}
        return tuple(
            (b.name, name)
            for b in self.budgets
            if b.mode is BudgetMode.SUBSCRIPTION
            for name in b.providers or ()
            if name not in known
        )

    def modes(self, providers: Iterable[str | None] = ()) -> tuple[ProviderMode, ...]:
        """The modes of the configured providers, then of ``providers`` (e.g. those seen
        in the spans), each once."""
        names: list[str | None] = list(self.providers)
        names += [p for p in dict.fromkeys(providers) if p not in names]
        return tuple(self.mode_for(p) for p in names)


def load_spend(data: Any) -> SpendSettings:
    """Parse ``[spend]``; raises ValueError on anything unexpected."""
    if data is None:
        return SpendSettings()
    if not isinstance(data, Mapping):
        raise ValueError("[spend] must be a table")
    if extra := set(data) - SPEND_KEYS:
        raise ValueError(f"unknown [spend] keys: {sorted(extra)}")
    default_prices = data.get("default_prices", True)
    if not isinstance(default_prices, bool):
        raise ValueError("[spend] default_prices must be true or false")
    providers = data.get("providers", {})
    if not isinstance(providers, Mapping):
        raise ValueError("[spend.providers] must be a table of providers")
    loaded = tuple(_load_provider(name, p) for name, p in providers.items())
    return SpendSettings(default_prices=default_prices, providers=loaded)


def _load_provider(name: Any, data: Any) -> ProviderSpend:
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError("[spend.providers] names must be 1-63 letters, digits, '_' or '-'")
    where = f"[spend.providers.{name}]"
    if not isinstance(data, Mapping):
        raise ValueError(f"{where} must be a table")
    if extra := set(data) - PROVIDER_KEYS:
        raise ValueError(f"unknown {where} keys: {sorted(extra)}")
    mode = data.get("mode", "auto")
    if not isinstance(mode, str) or mode not in SETTINGS:
        raise ValueError(f'{where} mode must be "auto", "api" or "subscription"')
    models = data.get("models", [])
    if not isinstance(models, list) or not all(isinstance(m, str) and m.strip() for m in models):
        raise ValueError(f"{where} models must be a list of model names or globs")
    quotas = data.get("quotas", [])
    if not isinstance(quotas, list) or not all(
        isinstance(q, str) and _QUOTA_NAME.fullmatch(q) for q in quotas
    ):
        raise ValueError(f"{where} quotas must be a list of [connectors.llm_quotas] names")
    limit = data.get("limit_tokens")
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0):
        raise ValueError(f"{where} limit_tokens must be a positive whole number")
    window = None
    if "limit_window" in data:
        try:
            window = parse_duration(data["limit_window"])
        except ValueError:
            window = None
        if window is None or window <= timedelta(0) or not isinstance(data["limit_window"], str):
            raise ValueError(f'{where} limit_window must be a duration such as "5h" or "7d"')
    if (limit is None) != (window is None):
        raise ValueError(f"{where} limit_tokens and limit_window go together")
    if limit is not None and mode == "api":
        raise ValueError(f'{where}: limit_tokens is for subscriptions, not mode = "api"')
    return ProviderSpend(
        name=name,
        mode=mode,
        models=tuple(m.strip() for m in models),
        quotas=tuple(quotas),
        limit_tokens=limit,
        limit_window=window,
    )


# --- reconciliation with the bill ---------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GapThreshold:
    """When a reconciled day is ``large``: the gap is at least ``ratio`` of the bill and
    at least ``min_usd`` either way."""

    ratio: float = 0.25
    min_usd: float = 5.0


@dataclass(frozen=True, slots=True)
class Estimate:
    """A provider's day priced from traces: USD, and the tokens left out of it (models
    without a price, calls whose provider is not known)."""

    usd: float = 0.0
    unpriced_tokens: int = 0
    unknown_tokens: int = 0


def reconcile(
    reported: Iterable[ReportedCost],
    estimated: Mapping[tuple[str, date], Estimate],
    thresholds: Mapping[str, GapThreshold] | None = None,
    scopes: Mapping[str, str] | None = None,
) -> tuple[Reconciliation, ...]:
    """Each (provider, day) a cost report covers that has an estimate. ``scopes`` gives
    what a provider's figures cover (``Reconciliation.bill_scope``; ``"tokens"`` by
    default). A day is ``large`` when it has a gap past the provider's thresholds and the
    bill is not a credit. Newest day first, then by provider."""
    bills: dict[tuple[str, date], float] = {}
    for c in reported:
        bills[(c.provider, c.day)] = bills.get((c.provider, c.day), 0.0) + c.usd
    out = []
    for (provider, day), bill in bills.items():
        if (found := estimated.get((provider, day))) is None:
            continue
        usd, bill = round(found.usd, 6), round(bill, 6)
        gap = round(usd - bill, 6)
        ratio = gap / bill if bill > 0 else None
        limit = (thresholds or {}).get(provider, GapThreshold())
        large = (
            gap != 0
            and bill >= 0
            and abs(gap) >= limit.min_usd
            and (ratio is None or abs(ratio) >= limit.ratio)
        )
        out.append(
            Reconciliation(
                provider=provider,
                day=datetime.combine(day, datetime.min.time(), UTC),
                estimated_usd=usd,
                reported_usd=bill,
                gap_usd=gap,
                gap_ratio=ratio,
                large=large,
                unpriced_tokens=found.unpriced_tokens,
                unknown_tokens=found.unknown_tokens,
                bill_scope=(scopes or {}).get(provider, "tokens"),
            )
        )
    return tuple(sorted(out, key=lambda r: (-r.day.timestamp(), r.provider)))
