"""GitHub Actions minutes used this quota period, from GitHub's billing usage report.

Rule 9 (``ci_economy``) calls a CI minutes quota exhausted only from a reading of minutes
*used*, never from failed runs (a run stopped by billing looks like any run that failed at
once). This reader supplies that reading for the ``ci_minutes`` resource of
``[connectors.github.ci]``, whose quota (``included_minutes``) and reset day
(``quota_reset_day``) stay configuration: the report does not state them.

Source: ``GET /users/{user}/settings/billing/usage`` or
``GET /organizations/{org}/settings/billing/usage`` with ``year`` and ``month`` (GitHub's
enhanced billing platform; the older ``/settings/billing/actions`` endpoint is gone; a 400
usually means the account is not on that platform). Its ``usageItems`` have a ``date``, a
``product``, a ``sku`` (e.g. ``actions_linux``), a ``quantity`` and a ``unitType``.

What counts: items of product Actions in minutes whose SKU is one of ``skus`` (default: the
standard GitHub-hosted runners, the ones included minutes pay for; larger runners are billed
per minute and never draw on them), dated on or after the last quota reset. The reset may
fall in the previous calendar month, so up to two months are read. Quantities are counted as
reported: runner multipliers (Windows, macOS) are not applied, as for build minutes
elsewhere in the connector, so the reading errs low, never high. Minutes of public
repositories are free on GitHub but may appear in the report: an account that runs Actions
on public repositories as well should not rely on this reading. A quantity that is not a
finite, plausible number makes the whole read fail (no reading rather than a wrong one).
A report larger than the client's body cap (4 MiB) fails the read too; very large
organizations may hit it.

Token: billing usage needs a token that may read the account's billing, which the token
that reads PRs and runs should not have. It comes from its own variable
(``billing_token_env``), goes only to ``api_url`` through its own client (whose metrics are
not mixed with the main client's), and is never logged.

Freshness: one read per ``billing_interval``, within the CI step's tick budget. A reading is
kept for at most two intervals and only within the quota period it was read in; after that,
or until the first read succeeds, the resource carries no ``used`` and rule 9 falls back to
projecting the burn against the quota. A failed read is logged once (by kind) until a read
succeeds again.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Collection
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from kelpie.connectors.github.client import GitHubClient, GitHubError

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = timedelta(minutes=15)
MIN_INTERVAL = timedelta(minutes=5)
MAX_INTERVAL = timedelta(hours=1)
KEEP_FOR = 2  # intervals a reading stays usable without a newer one
PRODUCT = "actions"
UNIT = "minutes"
# Standard GitHub-hosted runners: what included minutes are spent on.
DEFAULT_SKUS = ("actions_linux", "actions_windows", "actions_macos")
MAX_QUANTITY = 1_000_000.0  # minutes in one item; more is not a plausible day of one SKU
_HINTS = {
    "forbidden": "the billing token cannot read this account's billing usage",
    "unauthorized": "the billing token was refused",
    "not_found": "no such account, or the token cannot see it",
    "http_error": "is the account on GitHub's enhanced billing platform?",
}


class ReportError(ValueError):
    """The body is not a usable billing usage report."""


def last_reset(now: datetime, day: int) -> datetime:
    """The latest ``day`` of a month at 00:00 UTC at or before ``now``."""
    now = now.astimezone(UTC)
    candidate = now.replace(day=day, hour=0, minute=0, second=0, microsecond=0)
    if candidate <= now:
        return candidate
    year, month = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    return candidate.replace(year=year, month=month)


def _months(start: datetime, now: datetime) -> list[tuple[int, int]]:
    months = [(start.year, start.month)]
    if (now.year, now.month) != months[0]:
        months.append((now.year, now.month))
    return months


def _item_date(raw: Any) -> date | None:
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def minutes_since(
    body: Any, start: date, skus: Collection[str] = DEFAULT_SKUS
) -> tuple[float, int]:
    """Minutes of the counted Actions SKUs in a usage report on or after ``start``, and the
    number of items left out because they had no date. Raises :class:`ReportError` for a
    body that is not a report or a counted item whose quantity is not a plausible number."""
    if not isinstance(body, dict) or not isinstance(body.get("usageItems"), list):
        raise ReportError("not a billing usage report (no usageItems)")
    wanted = {s.lower() for s in skus}
    total, skipped = 0.0, 0
    for item in body["usageItems"]:
        if not isinstance(item, dict):
            continue
        product, unit, sku = item.get("product"), item.get("unitType"), item.get("sku")
        if not isinstance(product, str) or product.strip().lower() != PRODUCT:
            continue
        if not isinstance(unit, str) or unit.strip().lower() != UNIT:
            continue
        if not isinstance(sku, str) or sku.strip().lower() not in wanted:
            continue
        quantity = item.get("quantity")
        if (
            isinstance(quantity, bool)
            or not isinstance(quantity, int | float)
            # JSON may carry NaN, Infinity and huge integers: a range check refuses them all
            # (NaN compares false), and comes first because isfinite() overflows on ints.
            or not 0 <= quantity <= MAX_QUANTITY
            or not math.isfinite(quantity)
        ):
            raise ReportError("a usage item has no plausible quantity")
        day = _item_date(item.get("date"))
        if day is None:
            skipped += 1
            continue
        if day >= start:
            total += float(quantity)
    return total, skipped


@dataclass(frozen=True, slots=True)
class _Reading:
    minutes: float
    at: datetime  # when GitHub answered
    period: datetime  # the quota period it belongs to (its last reset)


class CiBilling:
    """Reads the Actions minutes an account used since the last quota reset."""

    def __init__(
        self,
        client: GitHubClient,
        account: str,
        *,
        organization: bool = False,
        interval: timedelta = DEFAULT_INTERVAL,
        reset_day: int = 1,
        skus: Collection[str] = DEFAULT_SKUS,
        budget: timedelta = timedelta(seconds=10),
    ) -> None:
        kind = "organizations" if organization else "users"
        self.client = client
        self.account = account
        self.path = f"/{kind}/{account}/settings/billing/usage"
        self.interval = interval
        self.reset_day = reset_day
        self.skus = tuple(skus)
        self.budget = budget
        self._tried_at: datetime | None = None
        self._tried_period: datetime | None = None
        self._reading: _Reading | None = None
        self._failing: str | None = None  # the kind of the last failure, logged once

    def __repr__(self) -> str:
        return f"CiBilling({self.path!r}, every {self.interval})"

    def used(self, now: datetime) -> tuple[float, datetime] | None:
        """``(minutes used this period, when read)``, or None when no recent reading."""
        period = last_reset(now, self.reset_day)
        due = self._tried_at is None or now - self._tried_at >= self.interval
        if due or self._tried_period != period:  # a new period is read at once
            self._tried_at, self._tried_period = now, period
            self._read(now, period)
        r = self._reading
        if r is None or r.period != period or now - r.at > KEEP_FOR * self.interval:
            return None
        return r.minutes, r.at

    def _read(self, now: datetime, period: datetime) -> None:
        total, skipped = 0.0, 0
        fetched: datetime | None = None
        months = _months(period, now)
        try:
            self.client.begin_tick(len(months), self.budget)
            for year, month in months:
                response = self.client.get(self.path, {"year": str(year), "month": str(month)})
                if response.stale:
                    raise GitHubError("stale", "billing usage could not be re-read")
                minutes, bad = minutes_since(response.body, period.date(), self.skus)
                total, skipped = total + minutes, skipped + bad
                at = response.fetched_at
                fetched = at if fetched is None else min(fetched, at)
        except GitHubError as exc:
            self._failed(exc.kind, str(exc))
            return
        except (ReportError, ArithmeticError) as exc:
            self._failed("bad_response", str(exc))
            return
        if not math.isfinite(total):
            self._failed("bad_response", "the minutes do not add up to a number")
            return
        if skipped:
            log.warning(
                "[connectors.github.ci] billing usage of %s: %d item(s) without a date were "
                "left out",
                self.account,
                skipped,
            )
        if self._failing is not None:
            log.info("[connectors.github.ci] billing usage of %s readable again", self.account)
        self._failing = None
        self._reading = _Reading(total, fetched or now, period)

    def _failed(self, kind: str, message: str) -> None:
        if self._failing == kind:
            return
        self._failing = kind
        hint = f": {_HINTS[kind]}" if kind in _HINTS else ""
        log.warning(
            "[connectors.github.ci] billing usage of %s not read (%s)%s; CI minutes fall back "
            "to the burn projection",
            self.account,
            kind,
            hint,
        )
        log.debug("billing usage error: %s", message)
