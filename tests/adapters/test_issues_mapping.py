"""Issues as tasks: status, owner and dependency rules (``kelpie.adapters.issues.mapping``)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from kelpie.adapters.issues.mapping import (
    MAX_BODY,
    Agent,
    Board,
    Event,
    Item,
    LinkedPull,
    login_key,
    map_items,
)
from kelpie.core.model import Dependency, OwnerKind, Status

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
REPO = "acme/app"
BOARDS = {REPO: Board(REPO, ready_labels=("agent-ready",))}
AGENTS = {
    "copilot": Agent("copilot", assignee="Copilot", author="copilot-swe-agent[bot]"),
    "openhands": Agent("openhands", role="fixer", label="fix-me"),
    "codex": Agent("codex", mention="@codex", author="chatgpt-codex-connector[bot]"),
}


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


def item(number: int = 1, **fields: object) -> Item:
    base = Item(
        id=f"{REPO}#{number}",
        board=REPO,
        title=f"Issue {number}",
        open=True,
        created_at=ago(days=2),
        updated_at=ago(minutes=5),
        author="alice",
        author_trusted=True,
        events=(),
    )
    return replace(base, **fields)  # type: ignore[arg-type]


def one(it: Item, boards: dict[str, Board] = BOARDS):  # type: ignore[no-untyped-def]
    (task,) = map_items([it], boards, AGENTS, "github-issues").tasks
    return task


def test_login_key() -> None:
    assert login_key("Copilot") == login_key("copilot[bot]") == "copilot"
    assert login_key(" Alice ") == "alice"


def test_closed_issues_are_done_or_cancelled() -> None:
    done = one(item(open=False, closed_reason="completed", closed_at=ago(hours=1)))
    assert (done.status, done.status_since, done.owner_id) == (Status.DONE, ago(hours=1), None)
    for reason in ("not_planned", "duplicate"):
        cancelled = one(item(open=False, closed_reason=reason, closed_at=ago(hours=2)))
        assert cancelled.status is Status.CANCELLED
        assert cancelled.platform_status == f"closed: {reason}"
    unknown = one(item(open=False, closed_reason=None, closed_at=None))
    assert (unknown.status, unknown.status_since) == (Status.DONE, ago(minutes=5))


def test_an_open_issue_nobody_holds_is_backlog_unless_a_ready_label_says_otherwise() -> None:
    task = one(item())
    assert (task.status, task.status_since, task.owner_id) == (Status.BACKLOG, ago(days=2), None)
    assert task.role == "issue-agent" and task.source == "github-issues" and task.repo == REPO
    ready = one(item(labels=("Agent-Ready",)))
    assert ready.status is Status.READY and ready.platform_status == "open, agent-ready"
    back = one(item(labels=("agent-ready",), events=(Event("unassigned", ago(hours=3), "x"),)))
    assert (back.status, back.status_since) == (Status.READY, ago(hours=3))


def test_a_ready_issue_blocked_off_the_boards_is_not_ready() -> None:
    blocked = item(labels=("agent-ready",), blocked_by=("other/repo#4",))
    task = one(blocked)
    assert task.status is Status.BACKLOG
    assert task.platform_status == "open, agent-ready, blocked by other/repo#4"
    out = map_items([blocked, item(4, labels=("agent-ready",))], BOARDS, AGENTS, "gi")
    on_board = map_items(
        [replace(blocked, blocked_by=(f"{REPO}#4",)), item(4, labels=("agent-ready",))],
        BOARDS,
        AGENTS,
        "gi",
    )
    assert out.tasks[0].status is Status.BACKLOG
    assert on_board.tasks[0].status is Status.READY  # a task here: dispatch_gate holds it


def test_an_agent_assigned_holds_it_since_it_was_assigned() -> None:
    events = (
        Event("assigned", ago(hours=5), "Copilot"),
        Event("unassigned", ago(hours=4), "Copilot"),
        Event("assigned", ago(hours=1), "Copilot"),
        Event("labeled", ago(minutes=30), "docs"),
    )
    task = one(item(assignees=("Copilot",), events=events, labels=("docs",)))
    assert (task.status, task.owner_id, task.status_since) == (
        Status.IN_PROGRESS,
        "copilot",
        ago(hours=1),
    )
    assert task.assignee == "Copilot"


def test_an_agent_label_holds_it_with_the_agents_role() -> None:
    events = (Event("labeled", ago(minutes=40), "fix-me"),)
    task = one(item(labels=("Fix-Me",), events=events))
    assert (task.status, task.owner_id, task.role) == (Status.IN_PROGRESS, "openhands", "fixer")
    assert task.status_since == ago(minutes=40)


def test_an_author_agents_start_ignores_unrelated_label_events() -> None:
    pull = LinkedPull(
        7, "codex/x", draft=True, created_at=ago(days=5), author="chatgpt-codex-connector[bot]"
    )
    events = (Event("labeled", ago(days=4), "priority"), Event("assigned", ago(days=3), "bob"))
    task = one(item(pulls=(pull,), events=events, labels=("priority",)))
    assert (task.owner_id, task.status_since) == ("codex", ago(days=5))


def test_a_pull_request_by_a_person_makes_them_the_owner() -> None:
    pull = LinkedPull(9, "fix/login", draft=True, created_at=ago(hours=4), author="carol")
    mapped = map_items([item(pulls=(pull,))], BOARDS, AGENTS, "gi")
    (task,) = mapped.tasks
    assert (task.status, task.owner_id) == (Status.IN_PROGRESS, "carol")
    assert any(o.id == "carol" and o.kind is OwnerKind.HUMAN for o in mapped.owners)


def test_a_mention_agent_is_seen_through_its_pull_request() -> None:
    pull = LinkedPull(
        7,
        "codex/fix-login",
        draft=True,
        created_at=ago(minutes=20),
        author="chatgpt-codex-connector[bot]",
    )
    task = one(item(pulls=(pull,)))
    assert (task.status, task.owner_id, task.branch) == (
        Status.IN_PROGRESS,
        "codex",
        "codex/fix-login",
    )
    assert task.status_since == ago(minutes=20)
    assert task.platform_status == "open, draft pull request #7"


def test_a_ready_pull_request_puts_it_in_review() -> None:
    pull = LinkedPull(8, "copilot/fix-1", draft=False, created_at=ago(hours=2), author="someone")
    task = one(item(pulls=(pull,), assignees=("Copilot",)))
    assert (task.status, task.status_since, task.owner_id) == (
        Status.REVIEW,
        ago(hours=2),
        "copilot",
    )


def test_a_person_assigned_is_a_human_owner() -> None:
    events = (Event("assigned", ago(hours=6), "bob"),)
    mapped = map_items([item(assignees=("bob",), events=events)], BOARDS, AGENTS, "gi")
    (task,) = mapped.tasks
    assert (task.status, task.owner_id, task.status_since) == (
        Status.IN_PROGRESS,
        "bob",
        ago(hours=6),
    )
    bob = next(o for o in mapped.owners if o.id == "bob")
    assert bob.kind is OwnerKind.HUMAN
    assert {o.id for o in mapped.owners if o.kind is OwnerKind.AGENT} == set(AGENTS)


def test_an_agent_and_a_person_assigned_the_agent_holds_it() -> None:
    task = one(item(assignees=("bob", "Copilot")))
    assert task.owner_id == "copilot"


def test_a_blocked_label_blocks_since_it_was_put_on() -> None:
    events = (
        Event("labeled", ago(hours=9), "blocked"),
        Event("unlabeled", ago(hours=8), "blocked"),
        Event("labeled", ago(hours=2), "Blocked"),
    )
    task = one(item(labels=("Blocked",), assignees=("Copilot",), events=events))
    assert (task.status, task.status_since) == (Status.BLOCKED, ago(hours=2))
    assert task.block is not None and task.block.reason == "label 'blocked'"
    assert task.block.since == ago(hours=2)
    assert task.owner_id == "copilot"  # who holds it is kept while blocked
    unread = one(item(labels=("blocked",), events=None))
    assert unread.status_since == ago(minutes=5)  # events not read: when it last changed


def test_without_events_times_fall_back_to_the_last_change() -> None:
    task = one(item(assignees=("Copilot",), events=None))
    assert (task.status, task.status_since) == (Status.IN_PROGRESS, ago(minutes=5))


def test_board_label_scope_and_agents() -> None:
    scoped = {REPO: Board(REPO, label="agent", agents=("openhands",))}
    out = map_items(
        [item(1), item(2, labels=("Agent",), assignees=("Copilot",))], scoped, AGENTS, "gi"
    )
    (task,) = out.tasks
    assert task.id == f"{REPO}#2"
    # Copilot is not handed this board's tasks, but its account is still Copilot.
    assert task.owner_id == "copilot"
    other = map_items([replace(item(3), board="other/repo")], BOARDS, AGENTS, "gi")
    assert other.tasks == ()


def test_dependencies_subtasks_body_and_reopen() -> None:
    it = item(
        blocked_by=(f"{REPO}#9", f"{REPO}#1", "other/repo#4"),
        sub_items=(f"{REPO}#10", "other/repo#11"),  # #11 is closed
        body="x" * (MAX_BODY + 10),
        events=(Event("closed", ago(days=1)), Event("reopened", ago(hours=20))),
    )
    mapped = map_items([it], BOARDS, AGENTS, "gi")
    (task,) = mapped.tasks
    assert mapped.dependencies == (
        Dependency(f"{REPO}#1", f"{REPO}#9"),
        Dependency(f"{REPO}#1", "other/repo#4"),
    )
    assert task.subtasks == (f"{REPO}#10", "other/repo#11")
    assert task.body is not None and len(task.body) == MAX_BODY
    assert task.reopened_at == (ago(hours=20),)
    assert task.status_since == ago(hours=20)
    assert task.body_author == "alice"


def test_body_author_is_none_for_bots_and_agents() -> None:
    assert one(item(author="dependabot[bot]", author_is_bot=True)).body_author is None
    assert one(item(author="copilot-swe-agent[bot]")).body_author is None
    assert one(item(body="")).body is None


def test_a_comment_does_not_restart_the_clock_when_events_are_unread() -> None:
    held = item(
        assignees=("Copilot",), events=None, updated_at=ago(minutes=1), seen_since=ago(hours=3)
    )
    assert one(held).status_since == ago(hours=3)  # first seen as it is, not the last comment
    cold = item(assignees=("Copilot",), events=None, updated_at=ago(hours=2), seen_since=NOW)
    assert one(cold).status_since == ago(hours=2)


def test_an_untrusted_author_writes_no_acceptance_criteria() -> None:
    assert one(item(author="stranger", author_trusted=False)).body_author is None


def test_a_parent_keeps_its_subtasks_only_when_every_open_one_is_a_task_here() -> None:
    subs = (f"{REPO}#2", "other/repo#3")
    off = item(sub_items=subs, open_sub_items=("other/repo#3",))
    tasks = {t.id: t for t in map_items([off, item(2)], BOARDS, AGENTS, "gi").tasks}
    assert tasks[f"{REPO}#1"].subtasks == ()  # an open sub-issue elsewhere: not finished
    here = item(sub_items=subs, open_sub_items=(f"{REPO}#2",))
    tasks = {t.id: t for t in map_items([here, item(2)], BOARDS, AGENTS, "gi").tasks}
    assert tasks[f"{REPO}#1"].subtasks == subs


def test_the_default_author_is_not_trusted() -> None:
    assert (
        Item(
            id="x#1", board=REPO, title="t", open=True, created_at=NOW, updated_at=NOW
        ).author_trusted
        is False
    )


def test_a_bots_pull_request_makes_it_an_agent_owner() -> None:
    pull = LinkedPull(
        3, "dependabot/x", draft=False, created_at=ago(hours=1), author="dependabot[bot]"
    )
    mapped = map_items([item(pulls=(pull,))], BOARDS, AGENTS, "gi")
    assert mapped.tasks[0].owner_id == "dependabot[bot]"
    bot = next(o for o in mapped.owners if o.id == "dependabot[bot]")
    assert bot.kind is OwnerKind.AGENT
