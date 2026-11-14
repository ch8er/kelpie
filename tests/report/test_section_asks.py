"""The report's unanswered asks (#297): agent-hours agents' questions to other agents stayed
open within the day, by the agent asked."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from kelpie.core.model import Block, Comment, Owner, OwnerKind, Status, Task
from kelpie.report.daily import ReportInputs, build_report, render_markdown

DAY = date(2026, 3, 8)
START = datetime(2026, 3, 8, tzinfo=UTC)
END = START + timedelta(days=1)
OWNERS = (
    Owner("dev-1", "developer"),
    Owner("architect-1", "architect"),
    Owner("ops-1", "ops"),
    Owner("alice", "lead", OwnerKind.HUMAN),
)


def task(tid: str, *comments: Comment, status: Status = Status.IN_PROGRESS) -> Task:
    return Task(tid, f"task {tid}", status, START, owner_id="dev-1", comments=comments)


def said(by: str, text: str, at: datetime, human: bool = False) -> Comment:
    return Comment(by, text, at, human)


def report(*tasks: Task, **kw: Any) -> str:
    inputs = ReportInputs(day=DAY, tz=UTC, tasks=tasks, owners=OWNERS, **kw)
    return render_markdown(build_report(inputs))


def section(markdown: str) -> str:
    assert "## Unanswered asks\n" in markdown, markdown
    return markdown.split("## Unanswered asks\n", 1)[1].split("\n## ", 1)[0]


def test_open_asks_count_agent_hours_by_the_agent_asked() -> None:
    noon = START + timedelta(hours=12)
    text = section(
        report(
            task("T-1", said("dev-1", "@architect-1 which schema, v3 or v4?", noon)),
            task("T-2", said("dev-1", "@architect-1 one table or two?", START - timedelta(1))),
            task("T-3", said("dev-1", "@ops-1 can I restart staging?", END - timedelta(hours=3))),
        )
    )
    assert "left 39.0 agent-hours unanswered" in text
    assert "| architect-1 | 2 | 36.0 | `T-1`, `T-2` |" in text  # 12 h + the whole day
    assert "| ops-1 | 1 | 3.0 | `T-3` |" in text
    assert text.index("architect-1") < text.index("ops-1")


def test_answered_asks_and_answers_after_the_day_are_read_as_of_its_end() -> None:
    asked = START + timedelta(hours=10)
    answered = task(
        "T-1",
        said("dev-1", "@architect-1 which schema?", asked),
        said("architect-1", "v4", asked + timedelta(hours=1)),
    )
    later = task(
        "T-2",
        said("dev-1", "@architect-1 which index?", asked),
        said("architect-1", "btree", END + timedelta(hours=2)),  # after the day
    )
    text = section(report(answered, later))
    assert "`T-2`" in text and "`T-1`" not in text
    assert "| architect-1 | 1 | 14.0 |" in text


def test_nothing_to_say_without_open_asks() -> None:
    noon = START + timedelta(hours=12)
    person = said("alice", "@architect-1 can you look?", noon, human=True)  # a person's
    done = task("T-2", said("dev-1", "@ops-1 ok?", noon), status=Status.DONE)
    plain = task("T-3", said("dev-1", "@ops-1 restarting staging now.", noon))  # no question
    assert "## Unanswered asks" not in report(task("T-1", person), done, plain)


def test_a_task_finished_after_the_day_was_open_all_of_it() -> None:
    noon = START + timedelta(hours=12)
    later = Task("T-1", "t", Status.DONE, END + timedelta(hours=3), owner_id="dev-1",
                 comments=(said("dev-1", "@ops-1 can I restart?", noon),))  # fmt: skip
    within = Task("T-2", "t", Status.DONE, noon + timedelta(hours=2), owner_id="dev-1",
                  comments=(said("dev-1", "@ops-1 and now?", noon),))  # fmt: skip
    before = Task("T-3", "t", Status.DONE, START - timedelta(hours=1), owner_id="dev-1",
                  comments=(said("dev-1", "@ops-1 old?", START - timedelta(hours=5)),))  # fmt: skip
    assert "| ops-1 | 2 | 14.0 | `T-1`, `T-2` |" in section(report(later, within, before))


def test_an_answer_at_midnight_belongs_to_the_next_day() -> None:
    asked = START + timedelta(hours=20)
    t = task("T-1", said("dev-1", "@ops-1 ok?", asked), said("ops-1", "yes", END))
    assert "| ops-1 | 1 | 4.0 |" in section(report(t))


def test_ties_go_by_name_and_task_ids_are_escaped() -> None:
    noon = START + timedelta(hours=12)
    tie = section(
        report(
            task("T-1", said("dev-1", "@ops-1 ok?", noon)),
            task("T-2", said("dev-1", "@architect-1 ok?", noon)),
        )
    )
    assert tie.index("architect-1") < tie.index("ops-1")
    odd = section(report(task("T|3", said("dev-1", "@ops-1 and?", START))))
    assert "| ops-1 | 1 | 24.0 | `T\\|3` |" in odd


def test_a_block_that_asks_an_agent_counts_from_when_it_was_set() -> None:
    """#461: a task still blocked on a question to another agent is an open ask."""
    six = START + timedelta(hours=18)
    asking = Task(
        "T-9",
        "task T-9",
        Status.BLOCKED,
        six,
        owner_id="dev-1",
        block=Block("@architect-1 which schema, v3 or v4?", six),
    )
    answered = Task(
        "T-8",
        "task T-8",
        Status.BLOCKED,
        six,
        owner_id="dev-1",
        block=Block("@ops-1 restart staging?", six),
        comments=(said("ops-1", "done", six + timedelta(hours=1)),),
    )
    later = Task(
        "T-7",
        "task T-7",
        Status.BLOCKED,
        END + timedelta(hours=1),
        owner_id="dev-1",
        block=Block("@ops-1 restart prod?", END + timedelta(hours=1)),
    )
    text = section(report(asking, answered, later))
    assert "| architect-1 | 1 | 6.0 | `T-9` |" in text
    assert "ops-1" not in text


def test_a_block_repeating_an_open_comment_ask_counts_once() -> None:
    six = START + timedelta(hours=18)
    asked = Task(
        "T-6",
        "task T-6",
        Status.BLOCKED,
        six,
        owner_id="dev-1",
        block=Block("@architect-1 which schema?", six),
        comments=(said("dev-1", "@architect-1 which schema?", six - timedelta(hours=2)),),
    )
    text = section(report(asked))
    assert "| architect-1 | 1 | 8.0 | `T-6` |" in text  # the comment ask, from 16:00
