"""Repeated huge-output offenders for the snapshot: ranking, the hourly watch, the app."""

from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kelpie.app import build_app, load_settings, merged_snapshot
from kelpie.core.model import Owner, Session, Snapshot, Span, SpanKind
from kelpie.core.spans import MemorySpanStore
from kelpie.core.storage import SqliteSpanStore
from kelpie.metrics import Metrics
from kelpie.traces.hooks import merge_hook_spans
from kelpie.traces.outputs import Call, OutputSettings, repeated_offenders
from kelpie.traces.store import OffenderWatch, SpanWindow

NOW = datetime(2026, 3, 3, 16, tzinfo=UTC)
SETTINGS = OutputSettings(threshold_tokens=4000, min_calls=3, repeat_days=2, owners={"dev": "lead"})


def call(day: int, key: str = "xcodebuild", tokens: int = 9000, **kw: object) -> Call:
    at = datetime(2026, 3, day, 10, tzinfo=UTC) + timedelta(minutes=len(str(kw)))
    base: dict[str, object] = {
        "at": at, "role": "dev", "key": key, "tokens": tokens, "estimated": False,
        "filter": None, "platform": None, "task_id": f"T-{day}", "session_id": "S-1",
    }  # fmt: skip
    return Call(**{**base, **kw})  # type: ignore[arg-type]


def test_two_hot_days_make_an_offender_with_its_owner_and_recent_tasks() -> None:
    found = [call(2), call(2), call(3), call(3, task_id="T-late")]
    [o] = repeated_offenders(found, SETTINGS)
    assert (o.role, o.command, o.filter, o.days, o.calls) == (
        "dev",
        "xcodebuild",
        "xcbeautify",
        2,
        4,
    )
    assert (
        o.owner == "lead"
        and o.recent_tasks == ("T-late", "T-3", "T-2")
        and o.tasks == ("T-2", "T-3", "T-late")
    )
    assert o.mean_tokens == 9000.0 and o.saved_tokens == round(4 * 9000 * 0.7)


def test_one_hot_day_small_outputs_and_adopted_filters_are_not_offenders() -> None:
    assert repeated_offenders([call(3), call(3), call(3)], SETTINGS) == ()
    assert repeated_offenders([call(d, tokens=100) for d in (2, 2, 3, 3)], SETTINGS) == ()
    adopted = [call(2), call(2), call(3), call(3, filter="xcbeautify")]
    assert repeated_offenders(adopted, SETTINGS) == ()


def tool_span(day: int, hour: int, n: int, chars: int = 40_000) -> Span:
    at = datetime(2026, 3, day, hour, tzinfo=UTC)
    return Span(
        f"tr-{day}-{hour}-{n}", f"sp-{n}", "execute_tool shell", SpanKind.TOOL, at, at,
        task_id=f"T-{day}", session_id="S-1", tool="shell",
        attributes={"kelpie.tool.command_prefix": "xcodebuild", "kelpie.tool.output_chars": chars},
    )  # fmt: skip


def snapshot(now: datetime = NOW) -> Snapshot:
    return Snapshot(
        now,
        owners=(Owner("agent-dev-1", "dev"),),
        sessions=(Session("S-1", "T-3", "agent-dev-1", NOW - timedelta(days=2)),),
    )


def test_the_watch_reads_tool_spans_of_the_last_days_hourly(tmp_path: Path) -> None:
    store = SqliteSpanStore(tmp_path / "kelpie.db")
    spans = [tool_span(d, 10, n) for d in (2, 3) for n in range(3)]
    spans += [tool_span(1, 10, n) for n in range(3)]  # before the window: not read
    llm = Span("tr-llm", "sp-llm", "chat", SpanKind.LLM, NOW, NOW, session_id="S-1", model="m")
    store.append_spans([*spans, llm], NOW)
    watch = OffenderWatch(store, SETTINGS, UTC)
    [o] = watch.offenders(snapshot(), None)
    assert o.role == "dev" and o.days == 2 and o.calls == 6 and o.owner == "lead"
    store.append_spans([tool_span(3, 12, 9, chars=400)], NOW)
    assert watch.offenders(snapshot(NOW + timedelta(minutes=59)), None) == (o,)  # cached
    [again] = watch.offenders(snapshot(NOW + timedelta(hours=1)), None)
    assert again.calls == 7
    store.close()


def test_the_watch_counts_days_in_the_reports_time_zone() -> None:
    store = MemorySpanStore()
    # 23:30 UTC on the 2nd is already the 3rd in Tokyo: one local day, not two
    store.append_spans([tool_span(2, 23, n) for n in range(3)], NOW)
    store.append_spans([tool_span(3, 1, n) for n in range(3)], NOW)
    assert OffenderWatch(store, SETTINGS, UTC).offenders(snapshot(), None)
    tokyo = OffenderWatch(store, SETTINGS, ZoneInfo("Asia/Tokyo"))
    assert tokyo.offenders(snapshot(), None) == ()


class BrokenTools(MemorySpanStore):
    def tool_spans_since(self, after: datetime) -> list[Span]:
        raise OSError("disk I/O error")


def test_the_snapshot_carries_offenders_and_survives_a_failed_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = MemorySpanStore()
    store.append_spans([tool_span(d, 10, n) for d in (2, 3) for n in range(3)], NOW)
    window = SpanWindow(store, timedelta(hours=2), timedelta(hours=24))
    watch = OffenderWatch(store, SETTINGS, UTC)
    snap = merged_snapshot([], Metrics(), lambda: NOW, window, offenders=watch)()
    assert [o.command for o in snap.output_offenders] == ["xcodebuild"]
    metrics = Metrics()
    metrics.set_traces(True)
    broken = OffenderWatch(BrokenTools(), SETTINGS, UTC)
    snap = merged_snapshot([], metrics, lambda: NOW, window, offenders=broken)()
    assert snap.output_offenders == ()
    assert "kelpie_span_store_errors_total 1" in metrics.render()
    assert "disk I/O error" in caplog.text


def test_build_app_wires_the_watch(tmp_path: Path) -> None:
    settings = load_settings(None)
    settings = replace(settings, db_path=tmp_path / "kelpie.db")
    app = build_app(settings, clock=lambda: NOW)
    try:
        app.span_store.append_spans(  # type: ignore[union-attr]
            [tool_span(d, 10, n) for d in (2, 3) for n in range(3)], NOW
        )
        snap = app.loop.snapshot_source()  # type: ignore[attr-defined]
        assert [o.command for o in snap.output_offenders] == ["xcodebuild"]
    finally:
        app.close()


def test_hook_spans_of_the_snapshot_count_and_stored_ones_are_not_doubled() -> None:
    """A farm without OTLP: the tool calls come from the hooks, in the snapshot only."""
    store = MemorySpanStore()
    stored = [tool_span(2, 10, n) for n in range(3)]
    store.append_spans(stored, NOW)
    hooks = [tool_span(3, 10, n + 10) for n in range(3)]
    snap = replace(snapshot(), spans=(*stored, *hooks))  # the window repeats stored spans
    [o] = OffenderWatch(store, SETTINGS, UTC).offenders(snap, None)
    assert o.calls == 6 and o.days == 2


class FlakyTools(MemorySpanStore):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def tool_spans_since(self, after: datetime) -> list[Span]:
        self.reads += 1
        raise OSError("disk I/O error")


def test_a_failed_read_is_retried_after_ten_minutes_not_every_tick() -> None:
    store = FlakyTools()
    watch = OffenderWatch(store, SETTINGS, UTC)
    for minute in (0, 1, 9):
        with contextlib.suppress(OSError):
            watch.offenders(snapshot(NOW + timedelta(minutes=minute)), None)
    assert store.reads == 1
    with pytest.raises(OSError):
        watch.offenders(snapshot(NOW + timedelta(minutes=10)), None)
    assert store.reads == 2


def otlp_and_hook(day: int, n: int) -> tuple[Span, Span]:
    """One xcodebuild call reported twice: OTLP without arguments (the exporter's own
    session) and the Hermes hook with the command and the platform's session."""
    at = datetime(2026, 3, day, 10, n, tzinfo=UTC)
    call = {"gen_ai.tool.call.id": f"call-{day}-{n}"}
    otlp = Span(
        f"tr-{day}-{n}", f"otlp-{n}", "execute_tool terminal", SpanKind.TOOL, at, at,
        session_id="chat-x", tool="terminal",
        attributes={**call, "kelpie.tool.output_chars": 40_000},
    )  # fmt: skip
    hook = Span(
        f"hook-{day}-{n}", f"hook-{n}", "terminal", SpanKind.TOOL, at, at,
        task_id=f"T-{day}", session_id="S-1", tool="terminal",
        attributes={**call, "kelpie.source": "hooks", "command": "xcodebuild -scheme App build"},
    )  # fmt: skip
    return otlp, hook


def test_calls_folded_with_their_hooks_keep_the_command() -> None:
    """The app folds hook spans into the OTLP spans of its window before the watch runs:
    that folded copy (command, task, session) must win over the raw stored one."""
    store = MemorySpanStore()
    pairs = [otlp_and_hook(d, n) for d in (2, 3) for n in range(3)]
    store.append_spans([otlp for otlp, _ in pairs], NOW)  # OTLP only reaches the store
    window = [s for p in pairs if p[0].start.day == 3 for s in p]
    older_hooks = [hook for otlp, hook in pairs if otlp.start.day == 2]
    folded = merge_hook_spans([*window, *older_hooks]).spans  # as merged_snapshot does
    [o] = OffenderWatch(store, SETTINGS, UTC).offenders(replace(snapshot(), spans=folded), None)
    assert (o.role, o.command, o.days, o.calls) == ("dev", "xcodebuild", 2, 6)
