from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Dependency,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.remote.protocol import (
    MAX_ID,
    PLATFORM_ACTIONS,
    AdapterReport,
    Outcome,
    ProtocolError,
    Result,
    WireAction,
    adapter_actions,
    decode_action,
    decode_actions,
    decode_poll,
    decode_report,
    decode_result,
    decode_snapshot,
    encode_action,
    encode_poll,
    encode_report,
    encode_result,
    encode_snapshot,
    valid_agent_name,
)
from tests import incidents
from tests.fakes import FakeAdapter, StoppingAdapter, WaitingAdapter

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
OFFERED = {
    "hermes": frozenset(
        {ActionKind.COMMENT, ActionKind.STOP, ActionKind.WAIT, ActionKind.WIP_COMMIT}
    )
}


def board() -> Snapshot:
    return Snapshot(
        now=NOW,
        tasks=(
            Task(
                "t_1a",
                "Fix login",
                Status.BLOCKED,
                NOW - timedelta(hours=2),
                role="developer",
                owner_id="coder-1",
                source="hermes",
                block=Block("waiting for CI", NOW - timedelta(hours=1), "c1"),
                estimate=timedelta(minutes=90),
                files=("a.py",),
                subtasks=("t_2b",),
            ),
        ),
        owners=(Owner("coder-1", "developer"), Owner("alice", "pm", OwnerKind.HUMAN)),
        dependencies=(Dependency("t_2b", "t_1a"),),
        conditions=(
            Condition("c1", ConditionKind.TASK_STATUS, "t_1a", {"task_id": "t_2b"}, True, NOW),
        ),
        sessions=(Session("run-1", "t_1a", "coder-1", NOW - timedelta(minutes=5)),),
        spans=(Span("tr", "sp", "bash", SpanKind.TOOL, NOW, task_id="t_1a", attributes={"k": 1}),),
    )


def test_snapshot_round_trip() -> None:
    snapshot = board()
    assert decode_snapshot(encode_snapshot(snapshot)) == snapshot


@pytest.mark.parametrize("path", incidents.all_paths(), ids=lambda p: p.stem)
def test_fixture_boards_round_trip(path) -> None:
    snapshot = incidents.load(path).snapshot
    part = replace(
        Snapshot(now=snapshot.now),
        tasks=snapshot.tasks,
        owners=snapshot.owners,
        dependencies=snapshot.dependencies,
        conditions=snapshot.conditions,
        sessions=snapshot.sessions,
        spans=snapshot.spans,
    )
    assert decode_snapshot(encode_snapshot(snapshot)) == part


def test_snapshot_leaves_out_what_agents_do_not_report() -> None:
    plain = encode_snapshot(replace(board(), budgets_ok=False))
    assert "budgets_ok" not in plain


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"tasks": []},  # no now
        {"now": NOW.isoformat(), "builds": []},  # a part only connectors give
        {"now": NOW.isoformat(), "tasks": [{"id": "t", "title": "x", "nope": 1}]},
        {"now": "2026-10-16T12:00:00", "tasks": []},  # naive time
        {"now": NOW.isoformat(), "tasks": [{"id": "t", "title": "x", "status": "zzz"}]},
    ],
)
def test_bad_snapshots_are_refused(data: object) -> None:
    with pytest.raises(ProtocolError):
        decode_snapshot(data)


def test_report_round_trip() -> None:
    parts = [
        AdapterReport("hermes", frozenset({ActionKind.STOP}), board()),
        AdapterReport("claude-code", frozenset(), error="OSError"),
    ]
    report = decode_report(encode_report("0.0.1", NOW, timedelta(seconds=15), parts, "c0ffee00", 7))
    assert report.sent_at == NOW
    assert report.interval == timedelta(seconds=15)
    assert (report.boot, report.seq) == ("c0ffee00", 7)
    assert report.adapters[0].snapshot == board()
    assert report.adapters[1].error == "OSError"
    assert report.adapters[1].snapshot is None


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"agent_version": 1},
        {"sent_at": "yesterday"},
        {"adapters": []},
        {"adapters": [{"name": "Hermes!", "actions": []}]},
        {"adapters": [{"name": "h", "actions": ["run_shell"], "error": "x"}]},
        {"adapters": [{"name": "h", "actions": [], "error": "x", "snapshot": {}}]},
        {"adapters": [{"name": "h", "actions": []}]},
        {"adapters": [{"name": "h", "actions": [], "error": "x"}] * 2},
        {"extra": True},
        {"interval": 0},
        {"interval": True},
        {
            "adapters": [
                {
                    "name": "h",
                    "actions": [],
                    "snapshot": {"now": NOW.isoformat(), "tasks": [{}] * 20_001},
                }
            ]
        },
    ],
)
def test_bad_reports_are_refused(change: dict[str, object]) -> None:
    body = encode_report(
        "0.0.1",
        NOW,
        timedelta(seconds=15),
        [AdapterReport("h", frozenset(), error="x")],
        "c0ffee00",
        1,
    )
    body.update(change)
    with pytest.raises(ProtocolError):
        decode_report(body)


def test_adapter_actions_follow_capabilities() -> None:
    base = adapter_actions(FakeAdapter())
    assert ActionKind.COMMENT in base and ActionKind.STOP not in base
    assert ActionKind.STOP in adapter_actions(StoppingAdapter())
    assert ActionKind.WAIT in adapter_actions(WaitingAdapter())
    narrowed = adapter_actions(StoppingAdapter(), [ActionKind.STOP])
    assert narrowed == frozenset({ActionKind.STOP})
    assert all(k in PLATFORM_ACTIONS for k in adapter_actions(StoppingAdapter()))


def wire(**kw: object) -> dict[str, object]:
    action = WireAction("a1", "hermes", ActionKind.COMMENT, "rule", "why", task_id="t_1a")
    return {**encode_action(action), **kw}


def test_action_round_trip_keeps_only_the_kinds_params() -> None:
    raw = wire(kind="stop", task_id=None, params={"session_id": "run-1", "cmd": "rm -rf /"})
    raw.pop("task_id")
    action = decode_action(raw, OFFERED)
    assert action.kind is ActionKind.STOP
    assert action.params == {"session_id": "run-1"}
    assert action.action().params == {"session_id": "run-1"}


def test_wip_commit_branch() -> None:
    params = {"branch": "kelpie/wip-t_1a", "draft_pr": True}
    action = decode_action(wire(kind="wip_commit", params=params), OFFERED)
    assert action.params == params


def test_adapters_declare_their_kinds() -> None:
    from kelpie.adapters.claude_code.adapter import ClaudeCodeAdapter
    from kelpie.adapters.hermes.adapter import HermesAdapter

    assert HermesAdapter.actions == {
        ActionKind.STOP,
        ActionKind.WAIT,
        ActionKind.REQUEUE,
        ActionKind.WAKE,
        ActionKind.COMMENT,
    }
    assert ClaudeCodeAdapter.actions == {ActionKind.STOP}


def test_wait_conditions_are_checked() -> None:
    conditions = [{"id": "c1", "kind": "task_status", "params": {"task_id": "t_2b"}}]
    action = decode_action(wire(kind="wait", params={"conditions": conditions}), OFFERED)
    assert action.params["conditions"] == conditions


@pytest.mark.parametrize(
    "change",
    [
        {"kind": "requeue"},  # not offered by this adapter
        {"kind": "run_shell"},
        {"adapter": "claude-code"},  # not an adapter of this agent
        {"task_id": None},
        {"task_id": "x" * (MAX_ID + 1)},
        {"task_id": "t\n1"},
        {"id": ""},
        {"rule": ""},
        {"reason": 5},
        {"params": []},
        {"kind": "stop", "params": {}},
        {"kind": "stop", "params": {"session_id": 5}},
        {"kind": "wait", "params": {}},
        {"kind": "wait", "params": {"conditions": [{"id": "c", "kind": "nope"}]}},
        {"kind": "wait", "params": {"conditions": [{"id": "c", "kind": "time", "x": 1}]}},
        {
            "kind": "wait",
            "params": {"conditions": [{"id": "c", "kind": "time", "params": {"a": [1]}}]},
        },
        {"kind": "wait", "params": {"conditions": [{"id": "c", "kind": "time"}] * 33}},
        {"command": "ls"},
        {"ttl": 0},
        {"ttl": 301},
        {"ttl": "1"},
        {"kind": "wip_commit", "params": {"branch": "--upload-pack=x"}},
        {"kind": "wip_commit", "params": {"branch": "a/../b"}},
        {"kind": "wip_commit", "params": {"draft_pr": "yes"}},
    ],
)
def test_actions_outside_the_protocol_are_refused(change: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        decode_action(wire(**change), OFFERED)


def test_reason_is_clipped_and_control_characters_dropped() -> None:
    action = decode_action(wire(reason="a\x1b[31m" + "b" * 9000), OFFERED)
    assert "\x1b" not in action.reason
    assert len(action.reason) == 4096


def test_results_and_polls_round_trip() -> None:
    result = Result("a1", Outcome.DONE, "the worker exits within 2 minutes")
    assert decode_result(encode_result(result)) == result
    results, wait = decode_poll(encode_poll([result], 20))
    assert results == [result] and wait == 20
    with pytest.raises(ProtocolError):
        decode_poll(encode_poll([], 60))
    with pytest.raises(ProtocolError):
        decode_result({"id": "a1", "outcome": "exploded"})
    with pytest.raises(ProtocolError):
        decode_actions({"actions": [{}] * 65}, OFFERED)


@pytest.mark.parametrize(
    ("name", "valid"),
    [("laptop", True), ("vm-2", True), ("a", True), ("-a", False), ("A", False), ("a:b", False)],
)
def test_agent_names(name: str, valid: bool) -> None:
    assert valid_agent_name(name) is valid


def task(**kw: object) -> dict[str, object]:
    return {"id": "t", "title": "x", "status": "ready", "status_since": NOW.isoformat(), **kw}


@pytest.mark.parametrize(
    "data",
    [
        {"tasks": [task(id=["unhashable"])]},
        {"tasks": [task(review_rounds="abc")]},
        {"tasks": [task(review_rounds=True)]},
        {"tasks": [task(id="x" * 5006)]},
        {"tasks": [task(id="t\n\x1b[2J")]},
        {"tasks": [task(estimate=1e999)]},
        {"tasks": [task(files=[1, 2])]},
        {"tasks": [task(files="abc")]},
        {"tasks": [task(status_since="9999-12-31T23:59:59+00:00")]},
        {"tasks": [task(status_since="0001-01-01T00:00:00+14:00")]},
        {"tasks": [task(estimate=8.6e13)]},
        {"tasks": [task(estimate=-5)]},
        {"tasks": [task(id="t\ud800")]},
        {"tasks": [task(), task()]},
        {"conditions": [{"id": "c", "kind": "custom", "params": {"a": float("nan")}}]},
        {
            "conditions": [
                {
                    "id": "c",
                    "kind": "custom",
                    "params": {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": {}}}}}}}}},
                }
            ]
        },
        {
            "spans": [
                {
                    "trace_id": "t",
                    "span_id": "s",
                    "name": "n",
                    "kind": "tool",
                    "start": NOW.isoformat(),
                    "input_tokens": 1.5,
                }
            ]
        },
    ],
)
def test_snapshot_fields_are_checked(data: dict[str, object]) -> None:
    with pytest.raises(ProtocolError):
        decode_snapshot({"now": NOW.isoformat(), **data})


def test_free_text_is_cleaned_not_refused() -> None:
    snap = decode_snapshot({"now": NOW.isoformat(), "tasks": [task(title="a\x1b[2Jb\nc\udfff")]})
    assert snap.tasks[0].title == "a [2Jb\nc "
    snap = decode_snapshot(
        {"now": "2026-10-16T14:00:00+02:00", "tasks": [task(status_since=NOW.isoformat())]}
    )
    assert snap.now == NOW and snap.now.tzinfo is UTC


@pytest.mark.parametrize(
    ("branch", "ok"),
    [
        ("kelpie/wip-t_1a", True),
        ("feature/a.b", True),
        ("a/.b", False),
        ("a.lock/b", False),
        ("a//b", False),
        ("a@{1}", False),
        ("a.", False),
        ("-x", False),
    ],
)
def test_branch_names(branch: str, ok: bool) -> None:
    raw = wire(kind="wip_commit", params={"branch": branch})
    if ok:
        assert decode_action(raw, OFFERED).params["branch"] == branch
    else:
        with pytest.raises(ProtocolError):
            decode_action(raw, OFFERED)


def test_agent_version_is_printable() -> None:
    body = encode_report("1.0\x1b[31m\nFAKE", NOW, timedelta(seconds=15), [], "c0ffee00", 1)
    body["adapters"] = [{"name": "h", "actions": [], "error": "x"}]
    with pytest.raises(ProtocolError, match="agent_version"):
        decode_report(body)


def test_unhashable_kinds_are_a_protocol_error() -> None:
    with pytest.raises(ProtocolError):
        decode_action(wire(kind=["stop"]), OFFERED)
    body = encode_report("0.0.1", NOW, timedelta(seconds=15), [], "c0ffee00", 1)
    body["adapters"] = [{"name": "h", "actions": [["stop"]], "error": "x"}]
    with pytest.raises(ProtocolError):
        decode_report(body)
