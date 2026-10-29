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
    Tally,
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
    tally = minutes_since(body, date(2026, 3, 1))
    assert (tally.minutes, tally.undated, tally.counted) == (357.5, 1, 3)  # one without a date
    assert tally.other_skus == {"actions_linux_16_core"}
    larger = minutes_since(body, date(2026, 3, 1), ["actions_linux_16_core"])
    assert (larger.minutes, larger.undated, larger.counted) == (500.0, 0, 1)


def test_sku_display_names_count_as_their_ids() -> None:
    """The endpoint's documented example says ``Actions Linux``; it must not read as 0."""
    body = report(
        item("2026-03-02", 10, sku="Actions Linux"),
        item("2026-03-02", 5, sku="actions-macos"),
        item("2026-03-02", 1, sku=" ACTIONS_WINDOWS "),
    )
    assert minutes_since(body, date(2026, 3, 1)).minutes == 16.0


def test_only_the_asked_month_up_to_today_counts() -> None:
    body = report(
        item("2026-02-26", 100),
        item("2026-03-02", 200),
        item("2026-03-25", 999),  # after today: not this reading's
    )
    start, today = date(2026, 2, 25), date(2026, 3, 20)
    feb = minutes_since(body, start, month=(2026, 2), until=today)
    march = minutes_since(body, start, month=(2026, 3), until=today)
    assert (feb.minutes, march.minutes) == (100.0, 200.0)


@pytest.mark.parametrize(
    ("raw", "counted"),
    [
        ("2026-03-01T01:00:00+09:00", False),  # 2026-02-28T16:00Z: before the reset
        ("2026-02-28T20:00:00-05:00", True),  # 2026-03-01T01:00Z
        ("2026-03-01T00:00:00.000Z", True),
        ("2026-03-01", True),
        ("2026-03-01T00:00:00", True),  # no offset: taken as UTC
    ],
)
def test_item_dates_are_read_in_utc(raw: str, counted: bool) -> None:
    tally = minutes_since(report(item("2026-03-02", 1, date=raw)), date(2026, 3, 1))
    assert tally.minutes == (1.0 if counted else 0.0)


@pytest.mark.parametrize(
    "raw",
    [
        "2026-13-01T00:00:00Z",
        "March 1",
        "",
        20260301,
        "0001-01-01T00:00:00+05:00",  # before the first UTC day
        "9999-12-31T23:00:00-05:00",  # after the last
    ],
)
def test_an_unreadable_date_is_left_out(raw: Any) -> None:
    tally = minutes_since(report(item("2026-03-02", 1, date=raw)), date(2026, 3, 1))
    assert tally == Tally(0.0, 1, 0, frozenset())


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


def test_an_answer_that_ignores_the_month_is_not_counted_twice() -> None:
    now = datetime(2026, 3, 20, tzinfo=UTC)
    both = report(item("2026-02-26", 1000), item("2026-03-02", 2000))
    with serve() as gh:
        gh.reply(usage_path(2026, 2), both)
        gh.reply(usage_path(2026, 3), both)
        billing = reader(gh.url, Clock(now), reset_day=25)
        assert billing.used(now) == (3000.0, now)


def test_a_report_with_none_of_the_counted_skus_reads_0_and_says_so(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = Clock(NOW)
    with serve() as gh, caplog.at_level(logging.WARNING):
        gh.reply(usage_path(2026, 3), report(item("2026-03-02", 9, sku="actions_linux_arm")))
        billing = reader(gh.url, clock)
        assert billing.used(NOW) == (0.0, NOW)
        clock.now = NOW + timedelta(minutes=16)
        billing.used(clock.now)
        gh.reply(usage_path(2026, 3), report(item("2026-03-02", 9)))
        clock.now = NOW + timedelta(minutes=32)
        assert billing.used(clock.now) == (9.0, clock.now)
        gh.reply(usage_path(2026, 3), report(item("2026-03-02", 9, sku="actions_linux_arm")))
        clock.now = NOW + timedelta(minutes=48)
        billing.used(clock.now)
    warned = [r.getMessage() for r in caplog.records if "none of billing_skus" in r.getMessage()]
    assert len(warned) == 2  # once, and again after a read that counted something
    assert "Actions minutes of actions_linux_arm but none of billing_skus" in warned[0]


def test_a_stale_cached_answer_gives_no_reading(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock(NOW)
    with serve() as gh, caplog.at_level(logging.WARNING):
        gh.reply(usage_path(2026, 3), report(item("2026-03-02", 100)))
        billing = reader(gh.url, clock)
        assert billing.used(NOW) == (100.0, NOW)
        gh.reply(usage_path(2026, 3), {"message": "Server Error"}, status=502)
        for minutes in (31, 47):  # past the cache and two intervals: only the stale answer
            clock.now = NOW + timedelta(minutes=minutes)
            assert billing.used(clock.now) is None
    warned = [r.getMessage() for r in caplog.records if "not read" in r.getMessage()]
    assert len(warned) == 1 and "(stale)" in warned[0]


def test_one_month_failing_fails_the_whole_read(caplog: pytest.LogCaptureFixture) -> None:
    now = datetime(2026, 3, 20, tzinfo=UTC)
    clock = Clock(now)
    with serve() as gh, caplog.at_level(logging.WARNING):
        gh.reply(usage_path(2026, 2), report(item("2026-02-26", 1000)))
        gh.reply(usage_path(2026, 3), {"message": "Forbidden"}, status=403)
        billing = reader(gh.url, clock, reset_day=25)
        for minutes in (0, 16):
            clock.now = now + timedelta(minutes=minutes)
            assert billing.used(clock.now) is None
    warned = [r.getMessage() for r in caplog.records if "not read" in r.getMessage()]
    assert len(warned) == 1 and "(forbidden)" in warned[0]


def test_a_spent_time_budget_fails_the_whole_read(caplog: pytest.LogCaptureFixture) -> None:
    now = datetime(2026, 3, 20, tzinfo=UTC)
    with serve() as gh, caplog.at_level(logging.WARNING):
        gh.reply(usage_path(2026, 2), report(item("2026-02-26", 1000)))
        gh.reply(usage_path(2026, 3), report(item("2026-03-02", 2000)))
        billing = reader(gh.url, Clock(now), reset_day=25, budget=timedelta(seconds=10))
        ticks = [0.0]
        billing.client.monotonic = lambda: ticks[0]
        get = billing.client.get

        def slow_get(path: str, params: Any = None) -> Any:
            answer = get(path, params)
            ticks[0] += 60  # the first month took the whole budget
            return answer

        billing.client.get = slow_get  # type: ignore[method-assign]
        assert billing.used(now) is None
    assert len(gh.requests) == 1
    assert "(budget)" in caplog.text


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
        (422, "http_error", None),
    ],
)
def test_a_refused_read_is_logged_once_with_what_to_fix(
    status: int, kind: str, hint: str | None, caplog: pytest.LogCaptureFixture
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
    assert f"({kind})" in billing_warnings[0]
    if hint is None:  # only a 400 hints at the billing platform
        assert f"({kind}); CI minutes fall back" in billing_warnings[0]
    else:
        assert hint in billing_warnings[0]
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
    ci = load_ci({**BASE, "billing_skus": ["Actions Linux 4 Core"]}, [REPO])
    assert ci.billing_skus == ("actions_linux_4_core",)
    with pytest.raises(ValueError, match="billing_skus must not be empty"):
        load_ci({**BASE, "billing_skus": []}, [REPO])
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
            cancel = load_github(
                {
                    "repos": [REPO],
                    "api_url": gh.url,
                    "token_env": "MAIN",
                    "ci": {
                        **BASE,
                        "billing_user": "farm-owner",
                        "billing_token_env": "BILL",
                        "cancel_token_env": "CANCEL",  # warned even while allow_cancel is off
                    },
                }
            )
            cancel.build_ci(connector, clock, {"MAIN": TOKEN, "CANCEL": BILLING, "BILL": BILLING})
    by_path = {r.path.split("?")[0]: r.headers["Authorization"] for r in gh.requests}
    assert by_path["/users/farm-owner/settings/billing/usage"] == f"Bearer {BILLING}"
    assert all(v == f"Bearer {TOKEN}" for k, v in by_path.items() if "/billing/" not in k)
    assert "holds the same token as $MAIN" in caplog.text
    assert "$BILL holds the same token as $CANCEL" in caplog.text
    assert TOKEN not in caplog.text and BILLING not in caplog.text


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
    assert "farm-owner, every 30m (NOT ready: [connectors.github.ci] billing_token_env" in unset
    monkeypatch.setenv("BILL", BILLING)
    status = render_status(settings, loaded)
    assert "billing usage of user farm-owner, every 30m (token from $BILL)" in status
    assert BILLING not in status
    (warning,) = billing_interval_warnings(loaded.rules, settings)
    assert "billing_interval (0:30:00) keeps a reading for up to 1:00:00, longer than" in warning


@pytest.mark.parametrize(("interval", "warned"), [("5m", False), ("6m", True)])
def test_a_reading_kept_past_max_reading_age_is_warned_about(
    tmp_path: Path, interval: str, warned: bool
) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text(
        '[engine.rules.ci_economy]\nmax_reading_age = "10m"\n'
        '[connectors.github]\nrepos = ["example/repo"]\n'
        '[connectors.github.ci]\nincluded_minutes = 3000\nbilling_user = "farm-owner"\n'
        f'billing_token_env = "BILL"\nbilling_interval = "{interval}"\n'
    )
    settings = load_settings(config)
    assert bool(billing_interval_warnings(load_rules(settings.engine).rules, settings)) is warned
