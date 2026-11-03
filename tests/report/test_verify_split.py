"""Before/after of an instruction split (#282): `kelpie fix --rule instruction_bloat` is
measured on tokens a day on instructions and tokens per completed task of its role."""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.core.model import InstructionFile, Owner, Span, SpanKind, Status, Task
from kelpie.core.storage import SqliteInstructionStore, TokenRow, read_instruction_tokens
from kelpie.instructions.watch import role_tokens
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.fixes import MANUAL, MODE, FixEvent
from kelpie.report.store import ReportStore

SPLIT = datetime(2026, 3, 1, 8, tzinfo=UTC)
DAY = date(2026, 3, 2)
H = timedelta(hours=1)
OWNERS = (Owner("rev-1", "reviewer"), Owner("dev-1", "developer"))


def samples(start: datetime, hours: int, reviewer: int, developer: int) -> list[TokenRow]:
    out = []
    for i in range(hours):
        at = start + i * H
        out += [
            TokenRow(at, "reviewer", reviewer, 3, 3),
            TokenRow(at, "developer", developer, 2, 0),
        ]
    return out


def done(start: datetime, n: int, owner: str) -> list[Task]:
    return [
        Task(f"T-{owner}-{start:%d}-{i}", "t", Status.DONE, start + i * H, owner_id=owner)
        for i in range(n)
    ]


def spans(start: datetime, n: int, tokens: int, session: str) -> list[Span]:
    return [
        Span("tr", f"s-{session}-{start:%d}-{i}", "chat", SpanKind.LLM, start + i * H,
             start + i * H, session_id=session, model="m", input_tokens=tokens)
        for i in range(n)
    ]  # fmt: skip


def inputs(**kw: Any) -> ReportInputs:
    from kelpie.core.model import Session

    before, after = SPLIT - 23 * H, SPLIT + H
    base: dict[str, Any] = {
        "day": DAY,
        "tz": UTC,
        "owners": OWNERS,
        "sessions": (
            Session("S-rev", "T-x", "rev-1", SPLIT - 24 * H),
            Session("S-dev", "T-y", "dev-1", SPLIT - 24 * H),
        ),
        "fixes": (
            FixEvent(
                SPLIT, MANUAL, "split the review skill", "instruction_bloat", None, "reviewer"
            ),
        ),
        "instruction_tokens": samples(before, 23, 900_000, 50_000)
        + samples(after, 20, 300_000, 50_000),
        "tasks": done(before, 12, "rev-1") + done(after, 15, "rev-1") + done(after, 4, "dev-1"),
        "spans": spans(before, 12, 60_000, "S-rev")
        + spans(after, 15, 40_000, "S-rev")
        + spans(after, 4, 99_000, "S-dev"),
    }
    return ReportInputs(**{**base, **kw})


def metrics(report: Any) -> dict[str, Any]:
    [v] = report.verifications
    return {m.name: m for m in v.metrics}


def test_a_split_is_measured_on_its_roles_instructions_and_work() -> None:
    report = build_report(inputs())
    found = metrics(report)
    read = found["tokens a day on instructions (reviewer)"]
    assert (read.before, read.after, read.n_before, read.n_after) == (900_000, 300_000, 23, 20)
    assert read.improved and not read.small
    per_task = found["tokens per completed task (reviewer)"]
    assert per_task.before == pytest.approx(60_000) and per_task.after == pytest.approx(40_000)
    assert (per_task.n_before, per_task.n_after) == (12, 15) and per_task.improved
    markdown = render_markdown(report)
    assert "| tokens a day on instructions (reviewer) | 900,000 | 300,000 | -67% better" in markdown
    assert "split the review skill" in markdown


def test_a_split_without_a_role_is_measured_over_every_role() -> None:
    fix = FixEvent(SPLIT, MANUAL, "one fact, one place", "instruction_bloat")
    found = metrics(build_report(inputs(fixes=(fix,))))
    read = found["tokens a day on instructions"]
    assert (read.before, read.after) == (950_000, 350_000)
    assert found["tokens per completed task"].n_after == 19


def test_other_fixes_get_no_instruction_rows() -> None:
    fix = FixEvent(SPLIT, MANUAL, "reviewers read only the diff", None, None, "reviewer")
    names = metrics(build_report(inputs(fixes=(fix,))))
    assert not any("instructions" in n or "per completed task" in n for n in names)


def test_a_split_before_the_samples_began_is_direction_only() -> None:
    report = build_report(inputs(instruction_tokens=samples(SPLIT + H, 20, 300_000, 0)))
    read = metrics(report)["tokens a day on instructions (reviewer)"]
    assert read.before is None and read.small


def test_one_split_in_a_group_is_credited_its_own_role() -> None:
    other = FixEvent(SPLIT + H, MANUAL, "developer prompt trimmed", None, None, "developer")
    split = inputs().fixes[0]
    [v] = build_report(inputs(fixes=(split, other))).verifications
    own = {(c.event.note, c.metric.name) for c in v.own}
    assert ("split the review skill", "tokens a day on instructions (reviewer)") in own
    # A task goes through several roles: per task is the group's, over every role.
    assert not any("per completed task" in name for _, name in own)
    assert "tokens per completed task" in {m.name for m in v.metrics}
    assert "tokens a day on instructions" in {m.name for m in v.metrics}


def test_the_rules_own_mode_switch_is_no_split() -> None:
    switch = FixEvent(SPLIT, MODE, "instruction_bloat: observe -> act", "instruction_bloat")
    names = metrics(build_report(inputs(fixes=(switch,))))
    assert not any("instructions" in n or "per completed task" in n for n in names)


def test_a_persons_tasks_and_sessions_are_not_agent_work() -> None:
    from kelpie.core.model import OwnerKind, Session

    person = Owner("alice", "reviewer", OwnerKind.HUMAN)
    base = inputs()
    theirs = done(SPLIT + H, 10, "rev-1")
    theirs = [Task(t.id + "-p", "t", Status.DONE, t.status_since, assignee="alice")
              for t in theirs]  # fmt: skip
    found = metrics(
        build_report(
            inputs(
                owners=(*OWNERS, person),
                tasks=[*base.tasks, *theirs],
                sessions=(*base.sessions, Session("S-alice", "T-z", "alice", SPLIT)),
                spans=[*base.spans, *spans(SPLIT + H, 3, 1_000_000, "S-alice")],
                fixes=(FixEvent(SPLIT, MANUAL, "facts in one place", "instruction_bloat"),),
            )
        )
    )
    per_task = found["tokens per completed task"]
    assert per_task.n_after == 19  # 15 reviewer + 4 developer tasks, not alice's 10
    assert per_task.after == pytest.approx((15 * 40_000 + 4 * 99_000) / 19)


def test_per_task_is_not_compared_when_traces_began_late() -> None:
    late = SPLIT - 6 * H
    report = build_report(
        inputs(spans=spans(late, 5, 60_000, "S-rev") + spans(SPLIT + H, 15, 40_000, "S-rev"))
    )
    [v] = report.verifications
    assert "tokens per completed task (reviewer)" not in {m.name for m in v.metrics}
    assert "tokens per completed task (reviewer)" in {x.name for x in v.excluded}


def test_few_samples_are_direction_only() -> None:
    few = samples(SPLIT - 4 * H, 3, 900_000, 0) + samples(SPLIT + H, 3, 300_000, 0)
    read = metrics(build_report(inputs(instruction_tokens=few)))[
        "tokens a day on instructions (reviewer)"
    ]
    assert (read.n_before, read.n_after) == (3, 3) and read.small


def test_token_samples_are_stored_per_role_and_read_back(tmp_path: Path) -> None:
    db = tmp_path / "k.db"
    store = SqliteInstructionStore(db)
    with ReportStore(db) as report:
        assert report.instruction_tokens(SPLIT - 24 * H, SPLIT) == []  # never written
    files = [
        InstructionFile("~/a/SKILL.md", "reviewer", 400, 100, 2, "on_demand",
                        tokens_per_day=300, token_basis="loads"),
        InstructionFile("~/b/SKILL.md", "reviewer", 40, 10, 1, "on_demand",
                        tokens_per_day=0, token_basis="loads"),
        InstructionFile("~/SOUL.md", None, 40, 10, 1, tokens_per_day=70,
                        token_basis="sessions"),
    ]  # fmt: skip
    rows = role_tokens(files, SPLIT)
    assert rows == [TokenRow(SPLIT, None, 70, 1, 0), TokenRow(SPLIT, "reviewer", 300, 2, 2)]
    store.record_tokens([])  # nothing to store
    store.record_tokens(role_tokens(files, SPLIT - 40 * 24 * H))
    store.record_tokens(rows)  # the 40 days old samples go
    assert store.tokens(SPLIT, SPLIT + H) == rows
    with ReportStore(db) as report:
        assert report.instruction_tokens(SPLIT - 50 * 24 * H, SPLIT + H) == rows
    with sqlite3.connect(db) as conn:
        assert read_instruction_tokens(conn, SPLIT + H, SPLIT + 2 * H) == []
    store.close()
