"""The provisioned Grafana dashboard and the Prometheus alert rules query only metrics that
exist, and every metric Kelpie exports is on the dashboard or allow-listed with a reason."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from kelpie.app import ControlLoop
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.limits import ProviderLimits
from kelpie.core.model import (
    BudgetScope,
    BudgetStatus,
    BudgetUnit,
    Owner,
    ProviderStatus,
    Reconciliation,
    Resource,
    ResourceKind,
    Snapshot,
    Status,
    Task,
)
from kelpie.metrics import Metrics

ROOT = Path(__file__).parent.parent
DASHBOARD = ROOT / "deploy/grafana/dashboards/kelpie.json"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
DATASOURCE = ROOT / "deploy/grafana/provisioning/datasources/prometheus.yml"
RULES = ROOT / "deploy/prometheus/rules"
# Metrics written by scripts/ for node_exporter's textfile collector: Kelpie reads them
# through Prometheus (rule 7, the report), so they carry scrape labels as well.
TEXTFILE_SCRIPTS = sorted((ROOT / "scripts").glob("kelpie-*.sh"))
SCRAPE_LABELS = {"instance", "job"}
# Exported metrics deliberately not on the dashboard, with the reason. Keep it short: a
# metric added to the code fails test_every_metric_is_on_the_dashboard until it gets a
# panel or a line here.
NOT_ON_DASHBOARD: dict[str, str] = {
    # The collector's own cost (#281): kelpie doctor reads it ("path scans cheap").
    "kelpie_path_scan_seconds": "the path-sizes script's scan time, checked by kelpie doctor",
    # Audit M6: an agent's reports carrying names the hub refuses; alerted once per agent.
    "kelpie_hub_refused_total": "the hub's refusals of an agent's names, alerted when they start",
    # #205: a 0/1 flag; rule 10 warns about the gap itself and kelpie status names it.
    "kelpie_budget_sources_disagree": "a flag beside the billed budgets, warned by rule 10",
}


def _dashboard() -> dict[str, Any]:
    return json.loads(DASHBOARD.read_text())


def _panels() -> list[dict[str, Any]]:
    return [p for p in _dashboard()["panels"] if p["type"] != "row"]


def _variables() -> list[dict[str, Any]]:
    return _dashboard()["templating"]["list"]


def _metrics() -> Metrics:
    """A Metrics with every optional feature on and at least one sample of every series."""
    metrics = Metrics()
    metrics.set_github(True)
    metrics.set_traces(True, ["model"])
    metrics.set_forward_destinations(["langfuse"])
    metrics.set_budgets_enabled(True)
    metrics.set_resources(True)
    metrics.set_alert_channels(["channel-a"])
    metrics.set_boards("hermes", ["board-a"])  # several boards: the board label (#283)
    metrics.agent_state("host-a", True, NOW)  # multi-host hub
    metrics.token_file = lambda: (True, 1)  # [traces] token_file
    metrics.traces_grace_request()
    metrics.set_cost_reports(
        {"provider-a": True},
        [Reconciliation("provider-a", NOW, 1.0, 1.2, -0.2, gap_ratio=-0.17)],
    )
    metrics.set_chat_channels(["channel-a"])
    metrics.chat_action("channel-a", "snoozed")
    metrics.chat_rejected("channel-a", "unknown_user")
    metrics.chat_api_error("channel-a", "getUpdates", "timeout")
    metrics.alert_sent("channel-a")
    metrics.alert_failed("channel-a", "timeout")
    metrics.alert_dropped("channel-a", "queue_full")
    metrics.set_providers(["provider-a"])
    metrics.provider_probed(ProviderStatus("provider-a", auth_ok=True, remaining=1, limit=2))
    metrics.provider_failed("provider-a", "timeout")
    metrics.provider_limit_hit("provider-a")
    metrics.set_limit_provider(lambda model: "provider-a")  # limit hits from traces
    metrics.trace_limits = (ProviderLimits("provider-a", 3, 10, (3, 4), 5, 2),)
    metrics.metrics_request(200)
    metrics.metric_points(2, 1, {"not_delta": 1})
    metrics.rules = [("rule-a", "observe", True)]
    metrics.set_adapters(["adapter-a"])
    metrics.connector_errors["connector-a"] += 1
    metrics.open_states["rule-a"] += 1
    metrics.actions[("rule-a", "alert", "observe", "observed")] += 1
    metrics.spans_rejected("too_big", 1)
    metrics.traces_request(200)
    metrics.set_conditions(True, ("out_of_scope", "rate_limited", "token_cap"))
    metrics.conditions_request(201)
    metrics.resource_failed("timeout")
    metrics.budgets = (
        BudgetStatus("b", BudgetScope.DAY, "farm", BudgetUnit.USD, 1.0, runs_out_at=NOW),
    )
    metrics.budgets_at = NOW
    card = Task("C", "c", Status.BACKLOG, NOW, prework=True, platform_status="triage")
    done = Task("D", "d", Status.DONE, NOW, role="role-a", owner_id="agent-a", board="board-a")
    slot = Resource("pool-a:agent", ResourceKind.SLOTS, used=1, limit=2)
    snapshot = Snapshot(
        now=NOW,
        owners=(Owner("agent-a", "role-a"),),
        tasks=(card, done),
        resources=(slot,),
    )
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


def _families() -> set[str]:
    """Metric families `/metrics` emits (a summary's _sum and _count are one family)."""
    return set(re.findall(r"^# TYPE (\w+)", _rendered(), re.M))


def _textfile() -> dict[str, set[str]]:
    """Textfile-collector metrics the scripts write, with the labels each one carries."""
    out: dict[str, set[str]] = {}
    for script in TEXTFILE_SCRIPTS:
        text = script.read_text()
        for name in re.findall(r"# TYPE (kelpie_\w+)", text):
            out[name] = set(SCRAPE_LABELS)
        for name, labels in re.findall(r"printf '(kelpie_\w+)\{([^}]*)\}", text):
            out.setdefault(name, set(SCRAPE_LABELS)).update(re.findall(r"(\w+)=", labels))
    return out


def _exprs() -> list[str]:
    return [t["expr"] for panel in _panels() for t in panel["targets"]]


def _variable_queries() -> list[str]:
    return [v["query"]["query"] for v in _variables() if v["type"] == "query"]


def _alert_exprs() -> list[str]:
    """The expressions of every rule in deploy/prometheus/rules/ (alerts and recordings)."""
    exprs: list[str] = []
    for path in sorted(RULES.glob("*.yml")):
        for group in yaml.safe_load(path.read_text())["groups"]:
            exprs.extend(rule["expr"] for rule in group["rules"])
    return exprs


def _names(exprs: list[str]) -> set[str]:
    return {name for e in exprs for name in re.findall(r"\bkelpie_\w+", e)}


def _source_names() -> set[str]:
    """Metric names written literally in metrics.py. A name built in an f-string
    (``kelpie_llm_provider_{field_}``) is not seen here: it is caught only if _metrics()
    switches its block on."""
    text = (ROOT / "src/kelpie/metrics.py").read_text()
    return set(re.findall(r'"(kelpie_[a-z0-9_]*[a-z0-9])(?=[\s{"])', text))


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


def test_the_fixture_renders_every_metric_in_the_code() -> None:
    # _metrics() must switch every optional family on, or the tests below miss some.
    missing = _source_names() - _exported_names()
    assert not missing, sorted(missing)


def test_every_queried_metric_is_exported() -> None:
    exported = _exported_names() | set(_textfile())
    for where, exprs in (
        ("dashboard", _exprs() + _variable_queries()),
        ("alert rules", _alert_exprs()),
    ):
        referenced = _names(exprs)
        assert referenced, where
        assert referenced <= exported, (where, sorted(referenced - exported))


def test_every_metric_is_on_the_dashboard() -> None:
    on_dashboard = _names(_exprs())
    covered = {
        family
        for family in _families() | set(_textfile())
        if family in on_dashboard or {f"{family}_sum", f"{family}_count"} & on_dashboard
    }
    missing = (_families() | set(_textfile())) - covered - set(NOT_ON_DASHBOARD)
    assert not missing, f"add a panel or a NOT_ON_DASHBOARD reason: {sorted(missing)}"
    # An allow-listed metric that got a panel, or left the code, leaves the list.
    assert not set(NOT_ON_DASHBOARD) & covered, sorted(set(NOT_ON_DASHBOARD) & covered)
    assert set(NOT_ON_DASHBOARD) <= _families() | set(_textfile())


def test_labels_and_outcomes_used_in_queries_are_exported() -> None:
    exported = set(re.findall(r'(\w+)="', _rendered()))
    exported |= {label for labels in _textfile().values() for label in labels}
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


def test_template_variables_used_are_defined() -> None:
    defined = {v["name"] for v in _variables()}
    used = {name for e in _exprs() for name in re.findall(r"\$(\w+)", e)}
    assert used <= defined, sorted(used - defined)
    assert {"board", "host"} <= defined
    for v in _variables():
        if v["type"] != "query":
            continue
        # "All" must match series without the label too (one board, a single host).
        assert v["includeAll"] and v["allValue"] == ".*", v["name"]


def test_langfuse_links_follow_the_variable() -> None:
    """Links to traces in Langfuse, when its URL is set: a text variable, empty by
    default (no host is assumed), the dashboard's link and the trace panels' links."""
    (var,) = [v for v in _variables() if v["name"] == "langfuse"]
    assert var["type"] == "textbox" and var["query"] == "" and var["current"]["value"] == ""
    (link,) = [x for x in _dashboard()["links"] if x["title"] == "Traces in Langfuse"]
    assert link["url"] == "${langfuse}" and link["targetBlank"]
    linked = {p["title"] for p in _panels() if p.get("links")}
    assert linked == {
        "Spans per minute received and rejected",
        "Spans in the snapshot",
        "Trace forwarding per minute",
        "Unattributed and unpriced tokens",
    }
    for p in _panels():
        for panel_link in p.get("links", []):
            assert panel_link["url"] == "${langfuse}" and panel_link["targetBlank"]


def _labels_by_metric() -> dict[str, set[str]]:
    """Labels each exported metric carries (a summary's samples count as its family's)."""
    out: dict[str, set[str]] = {name: set(labels) for name, labels in _textfile().items()}
    for name, labels in re.findall(r"^(kelpie_\w+)\{([^}]*)\}", _rendered(), re.M):
        out.setdefault(name, set()).update(re.findall(r'(\w+)="', labels))
    return out


def test_selectors_match_labels_of_their_metric() -> None:
    # A filter on a label the metric lacks matches nothing once a value is picked
    # ({board=~"$board"} on a gauge without a board label goes empty).
    labels = _labels_by_metric()
    for expr in _exprs() + _alert_exprs():
        for name, selector in re.findall(r"\b(kelpie_\w+)\{([^}]*)\}", expr):
            used = set(re.findall(r"(\w+)\s*(?:=~|!~|!=|=)", selector))
            assert used <= labels.get(name, set()), (name, sorted(used))
