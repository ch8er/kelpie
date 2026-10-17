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

# Filesystems that are not storage the agents can fill, nor macOS's own APFS volumes (VM,
# Preboot, Update...: the data volume is /System/Volumes/Data).
_FS = (
    'fstype!~"tmpfs|ramfs|overlay|squashfs|devtmpfs|nsfs|proc|sysfs|cgroup.*|fuse.*|autofs",'
    'mountpoint!~"/System/Volumes/(?:VM|Preboot|Update|xarts|iSCPreboot|Hardware|Recovery)(?:/.*)?"'
)
# The process script's file, written in the last 5 minutes (Prometheus's clock against the
# host's: keep them in sync).
_PROCESSES_FRESH = (
    '(time() - node_textfile_mtime_seconds{file=~"(.*/)?kelpie_processes[.]prom"} < 300)'
)
# On macOS, read-only filesystems cannot fill: the sealed system volume (/), mounted
# simulator runtimes and disk images (100 % full by design). Not on Linux, where a disk
# remounted read-only after errors must stay in sight.
_WRITABLE = (
    f"unless (node_filesystem_readonly{{{_FS}}} == 1"
    ' and on({host_label}) node_uname_info{sysname="Darwin"})'
)

# ``{host_label}`` is replaced by the configured host label, ``{window}`` and
# ``{half_window}`` by ``growth_window`` and its half, in custom queries too. Every result
# must keep the host label.
DEFAULT_QUERIES: Mapping[str, str] = {
    "disk_size": f"node_filesystem_size_bytes{{{_FS}}} {_WRITABLE}",
    "disk_avail": f"node_filesystem_avail_bytes{{{_FS}}} {_WRITABLE}",
    # Linux, else macOS (no MemAvailable there: free + inactive is what can be had).
    "memory_total": "node_memory_MemTotal_bytes or on({host_label}) node_memory_total_bytes",
    "memory_available": (
        "node_memory_MemAvailable_bytes"
        " or on({host_label}) (node_memory_free_bytes + node_memory_inactive_bytes)"
    ),
    "load1": "node_load1",
    "cores": 'count by ({host_label}) (node_cpu_seconds_total{mode="idle"})',
    "cpu_busy": (
        '100 * (1 - avg by ({host_label}) (rate(node_cpu_seconds_total{mode="idle"}[5m])))'
    ),
    # Linux: node_exporter's --collector.processes. macOS (no such collector): the agent
    # user's processes against kern.maxprocperuid, from scripts/kelpie-process-limit.sh.
    # The script's numbers only while its file is fresh: a stopped script must not freeze
    # the count.
    "processes": (
        "node_processes_pids or on({host_label}) max by ({host_label}) (kelpie_user_processes"
        f" and on({{host_label}}) {_PROCESSES_FRESH})"
    ),
    "processes_max": (
        "node_processes_max_processes or on({host_label}) min by ({host_label})"
        f" (kelpie_user_processes_max and on({{host_label}}) {_PROCESSES_FRESH})"
    ),
    # The host's OS (node_exporter's uname collector): load is off on macOS by default.
    "os": "node_uname_info",
    # How fast used space grows, in bytes per day, over ``growth_window`` (incident 19);
    # only for series that existed half a window ago, so a fresh Prometheus or host does not
    # forecast from a few minutes (one build's 5 GB in 3 minutes is no daily rate).
    "disk_growth": (
        f"-deriv(node_filesystem_avail_bytes{{{_FS}}}[{{window}}]) * 86400"
        f" and node_filesystem_avail_bytes{{{_FS}}} offset {{half_window}}"
    ),
    # Directory sizes from scripts/kelpie-path-sizes.sh (node_exporter's textfile collector),
    # and when that file was last written: sizes from a script that stopped are old.
    "path_bytes": "kelpie_path_bytes",
    "path_growth": (
        "deriv(kelpie_path_bytes[{window}]) * 86400 and kelpie_path_bytes offset {half_window}"
    ),
    "path_written": 'node_textfile_mtime_seconds{file=~"(.*/)?kelpie_paths[.]prom"}',
}
DEFAULT_GROWTH_WINDOW = timedelta(hours=6)
QUERY_NAMES = frozenset(DEFAULT_QUERIES)

valid_token = net.valid_header_value


class ReadObserver(Protocol):
    def resource_read(self, readings: int) -> None: ...

    def resource_failed(self, cause: str) -> None: ...

    def resource_query_failed(self, cause: str) -> None: ...


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
    growth_window: timedelta = DEFAULT_GROWTH_WINDOW
    # macOS counts threads waiting on I/O in its load average: 300-800 on a healthy Mac.
    load_on_macos: bool = False

    def query_text(self, name: str) -> str:
        """The query, its placeholders filled in (in custom queries too): ``{host_label}``,
        ``{window}`` (``growth_window``) and ``{half_window}``."""
        text = self.queries.get(name, DEFAULT_QUERIES[name])
        seconds = int(self.growth_window.total_seconds())
        return (
            text.replace("{host_label}", self.host_label)
            .replace("{half_window}", f"{seconds // 2}s")
            .replace("{window}", f"{seconds}s")
        )


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
        self._optional_warned: set[str] = set()
        self._macs: set[str] = set()  # hosts seen running macOS: an OS does not change
        self._os_known = False  # the os query answered at least once

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

    def _optional(
        self, name: str, now: datetime, by_mount: bool = False, label: str = "mountpoint"
    ) -> dict[tuple[str, str], Point]:
        """A query whose metric may not exist (growth needs history, directory sizes need
        the shipped script): an empty answer is fine, and so is a failure of only this
        query, logged once, so the readings that need no history still arrive."""
        try:
            found = self._series(name, now, by_mount, label)
        except QueryError as exc:
            if name not in self._optional_warned:  # once per process
                self._optional_warned.add(name)
                log.warning(
                    "Prometheus query %s failed (%s); readings come without it", name, exc.cause
                )
            if self.observer is not None:  # counted each time: a forecast quietly off shows
                self.observer.resource_query_failed(f"{name}_{exc.cause}")
            return {}
        return {k: p for k, p in found.items() if math.isfinite(p.value)}

    def _series(
        self, name: str, now: datetime, by_mount: bool = False, label: str = "mountpoint"
    ) -> dict[tuple[str, str], Point]:
        """Values of one query per (Kelpie host, mount or ``label``); unconfigured hosts are
        dropped."""
        out: dict[tuple[str, str], Point] = {}
        samples = self.client.query(self.config.query_text(name))
        for sample in sorted(samples, key=lambda s: sorted(s.labels.items())):
            host = self._hosts.get(sample.labels.get(self.config.host_label, ""))
            if host is None:
                continue
            mount = sample.labels.get(label, "") if by_mount else ""
            # First wins: bind mounts repeat.
            out.setdefault((host, mount), Point(sample.value, _stamp(sample, now), sample.labels))
        return out

    def _fetch(self, now: datetime) -> tuple[Resource, ...]:
        readings: list[Resource] = []
        size = self._series("disk_size", now, True)
        avail = self._series("disk_avail", now, True)
        growth = self._optional("disk_growth", now, True)
        for (host, mount), total in sorted(size.items()):
            if total.value > 0 and (host, mount) in avail:
                free = avail[(host, mount)]
                rate = growth.get((host, mount))
                readings.append(
                    Resource(
                        f"{host}:{mount}",
                        ResourceKind.DISK,
                        host,
                        used=max(0.0, total.value - free.value) / GB,
                        limit=total.value / GB,
                        unit="GB",
                        at=min(total.at, free.at),
                        growth_per_day=rate.value / GB if rate is not None else None,
                        mount=mount,
                    )
                )
        sizes = self._optional("path_bytes", now, True, "path")
        grows = self._optional("path_growth", now, True, "path")
        written = self._optional("path_written", now)
        for (host, path), point in sorted(sizes.items()):
            if not path:
                continue
            rate = grows.get((host, path))
            at = point.at
            if (mtime := written.get((host, ""))) is not None:
                at = min(at, _when(mtime.value, now))  # the sizes are as old as the file
            readings.append(
                Resource(
                    f"{host}:path:{path}",
                    ResourceKind.PATH,
                    host,
                    used=max(0.0, point.value) / GB,
                    unit="GB",
                    at=at,
                    growth_per_day=rate.value / GB if rate is not None else None,
                    mount=point.labels.get("mountpoint") or None,
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
        if not self.config.load_on_macos:
            # Remembered: one failed os query must not bring every Mac's load (300-800) back.
            # Until one answer came, a failure drops every host's load for that fetch.
            try:
                os_names = self._series("os", now, True, "sysname")
                self._os_known = True
            except QueryError as exc:
                os_names = {}
                if not self._os_known:
                    load = {}
                    log.debug("os query failed (%s): no load readings until it answers", exc.cause)
            self._macs |= {host for host, name in os_names if name == "Darwin"}
        for (host, _), point in sorted(load.items()):
            mac = host in self._macs and not self.config.load_on_macos
            if not mac and cores.get((host, ""), Point(0, now)).value > 0:
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
    labels: Mapping[str, str] = field(default_factory=dict)


def _when(seconds: float, now: datetime) -> datetime:
    """A Unix time as a datetime, never later than ``now``."""
    try:
        return min(datetime.fromtimestamp(seconds, UTC), now)
    except (OverflowError, OSError, ValueError):
        return now


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
