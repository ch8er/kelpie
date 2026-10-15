"""Host resource readings from node_exporter metrics, via the Prometheus HTTP API.

One instant query (``GET /api/v1/query``) per metric. The queries are fixed text, never
built from data, and every result is matched to a host by a label value from the
configuration; series of hosts that are not configured are ignored. Disk, memory, load, CPU
and process counts become :class:`~kelpie.core.model.Resource` readings (``used``/``limit``
pairs, so a rule sees utilization) with ``at`` set to the time of the fetch.

The client is deliberately narrow. Redirects are never followed and proxies from the
environment are never used, so a bearer token goes only to the configured URL. Failure
messages and logs never include the token, the response body or the query string. Any
failure yields no readings for that fetch (a rule must see no data rather than stale data),
counts in ``kelpie_resource_read_failures_total`` and logs one warning per cause until a
fetch succeeds again. A failed fetch is not retried before the next ``interval``, so an
unreachable Prometheus costs at most one timeout per interval.
"""

from __future__ import annotations

import http.client
import json
import logging
import math
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from kelpie import __version__
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

_BAD_HEADER_VALUE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


def valid_token(token: str) -> bool:
    """A bearer token must fit in a header value without line breaks or control characters."""
    return bool(token) and _BAD_HEADER_VALUE.search(token) is None


class ReadObserver(Protocol):
    def resource_read(self, readings: int) -> None: ...

    def resource_failed(self, cause: str) -> None: ...


class QueryError(Exception):
    """A failed query. ``cause`` is a short category (a metric label); ``detail`` is safe
    to log: it never holds the token, the URL's query or the response body."""

    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(detail)
        self.cause = cause
        self.detail = detail


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None  # urllib then raises HTTPError with the 3xx code


# An explicit empty ProxyHandler: the token must not travel through a proxy.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


@dataclass(frozen=True, slots=True)
class Sample:
    labels: Mapping[str, str]
    value: float


@dataclass(frozen=True, slots=True)
class PrometheusClient:
    url: str  # base URL, e.g. http://prometheus:9090
    timeout: float = DEFAULT_TIMEOUT
    token: str | None = field(default=None, repr=False)

    def query(self, promql: str) -> list[Sample]:
        """Run one instant query; raises :class:`QueryError`."""
        base = self.url.rstrip("/")
        endpoint = f"{base}/api/v1/query?{urllib.parse.urlencode({'query': promql})}"
        request = urllib.request.Request(endpoint, method="GET")
        request.add_header("User-Agent", f"kelpie/{__version__}")
        request.add_header("Accept", "application/json")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        deadline = time.monotonic() + self.timeout
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                body = _read_limited(response, deadline)
        except urllib.error.HTTPError as exc:
            if 300 <= exc.code < 400:
                raise QueryError("redirect", f"HTTP {exc.code} redirect (not followed)") from None
            cause = "http_auth" if exc.code in (401, 403) else "http_error"
            raise QueryError(cause, f"HTTP {exc.code}") from None
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise QueryError("timeout", "timed out") from None
            name = type(exc.reason).__name__
            raise QueryError("connection", f"connection failed ({name})") from None
        except TimeoutError:
            raise QueryError("timeout", "timed out") from None
        except (OSError, http.client.HTTPException) as exc:
            raise QueryError("connection", f"connection failed ({type(exc).__name__})") from None
        return _parse(body)


def _read_limited(response: Any, deadline: float) -> bytes:
    chunks: list[bytes] = []
    size = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError
        chunk = response.read(64 * 1024)
        if not chunk:
            return b"".join(chunks)
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise QueryError("too_large", f"response over {MAX_RESPONSE_BYTES} bytes")
        chunks.append(chunk)


def _parse(body: bytes) -> list[Sample]:
    try:
        payload = json.loads(body)
    except ValueError:
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
    samples: list[Sample] = []
    for item in data["result"][:MAX_SERIES]:
        try:
            labels = item["metric"]
            value = float(item["value"][1])
        except (KeyError, IndexError, TypeError, ValueError):
            continue  # a malformed series is skipped, not fatal
        if not isinstance(labels, dict) or not math.isfinite(value):
            continue  # NaN and infinities carry no reading
        samples.append(Sample({str(k): str(v) for k, v in labels.items()}, value))
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
        except QueryError as exc:
            self._cached = ()
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

    def _series(self, name: str, by_mount: bool = False) -> dict[tuple[str, str], float]:
        """Values of one query per (Kelpie host, mount); unconfigured hosts are dropped."""
        out: dict[tuple[str, str], float] = {}
        samples = self.client.query(self.config.query_text(name))
        for sample in sorted(samples, key=lambda s: sorted(s.labels.items())):
            host = self._hosts.get(sample.labels.get(self.config.host_label, ""))
            if host is None:
                continue
            mount = sample.labels.get("mountpoint", "") if by_mount else ""
            out.setdefault((host, mount), sample.value)  # first wins: bind mounts repeat
        return out

    def _fetch(self, now: datetime) -> tuple[Resource, ...]:
        readings: list[Resource] = []
        size, avail = self._series("disk_size", True), self._series("disk_avail", True)
        for (host, mount), total in sorted(size.items()):
            if total > 0 and (host, mount) in avail:
                used = max(0.0, total - avail[(host, mount)])
                readings.append(
                    Resource(
                        f"{host}:{mount}",
                        ResourceKind.DISK,
                        host,
                        used=used / GB,
                        limit=total / GB,
                        unit="GB",
                        at=now,
                    )
                )
        total_mem, avail_mem = self._series("memory_total"), self._series("memory_available")
        for (host, _), total in sorted(total_mem.items()):
            if total > 0 and (host, "") in avail_mem:
                readings.append(
                    Resource(
                        f"{host}:memory",
                        ResourceKind.MEMORY,
                        host,
                        used=max(0.0, total - avail_mem[(host, "")]) / GB,
                        limit=total / GB,
                        unit="GB",
                        at=now,
                    )
                )
        load, cores = self._series("load1"), self._series("cores")
        for (host, _), value in sorted(load.items()):
            if cores.get((host, ""), 0) > 0:
                readings.append(
                    Resource(
                        f"{host}:load1",
                        ResourceKind.LOAD,
                        host,
                        used=value,
                        limit=cores[(host, "")],
                        unit="load",
                        at=now,
                    )
                )
        for (host, _), value in sorted(self._series("cpu_busy").items()):
            readings.append(
                Resource(
                    f"{host}:cpu",
                    ResourceKind.CPU,
                    host,
                    used=min(100.0, max(0.0, value)),
                    limit=100.0,
                    unit="percent",
                    at=now,
                )
            )
        procs, procs_max = self._series("processes"), self._series("processes_max")
        for (host, _), value in sorted(procs.items()):
            readings.append(
                Resource(
                    f"{host}:processes",
                    ResourceKind.PROCESSES,
                    host,
                    used=value,
                    limit=procs_max.get((host, "")),
                    unit="processes",
                    at=now,
                )
            )
        return tuple(readings)
