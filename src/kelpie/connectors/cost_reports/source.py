"""Daily cost reports from the providers' admin APIs: what the bill says each day.

Each configured provider is read at most once per ``interval`` (the first time on the
first tick), for its last ``days`` complete UTC days, on a background thread, so a tick
never waits for a provider; between reads the last reading is kept. All pages of one read
share :data:`READ_BUDGET`. Today is never asked for: its figure is not final. Paths go
under ``api_url``'s own path (a gateway prefix).

- ``anthropic``: ``GET /v1/organizations/cost_report`` with ``group_by[]=description``
  (and ``workspace_id`` with ``workspaces``), ``x-api-key`` and ``anthropic-version``.
  Amounts are decimal strings in cents. Only ``cost_type = "tokens"`` counts (web search,
  code execution and session costs are not token spend), per model.
- ``openai``: ``GET /v1/organization/costs`` (``project_ids`` with ``projects``), a bearer
  key. Amounts are USD. Line items do not name models reliably, so the figure is the
  provider's total for the day, all line items (images, audio, fine-tuning and storage
  included: scope it with ``projects`` to the farm's keys where it matters).

A read that fails (unreachable, timeout, 4xx/5xx, a redirect, an oversized or malformed
answer, a currency other than USD) keeps the last good reading and is retried after
:data:`RETRY_AFTER`; one warning is logged per provider and cause until it answers again.
Amounts may be negative (credits, adjustments). The key goes only to ``api_url``'s origin (:mod:`kelpie.net`: no
redirects, one deadline; an environment https proxy only as a CONNECT tunnel) and is never
logged nor part of any repr or error message.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from urllib.parse import urlencode, urlsplit

from kelpie import __version__, net
from kelpie.core.model import ReportedCost

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = timedelta(hours=1)
DEFAULT_TIMEOUT = 10.0
DEFAULT_DAYS = 7
MAX_DAYS = 31  # one page of daily buckets on both APIs
MAX_PAGES = 20  # pages per read (grouped results can spill over)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_USD = 1e9  # an item above this (either way) is a unit mix-up, not a reading
READ_BUDGET = timedelta(seconds=60)  # all pages of one provider's read
RETRY_AFTER = timedelta(minutes=5)  # after a failed read, instead of a whole interval
ANTHROPIC_VERSION = "2023-06-01"
USER_AGENT = f"kelpie/{__version__}"


class ReportKind(StrEnum):
    ANTHROPIC = "anthropic"
    OPENAI = "openai"


DEFAULT_API_URLS: Mapping[ReportKind, str] = {
    ReportKind.ANTHROPIC: "https://api.anthropic.com",
    ReportKind.OPENAI: "https://api.openai.com",
}


class HttpGet(Protocol):
    def __call__(
        self,
        url: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int = ...,
        read_body: bool = ...,
        proxy: bool = ...,
    ) -> net.Response: ...


@dataclass(frozen=True, slots=True)
class CostReportConfig:
    """One provider's cost report, ready to read. ``scopes``: workspace ids (anthropic) or
    project ids (openai) the figures are limited to; empty: the whole organization."""

    provider: str
    kind: ReportKind
    api_url: str
    admin_key: str = field(repr=False)
    interval: timedelta = DEFAULT_INTERVAL
    timeout: float = DEFAULT_TIMEOUT
    days: int = DEFAULT_DAYS
    scopes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ReportStatus:
    """The last read of one provider's report: ``ok`` and ``error`` (a short cause) of
    the last attempt, ``fetched_at`` of the last good one."""

    provider: str
    ok: bool | None = None  # None: not read yet
    error: str | None = None
    fetched_at: datetime | None = None
    days: int = 0  # days the last good reading covers


class ReportError(Exception):
    """A read that gave no reading; ``cause`` is short, the message safe to log."""

    def __init__(self, cause: str, detail: str) -> None:
        super().__init__(detail)
        self.cause = cause


@dataclass(slots=True)
class _State:
    costs: tuple[ReportedCost, ...] = ()
    status: ReportStatus | None = None
    attempted: float | None = None  # monotonic time of the last attempt
    warned: str | None = None  # the cause last logged
    busy: bool = False  # a read is running


class CostReportSource:
    """:class:`kelpie.core.ports.CostReportReader` over the configured providers.

    :meth:`read` returns the last good figures at once; reports that are due are read on
    one background thread (``background=False``: in the caller, for tests), so a slow
    provider never holds up a tick. A failed read is retried after :data:`RETRY_AFTER`
    (at most ``interval``)."""

    def __init__(
        self,
        reports: Sequence[CostReportConfig],
        http_get: HttpGet = net.get,
        monotonic: Callable[[], float] = time.monotonic,
        *,
        background: bool = True,
    ) -> None:
        self.reports = tuple(reports)
        self._get = http_get
        self._monotonic = monotonic
        self._background = background
        self._lock = threading.Lock()
        self._state = {r.provider: _State(status=ReportStatus(r.provider)) for r in self.reports}

    @property
    def providers(self) -> frozenset[str]:
        return frozenset(r.provider for r in self.reports)

    def read(self, now: datetime) -> tuple[ReportedCost, ...]:
        """Every provider's last good daily figures; starts reading those that are due.
        Never raises."""
        due = self._due()
        if due and self._background:
            worker = threading.Thread(
                target=self._read_all, args=(due, now), name="kelpie-cost-reports", daemon=True
            )
            worker.start()
        elif due:
            self._read_all(due, now)
        with self._lock:
            return tuple(c for r in self.reports for c in self._state[r.provider].costs)

    def statuses(self) -> tuple[ReportStatus, ...]:
        with self._lock:
            return tuple(
                self._state[r.provider].status or ReportStatus(r.provider) for r in self.reports
            )

    def _due(self) -> list[CostReportConfig]:
        clock = self._monotonic()
        due = []
        with self._lock:
            for report in self.reports:
                state = self._state[report.provider]
                wait = report.interval.total_seconds()
                if state.status is not None and state.status.ok is False:
                    wait = min(wait, RETRY_AFTER.total_seconds())
                if not state.busy and (state.attempted is None or clock - state.attempted >= wait):
                    state.attempted = clock
                    state.busy = True
                    due.append(report)
        return due

    def _read_all(self, reports: Sequence[CostReportConfig], now: datetime) -> None:
        for report in reports:
            try:
                self._read_one(report, now)
            finally:
                with self._lock:
                    self._state[report.provider].busy = False

    def _read_one(self, report: CostReportConfig, now: datetime) -> None:
        cause: str
        try:
            costs = fetch(report, now, self._get, self._monotonic)
        except ReportError as exc:
            cause, detail = exc.cause, str(exc)
        except Exception as exc:  # a bug must not stop the reads
            cause, detail = "error", f"unexpected {type(exc).__name__}"
        else:
            with self._lock:
                state = self._state[report.provider]
                if state.warned is not None:
                    log.info("cost report of %s read again", report.provider)
                state.warned = None
                state.costs = costs
                state.status = ReportStatus(report.provider, True, None, now, report.days)
            return
        with self._lock:
            state = self._state[report.provider]
            last = state.status or ReportStatus(report.provider)
            state.status = ReportStatus(report.provider, False, cause, last.fetched_at, last.days)
            first = state.warned != cause
            state.warned = cause
        if first:
            log.warning("cost report of %s not read: %s", report.provider, detail)


def window(now: datetime, days: int) -> tuple[datetime, datetime]:
    """The last ``days`` complete UTC days before ``now``: [start, end) at midnight UTC."""
    end = datetime.combine(now.astimezone(UTC).date(), datetime.min.time(), UTC)
    return end - timedelta(days=days), end


def fetch(
    report: CostReportConfig,
    now: datetime,
    http_get: HttpGet = net.get,
    monotonic: Callable[[], float] = time.monotonic,
) -> tuple[ReportedCost, ...]:
    """One provider's daily figures for its window, all pages within
    :data:`READ_BUDGET`; raises :class:`ReportError`."""
    start, end = window(now, report.days)
    pages = _Pages(report, http_get, monotonic, monotonic() + READ_BUDGET.total_seconds())
    if report.kind is ReportKind.ANTHROPIC:
        return _anthropic(report, start, end, pages)
    return _openai(report, start, end, pages)


@dataclass(frozen=True, slots=True)
class _Pages:
    report: CostReportConfig
    http_get: HttpGet
    monotonic: Callable[[], float]
    deadline: float  # monotonic

    def __call__(
        self, path: str, params: list[tuple[str, str]], headers: Mapping[str, str]
    ) -> Iterable[Mapping[str, Any]]:
        """Each page of ``path``; ``api_url``'s own path goes in front of it."""
        report = self.report
        prefix = urlsplit(report.api_url).path.rstrip("/")
        page: str | None = None
        for _ in range(MAX_PAGES):
            left = self.deadline - self.monotonic()
            if left <= 0:
                raise ReportError("timeout", f"no answer within {READ_BUDGET}")
            query = urlencode([*params, *([("page", page)] if page else [])])
            try:
                response = self.http_get(
                    report.api_url,
                    f"{prefix}{path}?{query}",
                    headers,
                    min(report.timeout, left),
                    max_bytes=MAX_RESPONSE_BYTES,
                    proxy=True,
                )
            except net.RequestError as exc:
                raise ReportError(exc.cause, exc.detail) from None
            if response.status in (401, 403):
                raise ReportError("auth", f"HTTP {response.status}: the admin key was refused")
            if response.status == 429:
                raise ReportError("rate_limited", "HTTP 429: too many requests")
            if not 200 <= response.status < 300:
                raise ReportError("http", f"HTTP {response.status}")
            try:
                body = json.loads(response.body)
            except (ValueError, RecursionError):
                raise ReportError("malformed", "the answer is not JSON") from None
            if not isinstance(body, Mapping) or not isinstance(body.get("data"), list):
                raise ReportError("malformed", "the answer has no data list")
            yield body
            if not body.get("has_more"):
                return
            page = body.get("next_page")
            if not isinstance(page, str) or not page:
                raise ReportError("malformed", "has_more without a next_page")
        raise ReportError("too_many_pages", f"more than {MAX_PAGES} pages")


def _anthropic(
    report: CostReportConfig, start: datetime, end: datetime, pages: _Pages
) -> tuple[ReportedCost, ...]:
    params = [
        ("starting_at", _rfc3339(start)),
        ("ending_at", _rfc3339(end)),
        ("bucket_width", "1d"),
        ("limit", str(MAX_DAYS)),
        ("group_by[]", "description"),
    ]
    if report.scopes:
        params.append(("group_by[]", "workspace_id"))
    headers = {
        "x-api-key": report.admin_key,
        "anthropic-version": ANTHROPIC_VERSION,
        "user-agent": USER_AGENT,
    }
    sums: dict[tuple[date, str | None], float] = {}
    for body in pages("/v1/organizations/cost_report", params, headers):
        for bucket in body["data"]:
            day = _bucket_day(bucket, "starting_at")
            sums.setdefault((day, None), 0.0)  # the day is covered, even with no spend
            for item in _results(bucket):
                if item.get("cost_type") != "tokens":
                    continue
                if report.scopes and item.get("workspace_id") not in report.scopes:
                    continue
                _usd_only(item.get("currency"))
                cents = _amount(item.get("amount"), MAX_USD * 100)
                model = item.get("model")
                key = (day, model if isinstance(model, str) and model else None)
                sums[key] = sums.get(key, 0.0) + cents / 100
    return _costs(report.provider, sums, start, end)


def _openai(
    report: CostReportConfig, start: datetime, end: datetime, pages: _Pages
) -> tuple[ReportedCost, ...]:
    params = [
        ("start_time", str(int(start.timestamp()))),
        ("end_time", str(int(end.timestamp()))),
        ("bucket_width", "1d"),
        ("limit", str(MAX_DAYS)),
        *(("project_ids", p) for p in report.scopes),
    ]
    headers = {"authorization": f"Bearer {report.admin_key}", "user-agent": USER_AGENT}
    sums: dict[tuple[date, str | None], float] = {}
    for body in pages("/v1/organization/costs", params, headers):
        for bucket in body["data"]:
            day = _bucket_day(bucket, "start_time")
            sums.setdefault((day, None), 0.0)  # the day is covered, even with no spend
            for item in _results(bucket):
                amount = item.get("amount")
                if not isinstance(amount, Mapping):
                    raise ReportError("malformed", "a result has no amount")
                _usd_only(amount.get("currency"))
                usd = _amount(amount.get("value"), MAX_USD)
                sums[(day, None)] = sums.get((day, None), 0.0) + usd
    return _costs(report.provider, sums, start, end)


def _results(bucket: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    results = bucket.get("results")
    if not isinstance(results, list) or not all(isinstance(r, Mapping) for r in results):
        raise ReportError("malformed", "a bucket has no results list")
    return results


def _bucket_day(bucket: Any, key: str) -> date:
    if not isinstance(bucket, Mapping):
        raise ReportError("malformed", "a bucket is not an object")
    value = bucket.get(key)
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, int | float):
            at = datetime.fromtimestamp(value, UTC)
        elif isinstance(value, str):
            at = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            raise ValueError
    except (ValueError, OverflowError, OSError):
        raise ReportError("malformed", f"a bucket has no valid {key}") from None
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return at.astimezone(UTC).date()


def _amount(value: Any, bound: float) -> float:
    """A finite number within ``bound`` either way (a decimal string or a number; a credit
    or an adjustment can be negative)."""
    try:
        if isinstance(value, bool):
            raise ValueError
        number = float(value) if isinstance(value, str | int | float) else math.nan
    except (ValueError, OverflowError):
        number = math.nan
    if not math.isfinite(number) or abs(number) > bound:
        raise ReportError("malformed", "an amount is not a valid number")
    return number


def _usd_only(currency: Any) -> None:
    if currency is not None and (not isinstance(currency, str) or currency.lower() != "usd"):
        raise ReportError("currency", f"amounts in {str(currency)[:8]!r}, not USD")


def _costs(
    provider: str, sums: Mapping[tuple[date, str | None], float], start: datetime, end: datetime
) -> tuple[ReportedCost, ...]:
    first, last = start.date(), end.date()
    return tuple(
        ReportedCost(provider, day, round(usd, 6), model)
        for (day, model), usd in sorted(sums.items(), key=lambda kv: (kv[0][0], kv[0][1] or ""))
        if first <= day < last
    )


def _rfc3339(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
