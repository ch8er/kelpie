"""The provisioned Grafana dashboard only queries metrics that Kelpie really exports."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kelpie.app import ControlLoop
from kelpie.core.engine import Engine
from kelpie.core.model import Snapshot
from kelpie.metrics import Metrics

ROOT = Path(__file__).parent.parent
DASHBOARD = ROOT / "deploy/grafana/dashboards/kelpie.json"
DATASOURCE = ROOT / "deploy/grafana/provisioning/datasources/prometheus.yml"


def _panels() -> list[dict[str, Any]]:
    dashboard = json.loads(DASHBOARD.read_text())
    return [p for p in dashboard["panels"] if p["type"] != "row"]


def _exported_names() -> set[str]:
    """Every metric name `/metrics` can emit with all optional features enabled."""
    metrics = Metrics()
    metrics.set_github(True)
    metrics.set_traces(True)
    metrics.set_forward_destinations(["langfuse"])
    metrics.set_budgets_enabled(True)
    metrics.set_resources(True)
    snapshot = Snapshot(now=datetime(2026, 1, 1, tzinfo=UTC))
    ControlLoop(Engine([]), lambda: snapshot, metrics).tick_once()  # fleet gauges
    names: set[str] = set()
    for line in metrics.render().splitlines():
        match = re.match(r"# (?:HELP|TYPE) (\w+)", line) or re.match(r"(\w+)", line)
        if match:
            names.add(match.group(1))
    return names


def test_dashboard_parses_and_has_panels() -> None:
    panels = _panels()
    assert len(panels) >= 20
    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids))


def test_dashboard_uses_the_provisioned_datasource() -> None:
    uid = re.search(r"uid: (\S+)", DATASOURCE.read_text())
    assert uid is not None
    for panel in _panels():
        assert panel["datasource"]["uid"] == uid.group(1)
        for target in panel["targets"]:
            assert target["datasource"]["uid"] == uid.group(1)


def test_every_queried_metric_is_exported() -> None:
    exported = _exported_names()
    referenced = {
        name
        for panel in _panels()
        for target in panel["targets"]
        for name in re.findall(r"\bkelpie_\w+", target["expr"])
    }
    assert referenced
    assert referenced <= exported, sorted(referenced - exported)
