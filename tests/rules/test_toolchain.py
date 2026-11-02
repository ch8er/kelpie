"""A toolchain change as the likely cause of an outage (incident 20, #284): every outage
kind names it, within the window only, preferring the outage's own host."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import Action, ActionKind
from kelpie.core.incidents import Outage, outage_state_id
from kelpie.core.model import Snapshot, ToolchainChange
from kelpie.rules import REGISTRY
from kelpie.rules._toolchain import SLACK, describe, likely_change, with_change
from tests.incidents import FIXTURES, load

H = timedelta(hours=1)
T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)  # an outage begins


def change(at: datetime, host: str = "mac-1", tool: str = "xcode") -> ToolchainChange:
    return ToolchainChange(host, tool, "16.1", at, "16.0")


def snap(*changes: ToolchainChange) -> Snapshot:
    return Snapshot(now=T0 + H, toolchain_changes=changes)


@pytest.mark.parametrize(
    ("at", "named"),
    [
        (T0 - 6 * H, True),  # the window's edge
        (T0 - 6 * H - timedelta(minutes=1), False),
        (T0 + SLACK, True),  # first seen at the next sample after the outage began
        (T0 + SLACK + timedelta(minutes=1), False),
    ],
)
def test_the_window_around_the_start(at: datetime, named: bool) -> None:
    assert (likely_change(snap(change(at)), T0, 6 * H) is not None) is named


def test_the_latest_change_wins_and_the_outages_host_first() -> None:
    near, far = change(T0 - H, "mac-2"), change(T0 - 3 * H, "mac-1")
    assert likely_change(snap(near, far), T0, 6 * H) == near
    assert likely_change(snap(near, far), T0, 6 * H, hosts=("mac-1",)) == far
    # no change on its own host: any host's (CI may run on the farm's Macs)
    assert likely_change(snap(near), T0, 6 * H, hosts=("mac-9",)) == near
    assert likely_change(snap(near), T0, timedelta(0)) is None


def test_the_line_says_what_where_and_when() -> None:
    c = ToolchainChange("mac-1", "coresimulator", "1010.10", T0 - 150 * timedelta(minutes=1))
    assert describe(c, T0) == (
        "coresimulator changed to 1010.10 on mac-1 at 2026-03-02 07:30 UTC, 2.5 h before it began"
    )
    assert describe(change(T0 + timedelta(minutes=5)), T0).endswith("5 min after it began")


def test_no_change_leaves_the_alert_as_it_was() -> None:
    alert = Action(ActionKind.ALERT, "x", "Outage.", params={"toolchain": None})
    assert with_change(alert, snap(), T0, 6 * H) is alert
    named = with_change(alert, snap(change(T0 - H)), T0, 6 * H)
    assert named.reason.startswith("Outage. Likely cause: xcode changed from 16.0 to 16.1")
    assert named.params["toolchain"].startswith("xcode changed")


def with_changes(name: str, *changes: ToolchainChange) -> tuple[list[Action], Snapshot]:
    incident = load(FIXTURES / f"{name}.yaml")
    rule = REGISTRY[incident.rule](incident.config)
    snapshot = replace(incident.snapshot, toolchain_changes=changes)
    return list(rule.evaluate(snapshot)), snapshot


@pytest.mark.parametrize(
    ("fixture", "began"),
    [
        ("20-ci-outage", None),
        ("20-ci-not-started", None),
        ("20-tool-outage", datetime(2026, 3, 2, 10, 5, tzinfo=UTC)),
        ("20-block-storm", datetime(2026, 3, 2, 10, 0, tzinfo=UTC)),
    ],
)
def test_every_outage_kind_names_the_change(fixture: str, began: datetime | None) -> None:
    plain, _ = with_changes(fixture)
    outages = [a for a in plain if Outage.from_params(a.params) is not None]
    assert outages
    since = began or min(a.since for a in outages if a.since is not None)
    found, _ = with_changes(fixture, change(since - 30 * timedelta(minutes=1)))
    named = [a for a in found if Outage.from_params(a.params) is not None]
    assert len(named) == len(outages)
    for a in named:
        outage = Outage.from_params(a.params)
        assert outage is not None and outage.toolchain is not None
        assert "Likely cause: xcode changed from 16.0 to 16.1 on mac-1" in a.reason
    # a change long before: no cause named, the alerts as before
    old, _ = with_changes(fixture, change(since - 7 * H))
    assert [a.reason for a in old] == [a.reason for a in plain]


@pytest.mark.parametrize("rule", ["stale_blocks", "ci_economy", "loop_breaker"])
def test_toolchain_window_is_config(rule: str) -> None:
    assert REGISTRY[rule]({"toolchain_window": "2h"}).toolchain_window == 2 * H  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="toolchain_window"):
        REGISTRY[rule]({"toolchain_window": "0s"})


def test_a_change_alone_raises_nothing() -> None:
    for rule in ("stale_blocks", "ci_economy", "loop_breaker"):
        assert list(REGISTRY[rule]({}).evaluate(snap(change(T0)))) == []


def test_outage_params_carry_it_and_old_entries_read_without_it() -> None:
    state = outage_state_id(None, "build", None)
    o = Outage(state=state, signature="x", job="build", toolchain="xcode ...")
    assert Outage.from_params(o.to_params()) == o
    params = o.to_params()
    del params["toolchain"]
    found = Outage.from_params(params)
    assert found is not None and found.toolchain is None
