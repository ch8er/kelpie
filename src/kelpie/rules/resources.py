"""Rule 7 — environment resources (incidents 3, 4; provider quotas and auth: incident 6).

Warn before a host runs out of something and the agents stop: disk, memory, CPU, load and
processes, each with a warning and a critical utilization threshold. Readings are
``Resource`` records per host (``used``/``limit``); where they come from is not the rule's
business (``[resources.prometheus]`` today). Readings older than ``max_reading_age`` are
ignored, so a source that stopped answering never leaves an old "all fine" or "99% full"
behind.

Heavy tasks (full builds, large test suites; marked by a label or a role) multiply the
load of a host. When a host is under pressure (memory, load or CPU past the warning
threshold) and more heavy tasks run on it than ``max_heavy_per_host``, one alert says so,
with the suggested limit and the heavy tasks still queued; Kelpie only suggests, the human
or the dispatcher serializes the work. Which host a task runs on comes from ``Task.host``,
which the platform adapter must set; the Hermes adapter does not yet. A running heavy task
without a host is attributed to the single host with readings, if there is exactly one.
With several hosts and unplaced tasks the rule cannot say where they run: it raises at
most one alert without a host when the farm-wide count exceeds what the hosts allow while
some host is under pressure. Queued (ready) tasks have no host yet by nature and are
listed as the shared queue.

Every alert is about a lasting state: a stable ``state`` and ``key`` per resource, so the
engine alerts when it opens, when it gets worse and when it ends, not on every tick.
LLM provider quotas and credential expiry (``llm_quota``, ``auth``, ``ci_minutes``
resources) are a separate step; ``_provider_alerts`` is where they plug in.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Resource, ResourceKind, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register

NAME = "resources"
DEFAULT_MAX_READING_AGE = timedelta(minutes=10)
_MAX_LISTED_TASKS = 5


@dataclass(frozen=True, slots=True)
class Thresholds:
    """Utilization (used / limit) at which a resource warns and turns critical."""

    warning: float | None
    critical: float | None = None

    def level(self, utilization: float) -> Severity | None:
        if self.critical is not None and utilization >= self.critical:
            return Severity.CRITICAL
        if self.warning is not None and utilization >= self.warning:
            return Severity.WARNING
        return None


# Utilization is a ratio; for load it is load per core, so 1.5 is 50% more runnable
# threads than cores.
DEFAULT_THRESHOLDS: Mapping[ResourceKind, Thresholds] = {
    ResourceKind.DISK: Thresholds(0.85, 0.95),
    ResourceKind.MEMORY: Thresholds(0.90, 0.97),
    ResourceKind.LOAD: Thresholds(1.5, 3.0),
    ResourceKind.CPU: Thresholds(None, None),  # off: load covers overload
    ResourceKind.PROCESSES: Thresholds(0.80, 0.95),
}
# Kinds where a heavy task adds to the pressure.
PRESSURE_KINDS = frozenset({ResourceKind.MEMORY, ResourceKind.LOAD, ResourceKind.CPU})
_RANK = {Severity.INFO: 0, Severity.WARNING: 1, Severity.CRITICAL: 2}


@dataclass(frozen=True, slots=True)
class Finding:
    resource: Resource
    utilization: float
    severity: Severity
    thresholds: Thresholds


@dataclass(frozen=True, slots=True)
class Resources:
    thresholds: Mapping[ResourceKind, Thresholds] = field(
        default_factory=lambda: dict(DEFAULT_THRESHOLDS)
    )
    heavy_labels: tuple[str, ...] = ("heavy",)
    heavy_roles: tuple[str, ...] = ()
    max_heavy_per_host: int | None = None  # None: heavy-task concurrency is not checked
    max_reading_age: timedelta = DEFAULT_MAX_READING_AGE
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        return [*self._host_alerts(snapshot), *self._provider_alerts(snapshot)]

    # --- hosts --------------------------------------------------------------------------

    def _host_alerts(self, snapshot: Snapshot) -> list[Action]:
        fresh = self._fresh(snapshot)
        findings = self._findings(fresh)
        heavy: list[Action] = []
        covered: set[str] = set()
        if self.max_heavy_per_host is not None:
            heavy, covered = self._heavy_alerts(findings, {h for h, _, _ in fresh}, snapshot)
        return [*heavy, *(self._alert(f, covered) for f in findings)]

    def _fresh(self, snapshot: Snapshot) -> dict[tuple[str, ResourceKind, str], Resource]:
        newest: dict[tuple[str, ResourceKind, str], Resource] = {}
        for r in snapshot.resources:
            if r.host is None or r.kind not in self.thresholds:
                continue
            if r.at is not None and snapshot.now - r.at > self.max_reading_age:
                continue  # stale: no data is better than old data
            key = (r.host, r.kind, r.name)
            held = newest.get(key)
            if held is None or (r.at or snapshot.now) >= (held.at or snapshot.now):
                newest[key] = r
        return newest

    def _findings(self, fresh: Mapping[tuple[str, ResourceKind, str], Resource]) -> list[Finding]:
        findings: list[Finding] = []
        for (_, kind, _), r in sorted(fresh.items(), key=lambda kv: kv[0]):
            utilization = r.utilization
            if utilization is None:
                continue
            thresholds = self.thresholds[kind]
            if (severity := thresholds.level(utilization)) is not None:
                findings.append(Finding(r, utilization, severity, thresholds))
        return findings

    @staticmethod
    def _pressure(findings: Iterable[Finding]) -> dict[str, list[Finding]]:
        hosts: dict[str, list[Finding]] = {}
        for f in findings:
            if f.resource.kind in PRESSURE_KINDS and f.resource.host is not None:
                hosts.setdefault(f.resource.host, []).append(f)
        return hosts

    def _alert(self, f: Finding, covered: set[str]) -> Action:
        r = f.resource
        state = f"{NAME}:{r.kind}:{r.name}"
        limit = f.thresholds.critical if f.severity is Severity.CRITICAL else f.thresholds.warning
        return Action(
            ActionKind.ALERT,
            NAME,
            f"{_describe(f)}. "
            + (
                "Agents fail in confusing ways when it runs out."
                if f.severity is Severity.CRITICAL
                else "Free it before the agents stop."
            )
            + (
                f" Also covered by the heavy-task alert for {r.host}."
                if r.host in covered and f.resource.kind in PRESSURE_KINDS
                else ""
            ),
            severity=f.severity,
            # One state per resource, and the severity is not part of the key: a rule keeps
            # no history, so a reading that wobbles around the critical threshold must not
            # be announced again each time. The state tracker remembers the worst severity
            # it announced and escalates only upwards.
            key=state,
            state=state,
            params={
                "host": r.host,
                "kind": str(r.kind),
                "resource": r.name,
                "utilization": round(f.utilization, 3),
                "used": r.used,
                "limit": r.limit,
                "unit": r.unit,
                "threshold": limit,
            },
        )

    # --- heavy-task concurrency ---------------------------------------------------------

    def _is_heavy(self, task: Task) -> bool:
        return bool(set(task.labels) & set(self.heavy_labels)) or (
            task.role is not None and task.role in self.heavy_roles
        )

    def _heavy_alerts(
        self, findings: list[Finding], hosts: set[str], snapshot: Snapshot
    ) -> tuple[list[Action], set[str]]:
        """Heavy-task alerts, and the hosts they cover."""
        assert self.max_heavy_per_host is not None
        limit = self.max_heavy_per_host
        pressure = self._pressure(findings)
        if not pressure:
            return [], set()
        heavy = [t for t in snapshot.tasks if self._is_heavy(t)]
        sole = next(iter(hosts)) if len(hosts) == 1 else None
        placed: dict[str, list[str]] = {}
        unplaced: list[str] = []
        for t in heavy:
            if t.status is not Status.IN_PROGRESS:
                continue
            if (host := t.host or sole) is None:
                unplaced.append(t.id)
            else:
                placed.setdefault(host, []).append(t.id)
        queued = sorted(t.id for t in heavy if t.status is Status.READY)
        actions: list[Action] = []
        covered: set[str] = set()
        for host, host_pressure in sorted(pressure.items()):
            running = sorted(placed.get(host, []))
            if len(running) > limit:
                actions.append(self._host_alert(host, host_pressure, running, queued, limit))
                covered.add(host)
        total = len(unplaced) + sum(len(v) for v in placed.values())
        if unplaced and total > limit * max(1, len(hosts)):
            actions.append(
                self._unplaced_alert(
                    sorted(pressure),
                    sorted(unplaced + [i for v in placed.values() for i in v]),
                    queued,
                    limit,
                    len(hosts),
                )
            )
        return actions, covered

    def _host_alert(
        self, host: str, pressure: list[Finding], running: list[str], queued: list[str], limit: int
    ) -> Action:
        state = f"{NAME}:{host}:heavy"
        worst = max(pressure, key=lambda f: (_RANK[f.severity], f.utilization))
        return Action(
            ActionKind.ALERT,
            NAME,
            f"Host {host} is overloaded ({'; '.join(_describe(f) for f in pressure)}) while "
            f"{len(running)} heavy tasks run on it at once (limit {limit}) and "
            f"{len(queued)} more wait in ready. Suggest at most {limit} heavy task(s) at a "
            f"time on {host}: let {_ids(queued) or 'the queued ones'} wait for a free slot, "
            f"and run light tasks alongside.",
            severity=Severity.WARNING,
            key=f"{state}:{limit}",
            state=state,
            params={
                "host": host,
                "max_heavy": limit,
                "running_heavy": running,
                "queued_heavy": queued,
                "worst_resource": worst.resource.name,
            },
        )

    def _unplaced_alert(
        self, loaded: list[str], running: list[str], queued: list[str], limit: int, hosts: int
    ) -> Action:
        state = f"{NAME}:heavy:unplaced"
        return Action(
            ActionKind.ALERT,
            NAME,
            f"{len(running)} heavy tasks run at once across {hosts} hosts (limit {limit} per "
            f"host) while {', '.join(loaded)} {'is' if len(loaded) == 1 else 'are'} "
            f"overloaded, but the tasks do not say which host they run on, so Kelpie cannot "
            f"say where to hold back. Suggest at most {limit} heavy task(s) per host; "
            f"{len(queued)} more wait in ready. Per-host limits need an adapter that sets "
            f"Task.host.",
            severity=Severity.WARNING,
            key=f"{state}:{limit}",
            state=state,
            params={
                "max_heavy": limit,
                "running_heavy": running,
                "queued_heavy": queued,
                "overloaded_hosts": loaded,
            },
        )

    # --- provider quotas and auth (separate step) ---------------------------------------

    def _provider_alerts(self, snapshot: Snapshot) -> list[Action]:
        """LLM quotas and credential expiry (incident 6) plug in here."""
        return []


def _ids(ids: Sequence[str]) -> str:
    shown = ", ".join(ids[:_MAX_LISTED_TASKS])
    return (
        f"{shown} and {len(ids) - _MAX_LISTED_TASKS} more"
        if len(ids) > _MAX_LISTED_TASKS
        else shown
    )


def _pct(value: float) -> str:
    return f"{value * 100:.0f}%"


def _describe(f: Finding) -> str:
    r, host = f.resource, f.resource.host
    amount = f" ({r.used:g} of {r.limit:g} {r.unit})" if r.used is not None and r.limit else ""
    match r.kind:
        case ResourceKind.DISK:
            return f"disk {r.name} is {_pct(f.utilization)} full{amount}"
        case ResourceKind.MEMORY:
            return f"memory on {host} is {_pct(f.utilization)} used{amount}"
        case ResourceKind.LOAD:
            return f"load on {host} is {f.utilization:.1f}x its cores{amount}"
        case ResourceKind.CPU:
            return f"CPU on {host} is {_pct(f.utilization)} busy"
        case ResourceKind.PROCESSES:
            return f"{host} runs {_pct(f.utilization)} of its process limit{amount}"
        case _:
            return f"{r.kind} {r.name} is at {_pct(f.utilization)}"


# --- configuration ----------------------------------------------------------------------

_KINDS = {
    "disk": ResourceKind.DISK,
    "memory": ResourceKind.MEMORY,
    "load": ResourceKind.LOAD,
    "cpu": ResourceKind.CPU,
    "processes": ResourceKind.PROCESSES,
}


def _ratio(value: Any, name: str, kind: ResourceKind) -> float | None:
    if value is False or value is None:
        return None  # switched off
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{name} must be a positive number (or false to switch it off)")
    if kind is not ResourceKind.LOAD and value > 1:
        raise ValueError(f"{name} is a utilization between 0 and 1")
    return float(value)


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list | tuple) or not all(
        isinstance(v, str) and v.strip() for v in value
    ):
        raise ValueError(f"{name} must be a string or a list of strings")
    return tuple(v.strip() for v in value)


@register(NAME)
def build(config: dict[str, Any]) -> Resources:
    config = dict(config)
    thresholds = dict(DEFAULT_THRESHOLDS)
    for word, kind in _KINDS.items():
        warning, critical = thresholds[kind].warning, thresholds[kind].critical
        if f"{word}_warning" in config:
            warning = _ratio(config.pop(f"{word}_warning"), f"{word}_warning", kind)
        if f"{word}_critical" in config:
            critical = _ratio(config.pop(f"{word}_critical"), f"{word}_critical", kind)
        if warning is not None and critical is not None and warning >= critical:
            raise ValueError(f"{word}_warning must be below {word}_critical")
        thresholds[kind] = Thresholds(warning, critical)
    kwargs: dict[str, Any] = {"thresholds": thresholds}
    labels: tuple[str, ...] = ()
    for key in ("heavy_label", "heavy_labels"):
        if key in config:
            labels += _strings(config.pop(key), key)
    if labels:
        kwargs["heavy_labels"] = labels
    if "heavy_roles" in config:
        kwargs["heavy_roles"] = _strings(config.pop("heavy_roles"), "heavy_roles")
    if "max_heavy_per_host" in config:
        value = config.pop("max_heavy_per_host")
        if value is not False and value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("max_heavy_per_host must be a positive integer (or false)")
            kwargs["max_heavy_per_host"] = value
    if "max_reading_age" in config:
        value = config.pop("max_reading_age")
        try:
            age = parse_duration(value)
        except (AttributeError, TypeError) as exc:
            raise ValueError(f"max_reading_age: invalid duration {value!r}") from exc
        if age <= timedelta(0):
            raise ValueError("max_reading_age must be positive")
        kwargs["max_reading_age"] = age
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return Resources(**kwargs)
