"""The report's delegation section (#347): cards, held alerts, verdicts and the operator's
tokens on Kelpie's cards."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from kelpie.core.actions import ActionKind
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.model import Span, SpanKind
from kelpie.report.daily import ReportInputs, build_report, render_markdown

DAY = date(2026, 3, 8)
START = datetime(2026, 3, 8, tzinfo=UTC)
NOON = START + timedelta(hours=12)
BASE = {"problem": "stale:T-1", "incident": "task:T-1", "attempt": 1, "operator": "ops-1"}


def entry(
    kind: ActionKind,
    outcome: Outcome,
    params: dict[str, Any],
    at: datetime = NOON,
    detail: str | None = None,
    rule: str = "delegation",
) -> LedgerEntry:
    return LedgerEntry(
        at,
        "f",
        "task:T-1",
        rule,
        kind,
        Mode.ACT,
        outcome,
        "r",
        task_id="T-1",
        params=params,
        detail=detail,
    )


def report(*actions: LedgerEntry, spans: tuple[Span, ...] = ()) -> str:
    inputs = ReportInputs(day=DAY, tz=UTC, actions=actions, spans=spans)
    return render_markdown(build_report(inputs))


def section(markdown: str) -> str:
    assert "## Delegation to operator agents\n" in markdown, markdown
    return markdown.split("## Delegation to operator agents\n", 1)[1].split("\n## ", 1)[0]


def test_nothing_when_the_rule_did_nothing() -> None:
    assert "Delegation to operator agents" not in report()


def test_cards_holds_verdicts_and_the_operators_tokens() -> None:
    llm = Span(
        "TR-1",
        "SP-1",
        "chat",
        SpanKind.LLM,
        NOON + timedelta(minutes=5),
        end=NOON + timedelta(minutes=6),
        task_id="t_9",
        model="claude-sonnet-4",
        input_tokens=1200,
        output_tokens=300,
    )
    other = Span(
        "TR-2",
        "SP-2",
        "chat",
        SpanKind.LLM,
        NOON,
        end=NOON,
        task_id="T-5",
        model="claude-sonnet-4",
        input_tokens=999,
        output_tokens=1,
    )
    text = section(
        report(
            entry(ActionKind.DELEGATE, Outcome.EXECUTED, BASE, detail="card t_9 for ops-1"),
            entry(
                ActionKind.ALERT,
                Outcome.DELEGATED,
                {},
                detail="held for ops-1; state stale:T-1",
                rule="stale_blocks",
            ),
            entry(ActionKind.ALERT, Outcome.ALERTED, {**BASE, "verdict": "verified"}),
            entry(ActionKind.ALERT, Outcome.FAILED, {**BASE, "verdict": "not_fixed"}),
            entry(ActionKind.DELEGATE, Outcome.SUGGESTED, {**BASE, "problem": "p2"}),
            entry(
                ActionKind.DELEGATE,
                Outcome.EXECUTED,
                BASE,
                at=START - timedelta(hours=2),
                detail="card t_8 for ops-1",
            ),
            spans=(llm, other),
        )
    )
    assert "Kelpie handed 1 problem(s) to operator agents as cards; held back 1 alert(s)" in text
    assert "would have handed 1 more (suggest mode)" in text
    assert "| fixed, verified | 1 |" in text
    assert "not fixed" not in text  # its send failed: not said
    assert "Operator agents spent 1,500 tokens" in text
    assert "Cards by operator: ops-1 1." in text
