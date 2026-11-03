"""scripts/demo_metrics.py: the demo's day as OpenMetrics for the dashboard screenshot."""

from __future__ import annotations

import importlib.util
import re
from datetime import timedelta
from pathlib import Path
from types import ModuleType

from kelpie.demo.farm import DemoFarm
from kelpie.demo.story import DAY

SCRIPT = Path(__file__).parent.parent / "scripts/demo_metrics.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("demo_metrics", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_demo_day_becomes_openmetrics() -> None:
    script = _script()
    tick = DemoFarm.tick
    taken = script.record()
    assert DemoFarm.tick is tick  # put back
    assert len(taken) > 10
    times = [at for at, _ in taken]
    assert times == sorted(times) and times[0] >= DAY and times[-1] - DAY < timedelta(hours=12)
    text = script.openmetrics(taken)
    assert text.endswith("# EOF\n")
    names: list[str] = []
    for line in text.splitlines()[:-1]:
        match = re.fullmatch(r'(kelpie_\w+)\{.*instance="127\.0\.0\.1:9464"\} \S+ (\d+)', line)
        assert match, line
        assert DAY.timestamp() <= int(match.group(2)) <= times[-1].timestamp() + 30
        if not names or names[-1] != match.group(1):
            names.append(match.group(1))
    assert len(names) == len(set(names)), "a sample name's lines must be contiguous"
    assert {"kelpie_engine_ticks_total", "kelpie_tasks", "kelpie_actions_total"} <= set(names)
    # The wall-clock gauges follow the virtual clock.
    start = re.search(r"^kelpie_start_time_seconds\{.*\} (\S+) ", text, re.M)
    assert start is not None and float(start.group(1)) == DAY.timestamp()
