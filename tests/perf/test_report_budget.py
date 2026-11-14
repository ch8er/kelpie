"""Time budget of the daily report on a large synthetic database (roadmap "code health",
#248): the same code path as ``kelpie report`` (store reads, the report, its Markdown) on
the 2,000-task synthetic board, over 100,000 stored spans and a month of ledger. The
budget is several times what a 2-vCPU sandbox measures, so a slow CI runner passes and a
read or section that turns quadratic fails. ``python -m tests.perf.profile_report``
profiles the full size (1M spans, 6 months of ledger)."""

from __future__ import annotations

import time
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from kelpie.app import Settings
from kelpie.report.cli import sources
from kelpie.report.daily import build_report, render_markdown
from kelpie.report.run import load_inputs
from tests.perf.farm import NOW, large_farm
from tests.perf.report_db import build

# Measured on a 2-vCPU sandbox (2026-11-13), without coverage: ~0.6 s; under coverage CI
# is 2-3x slower. The margin is ~10x.
REPORT_BUDGET = timedelta(seconds=15)


def test_the_daily_report_on_a_large_database_fits_the_budget(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.db"
    spans, actions = build(path, NOW, spans=100_000, days=30, ledger_days=30)
    assert spans > 95_000 and actions == 90_000
    src = sources(replace(Settings(), db_path=path))
    board = large_farm()
    started = time.perf_counter()
    inputs = load_inputs(src, (NOW - timedelta(days=1)).date(), board)
    markdown = render_markdown(build_report(inputs))
    took = time.perf_counter() - started
    assert inputs.spans and inputs.actions and "##" in markdown
    assert took < REPORT_BUDGET.total_seconds(), f"the report took {took:.1f}s"
