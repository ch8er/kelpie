"""Repeated fragments, unused tools, model fit and diff-only review (fixture ``tokens-fit``),
and the ``tokens_fit`` section of the daily report."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from kelpie.core.budgets import Price, PriceTable
from kelpie.core.model import Return, Snapshot, Span, SpanKind, Status, Task
from kelpie.core.serde import snapshot_from_dict
from kelpie.core.spans import TokenUsage, counted_llm, span_key
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.sections import SECTIONS
from kelpie.report.sections.tokens_fit import _settings, tokens_fit
from kelpie.traces.fit import (
    FitSettings,
    Read,
    RoleModel,
    cheaper_model,
    fragments,
    model_switches,
    offered_tools,
    reads,
    review_rounds,
    role_models,
    routine,
    tool_tokens,
    tool_use,
)
from kelpie.traces.outputs import session_context
from tests.incidents import expand_repeats
from tests.traces.otlp_builders import S, sid
from tests.traces.test_semconv import mapped

FIXTURE = Path(__file__).parents[1] / "fixtures" / "traces" / "tokens-fit.yaml"
ONLY = {"tokens_fit": tokens_fit}
ROUTINE = FitSettings(routine_roles=("triage", "writer"))
CONFIGURED = {"tokens_fit": lambda ctx: tokens_fit(ctx, ROUTINE)}
T0 = datetime(2026, 3, 2, 9, tzinfo=UTC)
DAY_START = datetime(2026, 3, 2, tzinfo=UTC)
DAY_END = DAY_START + timedelta(days=1)


def _load() -> tuple[dict[str, Any], Snapshot]:
    data = yaml.safe_load(FIXTURE.read_text())
    assert data["id"] == FIXTURE.stem
    spans = expand_repeats(data["snapshot"]["spans"])
    return data, snapshot_from_dict({**data["snapshot"], "spans": spans})


def _prices(data: dict[str, Any]) -> PriceTable:
    return PriceTable(tuple((k, Price(**v)) for k, v in data["config"]["prices"].items()))


def _inputs(data: dict[str, Any], snapshot: Snapshot, priced: bool = True) -> ReportInputs:
    return ReportInputs(
        day=date.fromisoformat(data["day"]),
        tz=UTC,
        spans=snapshot.spans,
        tasks=snapshot.tasks,
        owners=snapshot.owners,
        sessions=snapshot.sessions,
        prices=_prices(data) if priced else PriceTable(),
    )


def _day(snapshot: Snapshot) -> list[Span]:
    return [s for s in snapshot.spans if DAY_START <= (s.end or s.start) < DAY_END]


def test_fixture_analyses() -> None:
    data, snapshot = _load()
    roles, _ = session_context(snapshot)
    tasks = {s.id: s.task_id for s in snapshot.sessions}
    day = _day(snapshot)
    expected, unexpected = data["expected"], data["not_expected"]

    found = fragments(reads(day, roles, tasks))
    flat = [
        {
            "role": f.role,
            "key": f.key,
            "args_hash": f.args_hash,
            "sessions": len(f.sessions),
            "tokens": f.tokens,
        }
        for f in found
    ]
    assert flat == expected["fragments"]
    assert not {f.args_hash for f in found} & set(unexpected["fragment_hashes"])

    uses = {u.role: u for u in tool_use(day, roles)}
    for role, unused in expected["unused"].items():
        assert list(uses[role].unused) == unused
    assert sorted(r for r, u in uses.items() if u.offered is None) == expected["unknown_tools"]

    counted = counted_llm({span_key(s): s for s in day}, day)
    prices = _prices(data)
    candidates = routine(role_models(day, counted, roles), None, tool_tokens(reads(day, roles)))
    farm = {
        m
        for m in counted_llm({span_key(s): s for s in snapshot.spans}, snapshot.spans).values()
        if m
    }
    assert [
        {
            "role": r.role,
            "model": r.model,
            "calls": r.calls,
            "cheaper": cheaper_model(prices, r.model, farm),
        }
        for r in candidates
    ] == expected["routine"]
    assert not {r.role for r in candidates} & set(unexpected["routine_roles"])

    every = counted_llm({span_key(s): s for s in snapshot.spans}, snapshot.spans)
    switches = model_switches(snapshot.spans, every, roles, snapshot.tasks, DAY_START, DAY_END)
    assert [
        {"role": s.role, "old": s.old, "new": s.new, "worse": s.worse} for s in switches
    ] == expected["switches"]
    [w] = switches
    assert w.at == T0 and (w.before.tasks, w.before.returns) == (1, 1)
    assert (w.after.tasks, w.after.returns, w.after.review_returns) == (2, 1, 2)

    rounds = review_rounds(reads(snapshot.spans, roles, tasks), ("reviewer",), DAY_START, DAY_END)
    got = [
        {
            "task_id": r.task_id,
            "round": r.round,
            "session_id": r.session_id,
            "tokens": r.tokens,
            "reread": r.reread,
            "whole": r.whole,
        }
        for r in rounds
    ]
    assert got == expected["review"]
    assert [r.unhashed for r in rounds] == [0, 1]


def test_fixture_in_the_daily_report() -> None:
    data, snapshot = _load()
    report = build_report(_inputs(data, snapshot), CONFIGURED)
    [section] = report.sections
    assert section.title == "Context and model fit"
    by_title = {a.title: a for a in section.advice}
    assert set(by_title) == set(data["expected"]["advice"])
    for title in data["not_expected"]["advice"]:
        assert title not in by_title
    # 144k input and 3.6k output tokens on the large model, then on the small one the farm
    # runs (the tiny one is priced but never seen in the traces)
    triage = by_title["Try `model-small` for triage"]
    assert triage.usd == pytest.approx(
        (144_000 * 3 + 3_600 * 15 - 144_000 * 0.25 - 3_600 * 1.25) / 1e6
    )
    assert triage.tasks == ("T-G1",)
    review = by_title["Review only the changes since the last round (reviewer)"]
    assert review.usd == pytest.approx(60_000 * 3 / 1e6) and review.tasks == ("T-D1",)
    guide = by_title["Put what every developer session loads into its cached instructions"]
    assert guide.usd == pytest.approx(60_050 * 2.7 / 1e6)
    assert guide.tasks == ("T-D1", "T-D2", "T-D3")
    assert [a.title for a in report.recommendations] == data["expected"]["advice"]
    for a in section.advice:
        assert not a.detail.endswith(".")
    markdown = render_markdown(report)
    assert "## Context and model fit" in markdown
    assert "| developer | `tool:Read` | `h-guide` | 3 | 20,000 | 60,050 |" in markdown
    assert "- developer: 5 offered, 2 called; never called: `Edit`, `NotebookEdit`" in markdown
    assert "Unused tools are unknown" not in markdown  # a data gap for the docs, not daily
    assert "| writer | `model-small` | 24 | 200 | none in the farm (hold: worse) | - |" in markdown
    assert "not additive" in markdown
    assert "- writer switched `model-large` → `model-small` at 2026-03-02 09:00 UTC" in markdown
    assert "(worse: check the switch)" in markdown
    assert "| T-D1 | 2 | S-R2 | 64,000 | 60,000 (94%) | yes |" in markdown
    assert "1 read(s) had no arguments hash" in markdown


def test_registered_and_quiet_without_spans() -> None:
    assert SECTIONS["tokens_fit"] is tokens_fit
    assert build_report(ReportInputs(day=date(2026, 3, 2), tz=UTC), ONLY).sections == ()


def test_nothing_found_means_no_section() -> None:
    llm = Span(
        "t",
        "a",
        "chat",
        SpanKind.LLM,
        T0,
        T0 + timedelta(seconds=5),
        session_id="S",
        model="m",
        input_tokens=100,
        output_tokens=5,
    )
    report = build_report(ReportInputs(day=date(2026, 3, 2), tz=UTC, spans=[llm]), ONLY)
    assert report.sections == ()


def test_without_prices_advice_has_no_dollars() -> None:
    data, snapshot = _load()
    [section] = build_report(_inputs(data, snapshot, priced=False), ONLY).sections
    titles = {a.title for a in section.advice}
    assert not any(t.startswith("Try ") for t in titles)  # not a configured routine role
    assert all(a.usd == 0 for a in section.advice)
    assert "Review only the changes since the last round (reviewer)" in titles


# --- unit cases --------------------------------------------------------------------------


def read(
    session: str,
    at: datetime,
    digest: str | None,
    tokens: int,
    key: str = "tool:Read",
    role: str = "reviewer",
    task: str | None = "T",
) -> Read:
    return Read(session, task, role, at, key, digest, tokens)


def test_offered_tools_parsing() -> None:
    assert offered_tools(" Read, ,Bash ") == ("Read", "Bash")
    assert offered_tools(None) == () and offered_tools(3) == ()
    cut = ",".join(f"tool{i:03d}" for i in range(40))[:256]
    assert offered_tools(cut)[-1] == "tool031"  # the cut name is dropped


def test_cheaper_model_needs_prices_and_skips_globs() -> None:
    prices = PriceTable(
        (
            ("big", Price(3, 15, 0.3)),
            ("small*", Price(0.1, 0.1, 0.01)),
            ("mid", Price(1, 5, 0.1)),
            ("odd", Price(0.5, 20, 0.05)),  # cheaper input, dearer output
        )
    )
    farm = {"big", "small*", "mid", "odd", "small-1"}
    assert cheaper_model(prices, "big", farm) == "mid"
    assert cheaper_model(prices, "big", {"big"}) is None  # mid is not run by the farm
    assert cheaper_model(prices, "mid", farm) is None
    assert cheaper_model(prices, "unknown", farm) is None
    assert cheaper_model(prices, None, farm) is None


def test_routine_needs_calls_short_outputs_and_a_model() -> None:
    usage = role_models([], {}, {})
    assert usage == [] and routine([]) == []
    cfg = FitSettings(routine_min_calls=2)
    r = RoleModel("triage", None, (), 10)
    assert routine([r], cfg) == []
    calls = tuple(TokenUsage("S", "T", "m", 1, 1000, 50, 0) for _ in range(3))
    busy = RoleModel("dev", "m", calls, 50)
    assert routine([busy], cfg) == [busy]
    assert routine([busy], cfg, {"dev": 5000.0}) == []  # reads files, runs tests


def test_review_rounds_only_count_reads_matching_review_reads() -> None:
    day = DAY_START
    found = [
        read("A", day - timedelta(hours=2), "h1", 5000),
        read("A", day - timedelta(hours=2), "t1", 9000, key="pytest"),
        read("B", T0, "h1", 4000),  # the file shrank: 4000 already read
        read("B", T0, "h1", 4000),  # read twice in one round: counted once
        read("B", T0, "t1", 9000, key="pytest"),  # tests rerun: not a review read
    ]
    [r] = review_rounds(found, ("reviewer",), DAY_START, DAY_END)
    assert (r.tokens, r.reread, r.whole) == (4000, 4000, True)
    assert review_rounds(found, ("qa",), DAY_START, DAY_END) == []
    # round 2 before the day: not reported
    assert review_rounds(found, ("reviewer",), DAY_END, DAY_END + timedelta(days=1)) == []


def test_switch_without_more_returns_is_not_worse() -> None:
    def call(i: int, at: datetime, model: str, sid: str) -> Span:
        return Span(
            "t",
            f"c{i}",
            "chat",
            SpanKind.LLM,
            at,
            at + timedelta(seconds=5),
            task_id="T1",
            session_id=sid,
            model=model,
            input_tokens=10,
            output_tokens=1,
        )

    spans = [
        call(1, DAY_START - timedelta(hours=5), "old", "S1"),
        call(2, T0, "new", "S2"),
        call(3, T0 + timedelta(hours=1), "new", "S2"),
    ]
    counted = counted_llm({span_key(s): s for s in spans}, spans)
    task = Task(
        "T1",
        "x",
        Status.IN_PROGRESS,
        T0,
        returns=(Return(DAY_START - timedelta(hours=4), "r", "review"),),
    )
    roles = {"S1": "writer", "S2": "writer"}
    [s] = model_switches(spans, counted, roles, [task], DAY_START, DAY_END)
    assert (s.old, s.new, s.at, s.worse) == ("old", "new", T0, False)
    assert model_switches(spans, counted, {}, [task], DAY_START, DAY_END) == []


def test_default_settings_price_no_model_switch() -> None:
    data, snapshot = _load()
    [section] = build_report(_inputs(data, snapshot), ONLY).sections
    assert not any(a.title.startswith("Try ") for a in section.advice)
    assert any(line.startswith("| triage | `model-large` |") for line in section.lines)


def test_section_settings_from_the_report_config() -> None:
    data, snapshot = _load()
    seen: list[FitSettings] = []

    class Ctx:  # a context that carries section settings, as ctx.config will
        def __init__(self, ctx: Any) -> None:
            self.__dict__.update({k: getattr(ctx, k) for k in ctx.__slots__})
            self.config = {"routine_roles": ["triage"], "allowed_models": ["model-tiny"]}

    def builder(ctx: Any) -> Any:
        seen.append(_settings(Ctx(ctx), None))
        return tokens_fit(Ctx(ctx))  # type: ignore[arg-type]

    [section] = build_report(_inputs(data, snapshot), {"tokens_fit": builder}).sections
    assert seen[0].routine_roles == ("triage",) and seen[0].allowed_models == ("model-tiny",)
    assert "Try `model-tiny` for triage" in {a.title for a in section.advice}
    assert _settings(Ctx.__new__(Ctx), None) == FitSettings()


def test_review_ignores_files_shared_across_tasks() -> None:
    found = [
        read("A", DAY_START - timedelta(hours=2), "agents", 3000, task="T1"),
        read("B", T0, "agents", 3000, task="T1"),
        read("C", T0, "agents", 3000, task="T2"),  # the same file in another task
        read("B", T0, "pr-file", 2000, task="T1"),
    ]
    assert review_rounds(found, ("reviewer",), DAY_START, DAY_END)[0].tokens == 2000


def test_a_recent_switch_is_too_early_to_tell() -> None:
    def call(i: int, at: datetime, model: str) -> Span:
        return Span(
            "t",
            f"c{i}",
            "chat",
            SpanKind.LLM,
            at,
            at + timedelta(seconds=5),
            task_id="T1",
            session_id="S",
            model=model,
            input_tokens=10,
            output_tokens=1,
        )

    spans = [
        call(1, DAY_START - timedelta(hours=5), "old"),
        call(2, DAY_END - timedelta(hours=2), "new"),
    ]
    counted = counted_llm({span_key(s): s for s in spans}, spans)
    task = Task("T1", "x", Status.IN_PROGRESS, T0)
    [s] = model_switches(spans, counted, {"S": "writer"}, [task], DAY_START, DAY_END)
    assert s.early and not s.worse and s.hold


def test_unknown_roles_get_no_advice() -> None:
    data, snapshot = _load()
    inputs = replace(_inputs(data, snapshot), owners=(), sessions=(), tasks=())
    [section] = build_report(inputs, CONFIGURED).sections
    assert section.advice == ()


def test_offered_tools_survive_ingest_and_are_cut() -> None:
    from tests.traces.test_semconv import one

    names = ",".join(f"tool{i:03d}" for i in range(40))
    span = one({"gen_ai.operation.name": "chat", "kelpie.tools.offered": names})
    kept = span.attributes["kelpie.tools.offered"]
    assert len(kept) == 256 and names.startswith(kept)
    assert offered_tools(kept)[-1] == "tool031"
    on_resource = mapped(
        [S(sid(1), name="op", attrs={"gen_ai.operation.name": "chat"})],
        resource={"kelpie.tools.offered": "Read,Bash"},
    ).spans[0]
    assert "kelpie.tools.offered" not in on_resource.attributes  # a span attribute only
