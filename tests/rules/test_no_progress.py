from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.model import Artifact, Session, Snapshot, Span, SpanKind, Status, Task
from kelpie.rules import REGISTRY
from kelpie.rules._tokens import outermost_llm_spans, tokens_since
from kelpie.rules.no_progress import NoProgress, _fmt, build

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
START = NOW - 180 * M  # session start, before every window used below
RULE = NoProgress(window=60 * M, min_tokens_without_progress=1000)


def llm(
    sid: str,
    at: datetime,
    tokens_in: int = 900,
    tokens_out: int = 100,
    parent: str | None = None,
    session: str = "S-1",
    trace: str = "TR-1",
    kind: SpanKind = SpanKind.LLM,
) -> Span:
    return Span(
        trace,
        sid,
        "chat",
        kind,
        at,
        parent_id=parent,
        session_id=session,
        input_tokens=tokens_in,
        output_tokens=tokens_out,
    )


def run(
    *spans: Span,
    session: Session | None = None,
    status_since: datetime = START,
    rule: NoProgress = RULE,
) -> list:
    task = Task("T-1", "t", Status.IN_PROGRESS, status_since)
    sessions = (session or Session("S-1", "T-1", "a-1", START),)
    return list(rule.evaluate(Snapshot(now=NOW, tasks=(task,), sessions=sessions, spans=spans)))


def test_registered() -> None:
    assert isinstance(REGISTRY["no_progress"]({}), NoProgress)


def test_alert_carries_tokens_window_and_recommendations() -> None:
    [a] = run(llm("A", NOW - 10 * M))
    assert a.kind is ActionKind.ALERT and a.severity is Severity.WARNING
    assert a.task_id == "T-1" and "no progress" in a.reason
    assert a.params == {
        "session_id": "S-1",
        "owner_id": "a-1",
        "tokens": 1000,
        "window": "1h",
        "recommendations": [
            "longer_heartbeat_interval",
            "cheaper_maintenance_model",
            "bounded_session_handoff",
        ],
    }


def test_threshold_boundary() -> None:
    assert len(run(llm("A", NOW - M, 900, 100))) == 1  # exactly min_tokens
    assert run(llm("A", NOW - M, 900, 99)) == []


def test_window_boundary_for_tokens() -> None:
    assert len(run(llm("A", NOW - 60 * M))) == 1  # starts exactly at the window start
    assert run(llm("A", NOW - 60 * M - timedelta(seconds=1))) == []


def test_tokens_sum_over_spans_and_ignore_other_sessions_and_tools() -> None:
    spans = (
        llm("A", NOW - 30 * M, 300, 100),
        llm("B", NOW - 20 * M, 300, 100),
        llm("C", NOW - 10 * M, 150, 50),
        llm("D", NOW - 5 * M, 5000, 0, session="S-other"),
        llm("E", NOW - 5 * M, 5000, 0, kind=SpanKind.TOOL),
    )
    [a] = run(*spans)
    assert a.params["tokens"] == 1000


def test_nested_llm_spans_are_not_counted_twice() -> None:
    outer = llm("OUT", NOW - 10 * M, 600, 100)
    inner = llm("IN", NOW - 10 * M, 600, 100, parent="OUT")
    deep = llm("DEEP", NOW - 10 * M, 600, 100, parent="IN")
    tool = llm("TOOL", NOW - 10 * M, 0, 0, parent="OUT", kind=SpanKind.TOOL)
    assert run(outer, inner, deep, tool) == []  # 700 tokens, not 2800
    [a] = run(outer, inner, deep, tool, llm("B", NOW - M, 250, 50))
    assert a.params["tokens"] == 1000


def test_llm_under_a_tool_under_an_llm_is_nested_too() -> None:
    outer = llm("OUT", NOW - 10 * M, 600, 100)
    tool = llm("T", NOW - 10 * M, 0, 0, parent="OUT", kind=SpanKind.TOOL)
    inner = llm("IN", NOW - 10 * M, 600, 100, parent="T")
    assert [s.span_id for s in outermost_llm_spans([outer, tool, inner])] == ["OUT"]


def test_unknown_parent_does_not_hide_a_span() -> None:
    orphan = llm("O", NOW - M, parent="GONE")
    assert outermost_llm_spans([orphan]) == [orphan]


def test_parent_cycle_terminates() -> None:
    a = llm("A", NOW - M, parent="B")
    b = llm("B", NOW - M, parent="A")
    assert isinstance(outermost_llm_spans([a, b]), list)


def test_parent_in_another_trace_is_not_an_ancestor() -> None:
    outer = llm("OUT", NOW - M, trace="TR-1")
    other = llm("IN", NOW - M, parent="OUT", trace="TR-2")
    assert len(outermost_llm_spans([outer, other])) == 2


def test_wrapper_before_the_window_hides_its_child_without_being_counted() -> None:
    outer = llm("OUT", NOW - 90 * M)
    inner = llm("IN", NOW - 10 * M, parent="OUT")
    assert tokens_since([outer, inner], NOW - 60 * M) == 0


def test_status_change_during_the_session_is_progress() -> None:
    spans = (llm("A", NOW - 10 * M),)
    assert run(*spans, status_since=NOW - 30 * M) == []
    assert run(*spans, status_since=NOW - 60 * M) == []  # boundary: inside the window
    assert len(run(*spans, status_since=NOW - 60 * M - timedelta(seconds=1))) == 1


def test_status_change_at_or_before_session_start_is_not_progress() -> None:
    session = Session("S-1", "T-1", "a-1", NOW - 30 * M)
    spans = (llm("A", NOW - 10 * M),)
    assert len(run(*spans, session=session, status_since=NOW - 30 * M)) == 1
    assert len(run(*spans, session=session, status_since=NOW - 45 * M)) == 1
    assert run(*spans, session=session, status_since=NOW - 29 * M) == []


def test_commit_is_progress_inside_the_window_only() -> None:
    spans = (llm("A", NOW - 10 * M),)
    committed = Session("S-1", "T-1", "a-1", START, last_commit_at=NOW - 60 * M)
    assert run(*spans, session=committed) == []
    old = Session("S-1", "T-1", "a-1", START, last_commit_at=NOW - 61 * M)
    [a] = run(*spans, session=old)
    assert a.since == NOW - 61 * M


def test_artifact_is_progress_inside_the_window_only() -> None:
    spans = (llm("A", NOW - 10 * M),)
    fresh = Session("S-1", "T-1", "a-1", START, artifacts=(Artifact("r.html", NOW - 5 * M),))
    assert run(*spans, session=fresh) == []
    old = Session(
        "S-1",
        "T-1",
        "a-1",
        START,
        artifacts=(Artifact("a", NOW - 120 * M), Artifact("b", NOW - 90 * M)),
    )
    [a] = run(*spans, session=old)
    assert a.since == NOW - 90 * M


def test_young_session_without_progress_is_flagged_from_its_start() -> None:
    session = Session("S-1", "T-1", "a-1", NOW - 20 * M)
    [a] = run(llm("A", NOW - 10 * M), session=session, status_since=NOW - 25 * M)
    assert a.since == NOW - 20 * M


def test_ended_session_older_than_the_window_is_ignored() -> None:
    old = Session("S-1", "T-1", "a-1", START, ended_at=NOW - 61 * M)
    assert run(llm("A", NOW - 70 * M), session=old) == []
    assert run(llm("A", NOW - 62 * M), session=old) == []
    recent = Session("S-1", "T-1", "a-1", START, ended_at=NOW - 5 * M)
    assert len(run(llm("A", NOW - 10 * M), session=recent)) == 1


def test_session_whose_task_is_missing_can_still_be_flagged() -> None:
    snap = Snapshot(
        now=NOW,
        sessions=(Session("S-1", "T-gone", "a-1", START),),
        spans=(llm("A", NOW - M),),
    )
    [a] = RULE.evaluate(snap)
    assert a.task_id == "T-gone"


def test_one_lasting_state_per_session_stable_across_ticks() -> None:
    [first] = run(llm("A", NOW - 10 * M))
    later = Snapshot(
        now=NOW + 7 * M,
        tasks=(Task("T-1", "t", Status.IN_PROGRESS, START),),
        sessions=(Session("S-1", "T-1", "a-1", START),),
        spans=(llm("A", NOW - 10 * M), llm("B", NOW + 5 * M)),
    )
    [second] = RULE.evaluate(later)
    assert first.state == second.state == "no_progress:S-1"
    assert first.key == second.key and first.since == second.since == START
    assert second.params["tokens"] == 2000  # volatile data stays out of the identity


def test_new_stall_after_progress_gets_a_new_key() -> None:
    spans = (llm("A", NOW - 10 * M),)
    committed = Session("S-1", "T-1", "a-1", START, last_commit_at=NOW - 90 * M)
    [a] = run(*spans, session=committed)
    [b] = run(*spans)
    assert a.state == b.state and a.key != b.key


def test_sessions_are_judged_separately() -> None:
    idle = Session("S-1", "T-1", "a-1", START)
    busy = Session("S-2", "T-1", "a-2", START, last_commit_at=NOW - M)
    spans = (llm("A", NOW - 10 * M), llm("B", NOW - 10 * M, session="S-2"))
    task = Task("T-1", "t", Status.IN_PROGRESS, START)
    snap = Snapshot(now=NOW, tasks=(task,), sessions=(idle, busy), spans=spans)
    assert [a.params["session_id"] for a in RULE.evaluate(snap)] == ["S-1"]


@pytest.mark.parametrize(
    ("d", "text"),
    [
        (timedelta(hours=1), "1h"),
        (timedelta(minutes=90), "1h30m"),
        (timedelta(seconds=45), "45s"),
        (timedelta(0), "0s"),
    ],
)
def test_fmt(d: timedelta, text: str) -> None:
    assert _fmt(d) == text


def test_build_defaults_and_overrides() -> None:
    assert build({}) == NoProgress()
    assert NoProgress().window == 60 * M and NoProgress().min_tokens_without_progress == 50_000
    rule = build({"window": "2h", "min_tokens_without_progress": 200_000})
    assert rule == NoProgress(window=120 * M, min_tokens_without_progress=200_000)


@pytest.mark.parametrize(
    "config",
    [
        {"window": "soon"},
        {"window": None},
        {"window": 0},
        {"window": "-5m"},
        {"min_tokens_without_progress": 0},
        {"min_tokens_without_progress": "100"},
        {"min_tokens_without_progress": True},
        {"min_tokens_without_progress": 1.5},
        {"treshold": 1},
    ],
)
def test_build_rejects_bad_config(config: dict) -> None:
    with pytest.raises(ValueError):
        build(config)
