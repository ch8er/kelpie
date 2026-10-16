"""Extra sections of the daily report, so new analyses join the one report instead of
starting another.

A section builder is a function of :class:`SectionContext` that returns a :class:`Section`
or None (nothing to say today). Register it in a module of this package::

    from kelpie.report.sections import Advice, Section, SectionContext, register_section

    @register_section("tokens")
    def tokens(ctx: SectionContext) -> Section | None:
        ...
        return Section("Token optimization", ("- cache hit rate ...",), advice=(...,))

Rules for builders, as for the rest of the report: no I/O, no clock reads (the day ends at
``ctx.end``), no LLM, and the same context gives the same section. The context is
read-only; ``inputs`` holds the report's own sequences, so do not mutate them.

Every module here is imported when the report is built (:func:`load_sections`); builders
run in name order. A section's ``advice`` competes for the report's top 3 recommendations
with the built-in ones, ranked the same way (dollars, and hours with ``[report]
hour_usd``); an advice whose title is already taken is dropped. Its Markdown ``lines`` are
rendered under ``## <title>`` after the top 3. A module that fails to import, a builder that
raises or one that returns something else than a ``Section`` is logged and noted in the
report: one analysis must not cost the whole report.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from kelpie.core.model import Span
from kelpie.core.spans import SpanKey, TokenUsage

if TYPE_CHECKING:
    from kelpie.report.daily import ReportInputs

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Spend:
    """Tokens and their price; ``unpriced_tokens`` are of models without a price."""

    tokens: int = 0
    usd: float = 0.0
    unpriced_tokens: int = 0

    def __add__(self, other: Spend) -> Spend:
        return Spend(
            self.tokens + other.tokens,
            self.usd + other.usd,
            self.unpriced_tokens + other.unpriced_tokens,
        )


@dataclass(frozen=True, slots=True)
class Advice:
    """A recommendation: what to do, why, and what it is worth per day (dollars, hours).
    The report renders ``**title** (value): detail.``, so ``detail`` has no final period."""

    title: str
    detail: str
    usd: float = 0.0
    hours: float = 0.0
    tasks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    lines: tuple[str, ...]  # Markdown body under "## <title>"; empty: the section is left out
    advice: tuple[Advice, ...] = ()  # candidates for the top 3


@dataclass(frozen=True, slots=True)
class SectionContext:
    """What a builder may read: the report's ``inputs``; the day (``start``, ``end``, its
    ``spans`` and ``spend``); ``counted``, the LLM spans whose tokens count (nested
    instrumentations left out) with the model to price them by; ``roles``, the role of each
    session's owner; and ``price``, which turns token usage into :class:`Spend`."""

    inputs: ReportInputs
    start: datetime
    end: datetime
    spans: tuple[Span, ...]
    counted: Mapping[SpanKey, str | None]
    price: Callable[[Iterable[TokenUsage]], Spend]
    spend: Spend
    roles: Mapping[str, str]


SectionBuilder = Callable[[SectionContext], "Section | None"]
SECTIONS: dict[str, SectionBuilder] = {}


def register_section(name: str) -> Callable[[SectionBuilder], SectionBuilder]:
    def decorator(builder: SectionBuilder) -> SectionBuilder:
        if name in SECTIONS:
            raise ValueError(f"report section already registered: {name}")
        SECTIONS[name] = builder
        return builder

    return decorator


def load_sections() -> list[str]:
    """Import every module of this package so their builders register; returns the
    modules that failed to import (logged, never raised)."""
    failed = []
    for module in pkgutil.iter_modules(__path__):
        try:
            importlib.import_module(f"{__name__}.{module.name}")
        except Exception:
            log.exception("report section module %s failed to load", module.name)
            failed.append(module.name)
    return failed


def run_sections(
    builders: Mapping[str, SectionBuilder], ctx: SectionContext, failed: Sequence[str] = ()
) -> tuple[Section, ...]:
    """Every builder's section in name order, with a note for each that failed."""
    out = [
        Section(name, (f"This section failed to load ({name}); see the log.",)) for name in failed
    ]
    for name in sorted(builders):
        try:
            found = builders[name](ctx)
            if found is not None and not (
                isinstance(found, Section)
                and isinstance(found.lines, tuple)
                and all(isinstance(a, Advice) for a in found.advice)
            ):
                raise TypeError(f"returned {type(found).__name__}, not a Section")
        except Exception:
            log.exception("report section %s failed", name)
            found = Section(name, (f"This section failed ({name}); see the log.",))
        if found is not None and found.lines:
            out.append(found)
    return tuple(out)
