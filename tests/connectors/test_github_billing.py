"""Minutes used from GitHub's billing usage report, for rule 9's quota (issue #43)."""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from kelpie.connectors.github.billing import CiBilling, last_reset, minutes_since
from kelpie.connectors.github.builds import CiSettings, GitHubCI, load_ci
from kelpie.connectors.github.client import GitHubClient
from kelpie.connectors.github.settings import load_github
from kelpie.core.model import ResourceKind, Snapshot
from tests.connectors.fake_github import TOKEN, Clock, serve

BILLING = "ghp_SyntheticBillingToken0123456789"
NOW = datetime(2026, 3, 20, 12, tzinfo=UTC)
REPO = "example/repo"


def usage_path(year: int, month: int, account: str = "farm-owner", org: bool = False) -> str:
    kind = "organizations" if org else "users"
    return f"/{kind}/{account}/settings/billing/usage?month={month}&year={year}"


def item(day: str, quantity: float, product: str = "Actions", unit: str = "minutes") -> dict:
    return {
        "date": f"{day}T00:00:00Z",
        "product": product,
        "sku": "actions_linux",
        "quantity": quantity,
        "unitType": unit,
        "pricePerUnit": 0.008,
        "grossAmount": 0.0,
        "discountAmount": 0.0,
        "netAmount": 0.0,
    }


# --- the report ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("now", "day", "expected"),
    [
        (NOW, 1, datetime(2026, 3, 1, tzinfo=UTC)),
        (NOW, 15, datetime(2026, 3, 15, tzinfo=UTC)),
        (NOW, 25, datetime(2026, 2, 25, tzinfo=UTC)),
        (datetime(2026, 1, 3, tzinfo=UTC), 10, datetime(2025, 12, 10, tzinfo=UTC)),
        (datetime(2026, 3, 15, tzinfo=UTC), 15, datetime(2026, 3, 15, tzinfo=UTC)),
    ],
)
def test_last_reset(now: datetime, day: int, expected: datetime) -> None:
    assert last_reset(now, day) == expected


def test_only_actions_minutes_since_the_reset_count() -> None:
    body = {
        "usageItems": [
            item("2026-03-01", 100),
            item("2026-03-10", 250.5),
            item("2026-02-28", 999),  # before the reset
            item("2026-03-05", 7, product="Packages"),
            item("2026-03-05", 7, unit="GigabyteHours"),
            item("2026-03-05", 7, product="actions"),  # case does not matter
            {"date": "2026-03-05", "product": "Actions", "unitType": "minutes"},  # no quantity
            "junk",
        ]
    }
    assert minutes_since(body, date(2026, 3, 1)) == (357.5, 2)


@pytest.mark.parametrize("body", [{}, [], {"usageItems": "x"}, None])
def test_a_body_that_is_not_a_report_is_refused(body: Any) -> None:
    with pytest.raises(ValueError, match="usageItems"):
        minutes_since(body, date(2026, 3, 1))


# --- reading it ----------------------------------------------------------------------------


def reader(url: str, clock: Clock, **kw: Any) -> CiBilling:
    client = GitHubClient(url, BILLING, clock=clock)
    return CiBilling(client, "farm-owner", interval=timedelta(minutes=15), **kw)


def test_reads_once_per_interval_and_keeps_the_reading_two_intervals() -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(usage_path(2026, 3), {"usageItems": [item("2026-03-02", 1200)]})
        billing = reader(gh.url, clock)
        assert billing.used(NOW) == (1200.0, NOW)
        assert billing.used(NOW + timedelta(minutes=10)) == (1200.0, NOW)  # cached
        assert len(gh.requests) == 1
        assert gh.requests[0].headers["Authorization"] == f"Bearer {BILLING}"
        gh.reply(usage_path(2026, 3), {"message": "Forbidden"}, status=403)
        later = NOW + timedelta(minutes=16)
        clock.now = later
        assert billing.used(later) == (1200.0, NOW)  # the last reading is still recent
        clock.now = NOW + timedelta(minutes=31)
        assert billing.used(clock.now) is None  # older than two intervals: no reading


def test_a_period_that_began_last_month_reads_both_months() -> None:
    now = datetime(2026, 3, 3, tzinfo=UTC)
    with serve() as gh:
        gh.reply(
            usage_path(2026, 2),
            {"usageItems": [item("2026-02-10", 50), item("2026-02-20", 300)]},
        )
        gh.reply(usage_path(2026, 3), {"usageItems": [item("2026-03-01", 10)]})
        billing = reader(gh.url, Clock(now), reset_day=15)
        assert billing.used(now) == (310.0, now)


def test_an_organization_is_read_at_its_own_path() -> None:
    with serve() as gh:
        gh.reply(usage_path(2026, 3, "farm-org", org=True), {"usageItems": [item("2026-03-02", 5)]})
        client = GitHubClient(gh.url, BILLING, clock=Clock(NOW))
        billing = CiBilling(client, "farm-org", organization=True)
        assert billing.used(NOW) == (5.0, NOW)


def test_a_refused_read_is_logged_once_with_what_to_fix(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock(NOW)
    with serve() as gh, caplog.at_level(logging.WARNING):
        gh.reply(usage_path(2026, 3), {"message": "Forbidden"}, status=403)
        billing = reader(gh.url, clock)
        for minutes in (0, 16, 32):
            clock.now = NOW + timedelta(minutes=minutes)
            assert billing.used(clock.now) is None
    warnings = [r.getMessage() for r in caplog.records if "billing usage" in r.getMessage()]
    assert len(warnings) == 1
    assert "cannot read this account's billing usage" in warnings[0]
    assert BILLING not in caplog.text


def test_ci_minutes_resource_carries_the_reading() -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(f"/repos/{REPO}", {"default_branch": "main"})
        gh.reply(
            f"/repos/{REPO}/actions/runs?created=%3E%3D2026-03-19&page=1&per_page=100",
            {"total_count": 0, "workflow_runs": []},
        )
        gh.reply(usage_path(2026, 3), {"usageItems": [item("2026-03-02", 2999)]})
        client = GitHubClient(gh.url, TOKEN, clock=clock)
        settings = CiSettings(repos=(REPO,), included_minutes=3000)
        source = GitHubCI(client, settings, billing=reader(gh.url, clock))
        (quota,) = source(Snapshot(now=NOW)).resources
    assert quota.kind is ResourceKind.CI_MINUTES
    assert (quota.used, quota.limit, quota.at) == (2999.0, 3000.0, NOW)


# --- config and token ----------------------------------------------------------------------


BASE = {"repos": [REPO], "included_minutes": 3000}


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"billing_user": "farm-owner"}, "needs billing_token_env"),
        ({"billing_user": "a", "billing_org": "b", "billing_token_env": "T"}, "not both"),
        ({"billing_token_env": "T"}, "needs billing_user or billing_org"),
        ({"billing_user": "-bad-", "billing_token_env": "T"}, "user or organization name"),
        ({"billing_user": "a", "billing_token_env": "1BAD"}, "needs billing_token_env"),
        ({"billing_user": "a", "billing_token_env": "T", "billing_interval": "1m"}, "5m to 1h"),
    ],
)
def test_billing_config_errors(extra: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_ci({**BASE, **extra}, [REPO])


def test_billing_needs_the_quota_it_counts_against() -> None:
    with pytest.raises(ValueError, match="needs included_minutes"):
        load_ci({"repos": [REPO], "billing_user": "a", "billing_token_env": "T"}, [REPO])


def test_billing_token_is_required_and_never_quoted() -> None:
    ci = load_ci({**BASE, "billing_user": "farm-owner", "billing_token_env": "BILL"}, [REPO])
    assert (ci.billing_account, ci.billing_organization) == ("farm-owner", False)
    assert ci.billing_interval == timedelta(minutes=15)
    with pytest.raises(ValueError, match=r"\$BILL is not set"):
        ci.billing_token({})
    with pytest.raises(ValueError, match="not allowed in an HTTP header") as bad:
        ci.billing_token({"BILL": "secret\nvalue"})
    assert "secret" not in str(bad.value)
    assert ci.billing_token({"BILL": BILLING}) == BILLING
    off = load_ci(BASE, [REPO])
    assert off.billing_token({}) is None


def test_the_billing_token_goes_to_the_billing_report_only() -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(f"/repos/{REPO}", {"default_branch": "main"})
        gh.reply(
            f"/repos/{REPO}/actions/runs?created=%3E%3D2026-03-19&page=1&per_page=100",
            {"total_count": 0, "workflow_runs": []},
        )
        gh.reply(usage_path(2026, 3), {"usageItems": [item("2026-03-02", 10)]})
        github = load_github(
            {
                "repos": [REPO],
                "api_url": gh.url,
                "token_env": "MAIN",
                "ci": {**BASE, "billing_user": "farm-owner", "billing_token_env": "BILL"},
            }
        )
        environ = {"MAIN": TOKEN, "BILL": BILLING}
        connector = github.build(github.token(environ), clock)
        source = github.build_ci(connector, clock, environ)
        assert source is not None and source.billing is not None
        source(Snapshot(now=NOW))
    by_path = {r.path.split("?")[0]: r.headers["Authorization"] for r in gh.requests}
    assert by_path["/users/farm-owner/settings/billing/usage"] == f"Bearer {BILLING}"
    assert all(v == f"Bearer {TOKEN}" for k, v in by_path.items() if "/billing/" not in k)


def test_status_names_the_account_and_the_variable_never_the_token(tmp_path: Any) -> None:
    from kelpie.app import billing_interval_warnings, load_rules, load_settings, render_status

    config = tmp_path / "kelpie.toml"
    config.write_text(
        '[engine.rules.ci_economy]\nmax_reading_age = "10m"\n'
        '[connectors.github]\nrepos = ["example/repo"]\n'
        '[connectors.github.ci]\nincluded_minutes = 3000\nbilling_user = "farm-owner"\n'
        'billing_token_env = "BILL"\nbilling_interval = "30m"\n'
    )
    settings = load_settings(config)
    loaded = load_rules(settings.engine)
    status = render_status(settings, loaded)
    assert "billing usage of user farm-owner, every 0:30:00 (token from $BILL)" in status
    (warning,) = billing_interval_warnings(loaded.rules, settings)
    assert "billing_interval (0:30:00) is longer than rule 'ci_economy' max_reading_age" in warning
