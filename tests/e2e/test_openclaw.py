"""Rule 10 (``loop_breaker``) stops a runaway loop on an OpenClaw Gateway, end to end.

The spool is written by the real hook script and the board is a Workboard database (see
:mod:`tests.sim.openclaw`); ``kelpie serve`` runs the real OpenClaw adapter, which writes the
stop marker and aborts the run through a fake ``openclaw`` CLI. When Node is installed, the
real plugin (``index.js``) then reads the markers and must refuse the stopped session's next
tool call, and only that session's. Without Node that part is skipped, except on CI, where
it fails.
"""

from __future__ import annotations

import json
import os

import pytest

from kelpie.adapters.openclaw.stop import ENV_EXACT, ENV_PREFIXES
from tests.sim.farm import Farm
from tests.sim.kelpie import wait_for
from tests.sim.openclaw import Gateway

ACT = {"mode": "act"}
LOOP, HEALTHY = "5b0c7a1e-loop-4f7e-9d1a-000000000001", "5b0c7a1e-okay-4f7e-9d1a-000000000002"
LOOP_KEY = "agent:main:subagent:workboard-default-c1"
HEALTHY_KEY = "agent:main:subagent:workboard-default-c2"
PARAMS = {"key": LOOP_KEY, "clearQueued": True}
HELD = (
    "every tool call and every run but the operator's message is refused until the operator "
    "messages it, the session is reset or the stop expires"
)


@pytest.fixture
def gateway(farm: Farm) -> Gateway:
    return Gateway(farm.root)


def runaway_gateway(farm: Farm, gw: Gateway) -> None:
    """Card c1's session repeats one call every 30 seconds, over several heartbeat runs, until
    now (the last run still going); card c2's session works normally."""
    gw.board.card(
        "c1",
        "running",
        farm.ago(hours=3),
        title="Nightly inbox sweep",
        agent="main",
        session_key=LOOP_KEY,
        started_at=farm.ago(hours=2),
    )
    gw.board.card(
        "c2",
        "running",
        farm.ago(hours=1),
        title="Summarise the changelog",
        agent="main",
        session_key=HEALTHY_KEY,
        started_at=farm.ago(minutes=30),
    )
    calls = 20
    for i in range(calls):
        at = farm.ago(seconds=30 * (calls - i))
        run = f"hb-{i // 5}"  # a heartbeat run every 2.5 minutes, each repeating the call
        gw.tool_call(
            LOOP, LOOP_KEY, run, "read", {"path": "inbox/status.md"}, at=at, call_id=f"call-{i}"
        )
        if i % 5 == 4 and i < calls - 1:
            gw.run_end(LOOP, LOOP_KEY, run, at=at)
    for i, tool in enumerate(["read", "edit", "exec", "read", "edit"]):
        gw.tool_call(
            HEALTHY,
            HEALTHY_KEY,
            "work-1",
            tool,
            {"step": i},
            at=farm.ago(minutes=9 - 2 * i),
            call_id=f"ok-{i}",
        )


def config(farm: Farm, gw: Gateway) -> dict[str, object]:
    return farm.config({"loop_breaker": ACT}, extra={"adapters": {"openclaw": gw.adapter_config()}})


def plugin_enforces_the_stop(gw: Gateway) -> None:
    """The real plugin reads the marker: the stopped session's next tool call is refused and
    recorded, the healthy session's is not."""
    refused = gw.plugin_refuses(LOOP, HEALTHY)
    if refused is None:
        if os.environ.get("CI"):
            pytest.fail("node is not installed: the plugin's side is not checked")
        pytest.skip("node is not installed: the plugin's side is not checked")
    assert refused == [True, False]
    stopped = [x for x in gw.plugin_spool() if x.get("type") == "stopped"]
    assert [(x["session_id"], x["event"]) for x in stopped] == [(LOOP, "before_tool_call")]


def test_runaway_loop_is_stopped_on_an_openclaw_session(farm: Farm, gateway: Gateway) -> None:
    runaway_gateway(farm, gateway)
    kelpie = farm.serve(config(farm, gateway))

    message = farm.wait_message("loop_breaker", "c1", "identical")
    wait_for("the abort", lambda: any("sessions.abort" in c["argv"] for c in gateway.calls()))
    kelpie.wait_ticks(3)  # a stopped session must not be stopped again
    farm.stop()

    assert message.startswith("\U0001f534 Runaway run: ") and " · c1" in message
    did = f"Kelpie did: Stopped run {LOOP}; its current run was aborted; {HELD}."
    assert message.splitlines()[-2] == did
    (abort,) = gateway.calls()
    assert abort["argv"] == [
        "gateway",
        "call",
        "sessions.abort",
        "--params",
        json.dumps(PARAMS),
        "--json",
    ]
    assert all(k in ENV_EXACT or k.startswith(ENV_PREFIXES) for k in abort["env"])
    markers = gateway.markers()
    assert set(markers) == {LOOP}
    assert markers[LOOP]["key"] == LOOP_KEY
    assert "identical" in markers[LOOP]["reason"]
    assert farm.messages("c2") == []
    assert len(farm.messages("loop_breaker", "c1")) == 1
    slack = farm.channels.slack_texts()
    assert sum(t.startswith("\U0001f534") and "c1" in t for t in slack) == 1
    plugin_enforces_the_stop(gateway)


def test_an_unreachable_gateway_still_leaves_the_stop_marker(farm: Farm, gateway: Gateway) -> None:
    """The abort fails (the Gateway is down): the human is told the run was not aborted, and
    the marker refuses the session's tool calls once the plugin runs again."""
    runaway_gateway(farm, gateway)
    gateway.unreachable()
    kelpie = farm.serve(config(farm, gateway))

    message = farm.wait_message("loop_breaker", "c1", "identical")
    kelpie.wait_ticks(3)
    farm.stop()

    assert message.splitlines()[-2] == (
        f"Kelpie did: Stopped run {LOOP}; {HELD}; its current run could not be aborted (the "
        "Gateway refused or did not answer (exit 1))."
    )
    assert len(farm.messages("loop_breaker", "c1")) == 1
    # The key is refused, so the run id is tried next; neither is retried on later ticks.
    assert [json.loads(c["argv"][4]) for c in gateway.calls()] == [
        PARAMS,
        {"runId": "hb-3", "clearQueued": True},
    ]
    assert any("cannot abort the run of session" in line for line in kelpie.log_lines())
    assert set(gateway.markers()) == {LOOP}
    plugin_enforces_the_stop(gateway)
