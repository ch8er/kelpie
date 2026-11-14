"""Asks in comments and block reasons, shared by rule ``unanswered_asks`` (asks to agents)
and rule 13 (``needs_you``: asks to people), issues #297 and #461.

An ask is a question an agent puts to an owner of the farm: an ``@<owner id>`` mention
(case-insensitive) in text that contains a question mark. Where it can be:

- **a comment** on an open task, written by an agent (not a person, not Kelpie), that
  mentions another owner of the kind asked for: an agent (:func:`open_asks`, rule
  ``unanswered_asks``) or a person (:func:`open_asks` with ``OwnerKind.HUMAN``, rule 13);
- **a block reason** of a blocked agent task that mentions another agent
  (:func:`block_ask`): the task's owner asked it and blocked on the answer
  ("@architect-1 which schema version?").

On a multi-host farm owner ids are ``<host>:<id>`` and comment authors are not: ``@dev-1``
names the ``dev-1`` of the task's own machine, ``@host-b:dev-1`` names that host's (and is no
ask unless it serves the task), and answers count only from the same machine. One
obligation per task, asker and recipient: it opens at the first ask after the last answer,
and later asks of the same pair add nothing.

**What counts as an answer** (the most conservative reading, so a reminder is never sent
for a question that was answered somewhere Kelpie can see):

- any later comment by the recipient on the same task;
- any later comment by the recipient on any task of the same machine that mentions the
  asker (``@<asker>``);
- any later comment by a person on the same task (a person settled it, or took it over);
- the task reaching a terminal status (done, failed, cancelled); for a block ask, the
  block clearing.

Pure functions over a :class:`~kelpie.core.model.Snapshot`; which asks a rule announces,
and when, is the rule's.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from kelpie.core.conditions import linked_conditions
from kelpie.core.model import Block, Comment, KelpieMark, Owner, OwnerKind, Snapshot, Status, Task

_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9][\w.:-]*)")


def local(name: str) -> str:
    """A name without a host prefix (``host-a:dev-1`` -> ``dev-1``), case-folded."""
    return name.rsplit(":", 1)[-1].casefold()


def mentions(text: str) -> set[str]:
    """The names mentioned as ``@name`` or ``@host:name`` (case-folded, without a trailing
    sentence dot or colon)."""
    return {m.rstrip(".:-").casefold() for m in _MENTION.findall(text)}


# (machine, author, a name the author mentioned) -> when that author last mentioned it
Replies = dict[tuple[str | None, str, str], datetime]


@dataclass(frozen=True, slots=True)
class Ask:
    task: Task
    asker: str
    recipient: Owner
    comment: Comment  # the first ask of the pair since the last answer (a block's: its reason)
    in_block: bool = False  # asked in the task's block reason, not in a comment


def owners_by_name(snapshot: Snapshot, kind: OwnerKind) -> dict[str, list[Owner]]:
    """The snapshot's owners of ``kind`` by their id without its host prefix, by id."""
    found: dict[str, list[Owner]] = {}
    for o in sorted(snapshot.owners, key=lambda o: o.id):
        if o.kind is kind:
            found.setdefault(local(o.id), []).append(o)
    return found


def resolve(name: str, task: Task, owners: dict[str, list[Owner]]) -> Owner | None:
    """The owner a mention on ``task`` names: among the owners with that id (without its
    host prefix), the first by id that serves the task's machine; ``@host:name`` must name
    that host too (on a multi-host farm, owner ids are ``<host>:<id>``)."""
    host, _, short = name.rpartition(":")
    for owner in owners.get(short, ()):
        if host and owner.id.casefold() != name and (owner.host or "").casefold() != host:
            continue
        if owner.serves(task):
            return owner
    return None


def replies(snapshot: Snapshot, kind: OwnerKind = OwnerKind.AGENT) -> Replies:
    """When each author of ``kind`` (an agent, or a person: comments ``human`` marks) last
    mentioned each name, per machine: an answer to the asker anywhere on the same machine
    (owners of two hosts may share a name)."""
    want_human = kind is OwnerKind.HUMAN
    found: Replies = {}
    for t in snapshot.tasks:
        for c in t.comments:
            if c.human is not want_human:
                continue
            author = local(c.author)
            for name in {local(m) for m in mentions(c.text)}:
                key = (t.host, author, name)
                if key not in found or c.at > found[key]:
                    found[key] = c.at
    return found


def open_asks(snapshot: Snapshot, kind: OwnerKind = OwnerKind.AGENT) -> list[Ask]:
    """Every unanswered comment ask to an owner of ``kind`` on the board's open tasks, in
    task order, then by when it was asked. Pure; status filters and thresholds are the
    rules'."""
    owners = owners_by_name(snapshot, kind)
    if not owners:
        return []
    answered = replies(snapshot, kind)
    found: list[Ask] = []
    for task in snapshot.tasks:
        if task.status.is_terminal or not task.comments:
            continue
        found.extend(_task_asks(task, owners, answered))
    return found


def _task_asks(task: Task, owners: dict[str, list[Owner]], answered: Replies) -> list[Ask]:
    comments = sorted(task.comments, key=lambda c: c.at)
    pending: dict[tuple[str, str], Ask] = {}
    for c in comments:
        author = local(c.author)
        if c.human:
            pending.clear()  # a person spoke on the task: what was asked is theirs now
            continue
        for pair in [p for p in pending if local(p[1]) == author]:
            del pending[pair]  # the recipient spoke on the task after the ask
        if "?" not in c.text:
            continue
        for name in sorted(mentions(c.text)):
            owner = resolve(name, task, owners)
            if owner is None or local(owner.id) == author:
                continue
            pending.setdefault((author, owner.id), Ask(task, c.author.strip(), owner, c))
    return [
        ask
        for pair, ask in sorted(pending.items(), key=lambda x: (x[1].comment.at, x[0]))
        if not answered_elsewhere(task.host, pair, ask.comment.at, answered)
    ]


def answered_elsewhere(
    host: str | None, pair: tuple[str, str], asked_at: datetime, answered: Replies
) -> bool:
    """Whether the recipient of ``pair`` (asker, recipient) mentioned the asker on the same
    machine after the ask."""
    asker, recipient = pair
    latest = answered.get((host, local(recipient), local(asker)))
    return latest is not None and latest > asked_at


def block_ask(
    task: Task,
    snapshot: Snapshot,
    agents: dict[str, list[Owner]] | None = None,
    answered: Replies | None = None,
) -> Ask | None:
    """The ask in a blocked agent task's block reason, while unanswered: the reason mentions
    another agent of the farm that serves the task and holds a question mark, and the block
    has no condition (linked, or named and missing: a wait Kelpie checks is that condition's)
    and is not Kelpie's own stop. The asker is the task's owner, an agent. Of the agents
    mentioned, the first in alphabetical order is the recipient. Whether the block asks a
    *person* instead (rule 13's) is the caller's test. ``agents`` and ``answered`` may be
    passed in once per tick (:func:`owners_by_name`, :func:`replies`)."""
    block: Block | None = task.block
    if task.status is not Status.BLOCKED or block is None or block.kelpie is KelpieMark.STOP:
        return None
    if "?" not in block.reason:
        return None
    conditions, missing = linked_conditions(task, snapshot)
    if conditions or missing is not None:
        return None  # it waits on a condition (rule 4's), or names one Kelpie cannot see
    asker = snapshot.owner(task.owner_id) if task.owner_id else None
    if asker is None or asker.kind is not OwnerKind.AGENT:
        return None
    agents = owners_by_name(snapshot, OwnerKind.AGENT) if agents is None else agents
    recipient = None
    for name in sorted(mentions(block.reason)):
        owner = resolve(name, task, agents)
        if owner is not None and owner.id != asker.id:
            recipient = owner
            break
    if recipient is None:
        return None
    for c in task.comments:
        if c.at <= block.since:
            continue
        if c.human or local(c.author) == local(recipient.id):
            return None  # a person or the recipient spoke on the task since the block
    answered = replies(snapshot) if answered is None else answered
    if answered_elsewhere(task.host, (asker.id, recipient.id), block.since, answered):
        return None
    reason = Comment(asker.id, block.reason, block.since)
    return Ask(task, asker.id, recipient, reason, in_block=True)
