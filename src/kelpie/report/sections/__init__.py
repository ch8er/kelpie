"""Extra sections of the daily report, so new analyses join the one report instead of
starting another.

A section builder is a pure function of :class:`SectionContext` (the report's inputs, the
day's bounds and spans, and its pricing) that returns a :class:`Section` or None (nothing to
say today). Register it in a module of this package::

    from kelpie.report.sections import Advice, Section, SectionContext, register_section

    @register_section("tokens")
    def tokens(ctx: SectionContext) -> Section | None:
        ...
        return Section("Token optimization", ("- cache hit rate ...",), advice=(...,))

Every module here is imported when the report is built; builders run in name order. A
section's ``advice`` competes for the report's top 3 recommendations with the built-in ones
(ranked the same way: dollars, and hours with ``[report] hour_usd``). Its Markdown
``lines`` are rendered under ``## <title>`` after the top 3. A builder that raises is
logged and skipped, and the report says so: one analysis must not cost the whole report.
"""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from kelpie.core.model import Span
from kelpie.core.spans import SpanKey, TokenUsage

if TYPE_CHECKING:
    from kelpie.report.daily import ReportInputs, Spend


@dataclass(frozen=True, slots=True)
class Advice:
    """A recommendation: what to do, why, and what it is worth per day (dollars, hours)."""

    title: str
    detail: str
    usd: float = 0.0
    hours: float = 0.0
    tasks: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Section:
    title: str
    lines: tuple[str, ...]  # Markdown body, rendered under "## <title>"
    advice: tuple[Advice, ...] = ()  # candidates for the top 3


@dataclass(frozen=True, slots=True)
class SectionContext:
    """What a builder may read: the report's inputs, the day (``spans`` are the day's, and
    ``counted`` maps the LLM spans whose tokens count to the model to price them by) and
    ``price``, which turns token usage into :class:`~kelpie.report.daily.Spend`."""

    inputs: ReportInputs
    start: datetime
    end: datetime
    spans: Sequence[Span]
    counted: dict[SpanKey, str | None]
    price: Callable[[Iterable[TokenUsage]], Spend]


SectionBuilder = Callable[[SectionContext], "Section | None"]
SECTIONS: dict[str, SectionBuilder] = {}


def register_section(name: str) -> Callable[[SectionBuilder], SectionBuilder]:
    def decorator(builder: SectionBuilder) -> SectionBuilder:
        if name in SECTIONS:
            raise ValueError(f"report section already registered: {name}")
        SECTIONS[name] = builder
        return builder

    return decorator


def load_sections() -> None:
    """Import every module of this package so their builders register."""
    for module in pkgutil.iter_modules(__path__):
        importlib.import_module(f"{__name__}.{module.name}")
