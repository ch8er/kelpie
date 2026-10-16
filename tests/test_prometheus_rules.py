"""The Prometheus rules file is wired into the stack and only uses metrics Kelpie exports."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from tests.test_grafana_dashboard import DASHBOARD, ROOT, _exported_names

RULES = ROOT / "deploy/prometheus/rules/kelpie.yml"
PROMETHEUS = ROOT / "deploy/prometheus/prometheus.yml"
COMPOSE = ROOT / "docker-compose.yml"


def _rules() -> list[dict[str, Any]]:
    groups = yaml.safe_load(RULES.read_text())["groups"]
    return [rule for group in groups for rule in group["rules"]]


def test_rules_parse_and_each_has_what_a_responder_needs() -> None:
    rules = _rules()
    assert rules
    for rule in rules:
        assert rule["alert"].startswith("Kelpie")
        assert rule["expr"].strip()
        assert rule["labels"]["severity"]
        assert rule["annotations"]["summary"] and rule["annotations"]["description"]


def test_rules_only_query_exported_metrics() -> None:
    referenced = {name for rule in _rules() for name in re.findall(r"\bkelpie_\w+", rule["expr"])}
    assert referenced
    assert referenced <= _exported_names(), sorted(referenced - _exported_names())


def test_a_lost_alert_is_alerted_on() -> None:
    (rule,) = [r for r in _rules() if "kelpie_alerts_failed_total" in r["expr"]]
    assert "increase(kelpie_alerts_failed_total[" in rule["expr"]
    assert "offset" in rule["expr"]  # also the first failure, when the series is new


def test_the_rules_are_loaded_by_prometheus_and_mounted_by_compose() -> None:
    config = yaml.safe_load(PROMETHEUS.read_text())
    assert "/etc/prometheus/rules/*.yml" in config["rule_files"]
    mounts = yaml.safe_load(COMPOSE.read_text())["services"]["prometheus"]["volumes"]
    host_dirs = {m.split(":")[0] for m in mounts if m.endswith(":/etc/prometheus/rules:ro")}
    assert host_dirs == {"./deploy/prometheus/rules"}
    assert (ROOT / host_dirs.pop()).joinpath(RULES.name).is_file()


def test_the_dashboard_shows_lost_alerts() -> None:
    text = Path(DASHBOARD).read_text()
    assert "increase(kelpie_alerts_failed_total[1h])" in text
