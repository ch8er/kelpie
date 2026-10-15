"""Checkpoints — commit reminders, WIP commit on exit, resume from the last commit.

Sessions die (crashes, timeouts, context limits). Work that was never committed dies with
them, and the next session starts from zero. This rule keeps work recoverable:

- **reminder**: a live session whose worktree has uncommitted changes and whose last
  commit is older than ``commit_every`` (default 30m), or that has ``max_changed_files``
  (default 20) changed files, gets a comment asking it to commit;
- **WIP commit**: a session that is over (ended, or silent past ``session_stale_after``)
  and left a dirty worktree gets a ``wip_commit`` to the task branch with its progress note.
  WIP commits must not trigger CI: ``wip_ci = "skip_ci"`` adds ``[skip ci]`` to the message,
  ``"draft_pr"`` asks the executor to push to a draft PR instead;
- **resume note**: a task back in the queue after a session that committed work gets one
  comment with the last commit, branch and progress note, so the next session continues
  instead of restarting.

This rule runs first among the rules so the WIP commit precedes any requeue (rule 1).
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
from kelpie.rules._tasks import DEFAULT_STALE_AFTER, is_live, last_exit, last_seen

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
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        tasks = {t.id: t for t in snapshot.tasks}
        for session in snapshot.sessions:
            task = tasks.get(session.task_id)
            if task is None or task.status.is_terminal:
                continue
            if is_live(session, snapshot.now, self.session_stale_after):
                actions.extend(self._remind(task, session, snapshot))
            elif session.dirty_worktree:
                actions.append(self._wip_commit(task, session))
        for task in snapshot.tasks:
            if task.status in _QUEUED:
                actions.extend(self._resume(task, snapshot))
        return actions

    def _remind(self, task: Task, session: Session, snapshot: Snapshot) -> Iterable[Action]:
        if not session.dirty_worktree and not session.changed_files:
            return
        since_commit = snapshot.now - (session.last_commit_at or session.started_at)
        too_many = (session.changed_files or 0) >= self.max_changed_files
        if since_commit < self.commit_every and not too_many:
            return
        why = (
            f"{session.changed_files} changed files"
            if too_many
            else f"no commit for {_minutes(since_commit)}"
        )
        anchor = (session.last_commit_at or session.started_at).isoformat()
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

    def _resume(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        end = last_exit(task, snapshot, self.session_stale_after)
        if end is None or task.status_since < last_seen(end.session):
            return  # nobody worked on it yet, or it is back for another reason
        committed = [s for s in snapshot.sessions_for(task.id) if s.last_commit]
        if not committed:
            return
        last = max(committed, key=lambda s: s.last_commit_at or s.started_at)
        where = f"commit {last.last_commit}" + (f" on {last.branch}" if last.branch else "")
        note = _note(last)
        yield Action(
            ActionKind.COMMENT,
            NAME,
            f"Resume from {where} instead of starting over.\n\n{note}".rstrip(),
            task_id=task.id,
            key=f"resume:{task.id}:{last.last_commit}",
            once=True,
            params={"commit": last.last_commit, "branch": last.branch, "session_id": last.id},
        )


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
    for key in ("commit_every", "session_stale_after"):
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
