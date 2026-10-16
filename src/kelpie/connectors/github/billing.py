"""GitHub Actions minutes used this quota period, from GitHub's billing usage report.

Rule 9 (``ci_economy``) calls a CI minutes quota exhausted only from a reading of minutes
*used*, never from failed runs (a run stopped by billing looks like any run that failed at
once). This reader supplies that reading for the ``ci_minutes`` resource of
``[connectors.github.ci]``, whose quota (``included_minutes``) and reset day
(``quota_reset_day``) stay configuration: the report does not state them.

Source: ``GET /users/{user}/settings/billing/usage`` or
``GET /organizations/{org}/settings/billing/usage`` with ``year`` and ``month`` (GitHub's
enhanced billing platform; the older ``/settings/billing/actions`` endpoint is gone). Its
``usageItems`` have a ``date``, a ``product``, a ``sku`` (e.g. ``actions_linux``), a
``quantity`` and a ``unitType``. Minutes used are the sum of ``quantity`` over the items whose
product is Actions and whose unit is minutes, dated on or after the last quota reset; the
reset may fall in the previous calendar month, so up to two months are read. Quantities are
counted as reported: runner multipliers (Windows, macOS) are not applied, as for build
minutes elsewhere in the connector.

Token: billing usage needs a token that may read the account's billing, which the token
that reads PRs and runs should not have. It comes from its own variable
(``billing_token_env``), goes only to ``api_url`` through its own client, and is never logged.

Freshness: one read per ``billing_interval``. A reading is kept for at most two intervals;
after that, or until the first read succeeds, the resource carries no ``used`` and rule 9
falls back to projecting the burn against the quota. A failed read is logged once (by kind)
until a read succeeds again.
"""

from __future__ import annotations

import logging
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


def minutes_since(body: Any, start: date) -> tuple[float, int]:
    """Actions minutes in a usage report on or after ``start``, and the number of items
    that could not be read (no date, no number). Raises ValueError for a report that is
    not one."""
    if not isinstance(body, dict) or not isinstance(body.get("usageItems"), list):
        raise ValueError("not a billing usage report (no usageItems)")
    total, skipped = 0.0, 0
    for item in body["usageItems"]:
        if not isinstance(item, dict):
            skipped += 1
            continue
        product, unit = item.get("product"), item.get("unitType")
        if not isinstance(product, str) or product.strip().lower() != PRODUCT:
            continue
        if not isinstance(unit, str) or unit.strip().lower() != UNIT:
            continue
        day, quantity = _item_date(item.get("date")), item.get("quantity")
        if (
            day is None
            or isinstance(quantity, bool)
            or not isinstance(quantity, int | float)
            or quantity < 0
        ):
            skipped += 1
            continue
        if day >= start:
            total += float(quantity)
    return total, skipped


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
    ) -> None:
        kind = "organizations" if organization else "users"
        self.client = client
        self.account = account
        self.path = f"/{kind}/{account}/settings/billing/usage"
        self.interval = interval
        self.reset_day = reset_day
        self._tried_at: datetime | None = None
        self._reading: tuple[float, datetime] | None = None  # minutes used, read at
        self._failing: str | None = None  # the kind of the last failure, logged once

    def __repr__(self) -> str:
        return f"CiBilling({self.path!r}, every {self.interval})"

    def used(self, now: datetime) -> tuple[float, datetime] | None:
        """``(minutes used this period, when read)``, or None when no recent reading."""
        if self._tried_at is None or now - self._tried_at >= self.interval:
            self._tried_at = now
            self._read(now)
        if self._reading is None or now - self._reading[1] > KEEP_FOR * self.interval:
            return None
        return self._reading

    def _read(self, now: datetime) -> None:
        start = last_reset(now, self.reset_day)
        total, skipped = 0.0, 0
        fetched: datetime | None = None
        try:
            self.client.begin_tick(len(_months(start, now)), self.client.timeout * 2)
            for year, month in _months(start, now):
                response = self.client.get(self.path, {"year": str(year), "month": str(month)})
                if response.stale:
                    raise GitHubError("stale", "billing usage could not be re-read")
                minutes, bad = minutes_since(response.body, start.date())
                total, skipped = total + minutes, skipped + bad
                fetched = (
                    response.fetched_at if fetched is None else min(fetched, response.fetched_at)
                )
        except GitHubError as exc:
            self._failed(exc.kind, str(exc))
            return
        except ValueError as exc:
            self._failed("bad_response", str(exc))
            return
        if skipped:
            log.warning(
                "[connectors.github.ci] billing usage of %s: %d item(s) without a date or "
                "quantity were left out",
                self.account,
                skipped,
            )
        if self._failing is not None:
            log.info("[connectors.github.ci] billing usage of %s readable again", self.account)
        self._failing = None
        self._reading = (total, fetched or now)

    def _failed(self, kind: str, message: str) -> None:
        if self._failing == kind:
            return
        self._failing = kind
        hint = (
            ": the billing token cannot read this account's billing usage"
            if kind in {"forbidden", "unauthorized", "not_found"}
            else ""
        )
        log.warning(
            "[connectors.github.ci] billing usage of %s not read (%s)%s; CI minutes fall back "
            "to the burn projection",
            self.account,
            kind,
            hint,
        )
        log.debug("billing usage error: %s", message)
