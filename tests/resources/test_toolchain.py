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


def ts(at: datetime) -> str:
    return str(at.timestamp())


def config(prom: FakeProm, **kwargs: object) -> PrometheusConfig:
    return PrometheusConfig(url=prom.url, hosts=HOSTS, timeout=2.0, **kwargs)  # type: ignore[arg-type]


def answer(
    prom: FakeProm,
    cfg: PrometheusConfig,
    seen: list[tuple[dict[str, str], str, str, datetime, datetime]],
    tool: str = "tool",
    version: str = "version",
) -> None:
    """``seen``: (host labels, tool, version, first seen, last seen)."""
    prom.responses = {
        cfg.query_text("toolchain_first"): vector(
            *(({**h, tool: t, version: v}, ts(a)) for h, t, v, a, _ in seen)
        ),
        cfg.query_text("toolchain_last"): vector(
            *(({**h, tool: t, version: v}, ts(b)) for h, t, v, _, b in seen)
        ),
    }


def test_a_new_version_after_another_is_a_change() -> None:
    start = NOW - 48 * H + timedelta(minutes=2)  # seen since the lookback began
    with fake_prometheus() as prom:
        cfg = config(prom)
        answer(
            prom,
            cfg,
            [
                (A, "xcode", "16.0 (16A242d)", start, NOW - 11 * H),
                (A, "xcode", "16.1 (16B40)", NOW - 11 * H + timedelta(minutes=5), NOW),
                (A, "coresimulator", "1010.10", start, NOW),  # unchanged
                (B, "xcode", "16.1 (16B40)", NOW - 3 * H, NOW),  # first reading: no change
            ],
        )
        source = PrometheusSource(cfg)
        assert isinstance(source, ToolchainReader)
        source.read(NOW)
        assert source.toolchain_changes(NOW) == (
            ToolchainChange(
                "mac-a",
                "xcode",
                "16.1 (16B40)",
                NOW - 11 * H + timedelta(minutes=5),
                "16.0 (16A242d)",
            ),
        )


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
        first = cfg.query_text("toolchain_first")
        assert first == (
            "min by (instance, name, v) (min_over_time(timestamp(farm_tool_version)[21600s:300s]))"
        )
        answer(
            prom,
            cfg,
            [
                (A, "jdk", "21.0.4", NOW - 6 * H, NOW - 2 * H),
                (A, "jdk", "21.0.5", NOW - 2 * H, NOW),
            ],
            tool="name",
            version="v",
        )
        source = PrometheusSource(cfg)
        source.read(NOW)
        assert [(c.tool, c.before, c.after) for c in source.toolchain_changes(NOW)] == [
            ("jdk", "21.0.4", "21.0.5")
        ]


def test_a_failed_toolchain_query_drops_only_the_changes(caplog: pytest.LogCaptureFixture) -> None:
    with fake_prometheus() as prom:
        cfg = config(prom)
        prom.responses = {cfg.query_text("toolchain_first"): {"status": "error"}}
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
