"""Kelpie's own metrics in the Prometheus text exposition format (stdlib only)."""

from __future__ import annotations

import threading
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING

from kelpie import __version__

if TYPE_CHECKING:
    from kelpie.app import RuleStatus
    from kelpie.core.engine import Engine
    from kelpie.core.ledger import Dispatched


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: str) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items()) + "}"


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
        self.ledger_up = True
        self.dry_run = True
        self.rules: list[tuple[str, str, bool]] = []  # (name, mode, implemented)

    def set_rules(self, statuses: Iterable[RuleStatus], dry_run: bool) -> None:
        with self._lock:
            self.rules = [(s.name, str(s.mode), s.implemented) for s in statuses]
            self.dry_run = dry_run

    def observe_tick(self, seconds: float, results: Sequence[Dispatched], engine: Engine) -> None:
        with self._lock:
            self.ticks += 1
            self.tick_seconds += seconds
            self.last_tick = time.time()
            for d in results:
                a = d.action
                self.actions[(a.rule, str(a.kind), str(d.mode), str(d.outcome))] += 1
            self.rule_errors = Counter(engine.rule_errors)
            self.ledger_up = engine.ledger_available

    def tick_failed(self) -> None:
        with self._lock:
            self.tick_failures += 1

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
                "Planned rules: 1 if implemented and loaded, 0 if not implemented yet.",
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
                "kelpie_actions_total",
                "counter",
                "Actions emitted by rules, by what happened to them.",
                [
                    f"kelpie_actions_total{_labels(rule=r, kind=k, mode=m, outcome=o)} {n}"
                    for (r, k, m, o), n in sorted(self.actions.items())
                ],
            )
            metric(
                "kelpie_ledger_up",
                "gauge",
                "1 if the action ledger was readable on the last tick.",
                [f"kelpie_ledger_up {int(self.ledger_up)}"],
            )
            return "\n".join(out) + "\n"
