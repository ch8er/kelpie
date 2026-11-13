"""Profile the daily report on a large synthetic database (roadmap "code health", #248).

    uv run python -m tests.perf.profile_report [--spans N] [--ledger-days D] [--db PATH]

Builds the database of :mod:`tests.perf.report_db` (unless ``--db`` names one that
exists), then times and profiles what ``kelpie report`` does for the last full day: the
store reads (:func:`kelpie.report.run.load_inputs`), the report (:func:`build_report`) and
its Markdown, on the 2,000-task board of :func:`tests.perf.farm.large_farm`. Prints the
phases' times and the top functions by cumulative time.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import tempfile
import time
from collections.abc import Callable, Sized
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from kelpie.app import Settings
from kelpie.core.storage import SqliteLedgerStore, SqliteSpanStore
from kelpie.report.cli import sources
from kelpie.report.daily import build_report, render_markdown
from kelpie.report.run import load_inputs
from tests.perf.farm import NOW, large_farm
from tests.perf.report_db import build, counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--spans", type=int, default=1_000_000)
    parser.add_argument("--ledger-days", type=int, default=182)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--top", type=int, default=25)
    parser.add_argument("--no-profile", action="store_true", help="times only")
    args = parser.parse_args()
    path = args.db or Path(tempfile.mkdtemp()) / "kelpie.db"
    if not path.exists():
        started = time.perf_counter()
        build(path, NOW, spans=args.spans, ledger_days=args.ledger_days)
        print(f"built {path} in {time.perf_counter() - started:.1f}s: {counts(path)}")
    settings = replace(Settings(), db_path=path)
    src = sources(settings)
    board = large_farm()
    day = (NOW - timedelta(days=1)).date()
    profile = cProfile.Profile()
    times: dict[str, float] = {}
    if not args.no_profile:
        profile.enable()
    t0 = time.perf_counter()
    inputs = load_inputs(src, day, board)
    t1 = time.perf_counter()
    report = build_report(inputs)
    t2 = time.perf_counter()
    render_markdown(report)
    t3 = time.perf_counter()
    profile.disable()
    times = {"load_inputs": t1 - t0, "build_report": t2 - t1, "render": t3 - t2}
    print({k: round(v, 2) for k, v in times.items()}, f"total {t3 - t0:.2f}s")
    print(f"spans read: {len(inputs.spans)}, actions read: {len(inputs.actions)}")
    if not args.no_profile:
        out = io.StringIO()
        pstats.Stats(profile, stream=out).sort_stats("cumulative").print_stats(args.top)
        print(out.getvalue())
    serve_reads(path)


def _timed(what: str, fn: Callable[[], object]) -> None:
    started = time.perf_counter()
    found = fn()
    size = len(found) if isinstance(found, Sized) else ""
    print(f"  {what}: {time.perf_counter() - started:.3f}s {size}")


def serve_reads(path: Path) -> None:
    """What ``kelpie serve`` reads from the same stores on a tick (or every few minutes)."""
    print("serve reads:")
    spans = SqliteSpanStore(path, readonly=True)
    day = timedelta(days=1)
    _timed("changes (24h window, cold cache)", lambda: spans.changes(NOW - day, 0).spans)
    _timed("totals_since 24h", lambda: spans.totals_since(NOW - day))
    _timed("usage_since 1d/7d/30d", lambda: spans.usage_since(NOW, [day, 7 * day, 30 * day]))
    _timed("tool_spans_since 7d", lambda: spans.tool_spans_since(NOW - 7 * day))
    _timed("attribution", lambda: spans.attribution().owners)
    spans.close()
    ledger = SqliteLedgerStore(path)
    _timed("ledger since 1h", lambda: ledger.since(NOW - timedelta(hours=1)))
    _timed(
        "ledger by_fingerprint 7d",
        lambda: ledger.by_fingerprint("fp-stale_blocks-T-1-1", NOW - 7 * day),
    )
    _timed("ledger by_subject 1h", lambda: ledger.by_subject("T-1", NOW - timedelta(hours=1)))
    ledger.close()


if __name__ == "__main__":
    main()
