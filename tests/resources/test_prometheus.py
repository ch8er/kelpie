import logging
import socket
import time
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.model import Resource, ResourceKind
from kelpie.core.ports import ResourceReader
from kelpie.resources import prometheus
from kelpie.resources.prometheus import (
    PrometheusClient,
    PrometheusConfig,
    PrometheusSource,
    QueryError,
)
from tests.resources.fake_prometheus import FakeProm, fake_prometheus, vector

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
GB = 10**9
HOSTS = {"host-a": "10.0.0.1:9100", "host-b": "10.0.0.2:9100"}
A, B, X = ({"instance": i} for i in ("10.0.0.1:9100", "10.0.0.2:9100", "10.9.9.9:9100"))


class Observer:
    def __init__(self) -> None:
        self.reads: list[int] = []
        self.failures: list[str] = []

    def resource_read(self, readings: int) -> None:
        self.reads.append(readings)

    def resource_failed(self, cause: str) -> None:
        self.failures.append(cause)


def config(prom: FakeProm, **kwargs: object) -> PrometheusConfig:
    kwargs.setdefault("hosts", HOSTS)
    kwargs.setdefault("timeout", 2.0)
    return PrometheusConfig(url=prom.url, **kwargs)  # type: ignore[arg-type]


def text(cfg: PrometheusConfig, name: str) -> str:
    return cfg.query_text(name)


def load_farm(prom: FakeProm, cfg: PrometheusConfig) -> None:
    def mount(inst: dict[str, str], path: str) -> dict[str, str]:
        return {**inst, "mountpoint": path, "fstype": "ext4"}

    prom.responses = {
        text(cfg, "disk_size"): vector(
            (mount(A, "/"), str(500 * GB)),
            (mount(A, "/data"), str(2000 * GB)),
            (mount(B, "/"), str(100 * GB)),
            (mount(X, "/"), str(100 * GB)),  # a host that is not configured
        ),
        text(cfg, "disk_avail"): vector(
            (mount(A, "/"), str(5 * GB)),
            (mount(A, "/data"), str(1500 * GB)),
            (mount(B, "/"), str(60 * GB)),
            (mount(X, "/"), str(1 * GB)),
        ),
        text(cfg, "memory_total"): vector((A, str(32 * GB)), (B, str(16 * GB)), (X, "1")),
        text(cfg, "memory_available"): vector((A, str(1 * GB)), (B, str(8 * GB))),
        text(cfg, "load1"): vector((A, "27.5"), (B, "2.1")),
        text(cfg, "cores"): vector((A, "8"), (B, "4")),
        text(cfg, "cpu_busy"): vector((A, "97.5"), (B, "12")),
        text(cfg, "processes"): vector((A, "31000")),
        text(cfg, "processes_max"): vector((A, "32768")),
    }


def by_name(readings: tuple[Resource, ...] | list[Resource]) -> dict[str, Resource]:
    return {r.name: r for r in readings}


# --- query building and parsing ---------------------------------------------------------


def test_reads_every_kind_per_configured_host() -> None:
    with fake_prometheus() as prom:
        cfg = config(prom)
        load_farm(prom, cfg)
        readings = by_name(PrometheusSource(cfg).read(NOW))
    assert set(readings) == {
        "host-a:/",
        "host-a:/data",
        "host-b:/",
        "host-a:memory",
        "host-b:memory",
        "host-a:load1",
        "host-b:load1",
        "host-a:cpu",
        "host-b:cpu",
        "host-a:processes",
    }
    root = readings["host-a:/"]
    assert (root.kind, root.host, root.unit, root.at) == (ResourceKind.DISK, "host-a", "GB", NOW)
    assert (root.used, root.limit) == (pytest.approx(495), pytest.approx(500))
    assert readings["host-a:memory"].used == pytest.approx(31)
    assert readings["host-a:memory"].limit == pytest.approx(32)
    assert readings["host-a:load1"].utilization == pytest.approx(27.5 / 8)
    assert readings["host-a:cpu"].utilization == pytest.approx(0.975)
    assert readings["host-a:processes"].utilization == pytest.approx(31000 / 32768)
    assert all(r.host in HOSTS for r in readings.values())  # host-x never shows up
    assert all(isinstance(r, Resource) for r in readings.values())


def test_a_source_is_a_resource_reader() -> None:
    with fake_prometheus() as prom:
        assert isinstance(PrometheusSource(config(prom)), ResourceReader)


def test_instant_queries_go_to_the_api_with_the_configured_promql() -> None:
    with fake_prometheus() as prom:
        cfg = config(prom, queries={"load1": "node_load1{job='nodes'}"}, host_label="host")
        PrometheusSource(cfg).read(NOW)
    assert {path for path, _, _ in prom.requests} == {"/api/v1/query"}
    sent = {promql for _, promql, _ in prom.requests}
    assert sent == {cfg.query_text(n) for n in prometheus.QUERY_NAMES}
    assert "node_load1{job='nodes'}" in sent  # custom query, used as written
    assert any(q == 'count by (host) (node_cpu_seconds_total{mode="idle"})' for q in sent)
    assert not any("{host_label}" in q for q in sent)
    assert "Authorization" not in prom.requests[0][2]
    assert prom.requests[0][2]["User-Agent"].startswith("kelpie/")


def test_base_url_with_a_path_prefix() -> None:
    with fake_prometheus() as prom:
        cfg = PrometheusConfig(url=prom.url + "/prometheus/", hosts=HOSTS)
        PrometheusSource(cfg).read(NOW)
    assert {path for path, _, _ in prom.requests} == {"/prometheus/api/v1/query"}


def test_custom_host_label_matches_series() -> None:
    with fake_prometheus() as prom:
        cfg = config(prom, host_label="nodename", hosts={"host-a": "alpha"})
        prom.responses = {
            text(cfg, "load1"): vector(({"nodename": "alpha"}, "1.5")),
            text(cfg, "cores"): vector(({"nodename": "alpha"}, "4")),
        }
        readings = by_name(PrometheusSource(cfg).read(NOW))
    assert readings["host-a:load1"].used == 1.5


def test_incomplete_data_gives_no_reading_for_that_kind() -> None:
    with fake_prometheus() as prom:
        cfg = config(prom)
        prom.responses = {
            text(cfg, "disk_size"): vector(({**A, "mountpoint": "/"}, str(100 * GB))),  # no avail
            text(cfg, "memory_total"): vector((A, "0")),  # a zero total cannot be judged
            text(cfg, "memory_available"): vector((A, "0")),
            text(cfg, "load1"): vector((A, "3")),  # no core count
            text(cfg, "processes"): vector((A, "100")),  # no max: a reading without a limit
        }
        readings = by_name(PrometheusSource(cfg).read(NOW))
    assert set(readings) == {"host-a:processes"}
    assert readings["host-a:processes"].limit is None


def test_bind_mounts_are_read_once_and_values_are_clamped() -> None:
    with fake_prometheus() as prom:
        cfg = config(prom)
        mount = {**A, "mountpoint": "/"}
        prom.responses = {
            text(cfg, "disk_size"): vector(
                ({**mount, "device": "/dev/sda1"}, str(100 * GB)),
                ({**mount, "device": "/dev/sdb1"}, str(999 * GB)),
            ),
            text(cfg, "disk_avail"): vector(
                ({**mount, "device": "/dev/sda1"}, str(250 * GB)),  # avail over size
                ({**mount, "device": "/dev/sdb1"}, str(1 * GB)),
            ),
            text(cfg, "cpu_busy"): vector((A, "130")),
        }
        readings = by_name(PrometheusSource(cfg).read(NOW))
    assert readings["host-a:/"].used == 0.0
    assert readings["host-a:/"].limit == pytest.approx(100)
    assert readings["host-a:cpu"].used == 100.0


def test_unusable_samples_are_skipped() -> None:
    with fake_prometheus() as prom:
        cfg = config(prom)
        prom.responses = {
            text(cfg, "load1"): {
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [
                        {"metric": A, "value": [1, "NaN"]},
                        {"metric": B, "value": [1, "+Inf"]},
                        {"metric": A, "value": [1]},
                        {"metric": A},
                        {"value": [1, "1"]},
                        "garbage",
                        {"metric": A, "value": [1, "2.5"]},
                    ],
                },
            },
            text(cfg, "cores"): vector((A, "2"), (B, "2")),
        }
        readings = by_name(PrometheusSource(cfg).read(NOW))
    assert set(readings) == {"host-a:load1"}
    assert readings["host-a:load1"].used == 2.5


# --- secrets ----------------------------------------------------------------------------


def test_bearer_token_is_sent_to_the_configured_host() -> None:
    with fake_prometheus() as prom:
        PrometheusSource(config(prom, token="s3cret")).read(NOW)
    assert {h["Authorization"] for _, _, h in prom.requests} == {"Bearer s3cret"}


def test_redirects_are_refused_and_the_token_goes_nowhere_else(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with fake_prometheus() as origin, fake_prometheus() as elsewhere:
        origin.redirect_to = elsewhere.url
        observer = Observer()
        source = PrometheusSource(config(origin, token="s3cret"), observer)
        with caplog.at_level(logging.INFO):
            assert source.read(NOW) == ()
        assert elsewhere.requests == []
        assert len(origin.requests) == 1  # the first query failed and the fetch stopped
    assert observer.failures == ["redirect"]
    assert "s3cret" not in caplog.text


def test_proxy_variables_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    with fake_prometheus() as prom:
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            monkeypatch.setenv(name, "http://127.0.0.1:9")  # nothing listens there
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        client = PrometheusClient(prom.url, 2.0, "s3cret")
        assert client.query("up") == []
    assert len(prom.requests) == 1


def test_the_token_is_not_part_of_the_client_repr() -> None:
    assert "s3cret" not in repr(PrometheusClient("http://x", 1.0, "s3cret"))
    assert "s3cret" not in repr(PrometheusConfig("http://x", {}, token="s3cret"))


# --- failures ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "cause"),
    [
        (lambda p: setattr(p, "status", 401), "http_auth"),
        (lambda p: setattr(p, "status", 403), "http_auth"),
        (lambda p: setattr(p, "status", 500), "http_error"),
        (lambda p: setattr(p, "status", 404), "http_error"),
        (lambda p: setattr(p, "raw", b"<html>not json</html>"), "invalid_response"),
        (lambda p: setattr(p, "raw", b"[1, 2]"), "invalid_response"),
        (
            lambda p: setattr(p, "raw", b'{"status":"error","errorType":"bad_data","error":"x"}'),
            "query_failed",
        ),
        (
            lambda p: setattr(p, "raw", b'{"status":"error","errorType":"bad data!"}'),
            "query_failed",
        ),
        (
            lambda p: setattr(
                p, "raw", b'{"status":"success","data":{"resultType":"matrix","result":[]}}'
            ),
            "invalid_response",
        ),
        (lambda p: setattr(p, "raw", b'{"status":"success","data":[]}'), "invalid_response"),
    ],
)
def test_failures_give_no_readings_and_are_counted(setup, cause: str) -> None:
    with fake_prometheus() as prom:
        setup(prom)
        observer = Observer()
        assert PrometheusSource(config(prom, token="s3cret"), observer).read(NOW) == ()
    assert observer.failures == [cause]
    assert observer.reads == []
    assert len(prom.requests) == 1  # no point asking the other queries


def test_timeout() -> None:
    with fake_prometheus() as prom:
        prom.delay = 1.5
        observer = Observer()
        started = time.monotonic()
        assert PrometheusSource(config(prom, timeout=0.2), observer).read(NOW) == ()
        assert time.monotonic() - started < 1.2  # one timeout, not one per query
    assert observer.failures == ["timeout"]


def test_connection_refused() -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again: nothing listens
    observer = Observer()
    cfg = PrometheusConfig(url=f"http://127.0.0.1:{port}", hosts=HOSTS, timeout=1.0)
    assert PrometheusSource(cfg, observer).read(NOW) == ()
    assert observer.failures == ["connection"]


def test_oversized_responses_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(prometheus, "MAX_RESPONSE_BYTES", 1000)
    with fake_prometheus() as prom:
        prom.raw = b"x" * 5000
        with pytest.raises(QueryError) as caught:
            PrometheusClient(prom.url, 2.0).query("up")
    assert caught.value.cause == "too_large"


@pytest.mark.parametrize("headers", [False, True])
def test_a_drip_server_cannot_outlast_the_deadline(headers: bool) -> None:
    with fake_prometheus() as prom:
        prom.drip = 0.3  # one byte every 0.3s: every single read is "in time"
        prom.drip_headers = headers
        prom.raw = b'{"status":"success","data":{"resultType":"vector","result":[]}}'
        observer = Observer()
        started = time.monotonic()
        assert PrometheusSource(config(prom, timeout=1.0), observer).read(NOW) == ()
        assert time.monotonic() - started < 2.0
    assert observer.failures == ["timeout"]


def test_warns_once_per_cause_and_again_after_recovery(caplog: pytest.LogCaptureFixture) -> None:
    with fake_prometheus() as prom:
        cfg = config(prom, interval=timedelta(seconds=10))
        observer = Observer()
        source = PrometheusSource(cfg, observer)
        with caplog.at_level(logging.INFO):
            prom.status = 500
            for i in range(3):  # three failed fetches
                assert source.read(NOW + timedelta(seconds=10 * i)) == ()
            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 1
            prom.status = 401  # a new cause warns again
            source.read(NOW + timedelta(seconds=30))
            assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2
            prom.status = 200
            source.read(NOW + timedelta(seconds=40))
            assert any("answers again" in r.message for r in caplog.records)
            prom.status = 500  # an outage after the recovery warns once more
            source.read(NOW + timedelta(seconds=50))
            assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 3
    assert observer.failures == ["http_error"] * 3 + ["http_auth", "http_error"]
    assert observer.reads == [0]


def test_results_are_cached_for_the_interval_and_failures_are_not_retried_early() -> None:
    with fake_prometheus() as prom:
        cfg = config(prom, interval=timedelta(seconds=60))
        load_farm(prom, cfg)
        source = PrometheusSource(cfg)
        first = source.read(NOW)
        asked = len(prom.requests)
        assert asked == len(prometheus.QUERY_NAMES)
        assert source.read(NOW + timedelta(seconds=59)) is first
        assert len(prom.requests) == asked
        prom.status = 500
        assert source.read(NOW + timedelta(seconds=60)) == ()  # refetched, failed
        assert len(prom.requests) == asked + 1
        assert source.read(NOW + timedelta(seconds=100)) == ()  # not asked again yet
        assert len(prom.requests) == asked + 1
        prom.status = 200
        again = by_name(source.read(NOW + timedelta(seconds=120)))
        assert again["host-a:memory"].at == NOW + timedelta(seconds=120)


def test_works_without_an_observer() -> None:
    with fake_prometheus() as prom:
        prom.status = 500
        assert PrometheusSource(config(prom)).read(NOW) == ()
        prom.status = 200
        assert PrometheusSource(config(prom)).read(NOW) == ()
