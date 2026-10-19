"""Kelpie's own metrics in the Prometheus text exposition format (stdlib only)."""

from __future__ import annotations

import contextlib
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, NamedTuple

from kelpie import __version__
from kelpie.core.model import Reconciliation, ResourceKind, Session, Snapshot, Status
from kelpie.core.stages import STAGES, UNKNOWN_ROLE, live_stage, rework_role, task_roles
from kelpie.rules._tasks import is_live
from kelpie.rules.bottlenecks import RoleLoad, role_loads

if TYPE_CHECKING:
    from kelpie.app import RuleStatus
    from kelpie.core.budgets import BudgetEvaluation, BudgetStatus
    from kelpie.core.engine import Engine
    from kelpie.core.ledger import Dispatched
    from kelpie.core.model import ProviderStatus


class RoleGauges(NamedTuple):
    """Live throughput gauges of one role."""

    agents: int
    with_work: int  # live session or an open task
    ownerless_queue: int
    done_24h: int
    returns_24h: int
    reopened_24h: int = 0  # done tasks moved back out of done


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: str) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


# Task budgets can have a subject per task: only these many per budget are exported (the
# highest spend), plus every task past a threshold.
MAX_TASK_SERIES = 20
MAX_PREWORK_SERIES = 10  # pre-work statuses exported (platform statuses are config data)
MAX_SLOT_POOLS = 20  # tool slot pools exported (pools are config data)


def _bounded(statuses: Iterable[BudgetStatus]) -> tuple[BudgetStatus, ...]:
    """Keep the series count bounded: per-task subjects only when past a threshold or
    among the top :data:`MAX_TASK_SERIES` by spend; agent, project and day always."""
    statuses = tuple(statuses)
    top: set[tuple[str, str]] = set()
    by_budget: dict[str, list[BudgetStatus]] = {}
    for b in statuses:
        if str(b.scope) == "task":
            by_budget.setdefault(b.budget, []).append(b)
    for rows in by_budget.values():
        rows.sort(key=lambda b: -b.spent)
        top.update((b.budget, b.subject) for b in rows[:MAX_TASK_SERIES])
    return tuple(
        b
        for b in statuses
        if str(b.scope) != "task" or str(b.state) != "ok" or (b.budget, b.subject) in top
    )


class Metrics:
    """Thread-safe counters fed by the control loop and read by ``/metrics``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started = time.time()
        self.ticks = 0
        self.tick_failures = 0
        self.tick_seconds = 0.0
        self.last_tick = 0.0
        self.actions: Counter[tuple[str, str, str, str]] = Counter()
        self.rule_errors: Counter[str] = Counter()
        self.adapter_errors: Counter[str] = Counter()
        # multi-host: agent -> (up, unix time of its last report or 0)
        self.agents: dict[str, tuple[bool, float]] = {}
        self.ledger_up = True
        self.open_states: Counter[str] = Counter()
        self.tasks: Counter[str] = Counter()
        self.idle_agents: Counter[str] = Counter()  # role -> idle agents (0 if all busy)
        # platform status -> (cards in it before work, seconds the oldest has sat there)
        self.prework: dict[str, tuple[int, float]] = {}
        # tool slot pool -> (slots, {holder: slots held}) (Hermes [adapters.hermes.tools])
        self.slots: dict[str, tuple[int, dict[str, int]]] = {}
        # throughput per role now; open tasks per stage and role
        self.roles: dict[str, RoleGauges] = {}
        # roles that review: returns count for the role that redoes the work ([report])
        self.review_roles: tuple[str, ...] = ("reviewer",)
        self.stages: Counter[tuple[str, str]] = Counter()
        self.fleet_seen = False
        self.dry_run = True
        self.rules: list[tuple[str, str, bool]] = []  # (name, mode, implemented)
        self.traces_enabled = False
        self.traces_requests: Counter[str] = Counter()
        self.spans_received_total: Counter[str] = Counter()
        self.spans_rejected_total: Counter[str] = Counter()
        self.span_rows: Callable[[], int] | None = None  # the span store's count
        self.span_store_errors = 0
        self.forward: dict[str, Counter[str]] = {}  # destination -> {forwarded, failed, dropped}
        self.snapshot_span_count = 0
        self.snapshot_spans_dropped = 0
        self.snapshot_hook_merged = 0
        self.budgets_enabled = False
        self.budgets: tuple[BudgetStatus, ...] = ()
        # Providers' cost reports: up per provider, and the newest reconciled day of each.
        self.cost_reports: dict[str, bool] = {}
        self.reconciled: dict[str, Reconciliation] = {}
        self.budgets_at: datetime | None = None
        self.budgets_unattributed: dict[str, int] = {}
        self.budgets_stale_now = False
        self.resources_enabled = False
        self.resource_up = True
        self.resource_readings = 0
        self.resource_failures: Counter[str] = Counter()
        self.connector_errors: Counter[str] = Counter()
        self.github_enabled = False
        self.github_requests: Counter[str] = Counter()
        self.github_rate_remaining = -1
        self.github_rate_limit_value = -1
        self.block_condition_outcomes: dict[str, int] = dict.fromkeys(
            ("attached", "unresolved", "unconfirmed", "error"), 0
        )
        self.providers: tuple[str, ...] = ()
        self.provider_status: dict[str, ProviderStatus] = {}
        self.provider_failures: Counter[tuple[str, str]] = Counter()
        self.provider_hits: Counter[str] = Counter()
        self.alert_channels: tuple[str, ...] = ()
        self.alerts_sent: Counter[str] = Counter()
        self.alerts_failed: Counter[tuple[str, str]] = Counter()
        self.alerts_dropped: Counter[tuple[str, str]] = Counter()

    def set_rules(self, statuses: Iterable[RuleStatus], dry_run: bool) -> None:
        with self._lock:
            self.rules = [
                (s.name, str(s.mode) if s.implemented else "", s.implemented) for s in statuses
            ]
            self.dry_run = dry_run
            for name, _, implemented in self.rules:
                if implemented:  # expose 0 so rate() sees the first error
                    self.rule_errors.setdefault(name, 0)

    def set_adapters(self, names: Iterable[str]) -> None:
        with self._lock:
            for name in names:  # expose 0 so rate() sees the first error
                self.adapter_errors.setdefault(name, 0)

    # --- traces (kelpie.traces.ingest.IngestObserver, kelpie.traces.store.StoreObserver) --

    def set_traces(self, enabled: bool, kinds: Iterable[str] = ()) -> None:
        with self._lock:
            self.traces_enabled = enabled
            for kind in kinds:  # expose 0 so rate() sees the first span
                self.spans_received_total.setdefault(kind, 0)

    def traces_request(self, code: int) -> None:
        with self._lock:
            self.traces_requests[str(code)] += 1

    def spans_received(self, kind: str, count: int) -> None:
        with self._lock:
            self.spans_received_total[kind] += count

    def spans_rejected(self, reason: str, count: int) -> None:
        with self._lock:
            self.spans_rejected_total[reason] += count

    def set_span_rows_source(self, count: Callable[[], int]) -> None:
        with self._lock:
            self.span_rows = count

    def snapshot_spans(self, count: int, dropped: int) -> None:
        with self._lock:
            self.snapshot_span_count = count
            self.snapshot_spans_dropped += dropped

    def hook_spans_merged(self, count: int) -> None:
        with self._lock:
            self.snapshot_hook_merged = count

    def span_store_failed(self) -> None:
        with self._lock:
            self.span_store_errors += 1

    # --- budgets ------------------------------------------------------------------------

    def set_budgets_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.budgets_enabled = enabled

    def set_budgets(self, result: BudgetEvaluation, now: datetime) -> None:
        with self._lock:
            self.budgets, self.budgets_at = _bounded(result.statuses), now
            self.budgets_unattributed = dict(result.unattributed)
            self.budgets_stale_now = False

    def set_cost_reports(
        self, up: Mapping[str, bool], reconciliations: Iterable[Reconciliation]
    ) -> None:
        """Each provider's last read (True: read) and its newest reconciled day."""
        newest: dict[str, Reconciliation] = {}
        for r in reconciliations:
            if r.provider not in newest or r.day > newest[r.provider].day:
                newest[r.provider] = r
        with self._lock:
            self.cost_reports = dict(up)
            self.reconciled = newest

    def budgets_stale(self) -> None:
        """Spend could not be read this tick: the last values stay, marked stale."""
        with self._lock:
            self.budgets_stale_now = True

    # --- host resources (kelpie.resources.prometheus.ReadObserver) ---------------------

    def set_resources(self, enabled: bool) -> None:
        with self._lock:
            self.resources_enabled = enabled

    def resource_read(self, readings: int) -> None:
        with self._lock:
            self.resource_up = True
            self.resource_readings = readings

    def resource_failed(self, cause: str) -> None:
        with self._lock:
            self.resource_up = False
            self.resource_readings = 0
            self.resource_failures[cause] += 1

    def resource_query_failed(self, cause: str) -> None:
        """One optional query failed; the source still answers."""
        with self._lock:
            self.resource_failures[cause] += 1

    # --- alert channels (kelpie.alerts.delivery.AlertObserver) -------------------------

    def set_alert_channels(self, names: Iterable[str]) -> None:
        with self._lock:
            self.alert_channels = tuple(names)

    def alert_sent(self, channel: str) -> None:
        with self._lock:
            self.alerts_sent[channel] += 1

    def alert_failed(self, channel: str, cause: str) -> None:
        with self._lock:
            self.alerts_failed[(channel, cause)] += 1

    def alert_dropped(self, channel: str, reason: str) -> None:
        with self._lock:
            self.alerts_dropped[(channel, reason)] += 1

    # --- LLM providers (kelpie.connectors.llm_quotas.source.ProbeObserver) -------------

    def set_providers(self, names: Iterable[str]) -> None:
        with self._lock:
            self.providers = tuple(names)

    def provider_probed(self, status: ProviderStatus) -> None:
        with self._lock:
            self.provider_status[status.name] = status

    def provider_failed(self, provider: str, cause: str) -> None:
        with self._lock:
            self.provider_failures[(provider, cause)] += 1

    def provider_limit_hit(self, provider: str) -> None:
        with self._lock:
            self.provider_hits[provider] += 1

    # --- trace forwarding (kelpie.traces.forward.ForwardObserver) ----------------------

    def set_forward_destinations(self, names: Iterable[str]) -> None:
        with self._lock:
            for name in names:  # expose 0 so rate() sees the first event
                self.forward.setdefault(name, Counter(forwarded=0, failed=0, dropped=0))

    def _forward(self, destination: str, what: str) -> None:
        with self._lock:
            self.forward.setdefault(destination, Counter())[what] += 1

    def forwarded(self, destination: str) -> None:
        self._forward(destination, "forwarded")

    def forward_failed(self, destination: str) -> None:
        self._forward(destination, "failed")

    def forward_dropped(self, destination: str, count: int = 1) -> None:
        with self._lock:
            self.forward.setdefault(destination, Counter())["dropped"] += count

    # --- GitHub connector (kelpie.connectors.github observers) -------------------------

    def set_github(self, enabled: bool) -> None:
        with self._lock:
            self.github_enabled = enabled

    def github_request(self, outcome: str) -> None:
        with self._lock:
            self.github_requests[outcome] += 1

    def github_rate_limit(self, remaining: int, limit: int) -> None:
        with self._lock:
            self.github_rate_remaining = remaining
            self.github_rate_limit_value = limit

    def block_conditions(self, outcomes: Mapping[str, int]) -> None:
        with self._lock:
            self.block_condition_outcomes = dict(outcomes)

    def connector_failed(self, name: str) -> None:
        with self._lock:
            self.connector_errors[name] += 1

    def adapter_failed(self, name: str) -> None:
        with self._lock:
            self.adapter_errors[name] += 1

    # --- multi-host (kelpie.remote.farm.FarmObserver) ---------------------------------------

    def agent_state(self, agent: str, up: bool, last_report: datetime | None) -> None:
        with self._lock:
            self.agents[agent] = (up, last_report.timestamp() if last_report else 0.0)

    def observe_tick(
        self,
        seconds: float,
        results: Sequence[Dispatched],
        engine: Engine,
        snapshot: Snapshot | None = None,
    ) -> None:
        with self._lock:
            if snapshot is not None:
                self._fleet(snapshot)
            self.ticks += 1
            self.tick_seconds += seconds
            self.last_tick = time.time()
            for d in results:
                a = d.action
                self.actions[(a.rule, str(a.kind), str(d.mode), str(d.outcome))] += 1
            self._sync(engine)

    def _fleet(self, snapshot: Snapshot) -> None:
        """Tasks per status and idle agents per role, from the tick's snapshot.

        Idle agents are counted as the bottlenecks rule counts them by default (no
        ``min_idle_time``, no ``ready_is_queued``), so they may differ from the rule's view."""
        self.fleet_seen = True
        self.tasks = Counter(dict.fromkeys(Status, 0))
        self.tasks.update(t.status for t in snapshot.tasks)
        prework: dict[str, list[float]] = {}
        for t in snapshot.tasks:
            if t.prework:
                age = (snapshot.now - t.status_since).total_seconds()
                prework.setdefault(t.platform_status or "unknown", []).append(age)
        self.prework = {
            k: (len(v), max(v)) for k, v in sorted(prework.items())[:MAX_PREWORK_SERIES]
        }
        slots: dict[str, tuple[int, dict[str, int]]] = {}
        for r in snapshot.resources:
            if r.kind is ResourceKind.SLOTS and r.used is not None and r.limit is not None:
                pool, _, holder = r.name.rpartition(":")
                held = slots.setdefault(pool, (int(r.limit), {}))[1]
                held[holder] = held.get(holder, 0) + int(r.used)
        self.slots = dict(sorted(slots.items())[:MAX_SLOT_POOLS])
        loads = role_loads(snapshot)
        self.idle_agents = Counter({r: len(load.idle) for r, load in loads.items()})
        self._throughput(snapshot, loads)

    def _throughput(self, snapshot: Snapshot, loads: Mapping[str, RoleLoad]) -> None:
        """Agents, agents with work and the ownerless queue per role (as the bottlenecks
        rule counts them), tasks done, returns and reopens over the last 24h, and open tasks per
        stage. Not the daily report's utilization, queue or rework rate: those are measured
        over a day (:mod:`kelpie.report.throughput`)."""
        now, day = snapshot.now, timedelta(days=1)
        of_task = task_roles(snapshot.tasks, snapshot.owners)
        done: Counter[str] = Counter()
        returns: Counter[str] = Counter()
        reopened: Counter[str] = Counter()
        self.stages = Counter()
        live = {s.task_id for s in snapshot.sessions if is_live(s, now)}
        owner_roles = {o.id: o.role for o in snapshot.owners}
        by_task: dict[str, list[Session]] = {}
        for s in snapshot.sessions:
            by_task.setdefault(s.task_id, []).append(s)

        def own_role(s: Session) -> str | None:
            return owner_roles.get(s.owner_id)

        for t in snapshot.tasks:
            role = of_task.get(t.id, UNKNOWN_ROLE)
            if t.status is Status.DONE and now - day <= t.status_since <= now:
                done[role] += 1
            for back in t.returns:
                if now - day <= back.at <= now:  # for the role that redoes it, not review
                    returns[
                        rework_role(
                            t, back.at, by_task.get(t.id, ()), own_role, self.review_roles, now
                        )
                    ] += 1
            reopened[role] += sum(1 for at in t.reopened_at if now - day <= at <= now)
            if (stage := live_stage(t, t.id in live)) is not None:
                self.stages[(stage, role)] += 1
        for role in {role for _, role in self.stages} | set(loads):
            for stage in STAGES:
                self.stages[(stage, role)] += 0  # every stage of every known role
        self.roles = {
            role: RoleGauges(
                agents=len(loads[role].agents) if role in loads else 0,
                with_work=len(loads[role].busy) if role in loads else 0,
                ownerless_queue=len(loads[role].queue) if role in loads else 0,
                done_24h=done[role],
                returns_24h=returns[role],
                reopened_24h=reopened[role],
            )
            for role in sorted(
                set(loads)
                | set(done)
                | {r for r, n in returns.items() if n}
                | {r for r, n in reopened.items() if n}
            )
        }

    def tick_failed(self, engine: Engine) -> None:
        with self._lock:
            self.tick_failures += 1
            self._sync(engine)

    def _sync(self, engine: Engine) -> None:
        self.rule_errors.update(
            {r: n - self.rule_errors.get(r, 0) for r, n in engine.rule_errors.items()}
        )
        self.ledger_up = engine.ledger_available
        # The state store shares the ledger's fate; on failure keep the last value.
        with contextlib.suppress(Exception):
            self.open_states = Counter(s.rule for s in engine.states.store.open_states())

    def render(self) -> str:
        with self._lock:
            out: list[str] = []

            def metric(name: str, kind: str, help_: str, samples: Iterable[str]) -> None:
                out.extend([f"# HELP {name} {help_}", f"# TYPE {name} {kind}", *samples])

            metric(
                "kelpie_build_info",
                "gauge",
                "Kelpie build information.",
                [f"kelpie_build_info{_labels(version=__version__)} 1"],
            )
            metric(
                "kelpie_start_time_seconds",
                "gauge",
                "Unix time the server started.",
                [f"kelpie_start_time_seconds {self.started:.3f}"],
            )
            metric(
                "kelpie_dry_run",
                "gauge",
                "1 if the global dry-run override is on.",
                [f"kelpie_dry_run {int(self.dry_run)}"],
            )
            metric(
                "kelpie_rule_loaded",
                "gauge",
                "Planned rules: 1 if loaded, 0 if not implemented yet. mode is the configured "
                "mode (empty if not implemented); dry-run overrides it, see kelpie_dry_run.",
                [
                    f"kelpie_rule_loaded{_labels(rule=n, mode=m)} {int(impl)}"
                    for n, m, impl in self.rules
                ],
            )
            metric(
                "kelpie_engine_ticks_total",
                "counter",
                "Completed control-loop ticks.",
                [f"kelpie_engine_ticks_total {self.ticks}"],
            )
            metric(
                "kelpie_engine_tick_failures_total",
                "counter",
                "Ticks that raised before finishing.",
                [f"kelpie_engine_tick_failures_total {self.tick_failures}"],
            )
            metric(
                "kelpie_engine_tick_duration_seconds",
                "summary",
                "Time spent per completed tick.",
                [
                    f"kelpie_engine_tick_duration_seconds_sum {self.tick_seconds:.6f}",
                    f"kelpie_engine_tick_duration_seconds_count {self.ticks}",
                ],
            )
            metric(
                "kelpie_engine_last_tick_timestamp_seconds",
                "gauge",
                "Unix time of the last completed tick (0 before the first).",
                [f"kelpie_engine_last_tick_timestamp_seconds {self.last_tick:.3f}"],
            )
            metric(
                "kelpie_rule_errors_total",
                "counter",
                "Rule evaluations that raised.",
                [
                    f"kelpie_rule_errors_total{_labels(rule=r)} {n}"
                    for r, n in sorted(self.rule_errors.items())
                ],
            )
            metric(
                "kelpie_adapter_errors_total",
                "counter",
                "Adapter snapshots that raised; the adapter is skipped for that tick.",
                [
                    f"kelpie_adapter_errors_total{_labels(adapter=a)} {n}"
                    for a, n in sorted(self.adapter_errors.items())
                ],
            )
            if self.agents:
                metric(
                    "kelpie_agent_up",
                    "gauge",
                    "1 while the agent's board is merged (fresh reports, clock in range).",
                    [
                        f"kelpie_agent_up{_labels(agent=a)} {int(up)}"
                        for a, (up, _) in sorted(self.agents.items())
                    ],
                )
                metric(
                    "kelpie_agent_last_report_timestamp_seconds",
                    "gauge",
                    "Unix time the hub accepted the agent's last report (0: none yet).",
                    [
                        f"kelpie_agent_last_report_timestamp_seconds{_labels(agent=a)} {ts:.3f}"
                        for a, (_, ts) in sorted(self.agents.items())
                    ],
                )
            metric(
                "kelpie_actions_total",
                "counter",
                "Actions emitted by rules, by what happened to them. mode is the effective "
                "mode (observe under dry-run).",
                [
                    f"kelpie_actions_total{_labels(rule=r, kind=k, mode=m, outcome=o)} {n}"
                    for (r, k, m, o), n in sorted(self.actions.items())
                ],
            )
            metric(
                "kelpie_open_states",
                "gauge",
                "Long-lived alert states currently open, by rule.",
                [
                    f"kelpie_open_states{_labels(rule=r)} {n}"
                    for r, n in sorted(self.open_states.items())
                ],
            )
            if self.fleet_seen:
                metric(
                    "kelpie_tasks",
                    "gauge",
                    "Tasks in the last snapshot, by normalized status.",
                    [f"kelpie_tasks{_labels(status=str(k))} {n}" for k, n in self.tasks.items()],
                )
                metric(
                    "kelpie_prework_tasks",
                    "gauge",
                    "Cards in a status before any work starts (Hermes triage, scheduled), by "
                    "the platform's own status (incident 17); also those still waiting for "
                    "parents of their own. A status shows only while a card is in it.",
                    [
                        f"kelpie_prework_tasks{_labels(platform_status=k)} {n}"
                        for k, (n, _) in self.prework.items()
                    ],
                )
                metric(
                    "kelpie_prework_oldest_seconds",
                    "gauge",
                    "How long the oldest card in each pre-work status has sat there.",
                    [
                        f"kelpie_prework_oldest_seconds{_labels(platform_status=k)} {age:.0f}"
                        for k, (_, age) in self.prework.items()
                    ],
                )
                if self.slots:
                    metric(
                        "kelpie_tool_slots",
                        "gauge",
                        "Tool slots of each pool (simulator lock files, [adapters.hermes.tools]).",
                        [
                            f"kelpie_tool_slots{_labels(pool=p)} {total}"
                            for p, (total, _) in self.slots.items()
                        ],
                    )
                    metric(
                        "kelpie_tool_slots_held",
                        "gauge",
                        "Tool slots held now, by pool and holder: agent (a holder working in a "
                        "task workspace) or ci (any other: CI runners share the slots).",
                        [
                            f"kelpie_tool_slots_held{_labels(pool=p, holder=h)} {n}"
                            for p, (_, held) in self.slots.items()
                            for h, n in sorted(held.items())
                        ],
                    )
                metric(
                    "kelpie_idle_agents",
                    "gauge",
                    "Agent owners with no live session and no open task, by role.",
                    [
                        f"kelpie_idle_agents{_labels(role=r)} {n}"
                        for r, n in sorted(self.idle_agents.items())
                    ],
                )
                self._render_throughput(metric)
            metric(
                "kelpie_ledger_up",
                "gauge",
                "1 unless the last action-ledger read or write failed.",
                [f"kelpie_ledger_up {int(self.ledger_up)}"],
            )
            if self.github_enabled:
                self._render_github(metric)
            if self.traces_enabled:
                self._render_traces(metric)
            if self.forward:
                self._render_forward(metric)
            if self.budgets_enabled:
                self._render_budgets(metric)
            if self.cost_reports:
                self._render_cost_reports(metric)
            if self.resources_enabled:
                self._render_resources(metric)
            if self.providers:
                self._render_providers(metric)
            if self.alert_channels:
                self._render_alerts(metric)
            return "\n".join(out) + "\n"

    def _render_throughput(self, metric: Callable[[str, str, str, Iterable[str]], None]) -> None:
        names = (
            ("kelpie_role_agents", "agents", "Agent owners per role."),
            (
                "kelpie_role_agents_with_work",
                "with_work",
                "Agents with a live session or an open task, by role.",
            ),
            (
                "kelpie_role_ownerless_queue_tasks",
                "ownerless_queue",
                "Ownerless tasks waiting for the role (ready, review, accepted).",
            ),
            (
                "kelpie_role_tasks_done_24h",
                "done_24h",
                "Tasks of the role that reached done in the last 24h.",
            ),
            (
                "kelpie_role_returns_24h",
                "returns_24h",
                "Returns (sent back) in the last 24h, counted for the role that redoes the work.",
            ),
            (
                "kelpie_role_reopened_after_done_24h",
                "reopened_24h",
                "Done tasks of the role moved back out of done in the last 24h.",
            ),
        )
        for name, field, help_ in names:
            samples = [
                f"{name}{_labels(role=r)} {getattr(v, field)}" for r, v in self.roles.items()
            ]
            metric(name, "gauge", help_, samples)
        metric(
            "kelpie_role_reopened_after_done_ratio",
            "gauge",
            "Of the role's completions in the last 24h (tasks still done, plus those reopened "
            "since), the share reopened; roles with a completion only.",
            [
                f"kelpie_role_reopened_after_done_ratio{_labels(role=r)} "
                f"{v.reopened_24h / (v.done_24h + v.reopened_24h):.4g}"
                for r, v in self.roles.items()
                if v.done_24h + v.reopened_24h
            ],
        )
        metric(
            "kelpie_tasks_by_stage",
            "gauge",
            "Open tasks now per stage (working, queue, review, waiting, ownerless) and role.",
            [
                f"kelpie_tasks_by_stage{_labels(role=r, stage=s)} {n}"
                for (s, r), n in sorted(self.stages.items(), key=lambda kv: kv[0][::-1])
            ],
        )

    def _render_resources(self, metric: Callable[[str, str, str, Iterable[str]], None]) -> None:
        metric(
            "kelpie_resource_source_up",
            "gauge",
            "1 if the last read of host resources (Prometheus) succeeded.",
            [f"kelpie_resource_source_up {int(self.resource_up)}"],
        )
        metric(
            "kelpie_resource_readings",
            "gauge",
            "Host resource readings in the last successful read (0 after a failure).",
            [f"kelpie_resource_readings {self.resource_readings}"],
        )
        metric(
            "kelpie_resource_read_failures_total",
            "counter",
            "Failed reads of host resources, by cause; rules see no host readings meanwhile, "
            "except for causes named <query>_<cause>: one optional query failed.",
            [
                f"kelpie_resource_read_failures_total{_labels(cause=c)} {n}"
                for c, n in sorted(self.resource_failures.items())
            ],
        )

    def _render_alerts(self, metric: Callable[[str, str, str, Iterable[str]], None]) -> None:
        metric(
            "kelpie_alerts_sent_total",
            "counter",
            "Alerts delivered, by channel.",
            [
                f"kelpie_alerts_sent_total{_labels(channel=n)} {self.alerts_sent.get(n, 0)}"
                for n in self.alert_channels
            ],
        )
        metric(
            "kelpie_alerts_failed_total",
            "counter",
            "Alerts that could not be delivered after retries, by channel and cause.",
            [
                f"kelpie_alerts_failed_total{_labels(channel=n, cause=c)} {v}"
                for (n, c), v in sorted(self.alerts_failed.items())
            ],
        )
        metric(
            "kelpie_alerts_dropped_total",
            "counter",
            "Alerts not delivered (queue_full, duplicate, shutdown), by channel and reason.",
            [
                f"kelpie_alerts_dropped_total{_labels(channel=n, reason=r)} {v}"
                for (n, r), v in sorted(self.alerts_dropped.items())
            ],
        )

    def _render_providers(self, metric: Callable[[str, str, str, Iterable[str]], None]) -> None:
        probed = [(n, self.provider_status[n]) for n in self.providers if n in self.provider_status]
        metric(
            "kelpie_llm_provider_up",
            "gauge",
            "1 if the last probe of an LLM provider gave a reading (0: its quota is unknown).",
            [f"kelpie_llm_provider_up{_labels(provider=n)} {int(s.ok)}" for n, s in probed],
        )
        metric(
            "kelpie_llm_provider_auth_ok",
            "gauge",
            "1 if the provider accepted the key at the last probe, 0 if it rejected it.",
            [
                f"kelpie_llm_provider_auth_ok{_labels(provider=n)} {int(s.auth_ok)}"
                for n, s in probed
                if s.auth_ok is not None
            ],
        )
        for field_, help_ in (
            ("remaining", "Quota left at the last probe, in the provider's unit."),
            ("limit", "Quota per window at the last probe, in the provider's unit."),
        ):
            metric(
                f"kelpie_llm_provider_{field_}",
                "gauge",
                help_,
                [
                    f"kelpie_llm_provider_{field_}{_labels(provider=n)} {value:g}"
                    for n, s in probed
                    if (value := getattr(s, field_)) is not None
                ],
            )
        metric(
            "kelpie_llm_provider_probe_failures_total",
            "counter",
            "Failed LLM provider probes, by provider and cause.",
            [
                f"kelpie_llm_provider_probe_failures_total{_labels(provider=p, cause=c)} {n}"
                for (p, c), n in sorted(self.provider_failures.items())
            ],
        )
        metric(
            "kelpie_llm_provider_limit_hits_total",
            "counter",
            "Times a probe found an LLM provider at its limit (once per limit period).",
            [
                f"kelpie_llm_provider_limit_hits_total{_labels(provider=n)} "
                f"{self.provider_hits.get(n, 0)}"
                for n in self.providers
            ],
        )

    def _render_forward(self, metric: Callable[[str, str, str, Iterable[str]], None]) -> None:
        for what, name, help_ in (
            ("forwarded", "kelpie_traces_forwarded_total", "OTLP requests forwarded."),
            (
                "failed",
                "kelpie_traces_forward_failed_total",
                "OTLP requests that could not be forwarded after retries.",
            ),
            (
                "dropped",
                "kelpie_traces_forward_dropped_total",
                "OTLP requests dropped because the destination's queue was full.",
            ),
        ):
            metric(
                name,
                "counter",
                help_,
                [
                    f"{name}{_labels(destination=d)} {c[what]}"
                    for d, c in sorted(self.forward.items())
                ],
            )

    def _render_cost_reports(self, metric: Callable[[str, str, str, Iterable[str]], None]) -> None:
        metric(
            "kelpie_cost_report_up",
            "gauge",
            "1 when the provider's last cost report read worked, else 0.",
            [
                f"kelpie_cost_report_up{_labels(provider=p)} {int(ok)}"
                for p, ok in sorted(self.cost_reports.items())
            ],
        )
        if not self.reconciled:
            return
        rows = [(r, _labels(provider=r.provider)) for _, r in sorted(self.reconciled.items())]
        metric(
            "kelpie_spend_reported_usd",
            "gauge",
            "What the provider's cost report says was spent on its newest reconciled UTC day.",
            [f"kelpie_spend_reported_usd{lbl} {r.reported_usd:.6f}" for r, lbl in rows],
        )
        metric(
            "kelpie_spend_estimated_usd",
            "gauge",
            "What Kelpie priced from traces for the same provider and day.",
            [f"kelpie_spend_estimated_usd{lbl} {r.estimated_usd:.6f}" for r, lbl in rows],
        )
        metric(
            "kelpie_spend_gap_ratio",
            "gauge",
            "(estimate - bill) / bill for that day: negative when traces show less than was "
            "billed; absent when the bill is 0.",
            [
                f"kelpie_spend_gap_ratio{lbl} {r.gap_ratio:.6f}"
                for r, lbl in rows
                if r.gap_ratio is not None
            ],
        )
        metric(
            "kelpie_spend_reconciled_day_timestamp_seconds",
            "gauge",
            "Start (UTC midnight) of the newest day reconciled for the provider.",
            [
                f"kelpie_spend_reconciled_day_timestamp_seconds{lbl} {int(r.day.timestamp())}"
                for r, lbl in rows
            ],
        )

    def _render_budgets(self, metric: Callable[[str, str, str, Iterable[str]], None]) -> None:
        rows = [
            (b, _labels(budget=b.budget, subject=b.subject, unit=str(b.unit))) for b in self.budgets
        ]
        metric(
            "kelpie_budget_spent",
            "gauge",
            "Spend per budget and subject over the budget's rolling window (usd: priced from "
            "traces with [prices]; an estimate in subscription mode).",
            [f"kelpie_budget_spent{lbl} {b.spent:.6f}" for b, lbl in rows],
        )
        metric(
            "kelpie_budget_remaining",
            "gauge",
            "Budget left before the hard threshold (soft if there is none), never below 0.",
            [f"kelpie_budget_remaining{lbl} {b.remaining:.6f}" for b, lbl in rows],
        )
        metric(
            "kelpie_budget_burn_rate_per_hour",
            "gauge",
            "Spend per hour over the budget's burn_window.",
            [f"kelpie_budget_burn_rate_per_hour{lbl} {b.burn_rate:.6f}" for b, lbl in rows],
        )
        at = self.budgets_at
        metric(
            "kelpie_budget_runs_out_seconds",
            "gauge",
            "Seconds until the budget runs out at the current burn rate; 0 once it has; "
            "absent without a forecast.",
            [
                f"kelpie_budget_runs_out_seconds{lbl}"
                f" {max((b.runs_out_at - at).total_seconds(), 0.0):.0f}"
                for b, lbl in rows
                if b.runs_out_at is not None and at is not None
            ],
        )
        metric(
            "kelpie_budget_next_limit_seconds",
            "gauge",
            "Subscription mode: seconds until the next provider limit forecast from observed "
            "limit hits; 0 while a limit is in force; absent without a forecast.",
            [
                f"kelpie_budget_next_limit_seconds{lbl}"
                f" {max((b.next_limit_at - at).total_seconds(), 0.0):.0f}"
                for b, lbl in rows
                if b.next_limit_at is not None and at is not None
            ],
        )
        metric(
            "kelpie_budget_state",
            "gauge",
            "1 for the budget's current state (ok, soft or hard) per subject.",
            [
                f"kelpie_budget_state"
                f"{_labels(budget=b.budget, subject=b.subject, state=str(b.state))} 1"
                for b, _ in rows
            ],
        )
        metric(
            "kelpie_budget_cost_unknown_tokens",
            "gauge",
            "Tokens in the budget's window from models with no price (in [prices] or the "
            "default table): counted in token budgets, left out of usd spend.",
            [
                f"kelpie_budget_cost_unknown_tokens"
                f"{_labels(budget=b.budget, subject=b.subject)} {b.cost_unknown_tokens}"
                for b, _ in rows
            ],
        )
        metric(
            "kelpie_budget_unattributed_tokens",
            "gauge",
            "Tokens in the budget's window whose subject (agent, task or project) is unknown, "
            "so no subject was charged for them.",
            [
                f"kelpie_budget_unattributed_tokens{_labels(budget=name)} {n}"
                for name, n in sorted(self.budgets_unattributed.items())
            ],
        )
        metric(
            "kelpie_budget_stale",
            "gauge",
            "1 when spend could not be read on the last tick: budget values are from the last "
            "good read.",
            [f"kelpie_budget_stale {int(self.budgets_stale_now)}"],
        )

    def _render_github(self, metric: Callable[[str, str, str, Iterable[str]], None]) -> None:
        metric(
            "kelpie_connector_errors_total",
            "counter",
            "Connector snapshot steps that raised; the tick goes on without them.",
            [
                f"kelpie_connector_errors_total{_labels(connector=c)} {n}"
                for c, n in sorted(self.connector_errors.items())
            ],
        )
        metric(
            "kelpie_github_requests_total",
            "counter",
            "GitHub API lookups by outcome: ok, not_modified (304), cached (no request), "
            "or an error kind (backoff, network, server, rate_limited, unauthorized, "
            "forbidden, not_found, redirect, bad_response, ...).",
            [
                f"kelpie_github_requests_total{_labels(outcome=o)} {n}"
                for o, n in sorted(self.github_requests.items())
            ],
        )
        metric(
            "kelpie_github_rate_limit_remaining",
            "gauge",
            "X-RateLimit-Remaining from the last GitHub response (-1 before the first).",
            [f"kelpie_github_rate_limit_remaining {self.github_rate_remaining}"],
        )
        metric(
            "kelpie_github_rate_limit",
            "gauge",
            "X-RateLimit-Limit from the last GitHub response (-1 if unknown).",
            [f"kelpie_github_rate_limit {self.github_rate_limit_value}"],
        )
        metric(
            "kelpie_block_conditions",
            "gauge",
            "Blocks in the last snapshot whose reason matched [[block_conditions]]: attached "
            "a condition; unresolved (bad ref, repo not allowed, 404) or unconfirmed (GitHub "
            "could not confirm the PR yet), both left as they are; error (the step failed on "
            "the block).",
            [
                f"kelpie_block_conditions{_labels(outcome=o)} {n}"
                for o, n in self.block_condition_outcomes.items()
            ],
        )

    def _span_rows(self) -> int:
        try:
            return self.span_rows() if self.span_rows is not None else 0
        except Exception:
            return 0

    def _render_traces(self, metric: Callable[[str, str, str, Iterable[str]], None]) -> None:
        metric(
            "kelpie_traces_requests_total",
            "counter",
            "OTLP trace export requests, by HTTP status code.",
            [
                f"kelpie_traces_requests_total{_labels(code=c)} {n}"
                for c, n in sorted(self.traces_requests.items())
            ],
        )
        metric(
            "kelpie_spans_received_total",
            "counter",
            "Spans accepted from OTLP, by Kelpie span kind; kind=ignored for spans that are "
            "not GenAI steps (used for linkage, not stored).",
            [
                f"kelpie_spans_received_total{_labels(kind=k)} {n}"
                for k, n in sorted(self.spans_received_total.items())
            ],
        )
        metric(
            "kelpie_spans_rejected_total",
            "counter",
            "Spans rejected from OTLP requests (reported back as partial success), by reason.",
            [
                f"kelpie_spans_rejected_total{_labels(reason=r)} {n}"
                for r, n in sorted(self.spans_rejected_total.items())
            ],
        )
        metric(
            "kelpie_span_store_rows",
            "gauge",
            "Spans in the span store.",
            [f"kelpie_span_store_rows {self._span_rows()}"],
        )
        metric(
            "kelpie_snapshot_spans",
            "gauge",
            "Stored spans in the last snapshot ([traces] window, capped).",
            [f"kelpie_snapshot_spans {self.snapshot_span_count}"],
        )
        metric(
            "kelpie_snapshot_spans_dropped_total",
            "counter",
            "Stored spans left out of snapshots by [traces] max_snapshot_spans (oldest first).",
            [f"kelpie_snapshot_spans_dropped_total {self.snapshot_spans_dropped}"],
        )
        metric(
            "kelpie_snapshot_hook_spans_merged",
            "gauge",
            "Hook tool spans in the last snapshot folded into an OTLP span of the same call.",
            [f"kelpie_snapshot_hook_spans_merged {self.snapshot_hook_merged}"],
        )
        metric(
            "kelpie_span_store_errors_total",
            "counter",
            "Span store reads (snapshot) or retention runs that failed.",
            [f"kelpie_span_store_errors_total {self.span_store_errors}"],
        )
