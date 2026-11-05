"""Review economy (#298): per repository and kind of change, review rounds, tokens and
findings, and a lighter path where full review almost never finds anything."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from kelpie.core.budgets import Price, PriceTable
from kelpie.core.model import Owner, Return, Session, Span, SpanKind, Status, Task
from kelpie.report.daily import ReportInputs, build_report, render_markdown
from kelpie.report.review_economy import (
    CODE,
    DEPENDENCY,
    DOCS,
    GENERATED,
    SMALL,
    TESTS,
    UNKNOWN,
    ReviewEconomySettings,
    classes,
    judge,
    kind_of,
    reviews,
)
from kelpie.report.sections import SectionContext
from kelpie.report.sections.review_economy import review_economy

FIXTURE = Path(__file__).parents[1] / "fixtures" / "traces" / "review-economy.yaml"
PRICES = PriceTable((("model-large", Price(input=3.0, output=15.0, cached_input=0.3)),))
CFG = ReviewEconomySettings()


def farm(fixture: dict[str, Any]) -> ReportInputs:
    """The fixture's classes as a board: tasks, one review session each, its model calls,
    and returns for the first ``sent_back`` tasks of a class."""
    now = datetime.fromisoformat(fixture["now"])
    days = fixture["days"]
    tasks, sessions, spans = [], [], []
    n = 0
    for c in fixture["classes"]:
        for i in range(c["tasks"]):
            n += 1
            start = now - timedelta(days=days) + timedelta(hours=n * days * 24 / 60)
            back = (Return(start + timedelta(minutes=30), "changes requested", "review"),)
            tasks.append(
                Task(
                    f"T-{n}",
                    f"{c['kind']} {i}",
                    Status.DONE,
                    start,
                    owner_id="dev-1",
                    repo=fixture["repo"],
                    files=tuple(c["files"]),
                    labels=tuple(c.get("labels", ())),
                    returns=back if i < c["sent_back"] else (),
                )
            )
            sessions.append(
                Session(f"S-{n}", f"T-{n}", "reviewer-1", start, start + timedelta(minutes=20))
            )
            spans.append(
                Span(
                    "tr",
                    f"c{n}",
                    "chat",
                    SpanKind.LLM,
                    start + timedelta(minutes=1),
                    start + timedelta(minutes=2),
                    task_id=f"T-{n}",
                    session_id=f"S-{n}",
                    model="model-large",
                    input_tokens=c["tokens"],
                )
            )
    return ReportInputs(
        day=(now - timedelta(days=1)).date(),
        tz=UTC,
        tasks=tuple(tasks),
        owners=(Owner("dev-1", "developer"), Owner("reviewer-1", "reviewer")),
        sessions=tuple(sessions),
        spans=tuple(spans),
        prices=PRICES,
    )


def test_fixture_recommends_docs_only_and_never_code() -> None:
    fixture = yaml.safe_load(FIXTURE.read_text())
    assert fixture["id"] == FIXTURE.stem
    inputs = farm(fixture)
    end = datetime.fromisoformat(fixture["now"])
    roles = {s.id: "reviewer" for s in inputs.sessions}
    found = reviews(inputs.tasks, inputs.sessions, inputs.spans, roles, ("reviewer",), end, CFG)
    verdicts = [judge(c, end, CFG) for c in classes(found)]
    got = {
        "recommend": [v for v in verdicts if v.recommend],
        "direction": [v for v in verdicts if v.direction],
    }
    for part in ("recommend", "direction"):
        want = fixture["expected"][part]
        assert [(v.cls.repo, v.cls.kind, v.cls.n, v.cls.k) for v in got[part]] == [
            (w["repo"], w["kind"], w["n"], w["k"]) for w in want
        ]
        for bad in fixture["not_expected"][part]:
            assert all(v.cls.kind != bad["kind"] for v in got[part])
    (docs,) = got["recommend"]
    # 24 reviews of 40k tokens over five days, at 30% of the cost: 24*40k*0.7/5 a day
    assert docs.saved_tokens_per_day == pytest.approx(24 * 40_000 * 0.7 / 5, rel=0.05)


def test_the_daily_report_shows_the_section_and_the_advice() -> None:
    fixture = yaml.safe_load(FIXTURE.read_text())
    report = build_report(farm(fixture))
    text = render_markdown(report)
    assert "## Review economy" in text
    assert "| `example/repo` | docs only | 24 | 1.0 |" in text
    assert "9 of 24 (38%)" in text
    assert "tests only in `example/repo`: direction only, 0 of 6" in text
    titles = [a.title for a in report.recommendations]
    advice = [a for a in report.recommendations if "Lighter review" in a.title]
    assert titles and advice, titles
    (docs,) = advice
    assert "docs only" in docs.title and docs.usd > 0
    assert "0 of 24 full reviews found something" in docs.detail
    assert "code in" not in " ".join(titles)


@pytest.mark.parametrize(
    ("files", "labels", "kind"),
    [
        (("README.md", "docs/a/b.md"), (), DOCS),
        (("tests/test_a.py", "src/pkg/tests/test_b.py", "web/a.spec.ts"), (), TESTS),
        (("api_pb2.py", "x/generated/y.go"), (), GENERATED),
        (("uv.lock", "requirements-dev.txt"), (), DEPENDENCY),
        (("src/a.py",), ("Dependencies",), DEPENDENCY),
        (("src/a.py", "README.md"), (), SMALL),
        (("src/a.py", "src/b.py", "src/c.py"), (), CODE),
        ((), (), UNKNOWN),
    ],
)
def test_kind_of(files: tuple[str, ...], labels: tuple[str, ...], kind: str) -> None:
    task = Task("T", "t", Status.DONE, datetime(2026, 3, 1, tzinfo=UTC), files=files, labels=labels)
    assert kind_of(task, CFG) == kind


def at(hour: float) -> datetime:
    return datetime(2026, 3, 2, tzinfo=UTC) + timedelta(hours=hour)


def test_a_review_finds_something_when_sent_back_or_followed_by_another_round() -> None:
    task = Task(
        "T-1", "t", Status.DONE, at(0), repo="r", files=("a.py",),
        returns=(Return(at(13), "fix", "review"),),
    )  # fmt: skip
    sessions = (
        Session("s1", "T-1", "rev", at(10), at(11)),  # followed by round 2
        Session("s2", "T-1", "rev", at(12), at(12.5)),  # sent back at 13:00
        Session("s3", "T-1", "rev", at(14), at(15)),  # nothing after: clean
        Session("d1", "T-1", "dev", at(9), at(10)),  # not a review
    )
    roles = {"s1": "reviewer", "s2": "reviewer", "s3": "reviewer", "d1": "developer"}
    found = reviews([task], sessions, [], roles, ("reviewer",), at(24), CFG)
    assert [(r.session_id, r.found) for r in found] == [("s1", True), ("s2", True), ("s3", False)]
    (cls,) = classes(found)
    assert (cls.n, cls.k, cls.rounds) == (3, 2, 3.0)


def test_never_for_a_class_above_the_threshold_or_for_code() -> None:
    task = Task("T", "t", Status.DONE, at(0), repo="r", files=("README.md",))
    many = [Session(f"s{i}", f"T{i}", "rev", at(0) + timedelta(minutes=i), None) for i in range(30)]
    tasks = [Task(f"T{i}", "t", Status.DONE, at(0), repo="r", files=task.files) for i in range(30)]
    tasks[0] = Task("T0", "t", Status.DONE, at(0), repo="r", files=task.files,
                    returns=(Return(at(1), "x"), ))  # fmt: skip
    tasks[1] = Task("T1", "t", Status.DONE, at(0), repo="r", files=task.files,
                    returns=(Return(at(1), "x"), ))  # fmt: skip
    roles = {s.id: "reviewer" for s in many}
    (cls,) = classes(reviews(tasks, many, [], roles, ("reviewer",), at(24), CFG))
    assert (cls.n, cls.k) == (30, 2)  # 6.7% > 5%
    v = judge(cls, at(24), CFG)
    assert not v.recommend and not v.direction
    code = [Task(t.id, "t", Status.DONE, at(0), repo="r", files=("a.py", "b.py", "c.py"))
            for t in tasks]  # fmt: skip
    (cls,) = classes(reviews(code, many, [], roles, ("reviewer",), at(24), CFG))
    v = judge(cls, at(24), CFG)
    assert cls.kind == CODE and cls.k == 0 and not v.recommend and not v.direction


def test_section_settings_and_nothing_without_reviews() -> None:
    inputs = ReportInputs(day=date(2026, 3, 2), tz=UTC)
    ctx = SectionContext(
        inputs=inputs,
        start=at(0),
        end=at(24),
        spans=(),
        counted={},
        price=lambda rows: build_report(inputs).spend,
        spend=build_report(inputs).spend,
        roles={},
    )
    assert review_economy(ctx) is None
    fixture = yaml.safe_load(FIXTURE.read_text())
    inputs = farm(fixture)
    report = build_report(
        ReportInputs(
            **{
                **{f: getattr(inputs, f) for f in ("day", "tz", "tasks", "owners", "sessions")},
                "spans": inputs.spans,
                "prices": PRICES,
                "section_config": {"review_economy": {"min_sample": 30}},
            }
        )
    )
    text = render_markdown(report)
    assert "docs only in `example/repo`: direction only, 0 of 24" in text
    assert not [a for a in report.recommendations if "Lighter review" in a.title]
