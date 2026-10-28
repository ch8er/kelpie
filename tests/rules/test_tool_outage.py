"""Rule 10, correlated tool failures: one outage (#229, signal 2, incident 20)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action
from kelpie.core.incidents import TOOL_FAILURES, Outage, is_outage_state
from kelpie.core.model import Session, Snapshot, Span, SpanKind
from kelpie.core.tools import (
    COMMAND_COMPLETE_ATTRIBUTE,
    COMMAND_WHOLE_ATTRIBUTE,
    EXIT_CODE_ATTRIBUTE,
    RESULT_ATTRIBUTE,
)
from kelpie.rules._tool_outage import tool_outage_state
from kelpie.rules.loop_breaker import NAME, LoopBreaker, build

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
RULE = LoopBreaker(tool_outage_tools=("ios_sim.sh", "xcodebuild"))
STATE = tool_outage_state(NAME, "ios_sim.sh", 127)


def call(
    task: str | None,
    minutes_ago: float,
    code: int | None = 127,
    *,
    command: str = "scripts/ios_sim.sh test",
    result: str | None = None,
    key: str = "command",
    whole: bool = True,
    complete: bool = True,
    **fields: Any,
) -> Span:
    at = NOW - minutes_ago * M
    attributes: dict[str, Any] = {key: command}
    if whole:
        attributes[COMMAND_WHOLE_ATTRIBUTE] = True
    elif complete:
        attributes[COMMAND_COMPLETE_ATTRIBUTE] = True
    if code is not None:
        attributes[EXIT_CODE_ATTRIBUTE] = code
    attributes[RESULT_ATTRIBUTE] = result or ("ok" if code == 0 else "error")
    base: dict[str, Any] = {
        "trace_id": f"TR-{task}",
        "task_id": task,
        "session_id": f"S-{task}",
        "end": at,
    }
    return Span(
        span_id=f"SP-{task}-{minutes_ago:g}",
        name="execute_tool terminal",
        kind=SpanKind.TOOL,
        start=at - 0.1 * M,
        tool="terminal",
        attributes=attributes,
        **{**base, **fields},
    )


THREE = (call("T-1", 50), call("T-2", 40), call("T-3", 30))


def found(*spans: Span, rule: LoopBreaker = RULE, **fields: Any) -> list[Action]:
    actions = rule.evaluate(Snapshot(now=NOW, spans=spans, **fields))
    return [a for a in actions if is_outage_state(a.state or "")]


def test_three_tasks_failing_the_same_way_are_one_outage() -> None:
    [alert] = found(*THREE, call("T-1", 20))
    outage = Outage.from_params(alert.params)
    assert outage is not None and outage.kind == TOOL_FAILURES
    assert (alert.state, alert.key, outage.state) == (STATE, STATE, STATE)
    assert outage.signature == "ios_sim.sh exit 127" and outage.tasks == ("T-1", "T-2", "T-3")
    assert alert.since == NOW - 50 * M and alert.observe_only
    assert "ios_sim.sh exits 127 (not found) for 3 tasks, 4 run(s)" in alert.reason
    assert alert.params["headline"] == "Tool outage: ios_sim.sh exits 127 for 3 tasks"


def test_one_task_retrying_or_tasks_too_far_apart_are_no_outage() -> None:
    assert found(*(call("T-1", m) for m in (50, 40, 30, 20))) == []
    assert found(call("T-1", 120), call("T-2", 80), call("T-3", 40)) == []  # 30m apart
    assert found(*THREE[:2]) == []


def test_each_exit_code_is_its_own_outage() -> None:
    codes = [call(t, m, 1) for t, m in (("T-4", 45), ("T-5", 35), ("T-6", 25))]
    alerts = found(*THREE, *codes)
    assert sorted(a.params["signature"] for a in alerts) == [
        "ios_sim.sh exit 1",
        "ios_sim.sh exit 127",
    ]
    mixed = (call("T-1", 50), call("T-2", 40, 1), call("T-3", 30, 65))
    assert found(*mixed) == []  # three failures, three classes


def test_a_success_since_ends_it_and_a_success_before_does_not() -> None:
    assert found(*THREE, call("T-9", 10, 0)) == []
    [alert] = found(call("T-9", 70, 0), *THREE)
    assert alert.since == NOW - 50 * M
    # Failures before the success do not count toward a new one.
    assert found(call("T-1", 80), call("T-2", 75), call("T-9", 70, 0), call("T-3", 60)) == []


def test_background_blocked_and_unknown_calls_are_neither() -> None:
    background = call("T-4", 20, 0, result="unknown")  # 0 means started
    blocked = call("T-5", 15, -1, result="blocked")
    no_code = call("T-6", 10, None, result="error")  # a failure without its class
    assert len(found(*THREE, background, blocked, no_code)) == 1
    assert found(*THREE[:2], blocked, no_code) == []
    # A background success is no success of the build: the outage stays open.
    [alert] = found(*THREE, background)
    assert "T-4" not in alert.reason


def test_only_configured_tools_by_a_whole_word_of_the_command() -> None:
    assert found(*THREE, rule=LoopBreaker()) == []  # no tools: off
    renamed = [call(t, m, command="scripts/ios_sim.sh.bak test") for t, m in (("T-1", 50),)]
    assert found(*renamed, *THREE[1:]) == []
    otlp = [call(t, m, key="kelpie.tool.command_prefix") for t, m in (("T-1", 50),)]
    assert len(found(*otlp, *THREE[1:])) == 1
    other = call("T-1", 50, command="make test")
    assert found(other, *THREE[1:]) == []
    xcode = [call(t, m, 65, command="cd app && xcodebuild test") for t, m in (("T-1", 50),)]
    xcode += [call("T-2", 40, 65, command="/usr/bin/xcodebuild"), call("T-3", 30, 65)]
    assert found(*xcode) == []  # T-3's command is ios_sim.sh
    xcode[2] = call("T-3", 30, 65, command="xcodebuild -scheme App")
    assert [a.params["signature"] for a in found(*xcode)] == ["xcodebuild exit 65"]


def test_a_session_counts_as_its_task_or_as_one_task() -> None:
    # Hermes' spans carry no task: the snapshot's sessions tie them to one.
    one = [call(None, m, session_id="S-a") for m in (50, 40)] + [call(None, 30, session_id="S-b")]
    assert found(*one) == []  # two sessions
    sessions = (Session("S-a", "T-1", "dev-1", NOW - 60 * M),)
    three = [*one, call(None, 20, session_id="S-c")]
    [alert] = found(*three, sessions=sessions)
    assert alert.params["tasks"] == ["T-1"]  # S-b and S-c are tied to no task
    assert "for 3 tasks" in alert.reason


def test_the_resolved_notice_names_the_success() -> None:
    snapshot = Snapshot(now=NOW, spans=(*THREE, call("T-9", 5, 0)))
    assert RULE.resolved(STATE, snapshot) == (
        "ios_sim.sh ran successfully again at 2026-03-02 11:55 UTC in T-9."
    )
    other = tool_outage_state(NAME, "xcodebuild", 65)
    assert RULE.resolved(other, snapshot) is None  # not this tool's outage
    assert RULE.resolved(STATE, Snapshot(now=NOW, spans=THREE)) is None  # no success


def test_switched_on_it_follows_the_rule_s_mode() -> None:
    rule = LoopBreaker(tool_outage_tools=("ios_sim.sh",), tool_outage_mode="rule")
    [alert] = found(*THREE, rule=rule)
    assert not alert.observe_only


def test_config() -> None:
    rule = build(
        {
            "tool_outage_tools": ["ios_sim.sh"],
            "tool_outage_min_tasks": 2,
            "tool_outage_window": "10m",
            "tool_outage_mode": "rule",
        }
    )
    assert rule.tool_outage_tools == ("ios_sim.sh",) and rule.tool_outage_min_tasks == 2
    assert rule.tool_outage_window == 10 * M and rule.tool_outage_mode == "rule"
    default = LoopBreaker()
    assert default.tool_outage_tools == () and default.tool_outage_mode == "observe"
    for bad in (
        {"tool_outage_tools": "ios_sim.sh"},
        {"tool_outage_tools": [""]},
        {"tool_outage_min_tasks": 1},
        {"tool_outage_min_tasks": True},
        {"tool_outage_window": "0s"},
        {"tool_outage_mode": "act"},
    ):
        with pytest.raises(ValueError):
            build(bad)


def test_through_the_engine_the_notice_names_the_recovery() -> None:
    from kelpie.core.config import EngineConfig, Mode
    from kelpie.core.engine import Engine
    from tests.fakes import FakeAdapter, FakeAlerter

    alerter = FakeAlerter()
    config = EngineConfig(dry_run=False, default_mode=Mode.SUGGEST)
    rule = LoopBreaker(tool_outage_tools=("ios_sim.sh",), tool_outage_mode="rule")
    engine = Engine([rule], adapters=[FakeAdapter()], alerter=alerter, config=config)  # type: ignore[list-item]
    engine.tick(lambda: Snapshot(now=NOW, spans=THREE))
    later = NOW + 30 * M
    fixed = call("T-9", -20, 0)
    engine.tick(lambda: Snapshot(now=later, spans=(*THREE, fixed)))
    [opened] = [e for e in alerter.events if e.state == STATE and e.transition.value == "new"]
    assert "Tool outage" in opened.reason
    [notice] = [e for e in alerter.events if e.state == STATE and e.transition.value == "resolved"]
    assert "ios_sim.sh ran successfully again at 2026-03-02 12:20 UTC in T-9." in notice.reason


def three(command: str, code: int = 127) -> list[Span]:
    return [call(t, m, code, command=command) for t, m in (("T-1", 50), ("T-2", 40), ("T-3", 30))]


@pytest.mark.parametrize(
    "command",
    [
        "ios_sim.sh test",  # as the redaction keeps scripts/ios_sim.sh test
        "cd *** && ios_sim.sh build",
        "set *** ; ios_sim.sh test",
        "FOO=*** ios_sim.sh",
        "env FOO=*** ios_sim.sh test",
        "time -p ios_sim.sh test",
        "sudo -u *** ios_sim.sh test",
        "nice -n *** ios_sim.sh test",
        "rtk ios_sim.sh test",
        "ios_sim.sh -s*** *** ***",  # a failure counts though a pipe may be masked
        "cat *** | ios_sim.sh",  # the last of a pipeline: its own exit code
    ],
)
def test_the_tool_s_own_exit_code(command: str) -> None:
    assert [a.params["signature"] for a in found(*three(command))] == ["ios_sim.sh exit 127"]


@pytest.mark.parametrize(
    "command",
    [
        "pkill ios_sim.sh",  # not the command
        "which ios_sim.sh",
        "command -v ios_sim.sh",  # exits 0 wherever the binary is
        "command -pv ios_sim.sh",
        "command -p -V ios_sim.sh",
        "lint.sh && ios_sim.sh test",  # which of them failed?
        "ios_sim.sh test || true",
        "ios_sim.sh test | xcbeautify",  # xcbeautify's exit code
        "ios_sim.sh test ; echo done",
        "ios_sim.sh & wait",
        "bash *** ***",  # masked whole: no tool
        "timeout 600 *** ***",
        "",
    ],
)
def test_not_the_tool_s_own_exit_code(command: str) -> None:
    assert found(*three(command)) == []


def test_a_success_counts_only_when_nothing_after_the_tool_was_masked() -> None:
    hidden = call("T-9", 10, 0, command="ios_sim.sh -s*** *** ***")  # | xcbeautify?
    assert len(found(*THREE, hidden)) == 1
    # The redaction kept a prefix: "xcodebuild clean build test | xcbeautify" reads as
    # "xcodebuild clean build test", and only the adapter knows the pipe was dropped.
    cut = call("T-9", 10, 0, command="ios_sim.sh test", whole=False)
    assert len(found(*THREE, cut)) == 1
    assert found(*THREE, call("T-9", 10, 0, command="ios_sim.sh test")) == []


def test_each_host_has_its_own_outage() -> None:
    from kelpie.core.model import Owner, Status, Task

    def task(tid: str, host: str | None) -> Task:
        return Task(tid, tid, Status.IN_PROGRESS, NOW - 90 * M, owner_id="dev-1", host=host)

    tasks = (*(task(t, "mac-a") for t in ("T-1", "T-2", "T-3")), task("T-9", "mac-b"))
    owners = (Owner("dev-1", "dev"),)
    healthy = call("T-9", 10, 0)
    [alert] = found(*THREE, healthy, tasks=tasks, owners=owners)
    assert alert.params["signature"] == "ios_sim.sh exit 127 on mac-a"
    assert alert.state == tool_outage_state(NAME, "ios_sim.sh", 127, "mac-a")
    assert "on mac-a for 3 tasks" in alert.reason
    # A task the snapshot does not hold (archived): its host is unknown, and on a farm
    # whose tasks name hosts it may be the healthy one: it ends no host's outage.
    unknown = call("T-8", 10, 0)
    assert len(found(*THREE, unknown, tasks=tasks, owners=owners)) == 1
    fixed = call("T-1", 5, 0)  # on mac-a itself
    snapshot = Snapshot(now=NOW, spans=(*THREE, healthy, fixed), tasks=tasks, owners=owners)
    assert found(*snapshot.spans, tasks=tasks, owners=owners) == []
    assert RULE.resolved(alert.state or "", snapshot) == (
        "ios_sim.sh ran successfully again at 2026-03-02 11:55 UTC in T-1."
    )
    assert RULE.resolved(alert.state or "", Snapshot(now=NOW, spans=(*THREE, healthy))) is None
    # On a farm whose tasks name no host, any success counts.
    assert found(*THREE, unknown) == []


def test_calls_without_a_task_or_session_count_per_trace() -> None:
    one = [call(None, m, session_id=None, trace_id="TR-x") for m in (50, 40, 30)]
    assert found(*one) == []  # one trace retrying
    many = [call(None, m, session_id=None, trace_id=f"TR-{m}") for m in (50, 40, 30)]
    [alert] = found(*many, Span("TR-0", "SP-0", "chat", SpanKind.LLM, NOW))  # not a tool span
    assert alert.params["tasks"] == []
    assert alert.reason.endswith("not the runs (no task is known).")


def test_an_error_without_a_failing_exit_code_is_not_grouped() -> None:
    zero = [call(t, m, 0, result="error") for t, m in (("T-1", 50), ("T-2", 40), ("T-3", 30))]
    assert found(*zero) == []


def test_a_command_of_more_lines_is_none() -> None:
    from kelpie.core.tools import COMMAND_MULTILINE_ATTRIBUTE

    spans = three("ios_sim.sh test")
    lines = [
        replace(sp, attributes={**sp.attributes, COMMAND_MULTILINE_ATTRIBUTE: True}) for sp in spans
    ]
    assert found(*lines) == []  # the second line's 127, e.g. a missing swiftlint


def test_a_task_that_left_the_snapshot_keeps_its_host_by_namespace() -> None:
    from kelpie.core.model import Owner, Status, Task

    owners = (Owner("mac-a:dev-1", "dev", host="mac-a"),)
    live = (Task("mac-a:T-4", "T-4", Status.IN_PROGRESS, NOW - 90 * M, host="mac-a"),)
    archived = [
        call(f"mac-a:T-{n}", m, session_id=f"mac-a:S-{n}") for n, m in ((1, 50), (2, 40), (3, 30))
    ]
    [alert] = found(*archived, tasks=live, owners=owners)
    assert alert.params["signature"] == "ios_sim.sh exit 127 on mac-a"
    assert found(*archived, call("mac-a:T-4", 10, 0), tasks=live, owners=owners) == []


def test_a_failure_counts_only_when_the_command_reaches_its_end() -> None:
    masked = three("xcodebuild -s*** *** ***", 65)  # the scheme's value is masked
    masked = [
        replace(sp, attributes={**sp.attributes, COMMAND_WHOLE_ATTRIBUTE: False}) for sp in masked
    ]
    masked = [
        replace(sp, attributes={**sp.attributes, COMMAND_COMPLETE_ATTRIBUTE: True}) for sp in masked
    ]
    rule = LoopBreaker(tool_outage_tools=("xcodebuild",))
    assert [a.params["signature"] for a in found(*masked, rule=rule)] == ["xcodebuild exit 65"]
    # "xcodebuild clean build test && swiftlint": the redaction kept four words, swiftlint
    # may be the 127.
    cut = [call(t, m, command="ios_sim.sh test", whole=False, complete=False)
           for t, m in (("T-1", 50), ("T-2", 40), ("T-3", 30))]  # fmt: skip
    assert found(*cut) == []
