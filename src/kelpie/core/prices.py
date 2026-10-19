"""Model prices: Kelpie's dated default table, ``[prices]`` overrides and model-id matching.

Pure: data and lookups, no I/O and no clock (callers pass the day a price is wanted for).

**The default table** (:data:`DEFAULT_PRICES`) holds USD per 1M tokens for popular
Anthropic, OpenAI and Google models, as published on each provider's pricing page on the
day in :data:`CHECKED`; :data:`SOURCES` names the pages. Prices change: a ``[prices]``
entry overrides any default, ``[spend] default_prices = false`` turns the table off, and a
model the table does not know is reported (``kelpie doctor``, ``kelpie status``, the daily
report) instead of silently costing $0. An announced change (an introductory price that
ends) is a second row with ``since``. What the table does not model, so an estimate can
differ from the bill: prompt-length tiers (Claude Haiku 5.5 over 100k prompt tokens,
OpenAI over 272k, Gemini over 200k are dearer; the base tier is used), 1-hour cache writes
(priced as 5-minute ones), batch and flex discounts, priority and fast tiers, regional or
US-only uplifts, and the storage fee of explicit Gemini caches.

**Matching** (:meth:`PriceTable.price_for`): a ``[prices]`` entry named exactly as the
span's model or as its canonical id wins, then the first ``[prices]`` glob in config order
that matches either, then the default table on the canonical id. The canonical id
(:func:`canonical_model`) is lower case, without a date suffix, ``-latest``, a Claude Code
``[1m]`` tag, a ``vendor/`` or ``models/`` prefix, Bedrock's ``anthropic.`` and region
prefixes and ``-v1:0`` suffix, and Vertex's ``@date``. So ``claude-haiku-4-5-20251001``,
``us.anthropic.claude-haiku-4-5-20251001-v1:0`` and ``claude-haiku-4-5@20251001`` all
find ``claude-haiku-4-5``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from fnmatch import fnmatchcase

from kelpie.core.spans import TokenUsage

__all__ = [
    "CHECKED",
    "DEFAULT_PRICES",
    "PER_TOKENS",
    "SOURCES",
    "DefaultPrice",
    "Price",
    "PriceTable",
    "canonical_model",
]

PER_TOKENS = 1_000_000  # prices are USD per this many tokens
CHECKED = date(2026, 10, 19)  # the day the default table was compared with the sources
SOURCES = {
    "anthropic": "https://platform.claude.com/docs/en/about-claude/pricing",
    "openai": "https://developers.openai.com/api/docs/pricing",
    "google": "https://ai.google.dev/gemini-api/docs/pricing",
}
MAX_MODEL = 200  # characters of a model id that are looked at


@dataclass(frozen=True, slots=True)
class Price:
    """USD per 1M tokens. Cached input tokens (cache reads) are part of ``input_tokens``
    and priced at ``cached_input`` instead of ``input``. ``cache_write`` is the price of
    writing the prompt cache (5-minute writes), None where writes cost the input price."""

    input: float
    output: float
    cached_input: float
    cache_write: float | None = None

    def cost(self, usage: TokenUsage) -> float:
        cached = min(usage.cached_tokens, usage.input_tokens)
        return (
            (usage.input_tokens - cached) * self.input
            + cached * self.cached_input
            + usage.output_tokens * self.output
        ) / PER_TOKENS


@dataclass(frozen=True, slots=True)
class DefaultPrice:
    """One row of the default table: ``model`` is a canonical id, ``aliases`` other
    canonical ids of the same model; ``since`` the first day of this price (None: before
    any other row of the model)."""

    model: str
    provider: str
    price: Price
    since: date | None = None
    aliases: tuple[str, ...] = ()


def _p(inp: float, out: float, cached: float, write: float | None = None) -> Price:
    return Price(inp, out, cached, write)


# USD per 1M tokens: input, output, cache read, cache write (5 minutes).
DEFAULT_PRICES: tuple[DefaultPrice, ...] = (
    # Anthropic: platform.claude.com/docs/en/about-claude/pricing (first-party API rates).
    DefaultPrice("claude-fable-5-1", "anthropic", _p(10, 50, 0.25, 12.5)),
    DefaultPrice("claude-mythos-5-1", "anthropic", _p(10, 50, 0.25, 12.5)),
    DefaultPrice("claude-fable-5", "anthropic", _p(10, 50, 1, 12.5)),
    DefaultPrice("claude-mythos-5", "anthropic", _p(10, 50, 1, 12.5)),
    DefaultPrice("claude-opus-5-5", "anthropic", _p(4, 20, 0.2, 5)),
    DefaultPrice("claude-opus-5", "anthropic", _p(5, 25, 0.5, 6.25)),
    DefaultPrice("claude-opus-4-8", "anthropic", _p(5, 25, 0.5, 6.25)),
    DefaultPrice("claude-opus-4-7", "anthropic", _p(5, 25, 0.5, 6.25)),
    DefaultPrice("claude-opus-4-6", "anthropic", _p(5, 25, 0.5, 6.25)),
    DefaultPrice("claude-opus-4-5", "anthropic", _p(5, 25, 0.5, 6.25)),
    DefaultPrice("claude-opus-4-1", "anthropic", _p(15, 75, 1.5, 18.75)),
    DefaultPrice(
        "claude-opus-4-0", "anthropic", _p(15, 75, 1.5, 18.75), aliases=("claude-opus-4",)
    ),
    DefaultPrice("claude-sonnet-5-5", "anthropic", _p(2, 10, 0.1, 2.5)),
    DefaultPrice("claude-sonnet-5", "anthropic", _p(2, 10, 0.2, 2.5)),
    DefaultPrice("claude-sonnet-4-6", "anthropic", _p(3, 15, 0.3, 3.75)),
    DefaultPrice("claude-sonnet-4-5", "anthropic", _p(3, 15, 0.3, 3.75)),
    DefaultPrice(
        "claude-sonnet-4-0", "anthropic", _p(3, 15, 0.3, 3.75), aliases=("claude-sonnet-4",)
    ),
    DefaultPrice("claude-haiku-5-5", "anthropic", _p(0.1, 0.5, 0.01, 0.125)),  # prompts <= 100k
    DefaultPrice("claude-haiku-4-5", "anthropic", _p(1, 5, 0.1, 1.25)),
    DefaultPrice("claude-3-5-haiku", "anthropic", _p(0.8, 4, 0.08, 1)),
    # OpenAI: developers.openai.com/api/docs/pricing (standard tier, short context).
    DefaultPrice("gpt-5.5", "openai", _p(5, 30, 0.5)),
    DefaultPrice("gpt-5.4", "openai", _p(2.5, 15, 0.25)),
    DefaultPrice("gpt-5.4-mini", "openai", _p(0.75, 4.5, 0.075)),
    DefaultPrice("gpt-5", "openai", _p(1.25, 10, 0.125)),
    # Google: ai.google.dev/gemini-api/docs/pricing (paid tier, standard, text).
    DefaultPrice("gemini-4-argon", "google", _p(2, 10, 0.1)),  # introductory
    DefaultPrice("gemini-4-argon", "google", _p(4, 20, 0.2), since=date(2026, 12, 1)),
    DefaultPrice("gemini-3.8-flash", "google", _p(0.75, 3.75, 0.075)),  # introductory
    DefaultPrice("gemini-3.8-flash", "google", _p(1.5, 7.5, 0.15), since=date(2027, 1, 1)),
    DefaultPrice("gemini-3-flash-preview", "google", _p(0.5, 3, 0.05)),
)

_TAG = re.compile(r"\[[^\]]*\]$")  # claude-opus-4-6[1m]
_BEDROCK_SUFFIX = re.compile(r"(?:-v\d+)?:\d+$")  # ...-v1:0
_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]{2,6}\.)?anthropic\.")  # us.anthropic., anthropic.
_VERSION = re.compile(r"-(?:latest|\d{8}|\d{4}-\d{2}-\d{2})$")


def canonical_model(model: str) -> str:
    """The id a model is priced by: ``us.anthropic.claude-haiku-4-5-20251001-v1:0`` and
    ``models/gemini-3.8-flash`` become ``claude-haiku-4-5`` and ``gemini-3.8-flash``."""
    s = model.strip().lower()[:MAX_MODEL]
    s = _TAG.sub("", s)
    s = s.split("@", 1)[0]
    s = _BEDROCK_SUFFIX.sub("", s)
    s = s.rsplit("/", 1)[-1]
    s = _BEDROCK_PREFIX.sub("", s)
    return _VERSION.sub("", s)


def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in "*?[")


def _exact(entries: tuple[tuple[str, Price], ...], model: str) -> Price | None:
    return next((price for pattern, price in entries if pattern == model), None)


def _glob(entries: tuple[tuple[str, Price], ...], model: str) -> Price | None:
    return next(
        (p for pattern, p in entries if _is_glob(pattern) and fnmatchcase(model, pattern)), None
    )


@dataclass(frozen=True, slots=True)
class PriceTable:
    """``[prices]`` entries (model name or glob -> :class:`Price`, in config order) over
    the default table. See the module docstring for the order things are matched in."""

    entries: tuple[tuple[str, Price], ...] = ()
    defaults: tuple[DefaultPrice, ...] = ()

    def price_for(self, model: str | None, on: date | None = None) -> Price | None:
        """The price of ``model`` on day ``on`` (None: the day the defaults were
        checked); None when nothing matches."""
        if not model:
            return None
        found = self.configured_price(model)
        if found is None and (row := self.default_for(model, on)) is not None:
            found = row.price
        return found

    def configured_price(self, model: str, *, globs: bool = True) -> Price | None:
        """The ``[prices]`` entry for ``model``, if any (the default table aside);
        ``globs=False``: only an entry that names the model."""
        if not self.entries:
            return None
        canonical = canonical_model(model)
        exact = tuple((canonical_model(p), v) for p, v in self.entries if not _is_glob(p))
        found = _exact(self.entries, model) or _exact(exact, canonical)
        if found is None and globs:
            found = _glob(self.entries, model) or _glob(self.entries, canonical)
        return found

    def named_price(self, model: str | None, on: date | None = None) -> Price | None:
        """Like :meth:`price_for` without ``[prices]`` globs: a price set for this very
        model, in ``[prices]`` or the default table."""
        if not model or _is_glob(model):
            return None
        found = self.configured_price(model, globs=False)
        if found is None and (row := self.default_for(model, on)) is not None:
            found = row.price
        return found

    def default_for(self, model: str, on: date | None = None) -> DefaultPrice | None:
        """The default-table row that prices ``model`` on ``on``, if any."""
        if not self.defaults:
            return None
        canonical = canonical_model(model)
        day = on or CHECKED
        rows = [
            r
            for r in self.defaults
            if (r.model == canonical or canonical in r.aliases)
            and (r.since is None or r.since <= day)
        ]
        return max(rows, key=lambda r: r.since or date.min) if rows else None

    def provider_for(self, model: str | None) -> str | None:
        """The provider the default table knows ``model`` under, if any."""
        if not model:
            return None
        canonical = canonical_model(model)
        return next(
            (r.provider for r in self.defaults if r.model == canonical or canonical in r.aliases),
            None,
        )

    def unpriced(self, models: Iterable[str | None]) -> tuple[str, ...]:
        """Those of ``models`` nothing prices, sorted and without repeats."""
        return tuple(sorted({m for m in models if m and self.price_for(m) is None}))

    def configured(self) -> PriceTable:
        """The same ``[prices]`` entries without the default table."""
        return PriceTable(self.entries)
