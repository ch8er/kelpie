import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie import net
from kelpie.connectors.llm_quotas import source as src
from kelpie.connectors.llm_quotas.config import load_llm_quotas
from kelpie.connectors.llm_quotas.source import (
    LlmQuotaSource,
    ProbeKind,
    ProviderConfig,
    _time,
)
from kelpie.core.model import LimitHit, ProviderStatus
from kelpie.core.ports import ProviderReader
from tests.connectors.fake_provider import FakeProvider, fake_provider

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
KEY = "sk-test-SECRET-key-0123456789"


class Observer:
    def __init__(self) -> None:
        self.probed: list[ProviderStatus] = []
        self.failures: list[tuple[str, str]] = []
        self.hits: list[str] = []

    def provider_probed(self, status: ProviderStatus) -> None:
        self.probed.append(status)

    def provider_failed(self, provider: str, cause: str) -> None:
        self.failures.append((provider, cause))

    def provider_limit_hit(self, provider: str) -> None:
        self.hits.append(provider)


def config(url: str, **kw: Any) -> ProviderConfig:
    fields = kw.pop("fields", None)
    kind = kw.pop("kind", ProbeKind.HEADERS)
    if fields is None:
        fields = dict(src.DEFAULT_HEADER_FIELDS) if kind is ProbeKind.HEADERS else {}
    return ProviderConfig(
        name=kw.pop("name", "main"),
        kind=kind,
        api_url=url,
        path=kw.pop("path", "/v1/models"),
        token=kw.pop("token", KEY),
        fields=fields,
        timeout=kw.pop("timeout", 2.0),
        **kw,
    )


def probe(
    server: FakeProvider, now: datetime = NOW, **kw: Any
) -> tuple[ProviderStatus, tuple[LimitHit, ...], Observer]:
    observer = Observer()
    statuses, hits = LlmQuotaSource([config(server.url, **kw)], observer).read(now)
    (status,) = statuses
    return status, hits, observer


# --- readings ---------------------------------------------------------------------------


def test_a_source_is_a_provider_reader() -> None:
    assert isinstance(LlmQuotaSource([]), ProviderReader)


def test_headers_kind_reads_rate_limit_headers_and_sends_the_key() -> None:
    with fake_provider() as server:
        server.headers = {
            "RateLimit-Remaining": "40",
            "RateLimit-Limit": "100",
            "RateLimit-Reset": "30",
        }
        status, hits, observer = probe(server, unit="requests")
    assert status == ProviderStatus(
        "main",
        ok=True,
        auth_ok=True,
        remaining=40,
        limit=100,
        unit="requests",
        resets_at=NOW + timedelta(seconds=30),
        checked_at=NOW,
    )
    assert hits == ()
    assert observer.probed == [status]
    path, headers = server.requests[0]
    assert path == "/v1/models"
    assert headers["Authorization"] == f"Bearer {KEY}"
    assert headers["User-Agent"].startswith("kelpie/")


def test_custom_auth_header_scheme_static_headers_and_url_prefix() -> None:
    with fake_provider() as server:
        probe(
            server,
            auth_header="x-api-key",
            auth_scheme="",
            headers={"x-api-version": "2"},
            path="/models?limit=1",
        )
        server_url = server.url
        path, headers = server.requests[0]
    assert headers["x-api-key"] == KEY
    assert "Authorization" not in headers
    assert headers["x-api-version"] == "2"
    assert path == "/models?limit=1"
    assert config(server_url + "/api/").request_path() == "/api/v1/models"


def test_status_url_kind_reads_configured_json_paths() -> None:
    with fake_provider() as server:
        server.body = json.dumps(
            {
                "data": {
                    "usage": {"used": 750, "limit": "1000"},
                    "reset": "2026-03-02T15:00:00Z",
                    "keys": [{"expires": 1772600000}],
                }
            }
        ).encode()
        status, _, _ = probe(
            server,
            kind=ProbeKind.STATUS_URL,
            fields={
                "used": "data.usage.used",
                "limit": "data.usage.limit",
                "resets_at": "data.reset",
                "expires_at": "data.keys.0.expires",
            },
        )
    assert status.ok and status.auth_ok
    assert (status.remaining, status.limit) == (250, 1000)
    assert status.resets_at == datetime(2026, 3, 2, 15, tzinfo=UTC)
    assert status.expires_at == datetime.fromtimestamp(1772600000, UTC)


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[1, 2]", b"[" * 100_000 + b"]" * 100_000],
    ids=["text", "array", "deep"],
)
def test_status_url_with_a_bad_body_is_unknown(body: bytes) -> None:
    with fake_provider() as server:
        server.body = body
        status, _, observer = probe(
            server, kind=ProbeKind.STATUS_URL, fields={"remaining": "remaining"}
        )
    assert not status.ok and status.error_kind == "invalid_response"
    assert status.remaining is None and status.auth_ok is None
    assert observer.failures == [("main", "invalid_response")]


def test_missing_and_unusable_values_are_left_unknown() -> None:
    with fake_provider() as server:
        server.body = json.dumps(
            {"remaining": True, "limit": -5, "reset": "soon", "nested": {"a": 1}}
        ).encode()
        status, _, _ = probe(
            server,
            kind=ProbeKind.STATUS_URL,
            fields={
                "remaining": "remaining",
                "limit": "limit",
                "resets_at": "reset",
                "expires_at": "nested",
                "used": "absent.path.3",
            },
        )
    assert status.ok
    assert status.remaining is status.limit is status.resets_at is status.expires_at is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("20", NOW + timedelta(seconds=20)),
        (20, NOW + timedelta(seconds=20)),
        ("1m30s", NOW + timedelta(seconds=90)),
        ("6m0s", NOW + timedelta(minutes=6)),
        ("250ms", NOW + timedelta(milliseconds=250)),
        ("2h", NOW + timedelta(hours=2)),
        ("1772460000", datetime.fromtimestamp(1772460000, UTC)),
        (1772460000000, datetime.fromtimestamp(1772460000, UTC)),
        ("2026-03-02T13:00:00Z", NOW + timedelta(hours=1)),
        ("2026-03-02T13:00:00", NOW + timedelta(hours=1)),  # naive: UTC
        ("Mon, 02 Mar 2026 13:00:00 GMT", NOW + timedelta(hours=1)),
        ("tomorrow", None),
        ("", None),
        ("1e400", None),
        ("99999999999999999999", None),  # too far ahead
        ("2999-01-01T00:00:00Z", None),
        (True, None),
    ],
)
def test_time_values(raw: Any, expected: datetime | None) -> None:
    assert _time(raw, NOW) == expected


# --- auth and limits ----------------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403])
def test_a_rejected_key_is_auth_failed(code: int) -> None:
    with fake_provider() as server:
        server.status = code
        status, hits, observer = probe(server)
    assert status == ProviderStatus(
        "main", ok=False, auth_ok=False, checked_at=NOW, error_kind="auth"
    )
    assert hits == ()
    assert observer.failures == [("main", "auth")]


def test_429_is_a_limit_hit_with_the_reset_time() -> None:
    with fake_provider() as server:
        server.status = 429
        server.headers = {"Retry-After": "120", "RateLimit-Limit": "50"}
        status, hits, observer = probe(server)
    assert status.ok and status.auth_ok
    assert status.error_kind == "rate_limited"
    assert (status.remaining, status.limit) == (0, 50)
    assert status.resets_at == NOW + timedelta(seconds=120)
    assert hits == (LimitHit("main", NOW, NOW + timedelta(seconds=120)),)
    assert observer.hits == ["main"]
    assert observer.failures == []


def test_429_prefers_the_configured_reset_header() -> None:
    with fake_provider() as server:
        server.status = 429
        server.headers = {"Retry-After": "120", "RateLimit-Reset": "60"}
        status, _, _ = probe(server)
    assert status.resets_at == NOW + timedelta(seconds=60)


@pytest.mark.parametrize("code", [500, 503, 404, 418])
def test_other_errors_leave_the_quota_unknown(code: int) -> None:
    with fake_provider() as server:
        server.status = code
        status, hits, observer = probe(server)
    assert status == ProviderStatus("main", ok=False, checked_at=NOW, error_kind="http_error")
    assert hits == ()
    assert observer.failures == [("main", "http_error")]


def test_limit_hits_are_recorded_once_per_period_and_kept_for_a_while() -> None:
    with fake_provider() as server:
        server.headers = {"RateLimit-Remaining": "0", "RateLimit-Reset": "3600"}
        observer = Observer()
        source = LlmQuotaSource([config(server.url, interval=timedelta(minutes=1))], observer)
        _, hits = source.read(NOW)
        assert hits == (LimitHit("main", NOW, NOW + timedelta(hours=1)),)
        # Still at 0 in the same period: the same hit, its reset time refreshed.
        server.headers = {"RateLimit-Remaining": "0", "RateLimit-Reset": "3000"}
        later = NOW + timedelta(minutes=10)
        _, hits = source.read(later)
        assert hits == (LimitHit("main", NOW, later + timedelta(seconds=3000)),)
        # Refilled, then exhausted again: a second hit.
        server.headers = {"RateLimit-Remaining": "10"}
        source.read(NOW + timedelta(hours=2))
        server.headers = {"RateLimit-Remaining": "0"}
        _, hits = source.read(NOW + timedelta(hours=3))
        assert [h.at for h in hits] == [NOW, NOW + timedelta(hours=3)]
        # Still at 0 without a reset time: no new hit.
        _, hits = source.read(NOW + timedelta(hours=4))
        assert len(hits) == 2
        # Old hits are dropped after the history window.
        _, hits = source.read(NOW + src.HIT_HISTORY + timedelta(hours=1))
        assert [h.at for h in hits] == [NOW + timedelta(hours=3)]
    assert observer.hits == ["main", "main"]


def test_a_new_period_while_still_exhausted_is_a_new_hit() -> None:
    with fake_provider() as server:
        server.headers = {"RateLimit-Remaining": "0", "RateLimit-Reset": "60"}
        source = LlmQuotaSource([config(server.url, interval=timedelta(minutes=1))])
        source.read(NOW)
        _, hits = source.read(NOW + timedelta(minutes=5))  # the first reset has passed
    assert len(hits) == 2


# --- scheduling, failures and warnings --------------------------------------------------


def test_probes_are_cached_for_the_interval() -> None:
    with fake_provider() as server:
        source = LlmQuotaSource([config(server.url, interval=timedelta(minutes=5))])
        first, _ = source.read(NOW)
        again, _ = source.read(NOW + timedelta(minutes=4))
        assert again == first and len(server.requests) == 1
        server.status = 500
        failed, _ = source.read(NOW + timedelta(minutes=5))
        assert len(server.requests) == 2
        assert failed[0].ok is False  # no stale reading after a failure


def test_an_unexpected_error_never_crashes_a_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: Any, **kw: Any) -> net.Response:
        raise RuntimeError(KEY)

    observer = Observer()
    source = LlmQuotaSource([config("https://provider.invalid")], observer, http_get=boom)
    (status,), _ = source.read(NOW)
    assert status.error_kind == "unexpected" and not status.ok
    assert observer.failures == [("main", "unexpected")]


def test_warns_once_per_cause_and_again_after_recovery(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    with fake_provider() as server:
        source = LlmQuotaSource([config(server.url, interval=timedelta(minutes=1))])
        server.status = 500
        for i in range(3):
            source.read(NOW + timedelta(minutes=i))
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
        server.status = 401
        source.read(NOW + timedelta(minutes=5))
        server.status = 200
        source.read(NOW + timedelta(minutes=6))
        server.status = 500
        source.read(NOW + timedelta(minutes=7))
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 3
    assert "key was rejected" in warnings[1]
    assert any("answers again" in r.getMessage() for r in caplog.records)


def test_one_failing_provider_does_not_hide_the_others() -> None:
    with fake_provider() as good:
        good.headers = {"RateLimit-Remaining": "7"}
        source = LlmQuotaSource(
            [
                config("http://127.0.0.1:9", name="down", timeout=0.5),
                config(good.url, name="up"),
            ]
        )
        statuses, _ = source.read(NOW)
    assert [(s.name, s.ok, s.error_kind) for s in statuses] == [
        ("down", False, "connection"),
        ("up", True, None),
    ]
    assert statuses[1].remaining == 7


# --- security ---------------------------------------------------------------------------


def test_redirects_are_refused_and_the_key_goes_nowhere_else() -> None:
    with fake_provider() as origin, fake_provider() as elsewhere:
        origin.redirect_to = elsewhere.url
        status, _, observer = probe(origin)
        assert elsewhere.requests == []
    assert status.error_kind == "redirect" and not status.ok
    assert observer.failures == [("main", "redirect")]


def test_proxy_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    with fake_provider() as server:
        status, _, _ = probe(server)
        assert len(server.requests) == 1
    assert status.ok


def test_the_key_is_in_no_repr_log_or_error(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    with fake_provider() as server:
        cfg = config(server.url)
        source = LlmQuotaSource([cfg])
        seen: list[str] = [repr(cfg), str(cfg), repr(source)]
        for code in (200, 401, 429, 500):
            server.status = code
            source._next_at.clear()
            statuses, hits = source.read(NOW)
            seen += [repr(statuses), repr(hits)]
        server.redirect_to = "http://127.0.0.1:9"
        source._next_at.clear()
        seen.append(repr(source.read(NOW)))
    with pytest.raises(net.RequestError) as caught:
        net.get("http://127.0.0.1:9", "/", {"Authorization": f"Bearer {KEY}"}, 0.5)
    seen += [str(caught.value), repr(caught.value), caught.value.detail]
    seen += [r.getMessage() for r in caplog.records]
    assert caplog.records  # something was logged
    assert not [s for s in seen if KEY in s]


def test_invalid_header_values_are_refused_without_quoting_them() -> None:
    bad = KEY + "\r\nX-Injected: 1"
    with fake_provider() as server:
        with pytest.raises(net.RequestError) as caught:
            net.get(server.url, "/", {"Authorization": f"Bearer {bad}"}, 1.0)
        assert server.requests == []
        status, _, _ = probe(server, token=bad)
        assert server.requests == []
    assert caught.value.cause == "invalid_header"
    assert KEY not in str(caught.value)
    assert status.error_kind == "invalid_header"
    with pytest.raises(net.RequestError):
        net.get("http://127.0.0.1:9", "/", {"Bad Name": "x"}, 1.0)


def test_a_key_with_a_line_break_stops_the_build() -> None:
    settings = load_llm_quotas(
        {
            "providers": [
                {
                    "name": "main",
                    "kind": "headers",
                    "api_url": "https://provider.example",
                    "path": "/v1/models",
                    "token_env": "KEY_VAR",
                }
            ]
        }
    )
    assert settings is not None
    with pytest.raises(ValueError, match=r"\$KEY_VAR contains a line break") as caught:
        settings.build(environ={"KEY_VAR": KEY + "\nX-Injected: 1"})
    assert KEY not in str(caught.value)
    with pytest.raises(ValueError, match=r"\$KEY_VAR is not set"):
        settings.build(environ={})
    source = settings.build(environ={"KEY_VAR": f"  {KEY} "})
    assert source.providers[0].token == KEY


@pytest.mark.parametrize("headers", [False, True], ids=["body", "headers"])
def test_a_drip_server_cannot_outlast_the_deadline(headers: bool) -> None:
    with fake_provider() as server:
        server.drip = 0.2  # one byte every 0.2s: every single read is "in time"
        server.drip_headers = headers
        server.body = b'{"remaining": 1}' * 10
        started = time.monotonic()
        status, _, _ = probe(
            server, kind=ProbeKind.STATUS_URL, fields={"remaining": "remaining"}, timeout=0.6
        )
        elapsed = time.monotonic() - started
    assert status.error_kind == "timeout"
    assert elapsed < 2.0


def test_oversized_responses_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(src, "MAX_RESPONSE_BYTES", 1000)
    with fake_provider() as server:
        server.body = b'{"remaining": 1, "pad": "' + b"x" * 5000 + b'"}'
        status, _, _ = probe(server, kind=ProbeKind.STATUS_URL, fields={"remaining": "remaining"})
        assert status.error_kind == "too_large"
        # The headers kind never reads the body, whatever its size.
        server.headers = {"RateLimit-Remaining": "3"}
        status, _, _ = probe(server)
    assert status.ok and status.remaining == 3
