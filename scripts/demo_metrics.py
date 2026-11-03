"""Record Kelpie's /metrics over the day of ``kelpie demo``, for the dashboard screenshot.

    uv run python scripts/demo_metrics.py demo.om       # OpenMetrics with timestamps
    promtool tsdb create-blocks-from openmetrics demo.om data/

The demo's Kelpie runs on a virtual clock and ticks only when the story says so, without
the control loop that feeds the metrics. This runs the same story with each tick fed to the
metrics as ``kelpie serve`` does (``ControlLoop.tick_once``), takes ``render()`` at the
virtual time after every tick, and repeats it every ``SCRAPE`` seconds until the next one, as
a Prometheus scraping that Kelpie would see. Wall-clock fields (start, last tick) are set to
the virtual clock. Everything is the demo's synthetic farm: no real host, repo or person.
See docs/assets/README.md for the rest of the pipeline.
"""

from __future__ import annotations

import io
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from kelpie.core.engine import Dispatched
from kelpie.core.model import Snapshot
from kelpie.demo.farm import DemoFarm
from kelpie.demo.narrate import Narrator
from kelpie.demo.story import DAY, Story

SCRAPE = timedelta(seconds=30)
LABELS = 'job="kelpie",instance="127.0.0.1:9464"'
SAMPLE = re.compile(r"^(\w+)(?:\{(.*)\})? (\S+)$")


def record() -> list[tuple[datetime, str]]:
    """``(virtual time, /metrics text)`` after every tick of the demo's day."""
    taken: list[tuple[datetime, str]] = []

    def tick(farm: DemoFarm) -> list[Dispatched]:
        # ControlLoop.tick_once (src/kelpie/app.py) without its failure branch, keeping the
        # results the story reads; follow it when it changes.
        assert farm.app is not None
        loop = farm.app.loop
        snapshots: list[Snapshot] = []

        def source() -> Snapshot:
            snapshots.append(loop.snapshot_source())
            return snapshots[-1]

        started = time.monotonic()
        results = farm.app.engine.tick(source)
        loop.metrics.observe_tick(
            time.monotonic() - started,
            results,
            farm.app.engine,
            snapshots[-1] if snapshots else None,
        )
        loop.metrics.started = DAY.timestamp()
        loop.metrics.last_tick = farm.now.timestamp()
        taken.append((farm.now, loop.metrics.render()))
        farm.results.extend(results)
        return results

    original = DemoFarm.tick
    DemoFarm.tick = tick  # type: ignore[method-assign]
    try:
        farm = DemoFarm(DAY)
        try:
            Story(farm, Narrator(io.StringIO(), pace=0.0)).run()
        finally:
            farm.close()
    finally:
        DemoFarm.tick = original  # type: ignore[method-assign]
    return taken


def openmetrics(taken: list[tuple[datetime, str]]) -> str:
    """One block per sample name (OpenMetrics wants a metric's samples together), each
    sample repeated every :data:`SCRAPE` until the next render."""
    series: dict[str, list[str]] = defaultdict(list)
    for i, (at, text) in enumerate(taken):
        until = taken[i + 1][0] if i + 1 < len(taken) else at + SCRAPE
        times: list[datetime] = []
        t = at
        while t < until:
            times.append(t)
            t += SCRAPE
        for line in text.splitlines():
            if line.startswith("#") or not line:
                continue
            match = SAMPLE.match(line)
            if not match:
                raise ValueError(f"not a sample line: {line!r}")
            name, labels, value = match.groups()
            label_text = f"{labels},{LABELS}" if labels else LABELS
            for t in times:
                series[name].append(f"{name}{{{label_text}}} {value} {t.timestamp():.0f}")
    return "".join("\n".join(lines) + "\n" for lines in series.values()) + "# EOF\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    Path(argv[1]).write_text(openmetrics(record()), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
