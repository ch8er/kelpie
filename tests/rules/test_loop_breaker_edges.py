"""Edges of ``loop_breaker`` (rule 10) that mutation testing found untested: a session left
out must not end the scan, thresholds are inclusive where the docs say "within", the error
streak is measured from its first to its last call, host sleeps count for liveness, and an
unverified streak only alerts. Tests only: the rule's code is unchanged."""

from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind
from kelpie.core.alerts import ANNOUNCES
from kelpie.core.model import HostSleep, Session, Snapshot, Span, SpanKind, Status, Task
from kelpie.core.spans import UNVERIFIED_ATTRIBUTE
from kelpie.rules.loop_breaker import build

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
S = timedelta(seconds=1)
M = timedelta(minutes=1)
H = timedelta(hours=1)


def session(sid: str = "S-1", **kw: Any) -> Session:
    return Session(
        sid,
        kw.pop("task_id", "T-1"),
        "agent-1",
        NOW - 5 * H,
        last_heartbeat=kw.pop("last_heartbeat", NOW - M),
        **kw,
    )


def call(at: datetime, i: int, sid: str = "S-1", **kw: Any) -> Span:
    attrs = dict(kw.pop("attributes", {}))
    if command := kw.pop("command", None):
        attrs["command"] = command
    return Span(
        "TR",
        f"{sid}-{i}",
        "execute_tool bash",
        SpanKind.TOOL,
        at,
        at + S,
        session_id=sid,
        tool="bash",
        args_hash=kw.pop("args", "a"),
        outcome=kw.pop("outcome", "ok"),
        attributes=attrs,
    )


def calls(times: list[datetime], **kw: Any) -> list[Span]:
    return [call(at, i, **kw) for i, at in enumerate(times)]


def evaluate(
    spans: list[Span], sessions: tuple[Session, ...] | None = None, **cfg: Any
) -> list[Action]:
    snap = Snapshot(
        now=NOW,
        tasks=(
            Task("T-1", "t", Status.IN_PROGRESS, NOW - H),
            Task("T-2", "t", Status.IN_PROGRESS, NOW - H),
        ),
        sessions=sessions if sessions is not None else (session(),),
        spans=tuple(spans),
        host_sleeps=cfg.pop("host_sleeps", ()),
    )
    return list(build(cfg).evaluate(snap))


def stopped(actions: list[Action]) -> list[str]:
    return [a.params["session_id"] for a in actions if a.kind is ActionKind.STOP]


def every_minute(n: int, last: datetime = NOW - M) -> list[datetime]:
    return [last - (n - 1 - i) * M for i in range(n)]


def test_a_session_left_out_does_not_end_the_scan() -> None:
    """An ended session and one without calls come first; the looping one is still stopped."""
    sessions = (
        session("S-0", ended_at=NOW - H),
        session("S-9", task_id="T-2"),
        session("S-1"),
    )
    spans = calls(every_minute(8), sid="S-1") + calls(every_minute(8), sid="S-0")
    assert stopped(evaluate(spans, sessions)) == ["S-1"]


def test_a_session_whose_host_slept_is_still_live() -> None:
    """Last heard 40 minutes ago, 35 of them asleep: live, and its loop is stopped."""
    s = session(last_heartbeat=NOW - 40 * M)
    sleep = HostSleep(NOW - 39 * M, NOW - 4 * M)
    spans = calls(every_minute(8, NOW - 2 * M))
    assert stopped(evaluate(spans, (s,), host_sleeps=(sleep,))) == ["S-1"]
    assert stopped(evaluate(spans, (s,))) == []  # without the sleep: stale, left alone


def test_identical_calls_within_the_window_inclusive() -> None:
    """The streak's last call exactly ``identical_window`` ago, and the streak exactly
    ``identical_window`` long, still count."""
    window = 10 * M
    last = NOW - window
    spans = calls([last - window + i * window / 7 for i in range(8)])
    assert stopped(evaluate(spans, identical_window="10m")) == ["S-1"]
    assert stopped(evaluate(calls([t - S for t in (s.start for s in spans)]))) == []


def test_error_streak_measured_from_its_last_error() -> None:
    """Four old errors and a fresh one: the streak is live because its last error is."""
    times = [NOW - 20 * M + i * M for i in range(4)] + [NOW - M]
    actions = evaluate(calls(times, outcome="error", args=None))
    [alert] = [
        a
        for a in actions
        if a.params.get("trigger") == "error_streak" and a.kind is ActionKind.ALERT
    ]
    assert alert.since == times[0]
    assert stopped(actions) == ["S-1"]


def test_error_window_inclusive() -> None:
    last = NOW - 10 * M
    times = [last - 4 * M + i * M for i in range(5)]
    assert stopped(evaluate(calls(times, outcome="error", args=None))) == ["S-1"]
    assert stopped(evaluate(calls([t - S for t in times], outcome="error", args=None))) == []


def test_error_span_from_first_to_last_inclusive() -> None:
    """Five errors exactly ``min_error_span`` (60 s) from first to last are a streak, even
    when the middle ones came in a burst; 59 s from first to last are not."""
    start = NOW - 2 * M
    offsets = [0, 2, 3, 4, 60]  # first to last: exactly min_error_span
    times = [start + o * S for o in offsets]
    assert stopped(evaluate(calls(times, outcome="error", args=None))) == ["S-1"]
    times = [start + o * S for o in [0, 2, 3, 4, 59]]
    assert stopped(evaluate(calls(times, outcome="error", args=None))) == []


POLLING = {
    "identical_calls": 3,
    "identical_window": "1h",
    "poll_comment_after": 3,
    "poll_grace": "10m",
    "poll_patterns": {"ci_status": ["gh pr checks"]},
}


def test_polling_stops_once_the_grace_after_the_coaching_point_has_passed() -> None:
    """Coaching at the third call; ten minutes after it (inclusive) the run is stopped,
    nine minutes after it is not."""
    twelve = calls(every_minute(12), command="gh pr checks 5")
    thirteen = calls(every_minute(13), command="gh pr checks 5")
    assert stopped(evaluate(twelve, **POLLING)) == []
    assert stopped(evaluate(thirteen, **POLLING)) == ["S-1"]


def test_a_verified_stop_announces_itself_and_says_nothing_of_doubt() -> None:
    actions = evaluate(calls(every_minute(8)))
    [alert] = [a for a in actions if a.kind is ActionKind.ALERT]
    assert alert.params[ANNOUNCES] == "stop:S-1"
    assert "unverified" not in alert.params
    assert "Not stopped" not in alert.reason and "Check the task" in alert.reason
    assert alert.key == "identical_calls:S-1"
    [stop] = [a for a in actions if a.kind is ActionKind.STOP]
    assert (stop.rule, stop.key, stop.task_id) == ("loop_breaker", "stop:S-1", "T-1")
    assert stop.params == {"session_id": "S-1", "trigger": "identical_calls"}


def test_an_unverified_streak_alerts_only() -> None:
    spans = calls(every_minute(8), attributes={UNVERIFIED_ATTRIBUTE: True})
    actions = evaluate(spans)
    assert stopped(actions) == []
    [alert] = actions
    assert alert.params["unverified"] is True and ANNOUNCES not in alert.params
    assert "Not stopped" in alert.reason and "Check the run." in alert.reason
    assert alert.key == "identical_calls:S-1:unverified"
    assert alert.state == "loop_breaker:identical_calls:S-1:unverified"
