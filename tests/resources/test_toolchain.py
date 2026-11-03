"""Toolchain versions per host from Prometheus (incident 20, #284): a version that first
shows up within the lookback, after another, is a change; config names the metric and its
labels."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.model import ToolchainChange
from kelpie.core.ports import ToolchainReader
from kelpie.resources.config import load_resources
from kelpie.resources.prometheus import PrometheusConfig, PrometheusSource
from tests.resources.fake_prometheus import FakeProm, fake_prometheus, vector

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
HOSTS = {"mac-a": "10.0.0.1:9100", "mac-b": "10.0.0.2:9100"}
A, B = ({"instance": i} for i in ("10.0.0.1:9100", "10.0.0.2:9100"))
H = timedelta(hours=1)
M = timedelta(minutes=1)


def ts(at: datetime) -> str:
    return str(at.timestamp())


def config(prom: FakeProm, **kwargs: object) -> PrometheusConfig:
    return PrometheusConfig(url=prom.url, hosts=HOSTS, timeout=2.0, **kwargs)  # type: ignore[arg-type]


def timeline(
    host: dict[str, str], tool: str, steps: list[tuple[datetime, str]], labels: tuple[str, str]
) -> list[dict[str, object]]:
    """Matrix series of one (host, tool): ``steps`` of (scrape time, version), one series
    per version, each point stamped at the step after its scrape."""
    by_version: dict[str, list[list[object]]] = {}
    for at, version in steps:
        by_version.setdefault(version, []).append([at.timestamp() + 60, str(at.timestamp())])
    return [
        {"metric": {**host, labels[0]: tool, labels[1]: v}, "values": points}
        for v, points in by_version.items()
    ]


def answer(prom: FakeProm, cfg: PrometheusConfig, *series: list[dict[str, object]]) -> None:
    result = [x for s in series for x in s]
    prom.responses = {
        cfg.query_text("toolchain"): {
            "status": "success",
            "data": {"resultType": "matrix", "result": result},
        }
    }


def every(
    start: datetime, end: datetime, version: str, step: timedelta = 5 * M
) -> list[tuple[datetime, str]]:
    out, at = [], start
    while at < end:
        out.append((at, version))
        at += step
    return out


LABELS = ("tool", "version")


def changes(cfg: PrometheusConfig, prom: FakeProm) -> tuple[ToolchainChange, ...]:
    source = PrometheusSource(cfg)
    assert isinstance(source, ToolchainReader)
    source.read(NOW)
    return source.toolchain_changes(NOW)


def test_a_new_version_after_another_is_a_change() -> None:
    with fake_prometheus() as prom:
        cfg = config(prom)
        switch = NOW - 11 * H
        answer(
            prom,
            cfg,
            timeline(
                A,
                "xcode",
                every(NOW - 48 * H, switch, "16.0 (16A242d)") + every(switch, NOW, "16.1 (16B40)"),
                LABELS,
            ),
            timeline(A, "coresimulator", every(NOW - 48 * H, NOW, "1010.10"), LABELS),
            timeline(B, "xcode", every(NOW - 3 * H, NOW, "16.1 (16B40)"), LABELS),  # first: none
        )
        assert changes(cfg, prom) == (
            ToolchainChange(
                "mac-a", "xcode", "16.1 (16B40)", switch, "16.0 (16A242d)", switch - 5 * M
            ),
        )


def test_a_rollback_is_a_change_too_and_before_is_the_version_just_before() -> None:
    with fake_prometheus() as prom:
        cfg = config(prom)
        steps = (
            every(NOW - 47 * H, NOW - 10 * H, "A")
            + every(NOW - 10 * H, NOW - 6 * H, "B")
            + every(NOW - 6 * H, NOW - 5 * H, "C")
            + every(NOW - 5 * H, NOW, "A")
        )
        answer(prom, cfg, timeline(A, "xcode", steps, LABELS))
        assert [(c.before, c.after, c.at) for c in changes(cfg, prom)] == [
            ("A", "B", NOW - 10 * H),
            ("B", "C", NOW - 6 * H),
            ("C", "A", NOW - 5 * H),
        ]


def test_a_silent_stretch_while_the_tool_was_replaced_is_kept() -> None:
    # the old version last seen at -3h, nothing for an hour, the new one from -2h
    with fake_prometheus() as prom:
        cfg = config(prom)
        steps = every(NOW - 10 * H, NOW - 3 * H + M, "A") + every(NOW - 2 * H, NOW, "B")
        answer(prom, cfg, timeline(A, "xcode", steps, LABELS))
        [c] = changes(cfg, prom)
        assert (c.last_before, c.at) == (NOW - 3 * H, NOW - 2 * H)


def test_no_metric_no_changes_and_the_other_readings_still_come() -> None:
    with fake_prometheus() as prom:
        source = PrometheusSource(config(prom))
        source.read(NOW)
        assert source.toolchain_changes(NOW) == ()


def test_metric_and_labels_are_config_data() -> None:
    with fake_prometheus() as prom:
        cfg = config(
            prom,
            toolchain_metric="farm_tool_version",
            toolchain_tool_label="name",
            toolchain_version_label="v",
            toolchain_lookback=6 * H,
        )
        assert cfg.query_text("toolchain") == (
            "max by (instance, name, v) (timestamp(farm_tool_version))[21600s:300s]"
        )
        steps = every(NOW - 6 * H, NOW - 2 * H, "21.0.4") + every(NOW - 2 * H, NOW, "21.0.5")
        answer(prom, cfg, timeline(A, "jdk", steps, ("name", "v")))
        assert [(c.tool, c.before, c.after) for c in changes(cfg, prom)] == [
            ("jdk", "21.0.4", "21.0.5")
        ]


def test_an_instant_vector_answer_is_no_timeline() -> None:
    with fake_prometheus() as prom:
        cfg = config(prom)
        prom.responses = {cfg.query_text("toolchain"): vector((A, "1"))}
        assert changes(cfg, prom) == ()


def test_a_failed_toolchain_query_drops_only_the_changes(caplog: pytest.LogCaptureFixture) -> None:
    with fake_prometheus() as prom:
        cfg = config(prom)
        prom.responses = {cfg.query_text("toolchain"): {"status": "error"}}
        source = PrometheusSource(cfg)
        source.read(NOW)
        assert source.toolchain_changes(NOW) == ()


def test_config_validates_names_and_lookback() -> None:
    base = {"url": "http://prom:9090", "hosts": {"mac-a": "10.0.0.1:9100"}}
    loaded = load_resources({"prometheus": {**base, "toolchain_metric": "farm:tool_info"}})
    assert loaded.prometheus is not None
    assert loaded.prometheus.toolchain_metric == "farm:tool_info"
    for bad in (
        {"toolchain_metric": 'x{a="b"} or vector(1)'},
        {"toolchain_tool_label": "a b"},
        {"toolchain_version_label": "instance"},
        {"toolchain_tool_label": "v", "toolchain_version_label": "v"},
        {"toolchain_lookback": "10m"},
    ):
        with pytest.raises(ValueError, match="toolchain"):
            load_resources({"prometheus": {**base, **bad}})


def test_a_matrix_answer_keeps_only_well_formed_points() -> None:
    import json

    from kelpie.resources import prometheus

    body = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [
                "not a series",
                {"metric": {"tool": "x"}},  # no values
                {"metric": ["not", "a", "dict"], "values": [[1, "1"]]},
                {
                    "metric": {"tool": "x"},
                    "values": [[1, "1"], ["bad"], [2, "NaN"], [3, "nope"], [4, "4"]],
                },
            ],
        },
    }
    found = prometheus._parse(json.dumps(body).encode(), matrix=True)
    assert [(s.timestamp, s.value) for s in found] == [(1.0, 1.0), (4.0, 4.0)]


def test_a_matrix_answer_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    from kelpie.resources import prometheus

    monkeypatch.setattr(prometheus, "MAX_POINTS", 3)
    body = {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {}, "values": [[i, str(i)] for i in range(10)]}],
        },
    }
    assert len(prometheus._parse(json.dumps(body).encode(), matrix=True)) == 3
