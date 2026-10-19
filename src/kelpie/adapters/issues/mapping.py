"""Issues as tasks, for any tracker (GitHub Issues now, Linear next).

A tracker reader turns its issues into :class:`Item` (what an issue is, whatever the tracker)
and :func:`map_items` turns items into Kelpie's tasks, owners and dependencies with a
:class:`Board`'s rules. The rules are configuration, never code paths: which label makes an
issue a task, which labels mean blocked or ready, and which agents work the board.

**Agents.** Every agent that takes tasks from issues and returns pull requests is started
by one of three things the tracker records, so an :class:`Agent` names its own:

- an **assignee** (GitHub's Copilot coding agent; the Claude Code GitHub Action with
  ``assignee_trigger``),
- a **label** (OpenHands' resolver, ``fix-me``; the Claude Code Action's ``label_trigger``),
- a **mention** in a comment (``@codex``, ``@claude``, ``@openhands-agent``): not recorded
  on the issue itself, so such an agent is seen through the pull request it opens
  (``author``, the account its pull requests come from).

**Status**, first match:

=====================================  ===========================================
closed as completed                    ``done``
closed as not planned or duplicate     ``cancelled``
open, a blocked label                  ``blocked`` (since the label was put on)
open, a linked ready pull request      ``review`` (since the pull request was opened)
open, a linked draft pull request, or  ``in_progress`` (since the agent was started:
an agent or person holds it            assigned or labelled, else the pull request)
open, a ready label, no open blocker   ``ready``
off the boards
open                                   ``backlog``
=====================================  ===========================================

Agents on these trackers are started by people, not by a dispatcher, so an issue nobody
holds is ``backlog`` unless a ready label says it waits for an agent.

A pull request is linked to an issue when its description closes it (``Fixes #12``):
GitHub's own link, which closes the issue on merge. Plain mentions never link (incident
17). Its head branch becomes the task's branch, so Kelpie's pull request lists link it too.

**Owner**, first match: an agent assigned, an agent's label, the agent whose account opened
the linked pull request, a person assigned, the person (or bot) who opened the linked pull
request (human owners). Issues blocked by open issues (GitHub's issue dependencies) depend
on them. A parent keeps its sub-issues as subtasks only when every open one is a task here
(one missing would read as finished).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from kelpie.core.model import Block, Dependency, Owner, OwnerKind, Status, Task

# Distinct from other adapters' roles: an issue agent is never proposed for another
# platform's task, nor the reverse, unless the config gives them the same role.
DEFAULT_ROLE = "issue-agent"
MAX_BODY = 8000  # characters of an issue's description kept on the task
DEFAULT_BLOCKED_LABELS = ("blocked",)
CANCELLED_REASONS = frozenset({"not_planned", "duplicate"})


class IssuesError(RuntimeError):
    """A repository's issues could not be read or changed. ``public_reason`` is safe to
    show; ``kind`` is the GitHub error kind, when there was one."""

    def __init__(
        self, message: str, public_reason: str | None = None, kind: str | None = None
    ) -> None:
        super().__init__(message)
        self.public_reason = public_reason or message
        self.kind = kind


def login_key(login: str) -> str:
    """Logins compare without case and without GitHub's ``[bot]`` suffix (a bot is assigned
    as ``Copilot`` and opens pull requests as ``copilot-swe-agent[bot]``)."""
    key = login.strip().lower()
    return key.removesuffix("[bot]")


@dataclass(frozen=True, slots=True)
class Agent:
    """An agent that works issues. ``name`` is its owner id in Kelpie."""

    name: str
    role: str = DEFAULT_ROLE
    assignee: str | None = None  # the account it is assigned as, to start it
    label: str | None = None  # the label that starts it
    mention: str | None = None  # the comment text that starts it (``@codex``)
    author: str | None = None  # the account its pull requests come from

    def holds(self, item: Item) -> bool:
        """Whether this agent was started on ``item`` (assigned or labelled)."""
        assigned = {login_key(a) for a in item.assignees}
        if self.assignee is not None and login_key(self.assignee) in assigned:
            return True
        return self.label is not None and _has_label(item, self.label)

    def opened(self, pull: LinkedPull) -> bool:
        return (
            self.author is not None
            and pull.author is not None
            and login_key(self.author) == login_key(pull.author)
        )


@dataclass(frozen=True, slots=True)
class Board:
    """One queue of issues: a GitHub repository (``owner/repo``) or a Linear team."""

    key: str
    label: str | None = None  # only issues with this label are tasks; None: every issue
    blocked_labels: tuple[str, ...] = DEFAULT_BLOCKED_LABELS
    ready_labels: tuple[str, ...] = ()  # an issue nobody holds with one is ready
    role: str = DEFAULT_ROLE  # the role of tasks no agent holds
    # Names of the agents Kelpie may hand its tasks to; empty: all of them. An agent's
    # account is that agent on every board.
    agents: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Event:
    """A change to an issue's labels, assignees or state, as the tracker records it."""

    kind: str  # labeled, unlabeled, assigned, unassigned, closed, reopened
    at: datetime
    name: str = ""  # the label or the login


@dataclass(frozen=True, slots=True)
class LinkedPull:
    """An open pull request that closes the issue."""

    number: int
    branch: str | None
    draft: bool
    created_at: datetime | None
    author: str | None = None


@dataclass(frozen=True, slots=True)
class Item:
    """An issue, whatever the tracker. ``events`` is None when they were not read: times
    then fall back to the earlier of ``updated_at`` (which any comment moves) and
    ``seen_since``, when the adapter first saw the issue as it is now."""

    id: str  # unique across boards and trackers: owner/repo#12, ENG-12
    board: str
    title: str
    open: bool
    created_at: datetime
    updated_at: datetime
    closed_reason: str | None = None  # completed, not_planned, duplicate
    closed_at: datetime | None = None
    body: str = ""
    author: str | None = None
    author_is_bot: bool = False
    # The author may state the task's acceptance criteria (GitHub: owner, member or
    # collaborator; a stranger's issue text is not a person's criteria).
    author_trusted: bool = False
    labels: tuple[str, ...] = ()
    assignees: tuple[str, ...] = ()
    events: tuple[Event, ...] | None = None
    blocked_by: tuple[str, ...] = ()  # ids of the open issues it is blocked by
    blockers_unknown: bool = False  # the tracker counts blockers it could not list
    sub_items: tuple[str, ...] = ()
    open_sub_items: tuple[str, ...] = ()  # the sub-issues still open
    pulls: tuple[LinkedPull, ...] = ()
    url: str | None = None
    seen_since: datetime | None = None


@dataclass(frozen=True, slots=True)
class Mapped:
    tasks: tuple[Task, ...]
    owners: tuple[Owner, ...]
    dependencies: tuple[Dependency, ...]


def _has_label(item: Item, label: str) -> bool:
    want = label.casefold()
    return any(name.casefold() == want for name in item.labels)


def _first_label(item: Item, labels: Iterable[str]) -> str | None:
    return next((label for label in labels if _has_label(item, label)), None)


def _last(item: Item, kinds: set[str], names: Iterable[str] = ()) -> datetime | None:
    """When the newest event of ``kinds`` (about one of ``names``, if given) happened."""
    if item.events is None:
        return None
    keys = {login_key(n) for n in names}
    times = [
        e.at for e in item.events if e.kind in kinds and (not keys or login_key(e.name) in keys)
    ]
    return max(times, default=None)


@dataclass(slots=True)
class _Owners:
    agents: dict[str, Agent]
    humans: dict[str, Owner] = field(default_factory=dict)


def holder(item: Item, working: Sequence[Agent]) -> tuple[Agent | None, str | None]:
    """The agent holding ``item``, else the person assigned or who opened its linked pull
    request (a login), else neither."""
    for agent in working:
        if agent.holds(item):
            return agent, None
    for pull in item.pulls:
        for agent in working:
            if agent.opened(pull):
                return agent, None
    agent_logins = {login_key(a.assignee) for a in working if a.assignee}
    person = next((a for a in item.assignees if login_key(a) not in agent_logins), None)
    if person is None:
        person = next((p.author for p in item.pulls if p.author is not None), None)
    return None, person


def is_bot(login: str) -> bool:
    return login.strip().lower().endswith("[bot]")


def _started(item: Item, agent: Agent | None, person: str | None) -> datetime | None:
    """When the holder was put on the issue."""
    if agent is not None:
        names = [n for n in (agent.assignee, agent.label) if n]
        return _last(item, {"assigned", "labeled"}, names) if names else None
    if person is not None:
        return _last(item, {"assigned"}, [person])
    return None


def _status(
    item: Item, board: Board, agent: Agent | None, person: str | None, known: set[str]
) -> tuple[Status, datetime, Block | None, str]:
    """The status, since when, the block and the platform's own status."""
    fallback = item.updated_at
    if item.seen_since is not None:
        fallback = min(fallback, item.seen_since)
    if not item.open:
        since = item.closed_at or fallback
        if item.closed_reason in CANCELLED_REASONS:
            return Status.CANCELLED, since, None, f"closed: {item.closed_reason}"
        return Status.DONE, since, None, "closed"
    blocked = _first_label(item, board.blocked_labels)
    if blocked is not None:
        put_on = [e.at for e in item.events or () if e.kind == "labeled" and _same(e, blocked)]
        since = max(put_on, default=None) or fallback
        return Status.BLOCKED, since, Block(f"label '{blocked}'", since), f"open, {blocked}"
    pull = item.pulls[0] if item.pulls else None
    if pull is not None and not pull.draft:
        since = pull.created_at or fallback
        return Status.REVIEW, since, None, f"open, pull request #{pull.number}"
    if pull is not None or agent is not None or person is not None:
        since = _started(item, agent, person) or (pull.created_at if pull else None) or fallback
        where = f"draft pull request #{pull.number}" if pull is not None else "assigned"
        return Status.IN_PROGRESS, since, None, f"open, {where}"
    since = _last(item, {"reopened", "unassigned", "unlabeled"}) or item.created_at
    ready = _first_label(item, board.ready_labels)
    if ready is None:
        return Status.BACKLOG, since, None, "open"
    elsewhere = [b for b in item.blocked_by if b not in known]
    if elsewhere:  # its dependency is not a task here, so no rule would hold it back
        return Status.BACKLOG, since, None, f"open, {ready}, blocked by {elsewhere[0]}"
    if item.blockers_unknown:
        return Status.BACKLOG, since, None, f"open, {ready}, blockers not read"
    return Status.READY, since, None, f"open, {ready}"


def _same(event: Event, label: str) -> bool:
    return event.name.casefold() == label.casefold()


def map_items(
    items: Iterable[Item],
    boards: Mapping[str, Board],
    agents: Mapping[str, Agent],
    source: str,
) -> Mapped:
    """Kelpie's view of ``items``: tasks (``source`` names the adapter), their owners (the
    configured agents, and every person holding a task) and dependencies."""
    owners = _Owners(dict(agents))
    tasks: list[Task] = []
    dependencies: list[Dependency] = []
    scoped = [(i, boards[i.board]) for i in items if _in_scope(i, boards)]
    known = {i.id for i, _ in scoped}
    for item, board in scoped:
        agent, person = holder(item, list(agents.values())) if item.open else (None, None)
        status, since, block, platform = _status(item, board, agent, person, known)
        owner = agent.name if agent is not None else person
        if person is not None and person not in owners.humans:
            # A bot's pull request (Dependabot, an agent not configured): an agent, not a
            # person who gates work.
            kind = OwnerKind.AGENT if is_bot(person) else OwnerKind.HUMAN
            owners.humans[person] = Owner(person, board.role, kind)
        pull = item.pulls[0] if item.pulls else None
        reopened = tuple(e.at for e in item.events or () if e.kind == "reopened")
        tasks.append(
            Task(
                id=item.id,
                title=item.title,
                status=status,
                status_since=since,
                role=agent.role if agent is not None else board.role,
                owner_id=owner,
                source=source,
                block=block,
                repo=item.board,
                labels=item.labels,
                body=item.body[:MAX_BODY] or None,
                branch=pull.branch if pull is not None else None,
                assignee=item.assignees[0] if item.assignees else None,
                reopened_at=reopened,
                subtasks=() if set(item.open_sub_items) - known else item.sub_items,
                platform_status=platform,
                body_author=item.author if _person_wrote(item, agents) else None,
                created_at=item.created_at,
            )
        )
        dependencies += [Dependency(item.id, b) for b in item.blocked_by if b != item.id]
    listed = [Owner(a.name, a.role, OwnerKind.AGENT) for a in owners.agents.values()]
    people = [o for name, o in sorted(owners.humans.items()) if name not in owners.agents]
    return Mapped(tuple(tasks), tuple(listed + people), tuple(dependencies))


def _in_scope(item: Item, boards: Mapping[str, Board]) -> bool:
    board = boards.get(item.board)
    return board is not None and (board.label is None or _has_label(item, board.label))


def _person_wrote(item: Item, agents: Mapping[str, Agent]) -> bool:
    """Whether a trusted person (not a bot or an agent's account) wrote the issue."""
    if item.author is None or item.author_is_bot or not item.author_trusted:
        return False
    key = login_key(item.author)
    return not any(
        key == login_key(login)
        for agent in agents.values()
        for login in (agent.author, agent.assignee)
        if login
    )
