"""Names from agents, the board and exporters stay text in the token, model-fit and
throughput sections: no table row splits, no link, no tag, no new line."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from kelpie.core.budgets import Price, PriceTable
from kelpie.core.model import Owner, Session, Span, SpanKind, Status, Task
from kelpie.core.serde import snapshot_from_dict
from kelpie.report import md
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.sections import Section
from kelpie.report.sections.throughput import throughput_section
from kelpie.report.sections.tokens import tokens as tokens_section
from kelpie.report.sections.tokens_fit import tokens_fit
from kelpie.traces.fit import FitSettings
from tests.incidents import expand_repeats
from tests.report.test_md import cells, live
from tests.traces.test_tokens import expand_calls

FIXTURES = Path(__file__).parents[1] / "fixtures" / "traces"
# a backtick, ]-closed link text, pipes (also after an even run of backslashes), a tag and
# a newline: the characters that break a code span, a table row, or the line
EVIL = "`]|\\\\|<b>``\nnext"
LINK = "[x](https://evil.example)"


def hostile(name: str, link: bool = True) -> str:
    return f"{name}{EVIL}{LINK if link else ''}"


def rename(value: Any, names: dict[str, str]) -> Any:
    """``value`` with every string equal to a key (or a comma-separated list of keys)
    replaced; dict keys too."""
    if isinstance(value, dict):
        return {rename(k, names): rename(v, names) for k, v in value.items()}
    if isinstance(value, list):
        return [rename(v, names) for v in value]
    if isinstance(value, str):
        if value in names:
            return names[value]
        if "," in value and " " not in value:
            return ",".join(names.get(p, p) for p in value.split(","))
    return value


def hostile_names(data: dict[str, Any], tools: tuple[str, ...]) -> dict[str, str]:
    snap = data["snapshot"]
    plain = {o["role"] for o in snap["owners"]}  # roles
    plain |= {t["id"] for t in snap["tasks"]} | {s["id"] for s in snap["sessions"]}
    plain |= set(tools)
    names = {p: hostile(p) for p in plain}
    names |= {m: hostile(m, link=False) for m in data["config"]["prices"]}  # no fnmatch `[`
    return names


def unspanned(line: str) -> str:
    """``line`` without its code spans (which render literally) and escaped characters."""
    out, i = [], 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
        elif line[i] == "`":
            run = re.match(r"`+", line[i:]).group()  # type: ignore[union-attr]
            end = re.search(r"(?<!`)" + re.escape(run) + r"(?!`)", line[i + len(run) :])
            i = i + len(run) + end.end() if end else i + len(run)
        else:
            out.append(line[i])
            i += 1
    return "".join(out)


def assert_safe(section: Section) -> None:
    """Every table row keeps its columns, no line has a live link or tag, and nothing but
    one line of text sits in a title or detail."""
    rows: list[str] = []
    for line in section.lines:
        assert "\n" not in line
        flat = unspanned(line)
        assert not live("](", flat) and not live("<", flat), line
        if line.startswith("|"):
            rows.append(line)
        elif rows:
            assert len({cells(r) for r in rows}) == 1, rows
            rows = []
    assert len({cells(r) for r in rows}) <= 1, rows
    for a in section.advice:
        for field in (a.title, a.detail):
            assert "\n" not in field
            flat = unspanned(field)
            assert not live("](", flat) and not live("<", flat), field


def render(report_sections: tuple[Section, ...]) -> str:
    return "\n".join(
        [*(line for s in report_sections for line in s.lines)]
        + [f"{a.title} {a.detail}" for s in report_sections for a in s.advice]
    )


def prices(data: dict[str, Any], names: dict[str, str]) -> PriceTable:
    return PriceTable(
        tuple((names.get(k, k), Price(**v)) for k, v in data["config"]["prices"].items())
    )


def test_tokens_section_keeps_hostile_names_as_text() -> None:
    data = yaml.safe_load((FIXTURES / "tokens-cache-context.yaml").read_text())
    names = hostile_names(data, ("load_skill",))
    data = rename(data, names)
    snapshot = snapshot_from_dict({**data["snapshot"], "spans": expand_calls(data["calls"])})
    inputs = ReportInputs(
        day=date.fromisoformat(data["day"]), tz=UTC, spans=snapshot.spans, tasks=snapshot.tasks,
        owners=snapshot.owners, sessions=snapshot.sessions, prices=prices(data, {}),
    )  # fmt: skip
    [section] = build_report(inputs, {"tokens": tokens_section}).sections
    assert_safe(section)
    out = render((section,))
    assert md.code(names["load_skill"]) in out  # the break hint
    assert md.text(names["developer"]) in out and md.text(names["S-T01"]) in out
    assert md.text(names["T-T02"]) in out  # the restart table
    assert section.advice
    assert md.code(names["model-large"], cell=True) in out


def test_tokens_fit_section_keeps_hostile_names_as_text() -> None:
    data = yaml.safe_load((FIXTURES / "tokens-fit.yaml").read_text())
    tools = ("Read", "Edit", "Bash", "WebSearch", "NotebookEdit", "git status", "pytest")
    names = hostile_names(data, tools)
    data = rename(data, names)
    spans = expand_repeats(data["snapshot"]["spans"])
    snapshot = snapshot_from_dict({**data["snapshot"], "spans": spans})
    inputs = ReportInputs(
        day=date.fromisoformat(data["day"]), tz=UTC, spans=snapshot.spans, tasks=snapshot.tasks,
        owners=snapshot.owners, sessions=snapshot.sessions, prices=prices(data, {}),
        review_roles=(names["reviewer"],),
    )  # fmt: skip
    cfg = FitSettings(routine_roles=(names["triage"], names["writer"]))
    [section] = build_report(inputs, {"tokens_fit": lambda ctx: tokens_fit(ctx, cfg)}).sections
    assert_safe(section)
    out = render((section,))
    assert md.code(f"tool:{names['Read']}", cell=True) in out  # the fragment key
    assert md.text(names["developer"]) in out and md.text(names["T-D1"]) in out
    assert md.code(names["model-large"], cell=True) in out
    assert md.code(names["WebSearch"]) in out  # unused tools
    assert any(a.title.startswith("Try ") for a in section.advice)


def test_throughput_section_keeps_hostile_roles_as_text() -> None:
    dev, ghost = hostile("dev"), hostile("ghost")
    day = datetime(2026, 3, 2, tzinfo=UTC)
    owners = (Owner("dev-1", dev),)
    tasks = (
        Task("T1", "t", Status.DONE, day + timedelta(hours=16), role=dev, owner_id="dev-1"),
        Task("T9", "t", Status.BACKLOG, day, role=ghost),
    )
    sessions = (
        Session("S1", "T1", "dev-1", day + timedelta(hours=8),
                ended_at=day + timedelta(hours=10), final_status=Status.REVIEW),
    )  # fmt: skip
    start = day + timedelta(hours=9)
    llm = Span(
        "t", "c", "chat", SpanKind.LLM, start, start + timedelta(minutes=1),
        task_id="T9", session_id="S9", model="m", input_tokens=10, output_tokens=1,
    )  # fmt: skip
    inputs = ReportInputs(
        day=day.date(), tz=UTC, tasks=tasks, owners=owners, sessions=sessions, spans=(llm,),
    )  # fmt: skip
    [section] = build_report(inputs, {"throughput": throughput_section}).sections
    assert_safe(section)
    out = render((section,))
    assert md.text(dev) in out
    assert f"Spend of roles not listed ({md.text(ghost)})" in out
    assert "\nnext" not in render_markdown(build_report(inputs, {"throughput": throughput_section}))
    assert replace(inputs, spans=()) is not inputs
