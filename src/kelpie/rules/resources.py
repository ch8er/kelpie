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

LLM providers (incident 6) come as ``ProviderStatus`` probes (``[connectors.llm_quotas]``),
``LimitHit`` records and ``llm_quota``/``auth`` resources from any other source. One alert
per provider covers its quota: little left (``remaining / limit`` at or below
``quota_warning``), none left, and limit hits counted over ``rate_limit_window``
(``rate_limit_events_warning`` or more). A quota with nothing left is critical when agent
tasks that use the provider are ready or running (``blocked_tasks``); which tasks use which
provider is config data (``provider_roles``, ``provider_owners``), and with a single
provider and no mapping every agent task uses it. Quota windows that refill within
``ignore_resets_within`` (per-minute limits) are not reported as low or exhausted: they
recover on their own. One alert per credential: rejected by the provider (401/403) or
expired is critical, expiring within ``auth_expiry_warning`` is a warning. A probe that
failed for another reason means "unknown" and raises nothing here (the connector logs and
counts it).

Every alert is about a lasting state: a stable ``state`` and ``key`` per resource, so the
engine alerts when it opens, when it gets worse and when it ends, not on every tick.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import OwnerKind, Resource, ResourceKind, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register

NAME = "resources"
DEFAULT_MAX_READING_AGE = timedelta(minutes=10)
DEFAULT_QUOTA_WARNING = 0.1
DEFAULT_RATE_LIMIT_EVENTS = 20
DEFAULT_RATE_LIMIT_WINDOW = timedelta(hours=24)
DEFAULT_AUTH_EXPIRY_WARNING = timedelta(hours=72)
DEFAULT_IGNORE_RESETS_WITHIN = timedelta(minutes=10)
# Tasks that would run now and fail without quota or a working key.
_ACTIVE = frozenset({Status.IN_PROGRESS, Status.READY})
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
    quota_warning: float | None = DEFAULT_QUOTA_WARNING  # remaining / limit; None: off
    rate_limit_events_warning: int | None = DEFAULT_RATE_LIMIT_EVENTS  # None: off
    rate_limit_window: timedelta = DEFAULT_RATE_LIMIT_WINDOW
    auth_expiry_warning: timedelta = DEFAULT_AUTH_EXPIRY_WARNING
    ignore_resets_within: timedelta = DEFAULT_IGNORE_RESETS_WITHIN
    provider_roles: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    provider_owners: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
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

    # --- LLM provider quotas and auth -------------------------------------------------

    def _is_fresh(self, at: datetime | None, now: datetime) -> bool:
        return at is None or now - at <= self.max_reading_age

    def _provider_alerts(self, snapshot: Snapshot) -> list[Action]:
        quotas = self._quotas(snapshot)
        # Providers the farm uses, for the single-provider default of ``_users``.
        known = set(quotas) | {p.name for p in snapshot.providers}
        actions: list[Action] = []
        for _, q in sorted(quotas.items()):
            if (alert := self._quota_alert(q, snapshot, known)) is not None:
                actions.append(alert)
        for _, c in sorted(self._credentials(snapshot).items()):
            if (alert := self._credential_alert(c, snapshot, known)) is not None:
                actions.append(alert)
        return actions

    def _quotas(self, snapshot: Snapshot) -> dict[str, _Quota]:
        now = snapshot.now
        quotas: dict[str, _Quota] = {}
        for r in snapshot.resources:
            if r.kind is not ResourceKind.LLM_QUOTA or not self._is_fresh(r.at, now):
                continue
            remaining = (
                max(r.limit - r.used, 0.0) if r.used is not None and r.limit is not None else None
            )
            held = quotas.get(r.name)
            if held is None or (r.at or now) >= (held.at or now):
                quotas[r.name] = _Quota(
                    r.name, remaining, r.limit, r.unit, r.resets_at, r.events, r.at
                )
        for p in snapshot.providers:
            if not p.ok or not self._is_fresh(p.checked_at, now):
                continue  # unknown: no data rather than old data
            held = quotas.get(p.name)
            if held is None or (p.checked_at or now) >= (held.at or now):
                hits = held.hits if held is not None else 0
                quotas[p.name] = _Quota(
                    p.name, p.remaining, p.limit, p.unit, p.resets_at, hits, p.checked_at
                )
        counts: dict[str, int] = {}
        for hit in snapshot.limit_hits:
            if hit.at <= now and now - hit.at <= self.rate_limit_window:
                counts[hit.limit] = counts.get(hit.limit, 0) + 1
        for name, n in counts.items():
            held = quotas.get(name)
            quotas[name] = (
                _Quota(name, None, None, "", None, n, None)
                if held is None
                else replace(held, hits=held.hits + n)
            )
        return quotas

    def _credentials(self, snapshot: Snapshot) -> dict[str, _Credential]:
        now = snapshot.now
        out: dict[str, _Credential] = {}
        for r in snapshot.resources:
            if (
                r.kind is ResourceKind.AUTH
                and r.expires_at is not None
                and self._is_fresh(r.at, now)
            ):
                out[r.name] = _Credential(r.name, r.host, r.expires_at, False)
        for p in snapshot.providers:
            if not self._is_fresh(p.checked_at, now):
                continue
            if p.auth_ok is False or p.expires_at is not None:
                out[p.name] = _Credential(p.name, None, p.expires_at, p.auth_ok is False)
        return out

    def _quota_alert(self, q: _Quota, snapshot: Snapshot, known: set[str]) -> Action | None:
        now = snapshot.now
        remaining = q.remaining
        if q.resets_at is not None and q.resets_at - now <= self.ignore_resets_within:
            remaining = None  # refilled already, or refills soon on its own
        exhausted = remaining is not None and remaining <= 0
        share = remaining / q.limit if remaining is not None and q.limit else None
        low = (
            not exhausted
            and share is not None
            and self.quota_warning is not None
            and share <= self.quota_warning
        )
        many_hits = (
            self.rate_limit_events_warning is not None and q.hits >= self.rate_limit_events_warning
        )
        if not (exhausted or low or many_hits):
            return None
        blocked = self._users(q.name, snapshot, known) if exhausted else []
        until = f" until {_at(q.resets_at, now)}" if q.resets_at is not None else ""
        parts: list[str] = []
        if exhausted:
            parts.append(f"LLM provider {q.name} has no quota left{until}.")
            if blocked:
                parts.append(
                    f"{len(blocked)} agent task(s) that use it cannot make progress: "
                    f"{_ids(blocked)}."
                )
        elif low and share is not None and remaining is not None and q.limit is not None:
            unit = f" {q.unit}" if q.unit else ""
            parts.append(
                f"LLM provider {q.name} has {_pct(share)} of its quota left "
                f"({_num(remaining)} of {_num(q.limit)}{unit}){until}."
            )
        if many_hits:
            parts.append(
                f"{q.name} hit its rate limit {q.hits} times in the last "
                f"{_duration(self.rate_limit_window)}; agents spend that time backing off "
                "and retrying."
            )
        parts.append(
            "Run fewer agents on it at once"
            + (" or move heavy work past the reset." if exhausted or low else ".")
        )
        state = f"{NAME}:llm_quota:{q.name}"
        return Action(
            ActionKind.ALERT,
            NAME,
            " ".join(parts),
            severity=Severity.CRITICAL if blocked else Severity.WARNING,
            key=state,
            state=state,
            params={
                "provider": q.name,
                "remaining": q.remaining,
                "limit": q.limit,
                "unit": q.unit,
                "resets_at": q.resets_at.isoformat() if q.resets_at else None,
                "exhausted": exhausted,
                "limit_hits": q.hits,
                "blocked_tasks": blocked,
            },
        )

    def _credential_alert(
        self, c: _Credential, snapshot: Snapshot, known: set[str]
    ) -> Action | None:
        now = snapshot.now
        where = f" on {c.host}" if c.host else ""
        if c.rejected:
            severity = Severity.CRITICAL
            reason = (
                f"LLM provider {c.name} rejected its key (expired or revoked): agent sessions "
                "that use it fail at startup until someone renews the key."
            )
        elif c.expires_at is not None and c.expires_at <= now:
            severity = Severity.CRITICAL
            reason = (
                f"Credential {c.name}{where} expired {_duration(now - c.expires_at)} ago: "
                "new agent sessions that use it fail at startup until someone signs in again."
            )
        elif c.expires_at is not None and c.expires_at - now <= self.auth_expiry_warning:
            severity = Severity.WARNING
            reason = (
                f"Credential {c.name}{where} expires in {_duration(c.expires_at - now)} "
                f"({c.expires_at.isoformat()}); renew it before agent sessions start failing."
            )
        else:
            return None
        blocked = self._users(c.name, snapshot, known) if severity is Severity.CRITICAL else []
        if blocked:
            reason += f" Affected: {_ids(blocked)}."
        state = f"{NAME}:auth:{c.name}"
        return Action(
            ActionKind.ALERT,
            NAME,
            reason,
            severity=severity,
            key=state,
            state=state,
            params={
                "credential": c.name,
                "host": c.host,
                "rejected": c.rejected,
                "expires_at": c.expires_at.isoformat() if c.expires_at else None,
                "blocked_tasks": blocked,
            },
        )

    def _users(self, name: str, snapshot: Snapshot, known: set[str]) -> list[str]:
        """Ready or running agent tasks that use provider (or credential) ``name``: by the
        configured roles and owners, or every agent task when no mapping is configured
        and ``name`` is the only provider in sight."""
        roles = set(self.provider_roles.get(name, ()))
        owners = set(self.provider_owners.get(name, ()))
        everyone = not (self.provider_roles or self.provider_owners) and known == {name}
        if not (roles or owners or everyone):
            return []
        out: list[str] = []
        for t in snapshot.tasks:
            if t.status not in _ACTIVE:
                continue
            owner = snapshot.owner(t.owner_id) if t.owner_id else None
            if owner is not None and owner.kind is OwnerKind.HUMAN:
                continue
            if (
                everyone
                or (t.role is not None and t.role in roles)
                or (owner is not None and owner.role in roles)
                or (t.owner_id is not None and t.owner_id in owners)
            ):
                out.append(t.id)
        return sorted(out)


@dataclass(frozen=True, slots=True)
class _Quota:
    name: str
    remaining: float | None
    limit: float | None
    unit: str
    resets_at: datetime | None
    hits: int
    at: datetime | None


@dataclass(frozen=True, slots=True)
class _Credential:
    name: str
    host: str | None
    expires_at: datetime | None
    rejected: bool


def _duration(span: timedelta) -> str:
    seconds = int(span.total_seconds())
    if seconds >= 2 * 86400:
        return f"{seconds // 86400}d"
    if seconds >= 3600:
        hours, minutes = divmod(seconds, 3600)
        return f"{hours}h{minutes // 60:02d}m" if minutes >= 60 else f"{hours}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{max(seconds, 0)}s"


def _num(value: float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:,.2f}"


def _at(when: datetime, now: datetime) -> str:
    return f"{when.isoformat()} (in {_duration(when - now)})"


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


def _ratio(value: Any, name: str, kind: ResourceKind | None) -> float | None:
    if value is False or value is None:
        return None  # switched off
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        raise ValueError(f"{name} must be a positive number (or false to switch it off)")
    if kind is not ResourceKind.LOAD and value > 1:
        raise ValueError(f"{name} is a ratio between 0 and 1")
    return float(value)


def _positive_duration(value: Any, name: str) -> timedelta:
    try:
        span = parse_duration(value)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if span <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return span


def _provider_map(value: Any, name: str) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping) or not all(isinstance(k, str) and k for k in value):
        raise ValueError(f'{name} must map provider names to lists, e.g. {{main = ["coder"]}}')
    return {k: _strings(v, f"{name}.{k}") for k, v in value.items()}


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
    for key in (
        "max_reading_age",
        "rate_limit_window",
        "auth_expiry_warning",
        "ignore_resets_within",
    ):
        if key in config:
            kwargs[key] = _positive_duration(config.pop(key), key)
    if "quota_warning" in config:
        kwargs["quota_warning"] = _ratio(config.pop("quota_warning"), "quota_warning", None)
    if "rate_limit_events_warning" in config:
        value = config.pop("rate_limit_events_warning")
        if value is False or value is None:
            kwargs["rate_limit_events_warning"] = None
        elif isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError("rate_limit_events_warning must be a positive integer (or false)")
        else:
            kwargs["rate_limit_events_warning"] = value
    for key in ("provider_roles", "provider_owners"):
        if key in config:
            kwargs[key] = _provider_map(config.pop(key), key)
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return Resources(**kwargs)
