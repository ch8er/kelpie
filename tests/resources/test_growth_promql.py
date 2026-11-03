"""The default disk_growth query, run by Prometheus's own engine (``promtool test rules``) on
synthetic disks: a short burst, steady growth, a cleanup and a sawtooth. Skipped unless
``promtool`` is on PATH."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path

import pytest

from kelpie.resources.prometheus import PrometheusConfig

PROMTOOL = shutil.which("promtool")
pytestmark = pytest.mark.skipif(PROMTOOL is None, reason="promtool is not on PATH")

DAY = 86400
SCRAPE = 30
T0 = 10 * 3600 + 14 * 60  # the event, off the 15-minute slice grid
EVALS = [T0 + 7 + m * 60 for m in (0, 10, 20, 30, 45, 60, 75, 90, 120, 180)]


def burst(at: float, minutes: int = 17, gb: float = 5.0, cleanup: bool = False) -> float:
    d = minutes * 60
    if at < T0 or (cleanup and at >= T0 + d):
        return 0.0
    return min((at - T0) / d, 1) * gb


# Used space in GB at a time, per disk.
DISKS: dict[str, Callable[[float], float]] = {
    "burst": lambda a: burst(a),
    "burst_cleaned": lambda a: burst(a, cleanup=True),
    "steady": lambda a: 30 * a / DAY,
    "cleanup": lambda a: 30 * a / DAY - (1.0 if a >= T0 else 0.0),
    "sawtooth": lambda a: 4 * a / 3600 - 3 * ((a - 420) // 3600),  # net 24 GB a day
    "builds": lambda a: math.floor(a / 1800) + min((a % 1800) / 600, 1),  # 1 GB per 30 min
}


def readings(tmp_path: Path) -> dict[str, list[float]]:
    """Each disk's disk_growth (GB a day) at every evaluation time."""
    query = PrometheusConfig(url="http://x", hosts={}).query_text("disk_growth")
    end = EVALS[-1] + 60
    disk = 'node_filesystem_avail_bytes{instance="a",fstype="ext4",mountpoint="/%s"}'
    series = [
        {
            "series": disk % n,
            "values": " ".join(f"{400 - used(i * SCRAPE):.6f}" for i in range(end // SCRAPE + 1)),
        }
        for n, used in DISKS.items()
    ]
    # An expectation that never holds, so promtool prints what it got for every time.
    tests = [
        {"expr": query, "eval_time": f"{e}s", "exp_samples": [{"labels": "x{}", "value": 0}]}
        for e in EVALS
    ]
    spec = {
        "rule_files": [],
        "evaluation_interval": "1m",
        "tests": [{"interval": f"{SCRAPE}s", "input_series": series, "promql_expr_test": tests}],
    }
    path = tmp_path / "growth_test.json"
    path.write_text(json.dumps(spec))
    assert PROMTOOL is not None
    run = subprocess.run(
        [PROMTOOL, "test", "rules", str(path)], capture_output=True, text=True, timeout=120
    )
    blocks = re.split(r"\n\s*expr: ", run.stdout + run.stderr)[1:]
    assert len(blocks) == len(EVALS), run.stdout + run.stderr
    found: dict[str, list[float]] = {n: [] for n in DISKS}
    for block in blocks:
        got = re.findall(r'mountpoint="/([^"]+)"\} ([-+0-9.eE]+)', block.split("got:", 1)[1])
        assert len({n for n, _ in got}) == len(got)  # one series per disk
        for name, value in got:
            found[name].append(float(value))
    return found


def test_the_growth_query_in_prometheus(tmp_path: Path) -> None:
    got = readings(tmp_path)
    assert all(len(v) == len(EVALS) for v in got.values()), got
    assert max(got["burst"]) < 8  # 5 GB in 17 minutes: about 5.6 at most; the 6h rate up to 30
    assert max(got["burst_cleaned"]) < 1
    assert all(29 < v < 31 for v in got["steady"])
    assert min(got["cleanup"]) > 20  # a small cleanup does not end a forecast
    assert all(20 < v < 28 for v in got["sawtooth"])
    assert max(got["builds"]) < 1  # bursts with gaps longer than a slice: no growth


def path_readings(tmp_path: Path, interval: timedelta) -> list[float]:
    """path_growth (GB a day) of a directory growing 30 GB a day, measured by the script
    every hour (#281): its size is a step that changes once an hour."""
    query = PrometheusConfig(url="http://x", hosts={}, path_interval=interval).query_text(
        "path_growth"
    )
    evals = [h * 3600 + 7 + m * 60 for h in (12, 13) for m in (0, 20, 40)]
    end = evals[-1] + 60
    size = [30 * 1e9 * ((i * SCRAPE) // 3600 * 3600) / DAY for i in range(end // SCRAPE + 1)]
    spec = {
        "rule_files": [],
        "evaluation_interval": "1m",
        "tests": [
            {
                "interval": f"{SCRAPE}s",
                "input_series": [
                    {
                        "series": 'kelpie_path_bytes{instance="a",path="/p",mountpoint="/"}',
                        "values": " ".join(f"{v:.0f}" for v in size),
                    }
                ],
                "promql_expr_test": [
                    {"expr": query, "eval_time": f"{e}s", "exp_samples": [{"labels": "x{}"}]}
                    for e in evals
                ],
            }
        ],
    }
    path = tmp_path / "path_test.json"
    path.write_text(json.dumps(spec))
    assert PROMTOOL is not None
    run = subprocess.run(
        [PROMTOOL, "test", "rules", str(path)], capture_output=True, text=True, timeout=120
    )
    out = run.stdout + run.stderr
    blocks = re.split(r"\n\s*expr: ", out)[1:]
    assert len(blocks) == len(evals), out
    values = []
    for block in blocks:
        got = re.findall(r'path="/p"\} ([-+0-9.eE]+)', block.split("got:", 1)[1])
        values.append(float(got[0]) / 1e9 if got else 0.0)
    return values


def test_directory_growth_from_hourly_sizes(tmp_path: Path) -> None:
    hourly = path_readings(tmp_path, timedelta(hours=1))
    assert all(15 < v < 40 for v in hourly), hourly  # 2h slices of hourly steps: 0.75x-1.1x
    # With quarter-hour slices (the old query), most slices of hourly steps hold no growth.
    old = path_readings(tmp_path, timedelta(minutes=1))
    assert max(old) < 10, old
