"""The issues adapter: a farm whose task queue is a tracker's issues (see :mod:`.mapping`).

One adapter per tracker: ``github-issues`` reads GitHub Issues through the GitHub
connector's client (:mod:`.github`), ``linear`` a Linear workspace's teams (:mod:`.linear`).
Agents there (Copilot coding agent, Codex cloud, the
Claude Code GitHub Action, OpenHands) run on someone else's machines, so the adapter has
tasks and owners but no sessions or spans.

**Actions** go through the tracker's API, only on tasks of the last read. Each re-reads the
issue first, holds all its writes before the first (``Tracker.reserve``: deferred whole,
never cut halfway) and says what it did in a comment signed by Kelpie, the rule's reason
quoted inert (:func:`note`):

- ``comment``: a comment.
- ``unblock``: the board's blocked labels come off.
- ``requeue``: the task starts over. A closed issue is reopened (not one closed as not
  planned or duplicate), and the agent holding it restarted (unassigned and assigned again,
  its label taken off and put back, or its mention posted again). No agent holds an open
  issue: refused, a human decides.
- ``reassign``: the new agent is started first, then the one holding it taken off
  (unassigned, its label removed); only an agent of this adapter that the board allows.
- ``delegate`` (GitHub Issues): a card for an operator agent (#347), an issue in the
  task's repository that starts the agent, found again by its key line.
- ``lift_hold`` (``lift_holds``, off by default): rule 6's pause hold past its expiry, named
  with this adapter as its ``executor`` and named after one of the board's pause labels:
  that label comes off each held task of this adapter that still carries it. A pause state
  (Linear), a flag or a label hold is left to a person (which state, or whether to unlock,
  is not Kelpie's call).

What cannot be done (an agent with no way to start it, a closed issue to reassign) is refused
before anything changes (``UnsupportedAction``), so a human is alerted instead.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import ClassVar, Protocol

from kelpie.adapters.issues.mapping import (
    CANCELLED_REASONS,
    Agent,
    Board,
    IssuesError,
    Item,
    holder,
    login_key,
    map_items,
)
from kelpie.core import actlock
from kelpie.core.actions import ActionKind
from kelpie.core.actlock import ActLock
from kelpie.core.alerts import format_age
from kelpie.core.model import Snapshot, Task
from kelpie.core.ports import Deferred, NothingToDo, PlatformEvent, UnsupportedAction

log = logging.getLogger(__name__)

SIGNATURE = "\n\n— Kelpie"
RESERVE = 6  # writes held for a requeue or reassign: reopen, off (2), on (2), note
MAX_LIFT = 50  # held tasks one lift_hold considers (rule 6 names at most this many)
MAX_LIFTS = 4  # tasks one lift_hold changes (a label and a note each, inside a tick's cap)
PAUSE_KIND = "pause"  # the only hold kind an issue tracker lifts (rule 6's hold kinds)
WAIT_KINDS = frozenset({"backoff", "rate_limited", "stale", "budget"})
MAX_QUOTE = 2000  # characters of a rule's reason quoted in a comment
MAX_CARD = 8000  # characters of a delegation card quoted in its issue
CARD_KEY = "kelpie-delegation: "  # the key line of a card (kelpie.core.delegation)
_CARD_LINE = re.compile(re.escape(CARD_KEY) + r"[0-9a-f]{32}")
MENTION_TEXT = "please pick this issue up again (Kelpie restarts its work)."
JOINER = "\u2060"  # word joiner: invisible, breaks "@name" for bots that match raw text
_TICKS = re.compile(r"`+")


class Tracker(Protocol):
    """A tracker's issues as items, and the changes actions make (GitHub Issues:
    :class:`.github.GitHubIssues`). Item ids are the tracker's."""

    def read(self, now: datetime) -> list[Item]: ...

    def fetch(self, item_id: str) -> Item: ...

    def add_labels(self, item_id: str, labels: Sequence[str]) -> None: ...

    def remove_labels(self, item_id: str, labels: Sequence[str]) -> None: ...

    def assign(self, item_id: str, logins: Sequence[str]) -> None: ...

    def unassign(self, item_id: str, logins: Sequence[str]) -> None: ...

    def comment(self, item_id: str, body: str) -> None: ...

    def reopen(self, item_id: str) -> None: ...

    def reserve(self, writes: int, agents: Sequence[Agent] = ()) -> None: ...


def inert_title(text: str) -> str:
    """A card's title as GitHub shows it: no mention pings, one line."""
    return " ".join(text.replace("@", "@" + JOINER).split())


def card_line(key: str) -> str:
    """A card's key line: a digest of the key, so it is one short line that links and
    pings nothing on GitHub, whatever the key holds (task ids, config text)."""
    return CARD_KEY + hashlib.sha256(key.encode()).hexdigest()[:32]


def is_card(item: Item) -> bool:
    """Whether ``item``'s description ends a card the way Kelpie writes one (anyone can
    write the line; a look-alike only keeps Kelpie from delegating about that issue)."""
    lines = item.body.splitlines()
    return bool(lines) and _CARD_LINE.fullmatch(lines[-1]) is not None


def _started_on(agent: Agent, item: Item) -> bool:
    """Whether every part of ``agent``'s start shows on ``item`` (its assignee and label)."""
    assigned = {login_key(a) for a in item.assignees}
    if agent.assignee is not None and login_key(agent.assignee) not in assigned:
        return False
    labels = {x.casefold() for x in item.labels}
    return agent.label is None or agent.label.casefold() in labels


def _waiting[T](read: Callable[[], T]) -> T:
    """``read()``, with the tracker asking Kelpie to wait turned into ``Deferred`` (not a
    failed attempt: nothing was written)."""
    try:
        return read()
    except IssuesError as exc:
        if exc.kind in WAIT_KINDS:
            raise Deferred(f"issues: the tracker cannot be asked now ({exc.kind})") from exc
        raise


def card_text(what: str, card: str, line: str) -> str:
    """A delegation card's description: Kelpie's words, the card quoted inert (as in
    :func:`note`, up to ``MAX_CARD`` characters), then the key ``line`` on its own."""
    return note(what, card, MAX_CARD) + f"\n\n{line}"


def signed(text: str) -> str:
    return text.rstrip() + SIGNATURE


def note(what: str, quoted: str, limit: int = MAX_QUOTE) -> str:
    """A comment: Kelpie's own words (``what``: fixed text and names from the config), then
    the rule's reason in a code block. The reason can quote text anyone wrote (titles, block
    reasons, labels); in a code block its mentions, issue references (``Fixes #n``), links
    and HTML do nothing for GitHub. Bots that match the raw text (``@claude`` for the Claude
    Code Action, ``/command`` lines) would still see them, so every ``@`` is followed by an
    invisible word joiner and every line is indented: it starts no agent and pings nobody."""
    text = quoted[:limit].replace("@", "@" + JOINER)
    text = "\n".join("  " + line for line in text.splitlines())
    longest = max((len(run) for run in _TICKS.findall(text)), default=0)
    fence = "`" * max(3, longest + 1)
    return signed(f"{what}\n\n{fence}text\n{text}\n{fence}")


@dataclass(slots=True)
class IssuesAdapter:
    tracker: Tracker
    boards: Sequence[Board]
    agents: Mapping[str, Agent]
    name: str = "github-issues"
    where: str = ""  # for kelpie status: the tracker's address
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    # Rule 6 may take pause labels off (``lift_hold``, act mode only): off by default.
    lift_holds: bool = False
    # Not wake: issue tasks are never waiting (Kelpie parks none), and restarting an agent
    # is not resuming a parked task.
    actions: ClassVar[frozenset[ActionKind]] = frozenset(
        {
            ActionKind.REQUEUE,
            ActionKind.UNBLOCK,
            ActionKind.COMMENT,
            ActionKind.REASSIGN,
            ActionKind.LIFT_HOLD,
            ActionKind.DELEGATE,  # GitHub Issues only (Linear refuses it, and at startup)
        }
    )
    # Kinds no issue task draws: no sessions to stop, no worktrees to commit, no parked task
    # to wake.
    inapplicable: ClassVar[frozenset[ActionKind]] = frozenset(
        {ActionKind.STOP, ActionKind.WIP_COMMIT, ActionKind.WAKE}
    )
    _last: tuple[datetime, int, int] | None = field(default=None, init=False, repr=False)
    # Agents that held a task at the last read through their pull request only: a re-read
    # issue does not show them (assignees and labels do show the others).
    _owners: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _known: set[str] = field(default_factory=set, init=False, repr=False)  # last read's tasks
    _board_of: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _cards: set[str] = field(default_factory=set, init=False, repr=False)  # cards (is_card)
    _problem: str | None = field(default=None, init=False, repr=False)
    # Per issue: how it looked (open, labels, assignees, linked pull requests) and since when.
    _seen: dict[str, tuple[object, datetime]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.boards = tuple(self.boards)
        self.agents = dict(self.agents)

    @property
    def _boards(self) -> dict[str, Board]:
        return {b.key: b for b in self.boards}

    def act_locks(self) -> tuple[ActLock, ...]:
        """One act lock per repository or team, in Kelpie's per-user state folder (audit
        M7). It keeps two instances on this host apart, not two hosts."""
        return tuple(
            ActLock(
                f"{self.name} {b.key}",
                actlock.keyed(self.name, self.where, b.key),
                create_folder=True,
            )
            for b in self.boards
        )

    # --- status --------------------------------------------------------------------------

    def check(self) -> str | None:
        """None if the last read saw everything (or none was made yet), else what it
        missed."""
        return self._problem

    def status_detail(self, now: datetime) -> list[str]:
        boards = ", ".join(b.key for b in self.boards) or "none"
        agents = ", ".join(sorted(self.agents)) or "none"
        lines = [f"boards: {boards}", f"agents: {agents}"]
        if self.where:
            lines.insert(0, f"tracker: {self.where}")
        if self._last is None:
            return [*lines, "issues: not read yet"]
        at, count, active = self._last
        return [*lines, f"issues: {count} tasks, {active} held, read {format_age(now - at)} ago"]

    # --- reading -------------------------------------------------------------------------

    def snapshot(self, now: datetime) -> Snapshot:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        try:
            items = self.tracker.read(now)
        except Exception as exc:
            self._problem = str(getattr(exc, "public_reason", None) or type(exc).__name__)
            raise
        self._problem = getattr(self.tracker, "problem", None)
        items = self._since_seen(items, now)
        mapped = map_items(items, self._boards, self.agents, self.name)
        held = sum(1 for t in mapped.tasks if t.owner_id is not None and not t.status.is_terminal)
        self._last = (now, len(mapped.tasks), held)
        by_id = {i.id: i for i in items}
        self._owners = {
            t.id: t.owner_id
            for t in mapped.tasks
            if t.owner_id in self.agents and not self.agents[t.owner_id].holds(by_id[t.id])
        }
        self._known = {t.id for t in mapped.tasks}
        self._board_of = {t.id: by_id[t.id].board for t in mapped.tasks}
        self._cards = {t.id for t in mapped.tasks if is_card(by_id[t.id])}
        return Snapshot(
            now=now,
            tasks=mapped.tasks,
            owners=mapped.owners,
            dependencies=mapped.dependencies,
        )

    def _since_seen(self, items: list[Item], now: datetime) -> list[Item]:
        """``items`` with ``seen_since``: when this adapter first saw each as it is now."""
        seen: dict[str, tuple[object, datetime]] = {}
        out: list[Item] = []
        for item in items:
            shape = (
                item.open,
                frozenset(label.casefold() for label in item.labels),
                frozenset(login.casefold() for login in item.assignees),
                tuple((p.number, p.draft) for p in item.pulls),
                (item.state or "").casefold(),  # a pause state's age starts when it is seen
            )
            kept = self._seen.get(item.id)
            # First seen: no later than its last change, so a comment cannot move it on.
            since = kept[1] if kept is not None and kept[0] == shape else min(item.updated_at, now)
            seen[item.id] = (shape, since)
            out.append(replace(item, seen_since=since))
        self._seen = seen
        return out

    # --- PlatformAdapter -----------------------------------------------------------------

    def list_tasks(self) -> Sequence[Task]:
        return self.snapshot(self.clock()).tasks

    def get_task(self, task_id: str) -> Task | None:
        return next((t for t in self.list_tasks() if t.id == task_id), None)

    def events(self, since: datetime) -> Iterable[PlatformEvent]:
        return ()

    def comment(self, task_id: str, body: str) -> None:
        self._issue(task_id)
        self.tracker.reserve(1)
        self.tracker.comment(task_id, note("A note from Kelpie:", body))

    def unblock(self, task_id: str, reason: str) -> None:
        item = self._issue(task_id)
        blocked = {label.casefold() for label in self._board(item).blocked_labels}
        present = [label for label in item.labels if label.casefold() in blocked]
        if not present:
            log.info("issues: %s carries no blocked label any more; nothing to unblock", task_id)
            return
        self.tracker.reserve(len(present) + 1)
        self.tracker.remove_labels(task_id, present)
        quoted = f"Removed: {', '.join(present)}\n{reason}"
        self._say(task_id, note("Unblocked: the blocked labels came off.", quoted))

    def requeue(self, task_id: str, reason: str) -> None:
        item = self._issue(task_id)
        agent = self._holding(item)
        if agent is None and item.open:
            raise UnsupportedAction(
                f"no agent holds {task_id}, so there is nothing to restart; a human decides"
            )
        if agent is not None:
            _startable(agent)
        if not item.open and item.closed_reason in CANCELLED_REASONS:
            raise UnsupportedAction(
                f"{task_id} was closed as {item.closed_reason}; Kelpie does not reopen it"
            )
        self.tracker.reserve(RESERVE, (agent,) if agent is not None else ())
        if not item.open:
            self.tracker.reopen(task_id)
        if agent is None:
            self._say(task_id, note("Reopened.", reason))
            return
        try:
            self._take_off(agent, item)
        except IssuesError as exc:
            raise IssuesError(
                str(exc),
                f"{agent.name} could not be taken off {task_id} completely "
                f"({exc.public_reason}); check its assignee and label",
            ) from exc
        try:
            self._start(agent, item)
        except IssuesError as exc:
            raise IssuesError(
                str(exc),
                f"{agent.name} was taken off {task_id} but not started again completely "
                f"({exc.public_reason}); start it by hand",
            ) from exc
        self._say(task_id, note(f"Restarted {agent.name}.", reason))

    def reassign(self, task_id: str, owner_id: str, reason: str) -> None:
        new = self.agents.get(owner_id)
        if new is None:
            raise UnsupportedAction(f"{owner_id!r} is not an agent of {self.name}")
        _startable(new)
        item = self._issue(task_id)
        board = self._board(item)
        if board.agents and owner_id not in board.agents:
            raise UnsupportedAction(f"{board.key} does not take tasks for {owner_id}")
        if not item.open:
            raise UnsupportedAction(f"{task_id} is closed; reopen it to hand it over")
        old = self._holding(item)
        if old is not None and old.name == new.name:
            log.info("issues: %s is already %s's; nothing to reassign", task_id, owner_id)
            return
        self.tracker.reserve(RESERVE, (new,) if old is None else (new, old))
        # The new agent first: a failure halfway leaves the issue held, never by nobody.
        self._start(new, item)
        if old is not None:
            try:
                self._take_off(old, item)
            except IssuesError as exc:
                raise IssuesError(
                    str(exc),
                    f"{new.name} was started on {task_id} but {old.name} could not be taken "
                    f"off ({exc.public_reason}); both may work on it",
                ) from exc
        what = f"Handed from {old.name} to {new.name}." if old else f"Handed to {new.name}."
        self._say(task_id, note(what, reason))

    def wip_commit(
        self, task_id: str, message: str, *, branch: str | None = None, draft_pr: bool = False
    ) -> None:
        raise UnsupportedAction("issue agents commit on their own machines; Kelpie cannot")

    def delegate(self, task_id: str, operator: str, title: str, body: str, key: str) -> str:
        """Open a delegation card (#347) for ``operator``, one of this adapter's agents, in
        the repository of ``task_id`` (``SupportsDelegate``), and start the agent on it the
        way it is started (assigned, labelled or mentioned). The card carries the board's
        ``label``, so it is a task here and Kelpie watches it; its text is Kelpie's own
        words with the card quoted inert (:func:`note`), then the key line, which makes the
        create idempotent: a card Kelpie's token opened with that line is returned (its
        agent started again if the issue no longer shows it) instead of a second one.

        ``UnsupportedAction`` before any write: a tracker that cannot open issues (Linear,
        for now), an operator that is no agent of this adapter, one the board does not
        allow or that cannot be started, or a task not of the last read."""
        create = getattr(self.tracker, "create_card", None)
        find = getattr(self.tracker, "find_card", None)
        if create is None or find is None:
            raise UnsupportedAction(f"{self.name} cannot open delegation cards yet")
        agent = self.agents.get(operator.strip())
        if agent is None:
            raise UnsupportedAction(f"{operator!r} is not an agent of {self.name}; no card made")
        _startable(agent)
        board = self._boards.get(self._board_of.get(task_id, ""))
        if task_id not in self._known or board is None:
            raise UnsupportedAction(f"{task_id} was not a task of {self.name} at the last read")
        if board.agents and agent.name not in board.agents:
            raise UnsupportedAction(f"{board.key} does not take tasks for {agent.name}")
        if task_id in self._cards:
            raise UnsupportedAction(f"{task_id} is a delegation card; no card about a card")
        line = card_line(key)
        made = _waiting(lambda: find(board.key, line))
        if made is not None:
            item = _waiting(lambda: self.tracker.fetch(made))
            if item.open and agent.mention is None and not _started_on(agent, item):
                self.tracker.reserve(RESERVE, (agent,))
                self._start(agent, item)  # a create whose start failed: start it now
            return made
        self.tracker.reserve(RESERVE, (agent,))
        text = card_text(f"Kelpie hands a problem to {agent.name}.", body, line)
        labels = [board.label] if board.label is not None else []
        made = create(board.key, inert_title(title), text, labels)
        try:
            self._start(agent, self.tracker.fetch(made))
        except (IssuesError, Deferred) as exc:
            reason = getattr(exc, "public_reason", None) or str(exc)
            then = (
                f"start {agent.name} on it by hand ({agent.mention})"
                if agent.mention is not None and agent.assignee is None and agent.label is None
                else "Kelpie starts it on the retry"
            )
            raise IssuesError(
                f"card {made} opened, {agent.name} not started: {exc}",
                f"card {made} was opened but {agent.name} was not started ({reason}); {then}",
            ) from exc
        return made

    def lift_hold(self, hold: str, kind: str, task_ids: Sequence[str], reason: str) -> str:
        """Take the hold's own pause label off its held tasks (``SupportsLiftHold``).

        The hold must be named after one of the board's ``pause_labels`` (``on-hold``): only
        that label comes off, never another pause someone else put on. Refused before any
        write (``UnsupportedAction``, a person is told): ``lift_holds`` off, a hold that is
        not a pause, no held task of this adapter at the last read, or no held task whose
        board has the hold's name as a pause label (a pause state on Linear: which state
        comes next is a person's call). Each task is re-read right before its own writes
        (its label, then a note, held together): one closed, blocked by a blocked label too,
        or no longer carrying the label is skipped; none lifted and none left is
        ``NothingToDo``. At most ``MAX_LIFTS`` tasks are lifted per action; the rest, and
        tasks that cannot be read now, are left to a person (the result says how many). A
        failure after a lift raises with what was lifted, so the engine retries the rest."""
        if not self.lift_holds:
            raise UnsupportedAction(f"allow_lift_holds is off for {self.name}")
        if kind != PAUSE_KIND:
            raise UnsupportedAction(
                f"{self.name} lifts pause holds only; hold {hold!r} is a {kind}"
            )
        ours = [t for t in dict.fromkeys(task_ids) if t in self._known][:MAX_LIFT]
        if not ours:
            raise UnsupportedAction(f"hold {hold!r} holds no task of {self.name}")
        want = hold.casefold()
        boards = [self._boards.get(self._board_of.get(t, "")) for t in ours]
        if not any(
            b is not None and want in {x.casefold() for x in b.pause_labels} for b in boards
        ):
            raise UnsupportedAction(
                f"hold {hold!r} is no pause label of {self.name}'s boards: name a pause hold "
                "after the label it lifts; a person lifts it"
            )
        lifted: list[str] = []
        left = 0
        for n, task_id in enumerate(ours):
            if len(lifted) >= MAX_LIFTS:
                left += len(ours) - n
                break
            try:
                item = self._issue(task_id)
            except Deferred:  # the tracker asks Kelpie to wait: no more reads or writes now
                if not lifted:
                    raise
                left += len(ours) - n
                break
            except (UnsupportedAction, IssuesError) as exc:
                log.info("issues: hold %r: %s not read: %s", hold, task_id, exc)
                left += 1
                continue
            board = self._board(item)
            label = next((x for x in item.labels if x.casefold() == want), None)
            if not item.open or _labelled(item, board.blocked_labels) or label is None:
                continue  # closed, blocked for another reason, or lifted meanwhile
            if want not in {x.casefold() for x in board.pause_labels}:
                left += 1  # on this board the name is no pause label
                continue
            try:
                self.tracker.reserve(2)
                self.tracker.remove_labels(task_id, [label])
            except Deferred:
                if not lifted:
                    raise
                left += len(ours) - n
                break
            except Exception as exc:
                if not lifted:
                    raise
                done = ", ".join(lifted)
                raise IssuesError(
                    f"hold {hold!r}: lifted on {done}, then {task_id} failed: {exc}",
                    f"hold '{hold}' lifted on {done} only; the rest is tried again",
                ) from exc
            lifted.append(task_id)
            quoted = f"Removed: {label}\n{reason}"
            self._say(task_id, note(f"Hold '{hold}' lifted: its label came off.", quoted))
        rest = f"; {left} left to a person" if left else ""
        if not lifted:
            if left:
                raise UnsupportedAction(f"hold {hold!r}: no task could be lifted here{rest}")
            raise NothingToDo(f"no task of hold {hold!r} carries its label any more")
        return f"label '{hold}' off {', '.join(lifted)}{rest}"

    # --- carrying actions out ------------------------------------------------------------

    def _issue(self, task_id: str) -> Item:
        """The issue behind a task of the last read, as the tracker has it now. Ids that were
        no task (another issue, a pull request, one without the board's label) are refused
        before anything is read or written."""
        if task_id not in self._known:
            raise UnsupportedAction(f"{task_id} was not a task of {self.name} at the last read")
        try:
            item = self.tracker.fetch(task_id)
        except IssuesError as exc:
            if exc.kind in WAIT_KINDS:  # nothing changed yet: ask again later
                raise Deferred(f"issues: {self.name} cannot be read now ({exc.kind})") from exc
            raise
        board = self._board(item)
        if board.label is not None and board.label.casefold() not in {
            label.casefold() for label in item.labels
        }:
            raise UnsupportedAction(f"{task_id} no longer carries the label {board.label!r}")
        return item

    def _board(self, item: Item) -> Board:
        board = self._boards.get(item.board)
        if board is None:  # the tracker refuses ids outside its boards; defence in depth
            raise UnsupportedAction(f"{item.id} is not on a configured board")
        return board

    def _say(self, task_id: str, text: str) -> None:
        """A comment explaining a change already made: best effort, so a refused comment does
        not make the engine retry (and repeat) the change."""
        try:
            self.tracker.comment(task_id, text)
        except Exception as exc:  # IssuesError, Deferred: the change stands either way
            log.warning("issues: %s changed, but the note was not posted: %s", task_id, exc)

    def _holding(self, item: Item) -> Agent | None:
        """The agent on ``item``: as the issue shows it now, else as the last read did (an
        agent seen only through its pull request)."""
        agent, _ = holder(item, list(self.agents.values()))
        if agent is None:
            agent = self.agents.get(self._owners.get(item.id, ""))
        return agent

    def _take_off(self, agent: Agent, item: Item) -> None:
        """Undo what started ``agent`` on ``item`` (a mention cannot be undone)."""
        if agent.assignee is not None:
            want = login_key(agent.assignee)
            logins = [a for a in item.assignees if login_key(a) == want]
            if logins:
                self.tracker.unassign(item.id, logins)
        if agent.label is not None:
            self.tracker.remove_labels(item.id, [agent.label])

    def _start(self, agent: Agent, item: Item) -> None:
        """Start ``agent`` on ``item`` the way it is started (all of its assignee and label,
        as :meth:`_take_off` removes all). A mention carries fixed words only: the rule's
        reason (which can quote anyone) goes in a separate note."""
        if agent.assignee is not None:
            self.tracker.assign(item.id, [agent.assignee])
        if agent.label is not None:
            self.tracker.add_labels(item.id, [agent.label])
        if agent.assignee is None and agent.label is None:
            assert agent.mention is not None  # _startable
            self.tracker.comment(item.id, signed(f"{agent.mention} {MENTION_TEXT}"))


def _labelled(item: Item, labels: Iterable[str]) -> list[str]:
    """The labels of ``item`` that are among ``labels`` (as the issue spells them)."""
    want = {label.casefold() for label in labels}
    return [label for label in item.labels if label.casefold() in want]


def _startable(agent: Agent) -> None:
    if agent.assignee is None and agent.label is None and agent.mention is None:
        raise UnsupportedAction(
            f"agent {agent.name} has no assignee, label or mention to start it with"
        )
