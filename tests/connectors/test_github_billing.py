"""Minutes used from GitHub's billing usage report, for rule 9's quota (issue #43)."""

from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from kelpie.app import billing_interval_warnings, load_rules, load_settings, render_status
from kelpie.connectors.github.billing import (
    CiBilling,
    ReportError,
    last_reset,
    minutes_since,
)
from kelpie.connectors.github.builds import CiSettings, GitHubCI, load_ci
from kelpie.connectors.github.client import GitHubClient
from kelpie.connectors.github.settings import load_github
from kelpie.core.model import ResourceKind, Snapshot
from tests.connectors.fake_github import TOKEN, Clock, serve

BILLING = "ghp_SyntheticBillingToken0123456789"
NOW = datetime(2026, 3, 20, 12, tzinfo=UTC)
REPO = "example/repo"
SECRET = "ghp_LeakedLookingValue0123456789abcdef"


def usage_path(year: int, month: int, account: str = "farm-owner", org: bool = False) -> str:
    kind = "organizations" if org else "users"
    return f"/{kind}/{account}/settings/billing/usage?month={month}&year={year}"


def item(day: str, quantity: Any, **fields: Any) -> dict[str, Any]:
    base = {
        "date": f"{day}T00:00:00Z",
        "product": "Actions",
        "sku": "actions_linux",
        "quantity": quantity,
        "unitType": "minutes",
        "pricePerUnit": 0.008,
        "grossAmount": 0.0,
        "discountAmount": 0.0,
        "netAmount": 0.0,
    }
    base.update(fields)
    return base


def report(*items: Any) -> dict[str, Any]:
    return {"usageItems": list(items)}


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


def test_only_standard_runner_minutes_since_the_reset_count() -> None:
    body = report(
        item("2026-03-01", 100),
        item("2026-03-10", 250.5, sku="actions_macos"),
        item("2026-02-28", 999),  # before the reset
        item("2026-03-05", 7, product="Packages"),
        item("2026-03-05", 7, unitType="GigabyteHours"),
        item("2026-03-05", 7, product="actions", sku="ACTIONS_WINDOWS"),  # any case
        item("2026-03-05", 500, sku="actions_linux_16_core"),  # a larger runner: not included
        {"product": "Actions", "sku": "actions_linux", "unitType": "minutes", "quantity": 3},
        "junk",
    )
    assert minutes_since(body, date(2026, 3, 1)) == (357.5, 1)  # one item without a date
    assert minutes_since(body, date(2026, 3, 1), ["actions_linux_16_core"]) == (500.0, 0)


@pytest.mark.parametrize("body", [{}, [], {"usageItems": "x"}, None])
def test_a_body_that_is_not_a_report_is_refused(body: Any) -> None:
    with pytest.raises(ReportError, match="usageItems"):
        minutes_since(body, date(2026, 3, 1))


@pytest.mark.parametrize(
    "quantity", [float("inf"), float("nan"), -1, 10**400, 2_000_000, "12", True, None]
)
def test_an_implausible_quantity_fails_the_whole_report(quantity: Any) -> None:
    body = report(item("2026-03-02", 1), item("2026-03-03", quantity))
    with pytest.raises(ReportError, match="plausible"):
        minutes_since(body, date(2026, 3, 1))


# --- reading it ----------------------------------------------------------------------------


def reader(url: str, clock: Clock, **kw: Any) -> CiBilling:
    client = GitHubClient(url, BILLING, clock=clock)
    return CiBilling(client, "farm-owner", interval=timedelta(minutes=15), **kw)


def test_reads_once_per_interval_and_keeps_the_reading_two_intervals() -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(usage_path(2026, 3), report(item("2026-03-02", 1200)))
        billing = reader(gh.url, clock)
        assert billing.used(NOW) == (1200.0, NOW)
        assert billing.used(NOW + timedelta(minutes=10)) == (1200.0, NOW)  # cached
        assert len(gh.requests) == 1
        assert gh.requests[0].headers["Authorization"] == f"Bearer {BILLING}"
        gh.reply(usage_path(2026, 3), {"message": "Forbidden"}, status=403)
        clock.now = NOW + timedelta(minutes=16)
        assert billing.used(clock.now) == (1200.0, NOW)  # the last reading is still recent
        clock.now = NOW + timedelta(minutes=31)
        assert billing.used(clock.now) is None  # older than two intervals: no reading


def test_a_reading_never_outlives_its_quota_period() -> None:
    """At the reset the old period's minutes must not read as this period's: a false
    "exhausted" right after the quota refilled is what issue #43 must never cause."""
    before = datetime(2026, 3, 31, 23, 55, tzinfo=UTC)
    after = datetime(2026, 4, 1, 0, 5, tzinfo=UTC)
    clock = Clock(before)
    with serve() as gh:
        gh.reply(usage_path(2026, 3), report(item("2026-03-02", 3000)))
        gh.reply(usage_path(2026, 4), {"message": "Server Error"}, status=500)
        billing = reader(gh.url, clock)
        assert billing.used(before) == (3000.0, before)
        clock.now = after  # within the interval, but a new period: read now, and it fails
        assert billing.used(after) is None
        gh.reply(usage_path(2026, 4), report(item("2026-04-01", 4)))
        clock.now = after + timedelta(minutes=16)
        assert billing.used(clock.now) == (4.0, clock.now)


def test_a_period_that_began_last_month_reads_both_months() -> None:
    now = datetime(2026, 1, 3, tzinfo=UTC)
    with serve() as gh:
        gh.reply(usage_path(2025, 12), report(item("2025-12-10", 50), item("2025-12-20", 300)))
        gh.reply(usage_path(2026, 1), report(item("2026-01-01", 10)))
        billing = reader(gh.url, Clock(now), reset_day=15)
        assert billing.used(now) == (310.0, now)


def test_an_organization_is_read_at_its_own_path() -> None:
    with serve() as gh:
        gh.reply(usage_path(2026, 3, "farm-org", org=True), report(item("2026-03-02", 5)))
        client = GitHubClient(gh.url, BILLING, clock=Clock(NOW))
        billing = CiBilling(client, "farm-org", organization=True)
        assert billing.used(NOW) == (5.0, NOW)


@pytest.mark.parametrize(
    ("status", "kind", "hint"),
    [
        (403, "forbidden", "cannot read this account's billing usage"),
        (401, "unauthorized", "was refused"),
        (404, "not_found", "no such account"),
        (400, "http_error", "enhanced billing platform"),
    ],
)
def test_a_refused_read_is_logged_once_with_what_to_fix(
    status: int, kind: str, hint: str, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock(NOW)
    with serve() as gh, caplog.at_level(logging.DEBUG):
        gh.reply(usage_path(2026, 3), {"message": f"denied for {SECRET}"}, status=status)
        billing = reader(gh.url, clock)
        for minutes in (0, 16, 32):
            clock.now = NOW + timedelta(minutes=minutes)
            assert billing.used(clock.now) is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    billing_warnings = [w for w in warnings if "billing usage" in w]
    assert len(billing_warnings) == 1
    assert f"({kind})" in billing_warnings[0] and hint in billing_warnings[0]
    assert SECRET not in caplog.text and BILLING not in caplog.text


@pytest.mark.parametrize(
    "body",
    [b"<html>maintenance</html>", json.dumps(report(item("2026-03-02", 1e308))).encode()],
)
def test_a_body_that_is_not_a_good_report_gives_no_reading(
    body: bytes, caplog: pytest.LogCaptureFixture
) -> None:
    with serve() as gh, caplog.at_level(logging.WARNING):
        gh.reply(usage_path(2026, 3), body)
        assert reader(gh.url, Clock(NOW)).used(NOW) is None
    assert "(bad_response)" in caplog.text


def test_items_without_a_date_are_left_out_with_a_warning_then_recovery_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock(NOW)
    with serve() as gh, caplog.at_level(logging.INFO):
        gh.reply(usage_path(2026, 3), {"message": "Forbidden"}, status=403)
        billing = reader(gh.url, clock)
        assert billing.used(NOW) is None
        undated = {k: v for k, v in item("2026-03-02", 9).items() if k != "date"}
        gh.reply(usage_path(2026, 3), report(item("2026-03-02", 20), undated))
        clock.now = NOW + timedelta(minutes=16)
        assert billing.used(clock.now) == (20.0, clock.now)
    assert "1 item(s) without a date were left out" in caplog.text
    assert "readable again" in caplog.text


@contextmanager
def redirecting() -> Iterator[tuple[str, list[str]]]:
    """A server that answers every GET with a redirect to another host."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization") or "")
            self.send_response(302)
            self.send_header("Location", "http://127.0.0.2:9/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()


def test_a_redirect_is_never_followed(caplog: pytest.LogCaptureFixture) -> None:
    with redirecting() as (url, seen), caplog.at_level(logging.WARNING):
        assert reader(url, Clock(NOW)).used(NOW) is None
    assert seen == [f"Bearer {BILLING}"]  # asked once, at api_url only
    assert "billing usage of farm-owner not read" in caplog.text


def test_a_network_failure_gives_no_reading(caplog: pytest.LogCaptureFixture) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # nothing listens there once closed
    with caplog.at_level(logging.WARNING):
        assert reader(f"http://127.0.0.1:{port}", Clock(NOW)).used(NOW) is None
    assert "(network)" in caplog.text


def test_ci_minutes_resource_carries_the_reading() -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(f"/repos/{REPO}", {"default_branch": "main"})
        gh.reply(
            f"/repos/{REPO}/actions/runs?created=%3E%3D2026-03-19&page=1&per_page=100",
            {"total_count": 0, "workflow_runs": []},
        )
        gh.reply(usage_path(2026, 3), report(item("2026-03-02", 2999)))
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
        (
            {
                "billing_user": "a",
                "billing_token_env": "T",
                "allow_cancel": True,
                "cancel_repos": [REPO],
                "cancel_token_env": "T",
            },
            "one token each",
        ),
    ],
)
def test_billing_config_errors(extra: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_ci({**BASE, **extra}, [REPO])


def test_billing_needs_the_quota_it_counts_against() -> None:
    with pytest.raises(ValueError, match="needs included_minutes"):
        load_ci({"repos": [REPO], "billing_user": "a", "billing_token_env": "T"}, [REPO])


def test_billing_never_shares_the_main_token_variable() -> None:
    ci = {**BASE, "billing_user": "a", "billing_token_env": "GITHUB_TOKEN"}
    with pytest.raises(ValueError, match="a token of its own"):
        load_github({"repos": [REPO], "ci": ci})


def test_billing_skus_are_config_data() -> None:
    ci = load_ci({**BASE, "billing_skus": ["Actions_Linux_4_Core"]}, [REPO])
    assert ci.billing_skus == ("actions_linux_4_core",)
    default = load_ci(BASE, [REPO]).billing_skus
    assert default == ("actions_linux", "actions_windows", "actions_macos")


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
    assert load_ci(BASE, [REPO]).billing_token({}) is None


def test_the_billing_token_goes_to_the_billing_report_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock(NOW)
    with serve() as gh:
        gh.reply(f"/repos/{REPO}", {"default_branch": "main"})
        gh.reply(
            f"/repos/{REPO}/actions/runs?created=%3E%3D2026-03-19&page=1&per_page=100",
            {"total_count": 0, "workflow_runs": []},
        )
        gh.reply(usage_path(2026, 3), report(item("2026-03-02", 10)))
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
        assert BILLING not in repr(source.billing) and BILLING not in repr(source.billing.client)
        source(Snapshot(now=NOW))
        with caplog.at_level(logging.WARNING):  # the same value under two names: warned
            github.build_ci(connector, clock, {"MAIN": TOKEN, "BILL": TOKEN})
    by_path = {r.path.split("?")[0]: r.headers["Authorization"] for r in gh.requests}
    assert by_path["/users/farm-owner/settings/billing/usage"] == f"Bearer {BILLING}"
    assert all(v == f"Bearer {TOKEN}" for k, v in by_path.items() if "/billing/" not in k)
    assert "holds the same token as $MAIN" in caplog.text and TOKEN not in caplog.text


def test_status_names_the_account_and_the_variable_never_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text(
        '[engine.rules.ci_economy]\nmax_reading_age = "10m"\n'
        '[connectors.github]\nrepos = ["example/repo"]\n'
        '[connectors.github.ci]\nincluded_minutes = 3000\nbilling_user = "farm-owner"\n'
        'billing_token_env = "BILL"\nbilling_interval = "30m"\n'
    )
    settings = load_settings(config)
    loaded = load_rules(settings.engine)
    monkeypatch.delenv("BILL", raising=False)
    unset = render_status(settings, loaded)
    assert "farm-owner, every 0:30:00 (NOT ready: [connectors.github.ci] billing_token_env" in unset
    monkeypatch.setenv("BILL", BILLING)
    status = render_status(settings, loaded)
    assert "billing usage of user farm-owner, every 0:30:00 (token from $BILL)" in status
    assert BILLING not in status
    (warning,) = billing_interval_warnings(loaded.rules, settings)
    assert "billing_interval (0:30:00) is longer than rule 'ci_economy' max_reading_age" in warning
