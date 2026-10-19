"""Shared task semantics for rules, so that every rule answers the same questions alike:
which conditions belong to a task right now, and is anyone still working on it."""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from kelpie.core.conditions import linked_conditions
from kelpie.core.model import (
    Condition,
    ConditionKind,
    PullRequest,
    Session,
    Snapshot,
    Status,
    Task,
)

__all__ = ["linked_conditions"]  # defined in kelpie.core.conditions, re-exported for rules

# Condition data older than this is not evidence: the connector may have stopped polling.
DEFAULT_MAX_CONDITION_AGE = timedelta(minutes=15)

# An open session that has not sent a heartbeat for this long is treated as gone: crashed
# sessions are exactly the ones that never report their end.
DEFAULT_STALE_AFTER = timedelta(minutes=30)


def finished_parent(task: Task, snapshot: Snapshot) -> datetime | None:
    """When the last subtask of a parent finished, if every subtask has and nothing else
    holds the parent: it is done in substance and only waits to be closed. None for any
    other task.

    Only a parent waiting to be picked up or worked (backlog, ready, in progress) counts: a
    blocked, waiting or reviewed parent has a reason of its own. It must have no open
    dependency besides its subtasks (an orchestrator may add work under it), and no open PR
    of its own or of a subtask (``PullRequest.task_ids``). Subtasks missing from the
    snapshot count as finished (adapters leave out finished tasks past their lookback); when
    none is known, a ready parent counts as finished since it became ready (a platform such
    as Hermes wakes a parent to ready when its subtasks are done).

    That is enough to leave the parent alone (no resume, no requeue); whether to propose
    closing it is :func:`family_prs`'s question."""
    if not task.subtasks or task.status not in _PARENT_OPEN:
        return None
    family = {task.id, *task.subtasks}
    if any(family.intersection(pr.task_ids) for pr in snapshot.pull_requests):
        return None
    by_id = {t.id: t for t in snapshot.tasks}
    if any(
        d.task_id == task.id
        and (dep := by_id.get(d.depends_on)) is not None
        and not dep.status.is_terminal
        for d in snapshot.dependencies
    ):
        return None
    known = [by_id[s] for s in task.subtasks if s in by_id]
    if not known:
        return task.status_since if task.status is Status.READY else None
    if not all(t.status.is_terminal for t in known):
        return None
    return max(t.status_since for t in known)


_PARENT_OPEN = frozenset({Status.BACKLOG, Status.READY, Status.IN_PROGRESS})
_REVERT = re.compile(r"^\s*revert\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class FamilyPRs:
    """The PRs of a parent and its subtasks (by task code in their title or branch), as far
    as the snapshot knows them."""

    known: bool  # a code host listed every repository's open and recent closed PRs in full
    merged: tuple[PullRequest, ...]  # merged into the default branch
    unmerged: tuple[PullRequest, ...]  # open, closed without merging, or merged reverts
    uncovered: tuple[str, ...]  # subtasks without a merged PR of their own

    @property
    def landed(self) -> bool:
        """Whether the family's work has certainly landed: only then may a parent be
        proposed for closing."""
        return self.known and bool(self.merged) and not self.unmerged and not self.uncovered


def family_prs(task: Task, snapshot: Snapshot) -> FamilyPRs:
    """What the PRs of a parent's family say about whether its work landed.

    On a live farm a parent was proposed for closing while two subtasks' PRs had been
    returned for conflicts. So its work has landed only when the snapshot knows every PR
    (``Snapshot.pull_requests_complete`` is True), every PR linked to the parent or a
    subtask is merged (an open PR, one closed without merging, or a merged revert means it
    has not), and every subtask has a merged PR of its own into the default branch. A
    subtask missing from the snapshot has none: its PRs cannot be linked. A PR merged into
    another branch (a stacked PR), or into one the connector could not tell, neither covers
    nor blocks. A PR closed without merging blocks only while the connector still lists it
    (``closed_lookback``)."""
    family = {task.id, *task.subtasks}
    merged: dict[tuple[str, int], PullRequest] = {}
    unmerged: dict[tuple[str, int], PullRequest] = {}
    for pr in (*snapshot.pull_requests, *snapshot.closed_pull_requests):
        if not family.intersection(pr.task_ids):
            continue
        key = (pr.repo, pr.number)
        if not pr.merged or _REVERT.match(pr.title):
            unmerged[key] = pr
        elif pr.into_default is True:
            merged[key] = pr
    covered = {t for pr in merged.values() for t in pr.task_ids}
    return FamilyPRs(
        known=snapshot.pull_requests_complete is True,
        merged=_by_ref(merged),
        unmerged=_by_ref(unmerged),
        uncovered=tuple(s for s in task.subtasks if s not in covered),
    )


def _by_ref(prs: dict[tuple[str, int], PullRequest]) -> tuple[PullRequest, ...]:
    return tuple(prs[k] for k in sorted(prs))


def planned(task: Task, labels: Collection[str] = ()) -> bool:
    """Whether a task is not ready for work for reasons of its own, so it does not wait on
    what it depends on (incident 20: a post-release task behind a person's planned task
    counted as 143 agent-hours of waiting on that person).

    It is in a pre-work status only a person moves it out of (``Task.prework``: Hermes
    triage, or scheduled for later; the adapter's config), or it carries one of ``labels``
    (a milestone or label that marks it as later, or a hold: rule 6's ``planned_labels``
    and hold labels; a label on a linked PR counts, see ``bottlenecks.with_pr_labels``). A
    plain backlog task is not planned: platforms keep dependents there until their parents
    finish (Hermes todo). Only a task not started yet (backlog, ready) is planned: one
    blocked or waiting was started, and waits on what its block or conditions name, labels
    or not. Rules 6 and 13, and through rule 13 the daily report's agent-hours waiting on
    people, read it alike. A planned task does not wait on what it depends on, and a planned
    human task gates nothing."""
    if task.status not in _NOT_STARTED:
        return False  # blocked or waiting: it was started, and waits on what it names
    return task.prework or not frozenset(labels).isdisjoint(task.labels)


_NOT_STARTED = frozenset({Status.BACKLOG, Status.READY})


def labels_list(value: object, name: str) -> list[str]:
    """A config list of non-empty labels (``hold_labels``, ``planned_labels``)."""
    if not isinstance(value, list | tuple) or not all(isinstance(x, str) and x for x in value):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return list(value)


def responsible(task: Task) -> str | None:
    """Who answers for a task: its owner, else whom the platform routed it to."""
    return task.owner_id or task.assignee


def last_seen(session: Session) -> datetime:
    """The last sign of life of a session."""
    return session.ended_at or session.last_heartbeat or session.started_at


def is_live(session: Session, now: datetime, stale_after: timedelta = DEFAULT_STALE_AFTER) -> bool:
    return session.ended_at is None and now - last_seen(session) < stale_after


def live_session(
    task_id: str, snapshot: Snapshot, stale_after: timedelta = DEFAULT_STALE_AFTER
) -> Session | None:
    """A session currently working on the task, if any."""
    return next(
        (s for s in snapshot.sessions_for(task_id) if is_live(s, snapshot.now, stale_after)),
        None,
    )


def fresh(
    condition: Condition,
    since: datetime,
    now: datetime,
    max_age: timedelta = DEFAULT_MAX_CONDITION_AGE,
) -> bool:
    """Whether the condition was checked after ``since`` and recently enough to act on."""
    checked = condition.checked_at
    return checked is not None and checked >= since and now - checked <= max_age


def dead_ends(conditions: Iterable[Condition], since: datetime) -> list[Condition]:
    """Unmet conditions flagged ``params.terminal`` (they can never be met), seen since ``since``.

    A connector sets the flag when what the condition waits for ended the wrong way: a PR
    closed without merging, a human's task failed or cancelled. Seen means checked after the
    wait began; age aside, so the alert holds while the connector is slow to check again.
    A condition flagged ``params.terminal_ok`` is not a dead end when every condition of the
    wait carries the flag: the rule that registered them releases the task when its targets
    end (rule 12). In a mixed wait nothing would release it, so it counts.
    """
    conditions = list(conditions)
    released = all(c.params.get("terminal_ok") is True for c in conditions)
    return [
        c
        for c in conditions
        if not c.satisfied
        and c.params.get("terminal") is True
        and not (released and c.params.get("terminal_ok") is True)
        and c.checked_at is not None
        and c.checked_at >= since
    ]


def dead_end_text(conditions: Iterable[Condition]) -> str:
    """What ended wrong, for an alert."""
    parts: list[str] = []
    for c in conditions:
        if c.kind is ConditionKind.PR_MERGED:
            parts.append(f"the PR was closed without merging ({c.id})")
        elif c.kind is ConditionKind.HUMAN_TASK:
            parts.append(
                f"the human task {c.params.get('task')} ended as {c.params.get('status')} ({c.id})"
            )
        else:
            parts.append(f"{c.id} ({c.kind}) can no longer be met")
    return "; ".join(parts)


# Statuses a session can declare as its end. in_progress/ready/backlog are not an end.
DECLARED = frozenset(
    {
        Status.DONE,
        Status.FAILED,
        Status.CANCELLED,
        Status.REVIEW,
        Status.ACCEPTED,
        Status.BLOCKED,
        Status.WAITING,
    }
)
HANDOFF = frozenset({Status.REVIEW, Status.ACCEPTED})  # queues for the next stage


@dataclass(frozen=True, slots=True)
class Exit:
    session: Session
    at: datetime  # ended_at, or the last sign of life of a silent session
    silent: bool


def last_exit(
    task: Task, snapshot: Snapshot, stale_after: timedelta = DEFAULT_STALE_AFTER
) -> Exit | None:
    """The task's most recent session if it is over; None if a session is live or none ran."""
    exits: list[Exit] = []
    for s in snapshot.sessions_for(task.id):
        if is_live(s, snapshot.now, stale_after):
            return None
        exits.append(Exit(s, last_seen(s), silent=s.ended_at is None))
    return max(exits, key=lambda e: (e.at, e.session.started_at), default=None)


def undeclared_exit(
    task: Task, snapshot: Snapshot, stale_after: timedelta = DEFAULT_STALE_AFTER
) -> Exit | None:
    """The exit that left a working task without a declared end (rule 1's business).

    Applies to in_progress, review and accepted tasks: the last session is over, nobody
    changed the task since, and the session neither declared a real end nor handed the
    task on to review/accepted during its run.
    """
    if task.status not in {Status.IN_PROGRESS, *HANDOFF}:
        return None
    end = last_exit(task, snapshot, stale_after)
    if end is None or task.status_since > end.at:
        return None
    if end.session.final_status in DECLARED:
        return None
    if task.status in HANDOFF and task.status_since >= end.session.started_at:
        return None  # handed on during the session: a declared end
    return end
