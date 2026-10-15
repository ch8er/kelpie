"""Checkpoints — commit reminders, WIP commit on exit, resume from the last commit.

Sessions die (crashes, timeouts, context limits). Work that was never committed dies with
them, and the next session starts from zero. This rule keeps work recoverable:

- **reminder**: a live session whose worktree has uncommitted changes and whose last
  commit is older than ``commit_every`` (default 30m), or that has more than
  ``max_changed_files`` (default 20) changed files, gets a comment asking it to commit;
- **WIP commit**: when the task's last session is over (ended, or silent past
  ``session_stale_after``) and no other session is live on it, a dirty worktree gets a
  ``wip_commit`` to the task branch with the session's progress note. WIP commits must not
  trigger CI: ``wip_ci = "skip_ci"`` adds ``[skip ci]`` to the message, ``"draft_pr"`` asks
  the executor to push to a draft PR instead;
- **resume note**: when that exit sends the task back to the queue (it is queued again, or
  rule 1 is about to requeue it), the task gets one comment naming the WIP commit or the
  last commit and branch, with the progress note, so the next session continues instead
  of restarting.

WIP commits and resume notes are one-off actions (``Action.once``) and are only emitted
for ``act_within`` (default 24h) after the exit, which must stay shorter than the engine's
``once_window``. This rule runs first among the rules so the WIP commit precedes any
requeue (rule 1).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import Session, Snapshot, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._tasks import DEFAULT_STALE_AFTER, Exit, is_live, last_exit, undeclared_exit

NAME = "checkpoints"

_QUEUED = {Status.READY, Status.BACKLOG}
_WIP_CI = ("skip_ci", "draft_pr")
_NOTE_LIMIT = 500


@dataclass(frozen=True, slots=True)
class Checkpoints:
    commit_every: timedelta = timedelta(minutes=30)
    max_changed_files: int = 20
    wip_ci: str = "skip_ci"
    session_stale_after: timedelta = DEFAULT_STALE_AFTER
    act_within: timedelta = timedelta(hours=24)
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        for task in snapshot.tasks:
            if task.status.is_terminal:
                continue
            sessions = snapshot.sessions_for(task.id)
            live = [s for s in sessions if is_live(s, snapshot.now, self.session_stale_after)]
            for session in live:
                actions.extend(self._remind(task, session, snapshot))
            end = None if live else last_exit(task, snapshot, self.session_stale_after)
            if end is None or snapshot.now - end.at >= self.act_within:
                continue  # someone is working on it, nobody ever did, or the exit is old
            if end.session.dirty_worktree:
                actions.append(self._wip_commit(task, end.session))
            actions.extend(self._resume(task, end, snapshot))
        return actions

    def _remind(self, task: Task, session: Session, snapshot: Snapshot) -> Iterable[Action]:
        if not session.dirty_worktree and not session.changed_files:
            return
        # A fresh worktree's HEAD is the base commit, often older than the session.
        anchor_at = max(session.last_commit_at or session.started_at, session.started_at)
        since_commit = snapshot.now - anchor_at
        too_many = (session.changed_files or 0) > self.max_changed_files
        if since_commit < self.commit_every and not too_many:
            return
        why = (
            f"{session.changed_files} changed files"
            if too_many
            else f"no commit for {_minutes(since_commit)}"
        )
        anchor = anchor_at.isoformat()
        yield Action(
            ActionKind.COMMENT,
            NAME,
            f"Checkpoint: {why} with uncommitted work. Please commit now (a WIP commit is "
            "fine) so the work survives if this session dies.",
            task_id=task.id,
            key=f"remind:{session.id}:{anchor}",
            params={"session_id": session.id, "changed_files": session.changed_files},
        )

    def _wip_commit(self, task: Task, session: Session) -> Action:
        note = _note(session)
        message = f"WIP: {task.title}"
        if self.wip_ci == "skip_ci":
            message += " [skip ci]"
        return Action(
            ActionKind.WIP_COMMIT,
            NAME,
            f"{message}\n\nSession {session.id} of {session.owner_id} ended with uncommitted "
            f"changes; saved by Kelpie so the next session can continue.\n\n{note}".rstrip(),
            task_id=task.id,
            key=f"wip:{session.id}",
            once=True,
            params={
                "session_id": session.id,
                "branch": session.branch,
                "skip_ci": self.wip_ci == "skip_ci",
                "draft_pr": self.wip_ci == "draft_pr",
            },
        )

    def _resume(self, task: Task, end: Exit, snapshot: Snapshot) -> Iterable[Action]:
        if task.status in _QUEUED:
            if task.status_since < end.session.started_at:
                return  # queued before this session: not sent back by it
        elif undeclared_exit(task, snapshot, self.session_stale_after) is None:
            return  # the session declared an end; nobody resumes from here
        session = end.session
        if session.dirty_worktree:
            where = "the WIP commit Kelpie saved"
            commit = None
        else:
            committed = [s for s in snapshot.sessions_for(task.id) if _committed(s)]
            if not committed:
                return  # nothing to resume from
            session = max(committed, key=lambda s: s.last_commit_at or s.started_at)
            where = f"commit {session.last_commit}"
            commit = session.last_commit
        branch = session.branch or end.session.branch
        if branch:
            where += f" on {branch} (see git log there for what was done)"
        note = _note(end.session) or _note(session)
        yield Action(
            ActionKind.COMMENT,
            NAME,
            f"Resume from {where} instead of starting over.\n\n{note}".rstrip(),
            task_id=task.id,
            key=f"resume:{task.id}:{end.session.id}",
            once=True,
            params={"commit": commit, "branch": branch, "session_id": session.id},
        )


def _committed(session: Session) -> bool:
    """The session made a commit (not just started from the base commit)."""
    if not session.last_commit:
        return False
    return session.last_commit_at is None or session.last_commit_at >= session.started_at


def _note(session: Session) -> str:
    if not session.summary:
        return ""
    text = session.summary.strip()
    if len(text) > _NOTE_LIMIT:
        text = text[: _NOTE_LIMIT - 1].rstrip() + "…"
    return f"Progress note from the last session:\n{text}"


def _minutes(d: timedelta) -> str:
    return f"{int(d.total_seconds() // 60)} minutes"


def _duration(value: Any, name: str) -> timedelta:
    try:
        result = parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if result <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return result


@register(NAME)
def build(config: dict[str, Any]) -> Checkpoints:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    for key in ("commit_every", "session_stale_after", "act_within"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    if "max_changed_files" in config:
        n = config.pop("max_changed_files")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ValueError("max_changed_files must be a positive integer")
        kwargs["max_changed_files"] = n
    if "wip_ci" in config:
        mode = config.pop("wip_ci")
        if mode not in _WIP_CI:
            raise ValueError(f"wip_ci must be one of {', '.join(_WIP_CI)}")
        kwargs["wip_ci"] = mode
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return Checkpoints(**kwargs)
