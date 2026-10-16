"""Token optimization: prompt cache, context growth and restarts from zero (fixture
``tokens-cache-context``), and the ``tokens`` section of the daily report."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from kelpie.core.actions import Severity
from kelpie.core.budgets import Price, PriceTable
from kelpie.core.model import Owner, Session, Snapshot, Span, SpanKind
from kelpie.core.serde import parse_datetime, parse_duration, snapshot_from_dict
from kelpie.core.spans import Attribution, counted_llm, span_key
from kelpie.core.states import OpenState
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.sections import SECTIONS, Section
from kelpie.report.sections.tokens import tokens as tokens_section
from kelpie.report.store import StateRow
from kelpie.traces.tokens import (
    SessionSteps,
    Step,
    TokenSettings,
    break_hint,
    cache_groups,
    cache_misses,
    context_growth,
    restarts,
    session_roles,
    session_steps,
)

FIXTURE = Path(__file__).parents[1] / "fixtures" / "traces" / "tokens-cache-context.yaml"
T0 = datetime(2026, 3, 2, 9, tzinfo=UTC)
CALL = timedelta(seconds=30)
ONLY = {"tokens": tokens_section}  # this section alone


# --- fixture ---------------------------------------------------------------------------


def expand_calls(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fixture-only ``calls`` -> span dicts (see the fixture's header comment)."""
    out: list[dict[str, Any]] = []
    for e in entries:
        sid, inputs = e["session"], e["input"]
        start, every = parse_datetime(e["start"]), parse_duration(e["every"])
        cached = e["cached"]
        if cached == "previous":
            cached = [0, *inputs[:-1]]
        overrides = {int(k): v for k, v in (e.get("cached_at") or {}).items()}
        tools = {int(k): v for k, v in (e.get("tools") or {}).items()}
        for i, size in enumerate(inputs, 1):
            at = start + (i - 1) * every
            common = {"trace_id": f"TR-{sid}", "task_id": e["task"], "session_id": sid}
            if i > 1:
                tool = tools.get(i, "bash")
                out.append(
                    common
                    | {
                        "span_id": f"{sid}-T{i}",
                        "name": f"execute_tool {tool}",
                        "kind": "tool",
                        "tool": tool,
                        "start": (at - CALL).isoformat(),
                        "end": (at - CALL / 2).isoformat(),
                    }
                )
            out.append(
                common
                | {
                    "span_id": f"{sid}-L{i}",
                    "name": "chat",
                    "kind": "llm",
                    "model": e["model"],
                    "start": at.isoformat(),
                    "end": (at + CALL).isoformat(),
                    "input_tokens": size,
                    "output_tokens": e["output"],
                    "cached_tokens": overrides.get(i, cached[i - 1]),
                }
            )
    return out


def _load() -> tuple[dict[str, Any], Snapshot]:
    data = yaml.safe_load(FIXTURE.read_text())
    assert data["id"] == FIXTURE.stem
    snapshot = snapshot_from_dict({**data["snapshot"], "spans": expand_calls(data["calls"])})
    return data, snapshot


def _prices(data: dict[str, Any]) -> PriceTable:
    return PriceTable(tuple((k, Price(**v)) for k, v in data["config"]["prices"].items()))


def _inputs(data: dict[str, Any], snapshot: Snapshot) -> ReportInputs:
    return ReportInputs(
        day=date.fromisoformat(data["day"]),
        tz=UTC,
        spans=snapshot.spans,
        tasks=snapshot.tasks,
        owners=snapshot.owners,
        sessions=snapshot.sessions,
        prices=_prices(data),
    )


def _day_sessions(snapshot: Snapshot, day: date) -> dict[str, SessionSteps]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    spans = [s for s in snapshot.spans if start <= s.start < start + timedelta(days=1)]
    index = {span_key(s): s for s in spans}
    return session_steps(spans, counted_llm(index, spans), *session_roles(snapshot))


def test_fixture_cache_growth_and_restarts() -> None:
    data, snapshot = _load()
    day = date.fromisoformat(data["day"])
    sessions = _day_sessions(snapshot, day)
    groups = {(g.role, g.model): g for g in cache_groups(sessions.values())}
    for spec in data["expected"]["cache"]:
        g = groups[(spec["role"], spec["model"])]
        assert (len(g.of("break")), len(g.of("expired")), len(g.of("partial"))) == (
            spec["break"],
            spec["expired"],
            spec["partial"],
        ), g
        assert g.missed() == spec["missed"]
    assert data["expected"]["break_hint_contains"] in break_hint(
        groups[("developer", "model-large")]
    )
    broken = {m.step.session_id for g in groups.values() for m in g.of("break")}
    assert not broken & set(data["not_expected"]["break_sessions"])

    growth = context_growth(sessions.values())
    runaway = {g.session.session_id for g in growth if g.runaway}
    assert runaway == set(data["expected"]["runaway"])
    assert not runaway & set(data["not_expected"]["runaway"])

    every = session_steps(
        snapshot.spans,
        counted_llm({span_key(s): s for s in snapshot.spans}, snapshot.spans),
        *session_roles(snapshot),
    )
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    abnormal = {x.id for x in snapshot.sessions if x.ended_at and x.final_status is None}
    found = restarts(every, start, start + timedelta(days=1), abnormal)
    flat = [
        {
            "session": r.session.session_id,
            "previous": r.previous.session_id,
            "steps": len(r.ramp),
            "tokens": r.tokens,
        }
        for r in found
    ]
    assert flat == data["expected"]["restarts"]
    assert not {r.session.session_id for r in found} & set(data["not_expected"]["restarts"])


def test_fixture_in_the_daily_report() -> None:
    data, snapshot = _load()
    report = build_report(_inputs(data, snapshot), ONLY)
    [section] = [s for s in report.sections if s.title == "Token optimization"]
    titles = [a.title for a in section.advice]
    for title in data["expected"]["advice"]:
        assert title in titles, titles
    for title in data["not_expected"]["advice"]:
        assert title not in titles
    by_title = {a.title: a for a in section.advice}
    breaks = by_title["Fix prompt-cache breaks for developer on `model-large`"]
    # 126k tokens read uncached at $3/M instead of cached at $0.3/M
    assert breaks.usd == pytest.approx(126_000 * 2.7 / 1e6)
    assert breaks.tasks == ("T-T01",)
    restart = by_title["Resume developer tasks instead of restarting from zero"]
    # the 54k of context it got back to (below the earlier 60k), read once uncached
    assert restart.usd == pytest.approx(54_000 * 3 / 1e6)
    assert restart.tasks == ("T-T02",)
    # the section's advice competes for the top 3: the three largest by dollars
    top = [a.title for a in report.recommendations]
    best = sorted(section.advice, key=lambda a: -a.usd)[:3]
    assert top == [a.title for a in best]
    markdown = render_markdown(report)
    assert "## Token optimization" in markdown
    assert "step 6 of S-T01 (after `load_skill`)" in markdown
    assert "- S-T01 (developer, T-T01): 12 steps, context 10,000 → 120,000 tokens" in markdown
    assert "- S-T03 restarted T-T02 after S-T02" in markdown
    assert "| triage | `model-small` | 6 | 0% | 0 | 0 | 5 | 100,000 |" in markdown


def test_no_model_calls_no_section() -> None:
    report = build_report(ReportInputs(day=date(2026, 3, 2), tz=UTC), ONLY)
    assert not [s for s in report.sections if s.title == "Token optimization"]


def test_section_without_prices_still_reports_tokens() -> None:
    data, snapshot = _load()
    inputs = _inputs(data, snapshot)
    report = build_report(
        ReportInputs(
            day=inputs.day,
            tz=UTC,
            spans=inputs.spans,
            tasks=inputs.tasks,
            owners=inputs.owners,
            sessions=inputs.sessions,
        ),
        ONLY,
    )
    [section] = [s for s in report.sections if s.title == "Token optimization"]
    assert section.advice and all(a.usd == 0 for a in section.advice)
    assert any("126,000" in line for line in section.lines)


# --- unit cases --------------------------------------------------------------------------


def step(
    i: int,
    input_tokens: int,
    cached: int,
    at: datetime | None = None,
    model: str | None = "m",
    after: str | None = None,
) -> Step:
    start = at or T0 + timedelta(minutes=i)
    return Step(
        key=("t", f"s{i}"),
        session_id="S",
        index=i,
        start=start,
        end=start + CALL,
        model=model,
        input_tokens=input_tokens,
        output_tokens=10,
        cached_tokens=cached,
        after_tool=after,
    )


def session(*steps: Step, role: str = "dev", task: str | None = "T") -> SessionSteps:
    return SessionSteps("S", role, task, tuple(steps))


def test_short_prompts_model_switches_and_parallel_calls_are_not_checked() -> None:
    small = session(step(1, 500, 0), step(2, 900, 0))
    assert cache_misses(small) == ({}, ())
    switched = session(step(1, 10_000, 0), step(2, 12_000, 0, model="other"))
    assert cache_misses(switched)[1] == ()
    parallel = session(step(1, 10_000, 0), step(2, 12_000, 0, at=T0 + timedelta(minutes=1)))
    assert cache_misses(parallel)[1] == ()


def test_classification_order_expired_before_break() -> None:
    late = T0 + timedelta(minutes=30)
    s = session(step(1, 10_000, 0), step(2, 20_000, 9_000), step(3, 30_000, 0, at=late))
    checked, misses = cache_misses(s)
    assert checked == {"m": 2}
    assert [(m.step.index, m.kind, m.missed) for m in misses] == [(3, "expired", 20_000)]
    s = session(step(1, 10_000, 0), step(2, 20_000, 9_000), step(3, 30_000, 1_000))
    assert [m.kind for m in cache_misses(s)[1]] == ["break"]
    settings = TokenSettings(cache_ttl=timedelta(hours=1))
    late_ok = session(step(1, 10_000, 0), step(2, 20_000, 9_000), step(3, 30_000, 0, at=late))
    assert [m.kind for m in cache_misses(late_ok, settings)[1]] == ["break"]


def test_hit_rate_and_empty_group() -> None:
    g = cache_groups([session(step(1, 10_000, 0), step(2, 20_000, 10_000))])[0]
    assert g.hit_rate == pytest.approx(10_000 / 30_000)
    assert g.missed() == 0 and break_hint(g) == ""
    assert cache_groups([]) == []


def test_break_hints() -> None:
    every = session(
        step(1, 10_000, 0),
        step(2, 20_000, 10_000),
        step(3, 30_000, 1_000),
        step(4, 40_000, 30_000),
        step(5, 50_000, 1_000),
    )
    [g] = cache_groups([every])
    assert "most calls" in break_hint(g)
    mixed = session(
        step(1, 10_000, 0),
        step(2, 20_000, 10_000),
        step(3, 30_000, 1_000, after="a"),
        step(4, 40_000, 30_000),
        step(5, 50_000, 40_000),
        step(6, 60_000, 50_000),
        step(7, 70_000, 1_000, after="b"),
        step(8, 80_000, 70_000),
        step(9, 90_000, 80_000),
    )
    [g] = cache_groups([mixed])
    assert break_hint(g).startswith("keep the system prompt")


def test_growth_runaway_needs_steps_size_and_factor() -> None:
    sizes = [40_000, 60_000, 90_000, 110_000, 120_000]
    big_start = session(*(step(i, n, 0) for i, n in enumerate(sizes, 1)))
    [g] = context_growth([big_start])
    assert not g.runaway and g.peak == 120_000  # only 3x its first context
    sizes = [10_000, 50_000, 150_000]
    few = session(*(step(i, n, 0) for i, n in enumerate(sizes, 1)))
    assert not context_growth([few])[0].runaway
    sizes = [10_000, 30_000, 60_000, 90_000, 101_000]
    grows = session(*(step(i, n, 0) for i, n in enumerate(sizes, 1)))
    [g] = context_growth([grows])
    assert g.runaway and g.per_step == pytest.approx(91_000 / 4)
    assert [u.input_tokens for u in g.excess] == [10_000, 40_000, 51_000]


def test_restart_needs_same_role_an_ended_predecessor_and_a_small_start() -> None:
    day = T0.replace(hour=0)

    def sess(sid: str, role: str, at: datetime, sizes: list[int]) -> SessionSteps:
        steps = tuple(
            Step(
                ("t", f"{sid}{i}"),
                sid,
                i,
                at + timedelta(minutes=i),
                at + timedelta(minutes=i) + CALL,
                "m",
                n,
                0,
                0,
            )
            for i, n in enumerate(sizes, 1)
        )
        return SessionSteps(sid, role, "T", steps)

    a = sess("A", "dev", T0, [10_000, 50_000])
    b = sess("B", "dev", T0 + timedelta(hours=1), [5_000, 20_000, 60_000])
    bad = {"A"}  # A left the task unfinished
    found = restarts({"A": a, "B": b}, day, day + timedelta(days=1), bad)
    assert [(r.session.session_id, len(r.ramp), r.tokens) for r in found] == [("B", 2, 20_000)]
    # after a declared hand-off the next session is a normal one
    assert restarts({"A": a, "B": b}, day, day + timedelta(days=1), set()) == []
    other_role = sess("B", "reviewer", T0 + timedelta(hours=1), [5_000])
    assert restarts({"A": a, "B": other_role}, day, day + timedelta(days=1), bad) == []
    overlapping = sess("B", "dev", T0 + timedelta(minutes=1), [5_000])
    assert restarts({"A": a, "B": overlapping}, day, day + timedelta(days=1), bad) == []
    tomorrow = day + timedelta(days=1)
    assert restarts({"A": a, "B": b}, tomorrow, tomorrow + timedelta(days=1), bad) == []
    unknown = {k: replace(v, role="unknown") for k, v in {"A": a, "B": b}.items()}
    assert restarts(unknown, day, day + timedelta(days=1), bad) == []
    # the ramp is at most as long as the earlier session
    long = sess("B", "dev", T0 + timedelta(hours=1), [1_000, 2_000, 3_000, 4_000])
    [r] = restarts({"A": a, "B": long}, day, day + timedelta(days=1), bad)
    assert len(r.ramp) == 2 and r.tokens == 2_000


def test_session_roles_fall_back_to_the_task_role() -> None:
    snapshot = snapshot_from_dict(
        {
            "now": T0.isoformat(),
            "tasks": [
                {
                    "id": "T1",
                    "title": "x",
                    "status": "in_progress",
                    "status_since": T0.isoformat(),
                    "role": "writer",
                }
            ],
        }
    )
    roles, tasks = session_roles(snapshot, Attribution(session_tasks={"S9": "T1"}))
    assert roles == {"S9": "writer"} and tasks == {"S9": "T1"}


def test_steps_skip_spans_without_a_session_and_nested_calls() -> None:
    outer = Span(
        "t",
        "a",
        "chat",
        SpanKind.LLM,
        T0,
        T0 + CALL,
        session_id="S",
        model="m",
        input_tokens=100,
        output_tokens=5,
    )
    inner = Span(
        "t",
        "b",
        "chat",
        SpanKind.LLM,
        T0,
        T0 + CALL,
        parent_id="a",
        session_id="S",
        model="m",
        input_tokens=100,
        output_tokens=5,
    )
    loose = Span("t", "c", "chat", SpanKind.LLM, T0, T0 + CALL, model="m", input_tokens=7)
    spans = [outer, inner, loose]
    found = session_steps(spans, counted_llm({span_key(s): s for s in spans}, spans))
    assert list(found) == ["S"] and len(found["S"].steps) == 1
    assert found["S"].role == "unknown" and found["S"].task_id is None


def test_section_builder_is_pure_over_its_context() -> None:
    data, snapshot = _load()
    report = build_report(_inputs(data, snapshot), ONLY)
    again = build_report(_inputs(data, snapshot), ONLY)
    first = [s for s in report.sections if s.title == "Token optimization"]
    assert first == [s for s in again.sections if s.title == "Token optimization"]
    assert isinstance(first[0], Section)
    ctx_free = tokens_section  # registered under "tokens"
    assert SECTIONS["tokens"] is ctx_free


def test_a_fixed_cached_prefix_is_a_partial_miss_with_its_own_advice() -> None:
    sizes = [10_000, 30_000, 50_000, 70_000, 90_000]
    spans = [
        Span(
            "t",
            f"c{i}",
            "chat",
            SpanKind.LLM,
            T0 + timedelta(minutes=i),
            T0 + timedelta(minutes=i) + CALL,
            session_id="S1",
            model="m",
            input_tokens=n,
            output_tokens=10,
            cached_tokens=2_000,
        )
        for i, n in enumerate(sizes)
    ]
    prices = PriceTable((("m", Price(input=3.0, output=15.0, cached_input=0.3)),))
    inputs = ReportInputs(
        day=date(2026, 3, 2),
        tz=UTC,
        spans=spans,
        prices=prices,
        owners=(Owner("dev-1", "dev"),),
        sessions=(Session("S1", "T", "dev-1", T0),),
    )
    report = build_report(inputs, ONLY)
    [section] = [s for s in report.sections if s.title == "Token optimization"]
    [advice] = section.advice
    assert advice.title == "Cache the growing conversation for dev on `m`"
    # four calls each re-read the previous prompt, 2k of it from the cache
    assert "Up to 152,000 tokens a day" in advice.detail
    assert advice.usd == pytest.approx(152_000 * 2.7 / 1e6)
    assert "**Restarts from zero**: none" in "\n".join(section.lines)


def test_no_priced_caching_advice_for_a_model_that_never_reports_cache_reads() -> None:
    sizes = [10_000, 30_000, 50_000, 70_000, 90_000]
    spans = [
        Span(
            "t",
            f"c{i}",
            "chat",
            SpanKind.LLM,
            T0 + timedelta(minutes=i),
            T0 + timedelta(minutes=i) + CALL,
            session_id="S1",
            model="m",
            input_tokens=n,
            output_tokens=10,
        )
        for i, n in enumerate(sizes)
    ]
    owners = (Owner("dev-1", "dev"),)
    sessions = (Session("S1", "T", "dev-1", T0),)
    prices = PriceTable((("m", Price(input=3.0, output=15.0, cached_input=0.3)),))
    inputs = ReportInputs(
        day=date(2026, 3, 2), tz=UTC, spans=spans, owners=owners, sessions=sessions, prices=prices
    )
    [section] = build_report(inputs, ONLY).sections
    assert section.advice == ()
    assert any("No call of `m` reported a cache read" in line for line in section.lines)
    # one call of the same model elsewhere that reads from the cache: the misses are real
    other = replace(spans[0], span_id="x", session_id="S2", cached_tokens=5_000)
    inputs = replace(inputs, spans=[*spans, other])
    [section] = build_report(inputs, ONLY).sections
    assert [a.title for a in section.advice] == ["Turn on prompt caching for dev on `m`"]


def test_unknown_roles_get_no_advice_and_cells_are_escaped() -> None:
    data, snapshot = _load()
    inputs = replace(_inputs(data, snapshot), owners=(), sessions=(), tasks=())
    [section] = build_report(inputs, ONLY).sections
    assert section.advice == ()
    assert any(line.startswith("| unknown |") for line in section.lines)
    roles = {"S-T01": "dev|ops"}
    owners = (Owner("o", "dev|ops"),)
    sessions = (Session("S-T01", "T-T01", "o", T0),)
    inputs = replace(_inputs(data, snapshot), owners=owners, sessions=sessions, tasks=())
    [section] = build_report(inputs, ONLY).sections
    assert any(line.startswith("| dev\\|ops |") for line in section.lines), roles


def test_openllmetry_cache_reads_are_mapped() -> None:
    from tests.traces.test_semconv import one

    s = one(
        {
            "gen_ai.operation.name": "chat",
            "gen_ai.usage.input_tokens": 9_000,
            "gen_ai.usage.cache_read_input_tokens": 8_000,
        }
    )
    assert (s.input_tokens, s.cached_tokens) == (9_000, 8_000)
    assert "gen_ai.usage.cache_read_input_tokens" not in s.attributes


def test_sessions_already_burned_as_chaos_get_no_token_advice() -> None:
    data, snapshot = _load()
    stuck = OpenState(
        "no_progress:S-T01",
        "no_progress",
        T0,
        T0,
        T0 + timedelta(hours=2),
        0,
        Severity.WARNING,
        "no progress",
    )
    inputs = replace(_inputs(data, snapshot), states=(StateRow(stuck),))
    [section] = build_report(inputs, ONLY).sections
    titles = {a.title for a in section.advice}
    assert "Fix prompt-cache breaks for developer on `model-large`" not in titles
    assert "Bound the context of developer sessions" not in titles
    assert "Resume developer tasks instead of restarting from zero" in titles  # S-T03
    assert any("step 6 of S-T01" in line for line in section.lines)  # still reported


def test_settings_from_the_report_config() -> None:
    from kelpie.report.sections import SECTION_KEYS

    data, snapshot = _load()
    keys = {"tokens": SECTION_KEYS["tokens"]}
    quiet = replace(
        _inputs(data, snapshot), section_config={"tokens": {"min_saving_tokens": 10**9}}
    )
    [section] = build_report(quiet, ONLY, keys).sections
    assert section.advice == () and section.lines  # still reported, nothing recommended
    misspelt = replace(quiet, section_config={"tokens": {"min_savings": 1}})
    [note] = build_report(misspelt, ONLY, keys).sections
    assert "unknown keys ['min_savings']" in note.lines[0]
