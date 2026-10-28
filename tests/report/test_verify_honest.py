"""Honest before/after (#193): what the report can and cannot separate.

Synthetic farm, modelled on a live case: three fixes landed within a day on one farm, an
output filter (rtk) for ``git diff`` on two roles, an agent-instructions change for every
role, and a summary mode of the build script. Their effects overlap, so the report
measures them together and credits each fix only with what it alone can explain.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Severity
from kelpie.core.model import Owner, Session, Span, SpanKind, Status
from kelpie.core.states import OpenState
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.fixes import CONFIG, MANUAL, FixEvent
from kelpie.report.store import StateRow
from kelpie.report.verify import Evidence, overlapping, verify_group
from kelpie.traces.outputs import Call
from tests.report.test_md import cells, live

DAY = date(2026, 10, 20)  # the report's day; the day before is Oct 19
RTK = datetime(2026, 10, 19, 14, tzinfo=UTC)  # rtk on the developers, reviewers 20 min on
INSTRUCTIONS = datetime(2026, 10, 20, 2, tzinfo=UTC)  # every role: no in-session CI polling
BUILD = datetime(2026, 10, 20, 9, tzinfo=UTC)  # the build script's summary mode
OWNERS = (
    Owner("dev-1", "developer"),
    Owner("rev-1", "reviewer"),
    Owner("ios-1", "ios"),
)
H = timedelta(hours=1)


def session(sid: str, owner: str, start: datetime, minutes: int, failed: bool = False) -> Session:
    return Session(
        sid,
        f"T-{sid}",
        owner,
        start,
        start + timedelta(minutes=minutes),
        Status.FAILED if failed else Status.DONE,
    )


# Long sessions that hold the tool calls, plus one finished session per hour and role.
HOLDERS = tuple(
    Session(f"{o.role}-run", f"T-{o.role}", o.id, datetime(2026, 10, 17, tzinfo=UTC))
    for o in OWNERS
)
HOURLY = tuple(
    session(f"{o.id}-{h}", o.id, datetime(2026, 10, 17, tzinfo=UTC) + h * H, 30, h % 5 == 0)
    for o in OWNERS
    for h in range(96)
)


def tool(sid: str, at: datetime, key: str, chars: int, filt: str | None = None, n: int = 0) -> Span:
    attrs: dict[str, Any] = {"kelpie.tool.command_prefix": key, "kelpie.tool.output_chars": chars}
    if filt:
        attrs["kelpie.tool.filter"] = filt
    return Span(f"tr-{sid}", f"{sid}-{key}-{at:%d%H%M}-{n}", "execute_tool", SpanKind.TOOL,
                at, at, session_id=sid, attributes=attrs)  # fmt: skip


def chat(sid: str, at: datetime, tokens: int) -> Span:
    return Span(f"tr-{sid}", f"{sid}-chat-{at:%d%H%M}", "chat", SpanKind.LLM, at, at,
                session_id=sid, model="m", input_tokens=tokens)  # fmt: skip


def hours(start: datetime, end: datetime, step: timedelta = H) -> Iterable[datetime]:
    at = start
    while at < end:
        yield at
        at += step


def live_case_spans() -> list[Span]:
    begin, end = datetime(2026, 10, 17, tzinfo=UTC), datetime(2026, 10, 21, tzinfo=UTC)
    spans: list[Span] = []
    for role, switched in (("developer", RTK), ("reviewer", RTK + 20 * timedelta(minutes=1))):
        for at in hours(begin, end, 30 * timedelta(minutes=1)):
            filt = "rtk" if at >= switched else None
            spans.append(tool(f"{role}-run", at, "git diff", 4_000 if filt else 40_000, filt))
    for at in hours(begin, end, 30 * timedelta(minutes=1)):
        spans.append(tool("ios-run", at, "make build", 2_000 if at >= BUILD else 80_000))
        for o in OWNERS:
            spans.append(chat(f"{o.role}-run", at, 50_000 if at < INSTRUCTIONS else 30_000))
    return spans


def inputs(**kw: Any) -> ReportInputs:
    base: dict[str, Any] = {
        "day": DAY,
        "tz": UTC,
        "owners": OWNERS,
        "sessions": (*HOLDERS, *HOURLY),
        "min_sample": 5,
    }
    return ReportInputs(**{**base, **kw})


LIVE_FIXES = (
    FixEvent(INSTRUCTIONS, MANUAL, "no in-session CI polling; screenshots downscaled"),
    FixEvent(BUILD, MANUAL, "build script prints a summary", command="make build", role="ios"),
)


def test_overlapping_fixes_are_measured_together_and_credited_to_none() -> None:
    report = build_report(inputs(spans=live_case_spans(), fixes=LIVE_FIXES))
    [v] = report.verifications
    assert v.together
    kinds = [(e.kind, e.role, e.command) for e in v.events]
    assert kinds == [
        ("filter", "developer", "git diff"),
        ("filter", "reviewer", "git diff"),
        (MANUAL, None, None),
        (MANUAL, "ios", "make build"),
    ]
    assert v.span == BUILD - RTK  # 19 hours from the first to the last
    # Together: from a day before the first fix to a day after the last, farm-wide.
    tokens = next(m for m in v.metrics if m.name == "tokens per day")
    assert tokens.before is not None and tokens.after is not None
    assert tokens.after < tokens.before and not tokens.small
    # Credited alone: each command's output per call, over its own window, nothing else.
    own = {(c.event.role, c.metric.name.split(" call")[0]): c.metric for c in v.own}
    assert set(own) == {
        ("developer", "output tokens per `git diff`"),
        ("reviewer", "output tokens per `git diff`"),
        ("ios", "output tokens per `make build`"),
    }
    assert own[("ios", "output tokens per `make build`")].change == pytest.approx(-0.975)
    # The instructions change touches every role: no role's tokens are credited to a fix.
    assert not any(c.metric.name.startswith("tokens per day") for c in v.own)
    markdown = render_markdown(report)
    assert "**4 fixes within 19.0h of each other**: their effects cannot be separated" in markdown
    assert "credited to none of them alone" in markdown
    assert "Per fix, only where one fix alone can explain it" in markdown


def test_fixes_on_different_roles_are_credited_per_role() -> None:
    dev = FixEvent(RTK, MANUAL, "developer prompt trimmed", role="developer")
    rev = FixEvent(RTK + H, MANUAL, "reviewer reads the diff only", role="reviewer")
    spans = [chat(f"{o.role}-run", at, 10_000) for o in OWNERS
             for at in hours(datetime(2026, 10, 17, tzinfo=UTC), RTK + 30 * H)]  # fmt: skip
    report = build_report(inputs(spans=spans, fixes=(dev, rev)))
    [v] = report.verifications
    assert v.together
    credited = {(c.event.role, c.metric.name) for c in v.own}
    assert ("developer", "tokens per day (developer)") in credited
    assert ("reviewer", "tokens per day (reviewer)") in credited
    assert any(name.startswith("review time") for role, name in credited if role == "reviewer")
    assert not any(role == "developer" and n.startswith("review") for role, n in credited)


def test_the_same_role_twice_is_never_credited_to_one() -> None:
    a = FixEvent(RTK, MANUAL, "first", role="developer", command="git diff")
    b = FixEvent(RTK + 2 * H, MANUAL, "second", role="developer", command="git diff")
    spans = [tool("developer-run", at, "git diff", 10_000)
             for at in hours(RTK - 24 * H, RTK + 30 * H)]  # fmt: skip
    report = build_report(inputs(spans=spans, fixes=(a, b)))
    [v] = report.verifications
    assert v.events == (a, b) and v.own == ()
    names = [m.name for m in v.metrics]
    assert names.count("output tokens per `git diff` call (developer)") == 1


def test_fixes_a_window_apart_are_separate() -> None:
    a = FixEvent(datetime(2026, 10, 19, 1, tzinfo=UTC), MANUAL, "first")
    b = FixEvent(datetime(2026, 10, 20, 2, tzinfo=UTC), MANUAL, "second")
    assert overlapping([b, a], timedelta(days=1)) == [(a,), (b,)]
    assert overlapping([b, a], timedelta(days=2)) == [(a, b)]
    report = build_report(inputs(fixes=(a, b)))
    assert [len(v.events) for v in report.verifications] == [1, 1]


def test_a_recent_fix_is_grouped_with_an_older_one_it_overlaps() -> None:
    older = FixEvent(datetime(2026, 10, 18, 20, tzinfo=UTC), MANUAL, "older, reported before")
    recent = FixEvent(datetime(2026, 10, 19, 6, tzinfo=UTC), MANUAL, "recent")
    report = build_report(inputs(fixes=(older, recent)))
    [v] = report.verifications
    assert v.events == (older, recent)
    # an older fix alone, outside the report's two days, is not reported again
    assert build_report(inputs(fixes=(older,))).verifications == ()


def test_traces_that_start_inside_the_window_are_not_compared() -> None:
    fix = FixEvent(datetime(2026, 10, 19, 12, tzinfo=UTC), MANUAL, "traces on, then a fix")
    spans = [chat("developer-run", at, 10_000)
             for at in hours(fix.at - 3 * H, fix.at + 20 * H)]  # fmt: skip
    report = build_report(inputs(spans=spans, fixes=(fix,)))
    [v] = report.verifications
    assert not any(m.name.startswith("tokens per day") for m in v.metrics)
    [x] = v.excluded
    assert x.name == "tokens per day" and x.first == fix.at - 3 * H
    assert x.after == pytest.approx(200_000)
    markdown = render_markdown(report)
    assert "Not compared (no full before to compare with)" in markdown
    assert "a newly enabled signal, or something new; after: 200,000." in markdown
    # the same traces from the start of the before-window on: compared as usual
    early = [chat("developer-run", fix.at - 24 * H, 0), *spans]
    [v] = build_report(inputs(spans=early, fixes=(fix,))).verifications
    assert v.excluded == () and any(m.name == "tokens per day" for m in v.metrics)


def _wait(at: datetime, hours_open: int, n: int) -> StateRow:
    return StateRow(
        OpenState(f"wait_condition:T-{n}", "wait_condition", at, at, at + hours_open * H, 0,
                  Severity.WARNING, "s", f"T-{n}")
    )  # fmt: skip


def test_a_cause_a_config_change_turned_on_is_listed_not_compared() -> None:
    config = FixEvent(datetime(2026, 10, 19, 8, tzinfo=UTC), CONFIG, "config changed")
    states = tuple(_wait(config.at + (i + 1) * H, 3, i) for i in range(6))
    report = build_report(inputs(fixes=(config,), states=states))
    [v] = report.verifications
    assert [x.name for x in v.excluded] == ["hours lost per day (wait_condition)"]
    assert v.excluded[0].after == pytest.approx(18)
    assert "new, worse" not in render_markdown(report)
    # without a config change, the same cause showing up after a fix is a new problem
    manual = FixEvent(config.at, MANUAL, "a manual fix")
    report = build_report(inputs(fixes=(manual,), states=states))
    assert "new, worse" in render_markdown(report)
    # a cause already seen before the window is compared, config change or not
    seen = (_wait(config.at - 20 * H, 2, 99), *states)
    [v] = build_report(inputs(fixes=(config,), states=seen)).verifications
    assert v.excluded == ()


def test_small_samples_say_direction_only_with_their_sizes() -> None:
    fix = FixEvent(BUILD, MANUAL, "build script prints a summary", command="make build")
    spans = [tool("ios-run", BUILD + d * H, "make build", 8_000, n=d) for d in (-2, -1, 1)]
    report = build_report(inputs(spans=spans, fixes=(fix,), min_sample=5))
    markdown = render_markdown(report)
    assert "| output tokens per `make build` call (direction only) | 2,000 | 2,000 |" in markdown
    assert "| 2 / 1 |" in markdown
    assert "fewer than 5 observations on a side" in markdown


def test_verify_group_of_one_is_verify() -> None:
    fix = FixEvent(RTK, MANUAL, "x")
    ev = Evidence(end=RTK + 24 * H)
    v = verify_group((fix,), ev)
    assert v.events == (fix,) and not v.together and v.own == ()


def test_a_chain_reaching_past_the_loaded_fixes_says_so() -> None:
    # A chain of fixes, each less than a day after the previous one: one group. The report
    # of Oct 20 reads fixes from Oct 17 on; the chain starts 6 hours after that.
    chain = tuple(
        FixEvent(datetime(2026, 10, 17, 6, tzinfo=UTC) + i * 16 * H, MANUAL, f"fix {i}")
        for i in range(4)
    )  # Oct 17 06:00, 22:00, Oct 18 14:00, Oct 19 06:00
    report = build_report(inputs(fixes=chain))
    [v] = report.verifications
    assert v.events == chain
    assert v.truncated == datetime(2026, 10, 17, tzinfo=UTC)
    assert "an older fix may belong to this group" in render_markdown(report)
    # a group that starts a window after the cut is whole
    [v] = build_report(inputs(fixes=LIVE_FIXES)).verifications
    assert v.truncated is None


def test_an_idle_start_of_the_history_is_not_traces_turning_on() -> None:
    from kelpie.core.spans import span_key

    # The loaded history starts Oct 17 00:00; the farm was idle until Oct 17 09:00.
    fix = FixEvent(datetime(2026, 10, 18, 2, tzinfo=UTC), MANUAL, "x")
    spans = [chat("developer-run", at, 1_000)
             for at in hours(datetime(2026, 10, 17, 9, tzinfo=UTC), fix.at + 30 * H)]  # fmt: skip
    counted = {span_key(s): "m" for s in spans}
    ev = Evidence(end=fix.at + 24 * H, spans=spans, counted=counted,
                  known_from=datetime(2026, 10, 17, tzinfo=UTC))  # fmt: skip
    v = verify_group((fix,), ev)
    assert v.excluded == () and any(m.name == "tokens per day" for m in v.metrics)
    # without knowing where the history starts, the same traces count as newly enabled
    unknown = Evidence(end=ev.end, spans=spans, counted=counted)
    assert [x.name for x in verify_group((fix,), unknown).excluded] == ["tokens per day"]


def test_hostile_text_in_a_group_keeps_its_cells() -> None:
    from kelpie.core.spans import span_key

    role = "dev | [x](https://evil.example)"
    a = FixEvent(RTK, MANUAL, "note\n# heading | cell", role=role, command="a|b `c`")
    b = FixEvent(RTK + H, MANUAL, "other", role="reviewer", rule="r|s")
    spans = [chat("S-1", at, 1_000) for at in hours(RTK - 30 * H, RTK + 30 * H)]
    calls = [Call(at, role, "a|b `c`", 100, False, None, None, None, None)
             for at in hours(RTK - 20 * H, RTK + 20 * H)]  # fmt: skip
    ev = Evidence(end=RTK + 30 * H, spans=spans, counted={span_key(s): "m" for s in spans},
                  roles={"S-1": role}, calls=calls)  # fmt: skip
    v = verify_group((a, b), ev)
    assert v.own and v.metrics
    lines = render_lines(build_report(inputs(fixes=())), v)
    tables = [ln for ln in lines if ln.startswith("|")]
    assert len(tables) > 4
    for row in tables:
        assert cells(row) == cells(header_of(lines, row)), row
    text = "\n".join(lines)
    assert not any(ln.startswith("#") for ln in lines)
    assert not live("[x](", text) and not live("<", text)


def render_lines(report: Any, v: Any) -> list[str]:
    from dataclasses import replace

    from kelpie.report.daily import _verification_lines

    return _verification_lines(replace(report, verifications=(v,)))


def header_of(lines: list[str], row: str) -> str:
    i = lines.index(row)
    while i > 0 and lines[i - 1].startswith("|"):
        i -= 1
    return lines[i]


def test_the_llm_digest_credits_a_group_to_no_one_fix() -> None:
    from kelpie.report.root_cause import digest

    report = build_report(inputs(spans=live_case_spans(), fixes=LIVE_FIXES))
    [check] = digest(report).data["fix_checks"]
    assert check["rule"] is None and check["kind"] == "group"
    assert check["fixes_together"] == 4
