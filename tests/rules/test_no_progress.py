from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.app import trace_window_warnings
from kelpie.core.actions import ActionKind, Severity
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome, Suppression
from kelpie.core.model import Artifact, Session, Snapshot, Span, SpanKind, Status, Task
from kelpie.rules import REGISTRY
from kelpie.rules._tokens import outermost_llm_spans, tokens_since
from kelpie.rules.no_progress import NoProgress, _fmt, build
from kelpie.traces.config import TraceSettings
from tests.fakes import FakeAdapter, FakeAlerter

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
START = NOW - 180 * M  # session start, before every window used below
RULE = NoProgress(window=60 * M, min_tokens_without_progress=1000, claim_grace=2 * M)


def llm(
    sid: str,
    at: datetime,
    tokens_in: int = 900,
    tokens_out: int = 100,
    parent: str | None = None,
    session: str | None = "S-1",
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
        "headline": "No progress on T-1: 1,000 tokens in 1h",
        "needed": "Check whether the session is stuck before it spends more",
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


def test_untagged_span_between_two_llm_spans_does_not_break_the_chain() -> None:
    outer = llm("OUT", NOW - 10 * M, 600, 100)
    middle = llm("MID", NOW - 10 * M, 0, 0, parent="OUT", session=None, kind=SpanKind.TOOL)
    inner = llm("IN", NOW - 10 * M, 600, 100, parent="MID")
    assert run(outer, middle, inner) == []  # 700 tokens, not 1400


def test_trace_shared_by_two_sessions_counts_each_chain_once() -> None:
    outer = llm("OUT", NOW - 10 * M, 600, 100, session="S-other")
    inner = llm("IN", NOW - 10 * M, 600, 100, parent="OUT", session="S-1")
    assert run(outer, inner) == []  # the wrapper's tokens belong to S-other
    own = llm("B", NOW - M, 900, 100, session="S-1", trace="TR-2")
    [a] = run(outer, inner, own)
    assert a.params["tokens"] == 1000  # only its own, unrelated call


def test_wrapper_before_the_window_hides_its_child_without_being_counted() -> None:
    outer = llm("OUT", NOW - 90 * M)
    inner = llm("IN", NOW - 10 * M, parent="OUT")
    assert tokens_since([outer, inner], NOW - 60 * M) == 0


def test_status_change_during_the_session_is_progress() -> None:
    spans = (llm("A", NOW - 10 * M),)
    assert run(*spans, status_since=NOW - 30 * M) == []
    assert run(*spans, status_since=NOW - 60 * M) == []  # boundary: inside the window
    assert len(run(*spans, status_since=NOW - 60 * M - timedelta(seconds=1))) == 1


def test_claiming_the_task_is_not_progress() -> None:
    session = Session("S-1", "T-1", "a-1", NOW - 30 * M)  # claim_grace is 2m
    spans = (llm("A", NOW - 10 * M),)
    assert len(run(*spans, session=session, status_since=NOW - 45 * M)) == 1  # before the start
    assert len(run(*spans, session=session, status_since=NOW - 30 * M)) == 1  # at the start
    assert len(run(*spans, session=session, status_since=NOW - 28 * M)) == 1  # end of the grace
    assert run(*spans, session=session, status_since=NOW - 28 * M + timedelta(seconds=1)) == []


def test_claim_grace_is_configurable() -> None:
    session = Session("S-1", "T-1", "a-1", NOW - 30 * M)
    spans = (llm("A", NOW - 10 * M),)
    none = NoProgress(60 * M, 1000, claim_grace=timedelta(0))
    assert run(*spans, session=session, status_since=NOW - 29 * M, rule=none) == []
    long = NoProgress(60 * M, 1000, claim_grace=10 * M)
    assert len(run(*spans, session=session, status_since=NOW - 21 * M, rule=long)) == 1


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
    default = NoProgress()
    assert build({}) == default
    assert default.window == 30 * M and default.min_tokens_without_progress == 100_000
    assert default.claim_grace == 2 * M
    rule = build({"window": "2h", "min_tokens_without_progress": 200_000, "claim_grace": 0})
    assert rule == NoProgress(120 * M, 200_000, claim_grace=timedelta(0))


@pytest.mark.parametrize(
    "config",
    [
        {"window": "soon"},
        {"window": None},
        {"window": 0},
        {"window": "-5m"},
        {"claim_grace": "-1m"},
        {"claim_grace": "soon"},
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


# --- startup check and engine ------------------------------------------------------------


def test_warns_when_window_is_longer_than_the_traces_window() -> None:
    traces = TraceSettings(window=30 * M)
    [warning] = trace_window_warnings([NoProgress(window=60 * M)], traces)
    assert "window (1h)" in warning and "window (30m)" in warning and "no_progress" in warning
    assert trace_window_warnings([NoProgress(window=30 * M)], traces) == []
    assert trace_window_warnings([NoProgress(window=60 * M)], TraceSettings(enabled=False)) == []
    assert trace_window_warnings([], traces) == []


def test_engine_announces_once_stays_quiet_and_resolves_after_progress() -> None:
    alerter = FakeAlerter()
    engine = Engine(
        [RULE],
        adapters=[FakeAdapter()],
        alerter=alerter,
        config=EngineConfig(dry_run=False, default_mode=Mode.ACT),
    )
    task = Task("T-1", "t", Status.IN_PROGRESS, START)
    spans = tuple(llm(f"S{i}", NOW - i * M, 100, 0) for i in range(20))  # 2000 tokens

    def tick(minutes: int, committed: bool = False) -> list:
        at = NOW + minutes * M
        session = Session("S-1", "T-1", "a-1", START, last_commit_at=at - M if committed else None)
        snap = Snapshot(now=at, tasks=(task,), sessions=(session,), spans=spans)
        return engine.tick(lambda: snap)

    [first] = tick(0)
    assert first.outcome is Outcome.ALERTED and len(alerter.sent) == 1
    for minutes in (5, 10, 15, 20):
        [later] = tick(minutes)
        assert later.suppression is Suppression.STATE_OPEN  # ONGOING: no new announcement
    assert len(alerter.sent) == 1
    assert tick(25, committed=True) == []  # progress: the rule reports nothing
    assert len(engine.states.store.open_states()) == 1  # a short gap is not a resolution
    [resolved] = tick(40, committed=True)
    assert resolved.action.reason.startswith("Resolved") and len(alerter.sent) == 2
    assert engine.states.store.open_states() == []


# --- runaway context ----------------------------------------------------------------------


def growing(session: str = "S-1", sizes: tuple[int, ...] = (), task: str | None = None) -> list:
    """LLM steps of ``session`` one minute apart, ending a minute ago, with these contexts."""
    n = len(sizes)
    return [
        replace(llm(f"{session}-{i}", NOW - (n - i) * M, tokens_in=size, session=session),
                task_id=task, model="model-a")
        for i, size in enumerate(sizes)
    ]  # fmt: skip


CONTEXT_RULE = NoProgress(
    context_alert_tokens=100_000, context_min_steps=3, min_tokens_without_progress=10**12
)


def context_alerts(
    *spans: Span, open_states: tuple[str, ...] = (), rule: NoProgress = CONTEXT_RULE
) -> list:
    snap = Snapshot(
        now=NOW,
        tasks=(Task("T-1", "t", Status.IN_PROGRESS, START),),
        sessions=(Session("S-1", "T-1", "a-1", START),),
        spans=spans,
        open_states=open_states,
    )
    return list(rule.evaluate(snap))


def test_a_session_whose_context_ran_away_gets_one_lasting_info_alert() -> None:
    [a] = context_alerts(*growing(sizes=(60_000, 80_000, 30_000, 70_000, 110_000)))
    assert a.kind is ActionKind.ALERT and a.severity is Severity.INFO and a.task_id == "T-1"
    assert a.state == a.key == "no_progress/context:S-1"
    assert a.since is None  # the engine keeps when it opened: the window slides
    # The compaction (80k -> 30k) is left out of the growth per step: (20k + 40k + 40k) / 3.
    assert a.params == {
        "session_id": "S-1",
        "context_tokens": 110_000,
        "steps": 5,
        "per_step": 33_333,
        "reread_tokens": 350_000,
        "window": "30m",
        "model": "model-a",
        "recommendations": ["compact_context", "bounded_session_handoff"],
        "headline": "Session S-1: 110k-token context, +33,333 per step",
        "needed": "Compact the context or bound the session",
    }
    assert "re-reads all of it: 350,000 input tokens in 30m" in a.reason


def test_runaway_context_thresholds_and_hysteresis() -> None:
    assert context_alerts(*growing(sizes=(99_000, 99_500, 99_999))) == []  # below the bar
    assert context_alerts(*growing(sizes=(150_000, 160_000))) == []  # too few steps
    assert context_alerts(*growing(sizes=(120_000, 130_000, 40_000))) == []  # compacted
    held = growing(sizes=(85_000, 85_000, 81_000))
    assert context_alerts(*held) == []
    [a] = context_alerts(*held, open_states=("no_progress/context:S-1",))  # kept open above 80%
    assert a.params["per_step"] == -2000
    off = NoProgress(context_alert_tokens=0, min_tokens_without_progress=10**12)
    assert context_alerts(*growing(sizes=(200_000,) * 6), rule=off) == []


def test_runaway_context_of_a_session_the_board_does_not_know() -> None:
    spans = growing("hs-9", (100_000, 110_000, 120_000), task="T-7")
    [a] = context_alerts(*spans)
    assert a.task_id == "T-7" and a.params["session_id"] == "hs-9"
    [b] = context_alerts(*growing("hs-9", (100_000, 110_000, 120_000)))
    assert b.task_id is None and "needed" not in b.params
    assert b.reason.startswith("Session hs-9 carries a 120,000-token context after 3 steps")
    # Steps before the window, or after now, do not count.
    old = [replace(s, start=s.start - 40 * M) for s in spans]
    future = [replace(s, start=s.start + 10 * M) for s in spans]
    assert context_alerts(*old) == [] and context_alerts(*future) == []


def test_an_ended_session_is_not_told_to_compact() -> None:
    snap = Snapshot(
        now=NOW,
        tasks=(Task("T-1", "t", Status.IN_PROGRESS, START),),
        sessions=(Session("S-1", "T-1", "a-1", START, ended_at=NOW - 2 * M),),
        spans=tuple(growing(sizes=(150_000, 160_000, 170_000))),
    )
    assert CONTEXT_RULE.evaluate(snap) == []


def test_side_calls_on_another_model_are_no_compaction() -> None:
    main = growing(sizes=(150_000, 152_000, 154_000, 156_000))
    side = [
        replace(s, span_id=f"side-{i}", start=s.start + timedelta(seconds=20), input_tokens=2_000,
                model="model-small")
        for i, s in enumerate(main)
    ]  # fmt: skip
    [a] = context_alerts(*main, *side)
    assert a.params["per_step"] == 2000 and a.params["steps"] == 8  # all the session's steps
    assert a.params["context_tokens"] == 156_000 and a.params["model"] == "model-a"


def test_steps_count_per_session_and_a_model_no_longer_at_work_does_not_speak() -> None:
    a = growing(sizes=(200_000,) * 4)
    b = [replace(s, span_id=f"b-{i}", start=s.start + timedelta(seconds=20), model="model-b")
         for i, s in enumerate(a)]  # fmt: skip
    [alert] = context_alerts(*a, *b)  # 8 steps on two models: judged on the session's steps
    assert alert.params["steps"] == 8
    one = growing(sizes=(1,))[0]
    late = [replace(one, span_id=f"late-{i}", start=NOW - M + i * timedelta(seconds=10),
                    input_tokens=50_000, model="model-b") for i in range(3)]  # fmt: skip
    old = [replace(s, start=s.start - 5 * M) for s in growing(sizes=(200_000,) * 5)]
    assert context_alerts(*old, *late) == []  # model-a stopped; model-b's context is small
    switched = [replace(s, input_tokens=180_000 + i) for i, s in enumerate(late)]
    [after] = context_alerts(*old, *switched)  # a runaway on the model that took over
    assert after.params["model"] == "model-b" and after.params["context_tokens"] == 180_002
    assert after.params["reread_tokens"] == 5 * 200_000 + 3 * 180_000 + 3  # all its steps


def test_runaway_context_hooks() -> None:
    state = "no_progress/context:hs-9"
    assert CONTEXT_RULE.boards(state) is False  # spans only: never blind to a missing board
    assert CONTEXT_RULE.boards("no_progress:S-1") is True
    quiet = Snapshot(now=NOW)
    assert CONTEXT_RULE.resolved(state, quiet) == (
        "Session hs-9's context is below the bar now: it compacted or went quiet."
    )
    ended = Snapshot(now=NOW, sessions=(Session("hs-9", "T-1", "a-1", START, ended_at=NOW),))
    assert CONTEXT_RULE.resolved(state, ended) == "Session hs-9 ended."
    assert CONTEXT_RULE.resolved("no_progress:S-1", quiet) is None


def test_runaway_context_settings() -> None:
    rule = build({"context_alert_tokens": 200_000, "context_min_steps": 10})
    assert (rule.context_alert_tokens, rule.context_min_steps) == (200_000, 10)
    assert build({"context_alert_tokens": 0}).context_alert_tokens == 0
    for key, bad in (("context_alert_tokens", -1), ("context_min_steps", 1),
                     ("context_min_steps", True), ("context_alert_tokens", "1")):  # fmt: skip
        with pytest.raises(ValueError, match=key):
            build({key: bad})
