"""Host resource readings from node_exporter metrics, via the Prometheus HTTP API.

One instant query (``GET /api/v1/query``) per metric. The queries are fixed text, never
built from data, and every result is matched to a host by a label value from the
configuration; series of hosts that are not configured are ignored. Disk, memory, load, CPU
and process counts become :class:`~kelpie.core.model.Resource` readings (``used``/``limit``
pairs, so a rule sees utilization) with ``at`` set to the sample's timestamp (Prometheus
clock, capped at the time of the fetch).

The client is deliberately narrow. Redirects are never followed and proxies from the
environment are never used, so a bearer token goes only to the configured URL. The timeout
is a deadline for the whole request, not per read (see :class:`PrometheusClient`). Failure
messages and logs never include the token, the response body or the query string. Any
failure yields no readings for that fetch (a rule must see no data rather than stale data),
counts in ``kelpie_resource_read_failures_total`` and logs one warning per cause until a
fetch succeeds again. A failed fetch is not retried before the next ``interval``, so an
unreachable Prometheus costs at most one timeout per interval.
"""

from __future__ import annotations

import json
import logging
import math
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Protocol

from kelpie import __version__, net
from kelpie.core.model import Resource, ResourceKind

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 5.0
DEFAULT_INTERVAL = timedelta(seconds=60)
DEFAULT_HOST_LABEL = "instance"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SERIES = 10_000
GB = 1e9

# Filesystems that are not storage the agents can fill.
_FS = 'fstype!~"tmpfs|ramfs|overlay|squashfs|devtmpfs|nsfs|proc|sysfs|cgroup.*|fuse.*|autofs"'

# ``{host_label}`` is replaced by the configured host label. Custom queries are used as
# written and must keep that label in their result.
DEFAULT_QUERIES: Mapping[str, str] = {
    "disk_size": f"node_filesystem_size_bytes{{{_FS}}}",
    "disk_avail": f"node_filesystem_avail_bytes{{{_FS}}}",
    "memory_total": "node_memory_MemTotal_bytes",
    "memory_available": "node_memory_MemAvailable_bytes",
    "load1": "node_load1",
    "cores": 'count by ({host_label}) (node_cpu_seconds_total{mode="idle"})',
    "cpu_busy": (
        '100 * (1 - avg by ({host_label}) (rate(node_cpu_seconds_total{mode="idle"}[5m])))'
    ),
    # Needs node_exporter's --collector.processes; without it there is no reading.
    "processes": "node_processes_pids",
    "processes_max": "node_processes_max_processes",
}
QUERY_NAMES = frozenset(DEFAULT_QUERIES)

valid_token = net.valid_header_value


class ReadObserver(Protocol):
    def resource_read(self, readings: int) -> None: ...

    def resource_failed(self, cause: str) -> None: ...


# A failed query. ``cause`` is a short category (a metric label); ``detail`` is safe to
# log: it never holds the token, the URL's query or the response body.
QueryError = net.RequestError


@dataclass(frozen=True, slots=True)
class Sample:
    labels: Mapping[str, str]
    value: float
    timestamp: float | None = None  # Unix seconds, as Prometheus stamped the sample


@dataclass(frozen=True, slots=True)
class PrometheusClient:
    """One GET per query through :func:`kelpie.net.get`: no redirects, no proxies, one
    deadline for the whole request (see :mod:`kelpie.net`). DNS resolution is not bounded,
    so use an IP address or a resolver you trust.
    """

    url: str  # base URL, e.g. http://prometheus:9090
    timeout: float = DEFAULT_TIMEOUT
    token: str | None = field(default=None, repr=False)

    def query(self, promql: str) -> list[Sample]:
        """Run one instant query; raises :class:`QueryError`."""
        parts = urllib.parse.urlsplit(self.url)
        path = f"{parts.path.rstrip('/')}/api/v1/query?{urllib.parse.urlencode({'query': promql})}"
        headers = {"User-Agent": f"kelpie/{__version__}", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = net.get(self.url, path, headers, self.timeout, MAX_RESPONSE_BYTES)
        if response.status >= 400:
            cause = "http_auth" if response.status in (401, 403) else "http_error"
            raise QueryError(cause, f"HTTP {response.status}")
        return _parse(response.body)


def _parse(body: bytes) -> list[Sample]:
    try:
        payload = json.loads(body)
    except (ValueError, RecursionError):  # RecursionError: absurdly nested JSON
        raise QueryError("invalid_response", "response is not JSON") from None
    if not isinstance(payload, dict):
        raise QueryError("invalid_response", "response is not a JSON object")
    if payload.get("status") != "success":
        error_type = payload.get("errorType")
        what = error_type if isinstance(error_type, str) and error_type.isidentifier() else "?"
        raise QueryError("query_failed", f"Prometheus rejected the query ({what})")
    data = payload.get("data")
    if (
        not isinstance(data, dict)
        or data.get("resultType") != "vector"
        or not isinstance(data.get("result"), list)
    ):
        raise QueryError("invalid_response", "unexpected result type (an instant vector is needed)")
    if len(data["result"]) > MAX_SERIES:
        log.warning(
            "a Prometheus query returned %d series; only the first %d are read, so some hosts "
            "may be missing: narrow the query",
            len(data["result"]),
            MAX_SERIES,
        )
    samples: list[Sample] = []
    for item in data["result"][:MAX_SERIES]:
        try:
            labels = item["metric"]
            value = float(item["value"][1])
            stamp = item["value"][0]
        except (KeyError, IndexError, TypeError, ValueError):
            continue  # a malformed series is skipped, not fatal
        if not isinstance(labels, dict) or not math.isfinite(value):
            continue  # NaN and infinities carry no reading
        timestamp = (
            float(stamp) if isinstance(stamp, int | float) and math.isfinite(stamp) else None
        )
        samples.append(Sample({str(k): str(v) for k, v in labels.items()}, value, timestamp))
    return samples


@dataclass(frozen=True, slots=True)
class PrometheusConfig:
    url: str
    hosts: Mapping[str, str]  # Kelpie host name -> value of ``host_label`` in Prometheus
    host_label: str = DEFAULT_HOST_LABEL
    timeout: float = DEFAULT_TIMEOUT
    interval: timedelta = DEFAULT_INTERVAL
    token: str | None = field(default=None, repr=False)
    queries: Mapping[str, str] = field(default_factory=dict)  # overrides of DEFAULT_QUERIES

    def query_text(self, name: str) -> str:
        text = self.queries.get(name)
        if text is not None:
            return text
        return DEFAULT_QUERIES[name].replace("{host_label}", self.host_label)


class PrometheusSource:
    """Implements :class:`kelpie.core.ports.ResourceReader`. Used from one thread."""

    def __init__(
        self,
        config: PrometheusConfig,
        observer: ReadObserver | None = None,
        client: PrometheusClient | None = None,
    ) -> None:
        self.config = config
        self.observer = observer
        self.client = client or PrometheusClient(config.url, config.timeout, config.token)
        self._hosts = {label: host for host, label in config.hosts.items()}
        self._cached: tuple[Resource, ...] = ()
        self._next_at: datetime | None = None
        self._warned: set[str] = set()

    def read(self, now: datetime) -> tuple[Resource, ...]:
        if self._next_at is not None and now < self._next_at:
            return self._cached
        self._next_at = now + self.config.interval
        try:
            self._cached = self._fetch(now)
        except Exception as caught:
            self._cached = ()  # no data rather than old data
            exc = (
                caught
                if isinstance(caught, QueryError)
                else QueryError("unexpected", type(caught).__name__)
            )
            if exc.cause not in self._warned:
                self._warned.add(exc.cause)
                log.warning(
                    "cannot read host resources from Prometheus (%s): %s; rules see no host "
                    "readings until it answers",
                    exc.cause,
                    exc.detail,
                )
            if self.observer is not None:
                self.observer.resource_failed(exc.cause)
        else:
            if self._warned:
                log.info("Prometheus answers again; host resource readings resumed")
            self._warned.clear()
            if self.observer is not None:
                self.observer.resource_read(len(self._cached))
        return self._cached

    # --- queries ------------------------------------------------------------------------

    def _series(
        self, name: str, now: datetime, by_mount: bool = False
    ) -> dict[tuple[str, str], Point]:
        """Values of one query per (Kelpie host, mount); unconfigured hosts are dropped."""
        out: dict[tuple[str, str], Point] = {}
        samples = self.client.query(self.config.query_text(name))
        for sample in sorted(samples, key=lambda s: sorted(s.labels.items())):
            host = self._hosts.get(sample.labels.get(self.config.host_label, ""))
            if host is None:
                continue
            mount = sample.labels.get("mountpoint", "") if by_mount else ""
            # First wins: bind mounts repeat.
            out.setdefault((host, mount), Point(sample.value, _stamp(sample, now)))
        return out

    def _fetch(self, now: datetime) -> tuple[Resource, ...]:
        readings: list[Resource] = []
        size = self._series("disk_size", now, True)
        avail = self._series("disk_avail", now, True)
        for (host, mount), total in sorted(size.items()):
            if total.value > 0 and (host, mount) in avail:
                free = avail[(host, mount)]
                readings.append(
                    Resource(
                        f"{host}:{mount}",
                        ResourceKind.DISK,
                        host,
                        used=max(0.0, total.value - free.value) / GB,
                        limit=total.value / GB,
                        unit="GB",
                        at=min(total.at, free.at),
                    )
                )
        total_mem = self._series("memory_total", now)
        avail_mem = self._series("memory_available", now)
        for (host, _), total in sorted(total_mem.items()):
            if total.value > 0 and (host, "") in avail_mem:
                free = avail_mem[(host, "")]
                readings.append(
                    Resource(
                        f"{host}:memory",
                        ResourceKind.MEMORY,
                        host,
                        used=max(0.0, total.value - free.value) / GB,
                        limit=total.value / GB,
                        unit="GB",
                        at=min(total.at, free.at),
                    )
                )
        load, cores = self._series("load1", now), self._series("cores", now)
        for (host, _), point in sorted(load.items()):
            if cores.get((host, ""), Point(0, now)).value > 0:
                readings.append(
                    Resource(
                        f"{host}:load1",
                        ResourceKind.LOAD,
                        host,
                        used=point.value,
                        limit=cores[(host, "")].value,
                        unit="load",
                        at=min(point.at, cores[(host, "")].at),
                    )
                )
        for (host, _), point in sorted(self._series("cpu_busy", now).items()):
            readings.append(
                Resource(
                    f"{host}:cpu",
                    ResourceKind.CPU,
                    host,
                    used=min(100.0, max(0.0, point.value)),
                    limit=100.0,
                    unit="percent",
                    at=point.at,
                )
            )
        procs, procs_max = self._series("processes", now), self._series("processes_max", now)
        for (host, _), point in sorted(procs.items()):
            ceiling = procs_max.get((host, ""))
            readings.append(
                Resource(
                    f"{host}:processes",
                    ResourceKind.PROCESSES,
                    host,
                    used=point.value,
                    limit=ceiling.value if ceiling else None,
                    unit="processes",
                    at=point.at,
                )
            )
        return tuple(readings)


@dataclass(frozen=True, slots=True)
class Point:
    value: float
    at: datetime


def _stamp(sample: Sample, now: datetime) -> datetime:
    """When the sample was taken, never later than ``now``: Prometheus stamps an instant
    query with its own clock, and a clock ahead of ours must not make a reading look newer
    than it is. (A clock far behind makes readings look stale, and the rule ignores them.)"""
    if sample.timestamp is None:
        return now
    try:
        at = datetime.fromtimestamp(sample.timestamp, UTC)
    except (OverflowError, OSError, ValueError):
        return now
    return min(at, now)
