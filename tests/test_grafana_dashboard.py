"""The provisioned Grafana dashboard only queries metrics that Kelpie really exports."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kelpie.app import ControlLoop
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.model import (
    BudgetScope,
    BudgetStatus,
    BudgetUnit,
    Owner,
    ProviderStatus,
    Snapshot,
    Status,
    Task,
)
from kelpie.metrics import Metrics

ROOT = Path(__file__).parent.parent
DASHBOARD = ROOT / "deploy/grafana/dashboards/kelpie.json"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
DATASOURCE = ROOT / "deploy/grafana/provisioning/datasources/prometheus.yml"


def _panels() -> list[dict[str, Any]]:
    dashboard = json.loads(DASHBOARD.read_text())
    return [p for p in dashboard["panels"] if p["type"] != "row"]


def _metrics() -> Metrics:
    """A Metrics with every optional feature on and at least one sample of every series."""
    metrics = Metrics()
    metrics.set_github(True)
    metrics.set_traces(True, ["model"])
    metrics.set_forward_destinations(["langfuse"])
    metrics.set_budgets_enabled(True)
    metrics.set_resources(True)
    metrics.set_alert_channels(["channel-a"])
    metrics.alert_sent("channel-a")
    metrics.alert_failed("channel-a", "timeout")
    metrics.alert_dropped("channel-a", "queue_full")
    metrics.set_providers(["provider-a"])
    metrics.provider_probed(ProviderStatus("provider-a", auth_ok=True, remaining=1, limit=2))
    metrics.provider_failed("provider-a", "timeout")
    metrics.provider_limit_hit("provider-a")
    metrics.rules = [("rule-a", "observe", True)]
    metrics.set_adapters(["adapter-a"])
    metrics.connector_errors["connector-a"] += 1
    metrics.open_states["rule-a"] += 1
    metrics.actions[("rule-a", "alert", "observe", "observed")] += 1
    metrics.spans_rejected("too_big", 1)
    metrics.traces_request(200)
    metrics.resource_failed("timeout")
    metrics.budgets = (
        BudgetStatus("b", BudgetScope.DAY, "farm", BudgetUnit.USD, 1.0, runs_out_at=NOW),
    )
    metrics.budgets_at = NOW
    card = Task("C", "c", Status.BACKLOG, NOW, prework=True, platform_status="triage")
    snapshot = Snapshot(now=NOW, owners=(Owner("agent-a", "role-a"),), tasks=(card,))
    ControlLoop(Engine([]), lambda: snapshot, metrics).tick_once()  # fleet gauges
    return metrics


def _rendered() -> str:
    return _metrics().render()


def _exported_names() -> set[str]:
    """Every metric name `/metrics` can emit with all optional features enabled."""
    names: set[str] = set()
    for line in _rendered().splitlines():
        match = re.match(r"# (?:HELP|TYPE) (\w+)", line) or re.match(r"(\w+)", line)
        if match:
            names.add(match.group(1))
    return names


def _exprs() -> list[str]:
    return [t["expr"] for panel in _panels() for t in panel["targets"]]


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


def test_labels_and_outcomes_used_in_queries_are_exported() -> None:
    exported = set(re.findall(r'(\w+)="', _rendered()))
    used: set[str] = set()
    for panel in _panels():
        for target in panel["targets"]:
            for group in re.findall(r"\bby \(([^)]*)\)", target["expr"]):
                used.update(label.strip() for label in group.split(","))
            used.update(re.findall(r"[{,]\s*(\w+)\s*(?:=|!=|=~|!~)", target["expr"]))
            used.update(re.findall(r"\{\{(\w+)\}\}", target["legendFormat"]))
    assert used
    assert used <= exported, sorted(used - exported)
    outcomes = {o for e in _exprs() for o in re.findall(r'outcome="(\w+)"', e)}
    assert outcomes <= {str(o) for o in Outcome}, outcomes
