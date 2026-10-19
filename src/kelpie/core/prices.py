"""Model prices: Kelpie's dated default table, ``[prices]`` overrides and model-id matching.

Pure: data and lookups, no I/O and no clock (callers pass the day a price is wanted for).

**The default table** (:data:`DEFAULT_PRICES`) holds USD per 1M tokens for popular
Anthropic, OpenAI and Google models, as published on each provider's pricing page on the
day in :data:`CHECKED`; :data:`SOURCES` names the pages. Prices change: a ``[prices]``
entry overrides any default, ``[spend] default_prices = false`` turns the table off, and a
model the table does not know is reported (``kelpie doctor``, ``kelpie status``, the daily
report) instead of silently costing $0. An announced change (an introductory price that
ends) is a second row with ``since``. A row prices every snapshot of its model (dated
ids) at the model's current price. What the table does not model, so an estimate can
differ from the bill: prompt-length tiers (Claude Haiku 5.5 over 100k prompt tokens,
Claude Sonnet 4.5 and 4 over 200k, OpenAI over 272k, Gemini over 200k are dearer; the
base tier is used), 1-hour cache writes (priced as 5-minute ones), batch and flex
discounts, priority and fast tiers, regional or US-only uplifts, and the storage fee of
explicit Gemini caches.

**Model ids.** :func:`model_id` is the id without what a platform wraps around it: lower
case, without a Claude Code ``[1m]`` tag, a ``vendor/`` or ``models/`` prefix (OpenRouter,
Gemini), Bedrock's ``anthropic.`` and region prefixes with their ``-v1:0`` suffix, or
Vertex's ``@date``; dotted Claude versions become dashed (OpenRouter's
``claude-sonnet-4.5``). :func:`canonical_model` also drops a date or ``-latest`` suffix: it
names the model family. So ``claude-haiku-4-5-20251001``,
``us.anthropic.claude-haiku-4-5-20251001-v1:0`` and ``claude-haiku-4-5@20251001`` all
belong to ``claude-haiku-4-5``.

**Matching** (:meth:`PriceTable.price_for`), first match wins. Anything an earlier Kelpie
matched still matches the same entry:

1. a ``[prices]`` entry named exactly as the span's model;
2. the first ``[prices]`` glob, in config order, that matches the model as written;
3. a ``[prices]`` entry with the same :func:`model_id` (the same snapshot on another
   platform: ``[prices."claude-haiku-4-5-20251001"]`` prices the Bedrock id of it);
4. a ``[prices]`` entry without a date that names the model's family
   (``[prices."claude-haiku-4-5"]`` prices every snapshot; a dated entry never prices
   another snapshot);
5. the first ``[prices]`` glob that matches the :func:`model_id`, then the canonical id
   (case-insensitive);
6. the default table, by family.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from fnmatch import fnmatchcase
from functools import lru_cache

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
    "model_id",
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
        written = min(usage.cache_write_tokens, usage.input_tokens - cached)
        write = self.input if self.cache_write is None else self.cache_write
        return (
            (usage.input_tokens - cached - written) * self.input
            + cached * self.cached_input
            + written * write
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
_BEDROCK_PREFIX = re.compile(r"^(?:[a-z]{2,6}\.)?anthropic\.")  # us.anthropic., anthropic.
_BEDROCK_SUFFIX = re.compile(r"(?:-v\d+)?:\d+$")  # ...-v1:0
_CLAUDE_DOT = re.compile(r"(?<=\d)\.(?=\d)")  # claude-sonnet-4.5 (OpenRouter)
_VERSION = re.compile(r"-(?:latest|\d{8}|\d{4}-\d{2}-\d{2})$")


@lru_cache(maxsize=4096)
def model_id(model: str) -> str:
    """The model's id without platform wrapping (module docstring), date kept:
    ``us.anthropic.claude-haiku-4-5-20251001-v1:0`` is ``claude-haiku-4-5-20251001``."""
    s = model.strip().lower()[:MAX_MODEL]
    s = _TAG.sub("", s)
    s = s.split("@", 1)[0]
    s = s.rsplit("/", 1)[-1]
    if _BEDROCK_PREFIX.match(s):
        s = _BEDROCK_SUFFIX.sub("", _BEDROCK_PREFIX.sub("", s))
    if s.startswith("claude-"):
        s = _CLAUDE_DOT.sub("-", s)
    return s


@lru_cache(maxsize=4096)
def canonical_model(model: str) -> str:
    """The model family a price applies to: :func:`model_id` without a date or
    ``-latest`` suffix (``claude-haiku-4-5``, ``gpt-5.4-mini``, ``gemini-3.8-flash``)."""
    return _VERSION.sub("", model_id(model))


def _is_glob(pattern: str) -> bool:
    return any(c in pattern for c in "*?[")


def _exact(entries: tuple[tuple[str, Price], ...], model: str) -> Price | None:
    return next((price for pattern, price in entries if pattern == model), None)


def _glob(entries: tuple[tuple[str, Price], ...], model: str) -> Price | None:
    return next((price for pattern, price in entries if fnmatchcase(model, pattern)), None)


@dataclass(frozen=True, slots=True)
class PriceTable:
    """``[prices]`` entries (model name or glob -> :class:`Price`, in config order) over
    the default table. See the module docstring for the order things are matched in."""

    entries: tuple[tuple[str, Price], ...] = ()
    defaults: tuple[DefaultPrice, ...] = ()
    # Derived once (lookups run per usage row): exact entries by model id, family entries
    # (no date) by canonical id, globs as written and lower-cased, default rows by family.
    _globs: tuple[tuple[str, Price], ...] = field(init=False, repr=False, compare=False)
    _by_id: tuple[tuple[str, Price], ...] = field(init=False, repr=False, compare=False)
    _family: tuple[tuple[str, Price], ...] = field(init=False, repr=False, compare=False)
    _lower_globs: tuple[tuple[str, Price], ...] = field(init=False, repr=False, compare=False)
    _rows: Mapping[str, tuple[DefaultPrice, ...]] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        exact = [(p, v) for p, v in self.entries if not _is_glob(p)]
        globs = tuple((p, v) for p, v in self.entries if _is_glob(p))
        rows: dict[str, list[DefaultPrice]] = {}
        for r in self.defaults:
            for name in (r.model, *r.aliases):
                rows.setdefault(name, []).append(r)
        derived = {
            "_globs": globs,
            "_by_id": tuple((model_id(p), v) for p, v in exact),
            "_family": tuple(
                (model_id(p), v) for p, v in exact if model_id(p) == canonical_model(p)
            ),
            "_lower_globs": tuple((p.lower(), v) for p, v in globs),
            "_rows": {k: tuple(v) for k, v in rows.items()},
        }
        for name, value in derived.items():
            object.__setattr__(self, name, value)

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
        ``globs=False``: only an entry that names the model or its family."""
        if not self.entries:
            return None
        found = _exact(self.entries, model)
        if found is None and globs:
            found = _glob(self._globs, model)
        if found is None:
            found = _exact(self._by_id, model_id(model)) or _exact(
                self._family, canonical_model(model)
            )
        if found is None and globs:
            found = _glob(self._lower_globs, model_id(model)) or _glob(
                self._lower_globs, canonical_model(model)
            )
        return found

    def named_price(self, model: str | None, on: date | None = None) -> Price | None:
        """Like :meth:`price_for` without ``[prices]`` globs: a price set for this very
        model or its family, in ``[prices]`` or the default table."""
        if not model or _is_glob(model):
            return None
        found = self.configured_price(model, globs=False)
        if found is None and (row := self.default_for(model, on)) is not None:
            found = row.price
        return found

    def default_for(self, model: str, on: date | None = None) -> DefaultPrice | None:
        """The default-table row that prices ``model`` on ``on`` (None: the day the table
        was checked), if any."""
        rows = self._rows.get(canonical_model(model), ())
        day = on or CHECKED
        current = [r for r in rows if r.since is None or r.since <= day]
        return max(current, key=lambda r: r.since or date.min) if current else None

    def provider_for(self, model: str | None) -> str | None:
        """The provider the default table knows ``model`` under, if any."""
        if not model:
            return None
        rows = self._rows.get(canonical_model(model), ())
        return rows[0].provider if rows else None

    def unpriced(self, models: Iterable[str | None]) -> tuple[str, ...]:
        """Those of ``models`` nothing prices, sorted and without repeats."""
        return tuple(sorted({m for m in models if m and self.price_for(m) is None}))

    def configured(self) -> PriceTable:
        """The same ``[prices]`` entries without the default table."""
        return PriceTable(self.entries)
