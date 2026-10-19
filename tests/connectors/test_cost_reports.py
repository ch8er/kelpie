"""Providers' cost reports: config, parsing of both API shapes, paging, failures, store."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

from kelpie import net
from kelpie.connectors.cost_reports import (
    CostReportConfig,
    CostReportSource,
    ReportKind,
    load_cost_reports,
)
from kelpie.connectors.cost_reports.source import ReportError, fetch, window
from kelpie.core.model import ReportedCost
from kelpie.core.storage import SqliteCostReports
from tests.connectors.fake_provider import fake_provider

NOW = datetime(2026, 10, 19, 15, 30, tzinfo=UTC)
KEY = "sk-ant-admin01-secret-value"
D1, D2 = date(2026, 10, 17), date(2026, 10, 18)


def anthropic_bucket(day: date, *results: Mapping[str, Any]) -> dict[str, Any]:
    start = datetime.combine(day, datetime.min.time(), UTC)
    return {
        "starting_at": start.isoformat().replace("+00:00", "Z"),
        "ending_at": (start + timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "results": list(results),
    }


def tokens(model: str, cents: str, **kw: Any) -> dict[str, Any]:
    return {"amount": cents, "currency": "USD", "cost_type": "tokens", "model": model, **kw}


class Fake:
    """An http_get that answers from a list of pages and records each request."""

    def __init__(self, *pages: Any, status: int = 200) -> None:
        self.pages = list(pages)
        self.status = status
        self.calls: list[tuple[str, str, dict[str, str]]] = []

    def __call__(
        self,
        url: str,
        path: str,
        headers: Mapping[str, str],
        timeout: float,
        max_bytes: int = 0,
        read_body: bool = True,
        proxy: bool = False,
    ) -> net.Response:
        self.calls.append((url, path, dict(headers)))
        if isinstance(self.pages[0], Exception):
            raise self.pages.pop(0)
        body = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        return net.Response(self.status, {}, data)

    def query(self, i: int = 0) -> list[tuple[str, str]]:
        return parse_qsl(urlsplit(self.calls[i][1]).query)


def config(kind: ReportKind = ReportKind.ANTHROPIC, **kw: Any) -> CostReportConfig:
    return CostReportConfig("p", kind, "https://api.example.test", KEY, **kw)


# --- config -------------------------------------------------------------------------------


def test_config_defaults_and_build() -> None:
    loaded = load_cost_reports(
        {
            "anthropic": {"kind": "anthropic", "admin_key_env": "K"},
            "openai": {
                "kind": "openai",
                "admin_key_env": "O",
                "projects": ["proj_1"],
                "days": 3,
                "interval": "2h",
            },
        }
    )
    assert loaded is not None and loaded.providers == {"anthropic", "openai"}
    a, o = loaded.reports
    assert a.api_url == "https://api.anthropic.com" and a.days == 7
    assert o.api_url == "https://api.openai.com" and o.scopes == ("proj_1",)
    assert o.interval == timedelta(hours=2)
    built = a.build({"K": KEY})
    assert built.admin_key == KEY and KEY not in repr(built) and KEY not in repr(a)
    with pytest.raises(ValueError, match=r"\$K is not set") as exc:
        a.build({})
    assert KEY not in str(exc.value)
    with pytest.raises(ValueError, match="printable ASCII"):
        a.build({"K": "a\nb"})
    assert load_cost_reports(None) is None


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ([], "must be a table of providers"),
        ({}, "must be a table of providers"),
        ({"a b": {}}, "provider names must be"),
        ({"a": 1}, "must be a table"),
        ({"a": {"kind": "anthropic", "admin_key_env": "K", "x": 1}}, "unknown"),
        ({"a": {"kind": "azure", "admin_key_env": "K"}}, "kind must be one of"),
        ({"a": {"kind": "anthropic"}}, "admin_key_env must be"),
        ({"a": {"kind": "anthropic", "admin_key_env": "$K"}}, "admin_key_env must be"),
        ({"a": {"kind": "anthropic", "admin_key_env": "K", "api_url": "http://api.test"}}, "https"),
        (
            {"a": {"kind": "anthropic", "admin_key_env": "K", "api_url": "https://u:p@api.test"}},
            "credentials",
        ),
        ({"a": {"kind": "anthropic", "admin_key_env": "K", "interval": "1m"}}, "at least"),
        ({"a": {"kind": "anthropic", "admin_key_env": "K", "interval": "soon"}}, "duration"),
        ({"a": {"kind": "anthropic", "admin_key_env": "K", "timeout": "2m"}}, "timeout"),
        ({"a": {"kind": "anthropic", "admin_key_env": "K", "days": 0}}, "days must be"),
        ({"a": {"kind": "anthropic", "admin_key_env": "K", "days": 40}}, "days must be"),
        ({"a": {"kind": "anthropic", "admin_key_env": "K", "projects": ["x"]}}, "not projects"),
        ({"a": {"kind": "openai", "admin_key_env": "K", "workspaces": ["x"]}}, "not workspaces"),
        ({"a": {"kind": "openai", "admin_key_env": "K", "projects": ["a&b"]}}, "list of ids"),
        ({"a": {"kind": "openai", "admin_key_env": "K", "projects": "p"}}, "list of ids"),
    ],
)
def test_bad_config_is_refused(section: Any, message: str) -> None:
    with pytest.raises(ValueError) as exc:
        load_cost_reports(section)
    assert message in str(exc.value)


# --- anthropic ----------------------------------------------------------------------------


def test_window_is_the_last_complete_utc_days() -> None:
    start, end = window(NOW, 2)
    assert (start, end) == (datetime(2026, 10, 17, tzinfo=UTC), datetime(2026, 10, 19, tzinfo=UTC))


def test_anthropic_tokens_per_model_and_day() -> None:
    fake = Fake(
        {
            "data": [
                anthropic_bucket(
                    D1,
                    tokens("claude-opus-5-5", "1234.5"),
                    tokens("claude-opus-5-5", "100"),
                    tokens("claude-haiku-4-5", "50"),
                    {"amount": "999", "currency": "USD", "cost_type": "web_search"},
                ),
                anthropic_bucket(D2),  # covered, nothing spent
            ],
            "has_more": False,
            "next_page": None,
        }
    )
    costs = fetch(config(days=2), NOW, fake)
    assert costs == (
        ReportedCost("p", D1, 0.0),
        ReportedCost("p", D1, 0.5, "claude-haiku-4-5"),
        ReportedCost("p", D1, 13.345, "claude-opus-5-5"),
        ReportedCost("p", D2, 0.0),
    )
    url, path, headers = fake.calls[0]
    assert url == "https://api.example.test" and path.startswith("/v1/organizations/cost_report?")
    assert headers["x-api-key"] == KEY and headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in headers
    assert dict(fake.query()) | {"group_by[]": "description"} == {
        "starting_at": "2026-10-17T00:00:00Z",
        "ending_at": "2026-10-19T00:00:00Z",
        "bucket_width": "1d",
        "limit": "31",
        "group_by[]": "description",
    }


def test_anthropic_pages_and_workspaces() -> None:
    first = {
        "data": [anthropic_bucket(D1, tokens("m", "100", workspace_id="wrk_farm"))],
        "has_more": True,
        "next_page": "page_2",
    }
    second = {
        "data": [
            anthropic_bucket(
                D1,
                tokens("m", "300", workspace_id="wrk_other"),
                tokens("m", "200", workspace_id="wrk_farm"),
            )
        ],
        "has_more": False,
        "next_page": None,
    }
    fake = Fake(first, second)
    costs = fetch(config(days=2, scopes=("wrk_farm",)), NOW, fake)
    assert costs == (ReportedCost("p", D1, 0.0), ReportedCost("p", D1, 3.0, "m"))
    assert ("group_by[]", "workspace_id") in fake.query(0)
    assert ("page", "page_2") in fake.query(1)


# --- openai -------------------------------------------------------------------------------


def openai_bucket(day: date, *values: float, currency: str = "usd") -> dict[str, Any]:
    start = int(datetime.combine(day, datetime.min.time(), UTC).timestamp())
    return {
        "object": "bucket",
        "start_time": start,
        "end_time": start + 86400,
        "results": [
            {
                "object": "organization.costs.result",
                "amount": {"value": v, "currency": currency},
                "line_item": None,
                "project_id": None,
            }
            for v in values
        ],
    }


def test_openai_daily_totals() -> None:
    fake = Fake({"data": [openai_bucket(D1, 1.25, 0.5), openai_bucket(D2)], "has_more": False})
    costs = fetch(config(ReportKind.OPENAI, days=2, scopes=("proj_a", "proj_b")), NOW, fake)
    assert costs == (ReportedCost("p", D1, 1.75), ReportedCost("p", D2, 0.0))
    _, path, headers = fake.calls[0]
    assert path.startswith("/v1/organization/costs?")
    assert headers["authorization"] == f"Bearer {KEY}" and "x-api-key" not in headers
    q = fake.query()
    assert ("start_time", str(int(datetime(2026, 10, 17, tzinfo=UTC).timestamp()))) in q
    assert [v for k, v in q if k == "project_ids"] == ["proj_a", "proj_b"]


def test_openai_other_currencies_are_refused() -> None:
    fake = Fake({"data": [openai_bucket(D1, 3.0, currency="eur")], "has_more": False})
    with pytest.raises(ReportError, match="not USD"):
        fetch(config(ReportKind.OPENAI), NOW, fake)


# --- failures -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "status", "cause"),
    [
        ({}, 401, "auth"),
        ({}, 403, "auth"),
        ({}, 429, "rate_limited"),
        ({}, 500, "http"),
        (b"not json", 200, "malformed"),
        ({"data": "x"}, 200, "malformed"),
        ({"data": [anthropic_bucket(D1, tokens("m", "nan"))]}, 200, "malformed"),
        ({"data": [anthropic_bucket(D1, tokens("m", True))]}, 200, "malformed"),
        ({"data": [{"starting_at": "yesterday", "results": []}]}, 200, "malformed"),
        ({"data": [{"starting_at": "2026-10-17T00:00:00Z", "results": "x"}]}, 200, "malformed"),
        ({"data": ["x"]}, 200, "malformed"),
        ({"data": [anthropic_bucket(D1, tokens("m", 10**400))]}, 200, "malformed"),
        ({"data": [anthropic_bucket(D1, tokens("m", "1e13"))]}, 200, "malformed"),
        ({"data": [anthropic_bucket(D1, tokens("m", "5", currency="EUR"))]}, 200, "currency"),
        ({"data": [], "has_more": True}, 200, "malformed"),  # more pages, no token
        ({"data": [], "has_more": True, "next_page": 7}, 200, "malformed"),
        (b"[" * 100_000, 200, "malformed"),  # too deep for the JSON parser
        (net.RequestError("timeout", "timed out"), 200, "timeout"),
    ],
)
def test_bad_answers_give_no_reading(answer: Any, status: int, cause: str) -> None:
    fake = Fake(answer, status=status)
    with pytest.raises(ReportError) as exc:
        fetch(config(), NOW, fake)
    assert exc.value.cause == cause and KEY not in str(exc.value)


def test_endless_pages_stop() -> None:
    page = {"data": [], "has_more": True, "next_page": "again"}
    with pytest.raises(ReportError, match="pages"):
        fetch(config(), NOW, Fake(page))


def test_source_reads_once_per_interval_and_keeps_the_last_reading(
    caplog: pytest.LogCaptureFixture,
) -> None:
    good = {"data": [anthropic_bucket(D2, tokens("m", "100"))], "has_more": False}
    fake = Fake(good, good, net.RequestError("connection", "connection failed (OSError)"), good)
    clock = [0.0]
    source = CostReportSource([config(days=1)], fake, monotonic=lambda: clock[0], background=False)
    assert source.read(NOW) == (ReportedCost("p", D2, 0.0), ReportedCost("p", D2, 1.0, "m"))
    assert source.statuses()[0].ok is True and source.statuses()[0].fetched_at == NOW
    clock[0] = 60
    source.read(NOW)
    assert len(fake.calls) == 1  # not due yet
    clock[0] = 3600
    source.read(NOW)
    assert len(fake.calls) == 2
    clock[0] = 7200
    with caplog.at_level(logging.WARNING):
        kept = source.read(NOW + timedelta(hours=2))
    assert kept[-1] == ReportedCost("p", D2, 1.0, "m")  # the last good reading
    status = source.statuses()[0]
    assert (status.ok, status.error, status.fetched_at) == (False, "connection", NOW)
    assert "cost report of p not read" in caplog.text and KEY not in caplog.text
    clock[0] = 7200 + 200
    source.read(NOW)
    assert len(fake.calls) == 3  # a failure is retried after 5 minutes, not an hour
    clock[0] = 7200 + 300
    source.read(NOW)
    assert source.statuses()[0].ok is True


def test_a_bug_in_parsing_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    import kelpie.connectors.cost_reports.source as source_module

    def broken(*args: Any) -> Any:
        raise KeyError("boom")

    monkeypatch.setattr(source_module, "fetch", broken)
    source = CostReportSource([config()], Fake({}), background=False)
    assert source.read(NOW) == () and source.statuses()[0].error == "error"


def test_over_the_network_the_key_goes_to_the_origin_only() -> None:
    with fake_provider() as server:
        server.body = json.dumps({"data": [anthropic_bucket(D2, tokens("m", "250"))]}).encode()
        report = replace(config(days=1), api_url=server.url)
        assert fetch(report, NOW)[-1] == ReportedCost("p", D2, 2.5, "m")
        path, sent = server.requests[0]
        headers = {k.lower(): v for k, v in sent.items()}
        assert path.startswith("/v1/organizations/cost_report?")
        assert headers["x-api-key"] == KEY and headers["user-agent"].startswith("kelpie/")
        server.redirect_to = "http://127.0.0.1:9"
        with pytest.raises(ReportError) as exc:
            fetch(report, NOW)
        assert exc.value.cause == "redirect"


# --- store --------------------------------------------------------------------------------


def test_store_replaces_the_days_a_reading_covers(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    store = SqliteCostReports(db)
    store.replace([ReportedCost("a", D1, 1.0, "m"), ReportedCost("a", D2, 2.0)], NOW)
    store.replace([ReportedCost("b", D2, 5.0)], NOW)
    store.replace([ReportedCost("a", D2, 3.0, "m")], NOW)  # only D2 of a is replaced
    assert store.read(D1) == (
        ReportedCost("a", D1, 1.0, "m"),
        ReportedCost("a", D2, 3.0, "m"),
        ReportedCost("b", D2, 5.0),
    )
    assert store.read(D2, D2)[0] == ReportedCost("a", D2, 3.0, "m")
    later = (NOW + timedelta(days=200)).date()
    store.replace([ReportedCost("a", later, 1.0)], NOW + timedelta(days=200))  # retention
    assert store.read(date(2000, 1, 1)) == (ReportedCost("a", later, 1.0),)
    store.close()
    readonly = SqliteCostReports(db, readonly=True)
    assert len(readonly.read(date(2000, 1, 1))) == 1
    readonly.close()


def test_credits_are_counted() -> None:
    fake = Fake({"data": [anthropic_bucket(D1, tokens("m", "500"), tokens("m", "-200"))]})
    assert fetch(config(days=2), NOW, fake)[-1] == ReportedCost("p", D1, 3.0, "m")


@pytest.mark.parametrize(
    "token", ["../x", "&starting_at=2000-01-01T00:00:00Z", "https://evil.test/", "a b#c"]
)
def test_a_page_token_stays_inside_the_query(token: str) -> None:
    fake = Fake({"data": [], "has_more": True, "next_page": token}, {"data": [], "has_more": False})
    fetch(config(), NOW, fake)
    url, path, _ = fake.calls[1]
    assert url == "https://api.example.test"
    assert urlsplit(path).path == "/v1/organizations/cost_report"
    assert [v for k, v in fake.query(1) if k == "page"] == [token]
    assert [v for k, v in fake.query(1) if k == "starting_at"] == ["2026-10-12T00:00:00Z"]


def test_api_url_path_is_a_prefix() -> None:
    fake = Fake({"data": []})
    fetch(replace(config(), api_url="https://gw.example.test/anthropic/"), NOW, fake)
    url, path, _ = fake.calls[0]
    assert url == "https://gw.example.test/anthropic/"
    assert path.startswith("/anthropic/v1/organizations/cost_report?")


def test_all_pages_share_one_deadline() -> None:
    clock = [0.0]
    timeouts: list[float] = []

    class Slow(Fake):
        def __call__(self, *args: Any, **kw: Any) -> net.Response:
            timeouts.append(args[3])
            clock[0] += 25  # each page takes 25 s
            return super().__call__(*args, **kw)

    slow = Slow({"data": [], "has_more": True, "next_page": "more"})
    with pytest.raises(ReportError) as exc:
        fetch(replace(config(), timeout=30.0), NOW, slow, lambda: clock[0])
    assert exc.value.cause == "timeout"
    assert timeouts == [30.0, 30.0, 10.0]  # the last page gets what is left of 60 s


def test_reads_run_in_the_background() -> None:
    import threading

    release = threading.Event()
    good = {"data": [anthropic_bucket(D2, tokens("m", "100"))], "has_more": False}

    class Blocking(Fake):
        def __call__(self, *args: Any, **kw: Any) -> net.Response:
            release.wait(5)
            return super().__call__(*args, **kw)

    source = CostReportSource([config(days=1)], Blocking(good))
    assert source.read(NOW) == ()  # the first read is still running: nothing yet
    assert source.read(NOW) == ()  # not started twice
    release.set()
    for _ in range(200):
        if source.read(NOW):
            break
        threading.Event().wait(0.01)
    assert source.read(NOW)[-1] == ReportedCost("p", D2, 1.0, "m")
