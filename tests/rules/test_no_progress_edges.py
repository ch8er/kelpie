"""Edges of rule 11 (no_progress) pinned by mutation testing."""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from kelpie.core.actions import Action
from kelpie.core.model import Session, Snapshot, Span, SpanKind, Status, Task
from kelpie.rules.no_progress import NoProgress

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
START = NOW - 180 * M
WINDOW_START = NOW - 60 * M
STALL = NoProgress(window=60 * M, min_tokens_without_progress=1000, context_alert_tokens=0)
CONTEXT = NoProgress(window=60 * M, min_tokens_without_progress=10**9)


def llm(
    sid: str,
    at: datetime,
    tokens_in: int = 900,
    session: str = "S-1",
    model: str | None = "model-a",
) -> Span:
    return Span(
        "TR-1",
        sid,
        "chat",
        SpanKind.LLM,
        at,
        session_id=session,
        model=model,
        input_tokens=tokens_in,
        output_tokens=100,
    )


def run(rule: NoProgress, spans: Sequence[Span], sessions: Sequence[Session] = ()) -> list[Action]:
    tasks = tuple(Task(s.task_id, "t", Status.IN_PROGRESS, START) for s in sessions)
    snap = Snapshot(now=NOW, tasks=tasks, sessions=tuple(sessions), spans=tuple(spans))
    return list(rule.evaluate(snap))


def steps(session: str, sizes: Sequence[int], models: Sequence[str] | None = None) -> list[Span]:
    names = models or ["model-a"] * len(sizes)
    return [
        llm(f"{session}-{i}", NOW - (len(sizes) - i) * M, n, session, names[i])
        for i, n in enumerate(sizes)
    ]


# --- stalled sessions ----------------------------------------------------------------------


def test_a_session_that_ended_exactly_at_the_window_start_still_counts() -> None:
    session = Session("S-1", "T-101", "agent-dev-1", START, ended_at=WINDOW_START)
    [a] = run(STALL, [llm("A", WINDOW_START)], [session])
    assert a.params["session_id"] == "S-1"


def test_an_old_ended_session_does_not_hide_the_next() -> None:
    old = Session("S-0", "T-100", "agent-dev-1", START, ended_at=WINDOW_START - M)
    stalled = Session("S-1", "T-101", "agent-dev-1", START)
    [a] = run(STALL, [llm("A", NOW - M)], [old, stalled])
    assert a.params["session_id"] == "S-1"


def test_a_quiet_session_does_not_hide_the_next() -> None:
    quiet = Session("S-0", "T-100", "agent-dev-1", START)
    stalled = Session("S-1", "T-101", "agent-dev-1", START)
    [a] = run(STALL, [llm("A", NOW - M)], [quiet, stalled])
    assert a.params["session_id"] == "S-1"


# --- runaway context -----------------------------------------------------------------------


def test_a_step_starting_exactly_now_is_in_the_window() -> None:
    spans = [*steps("S-1", [150_000] * 4), llm("S-1-now", NOW, 150_000)]
    [a] = run(CONTEXT, spans)
    assert a.params["steps"] == 5


def test_context_exactly_at_the_bar_alerts() -> None:
    [a] = run(CONTEXT, steps("S-1", [150_000] * 5))
    assert a.params["context_tokens"] == 150_000
    assert a.params["per_step"] == 0
    assert run(CONTEXT, steps("S-1", [149_999] * 5)) == []


def test_skipped_sessions_do_not_hide_later_ones() -> None:
    ended = Session("S-1", "T-101", "agent-dev-1", START, ended_at=NOW - M)
    spans = [
        *steps("S-1", [200_000] * 5),  # ended: skipped
        *steps("S-2", [200_000] * 2),  # too few steps
        *steps("S-3", [1_000] * 5),  # below the bar
        *steps("S-4", [200_000] * 5),
    ]
    [a] = run(CONTEXT, spans, [ended])
    assert a.params["session_id"] == "S-4"


def test_a_step_at_exactly_half_the_one_before_is_growth_not_compaction() -> None:
    [a] = run(CONTEXT, steps("S-1", [400_000, 400_000, 400_000, 400_000, 200_000]))
    assert a.params["per_step"] == -50_000  # (0 + 0 + 0 - 200k) / 4


def test_only_compactions_give_zero_growth() -> None:
    [a] = run(CONTEXT, steps("S-1", [4_000_000, 1_900_000, 900_000, 400_000, 150_000]))
    assert a.params["per_step"] == 0


def test_main_model_sums_its_steps() -> None:
    # model-a: 4 x 160k = 640k; model-b: one 200k step. Both among the last two steps.
    sizes = [160_000, 160_000, 160_000, 160_000, 200_000]
    models = ["model-a"] * 4 + ["model-b"]
    [a] = run(CONTEXT, steps("S-1", sizes, models))
    assert a.params["model"] == "model-a"
    assert a.params["context_tokens"] == 160_000


def test_main_model_tie_breaks_on_the_name() -> None:
    sizes = [160_000] * 6
    models = ["model-a", "model-b"] * 3
    [a] = run(CONTEXT, steps("S-1", sizes, models))
    assert a.params["model"] == "model-b"
