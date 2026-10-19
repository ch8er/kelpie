"""Checkpoints — commit reminders, WIP commit on exit, resume from the last commit.

Sessions die (crashes, timeouts, context limits). Work that was never committed dies with
them, and the next session starts from zero. This rule keeps work recoverable:

- **reminder**: a live session whose worktree has uncommitted changes and whose last
  commit is older than ``commit_every`` (default 30m; per role with
  ``commit_every_by_role``, e.g. reviewers commit rarely), or that has more than
  ``max_changed_files`` (default 20) changed files, gets a comment asking it to commit. Not
  while a long tool run is in progress: ``Session.running_tool`` with its start (the Hermes
  adapter reads a build or simulator process in the task's workspace from the host,
  ``[adapters.hermes.tools]``), less than ``tool_grace`` (2h) ago, or an open TOOL span of
  the session that started at least ``tool_long_after`` (5m) and less than ``tool_grace``
  ago (spans are every call, so a short one holds nothing; open TOOL spans come only from
  exporters that send unfinished spans);
- **WIP commit**: when the task's last session is over (ended, or silent past
  ``session_stale_after``) and no other session is live on it, a dirty worktree gets a
  ``wip_commit`` to the task branch with the session's progress note. WIP commits must not
  trigger CI: ``wip_ci = "skip_ci"`` adds ``[skip ci]`` to the message, ``"draft_pr"`` asks
  the executor to push to a draft PR instead. Never onto an open PR: a ``[skip ci]`` commit
  would give it a new head no CI ran on, which an automerge would merge. So no WIP commit when
  an open PR (drafts included) has its head on the session's or the task's branch or belongs
  to the task, or when no branch is known; and only once the open PRs were read in full after
  the exit (``Snapshot.pull_requests_at`` at least ``wip_pr_margin``, 1m, after it, from
  ``[connectors.github.pulls]``). Until then the decision waits (within ``act_within``;
  nothing is said meanwhile, not even the resume note). The WIP commit and the alert about
  changes left uncommitted share one once-key per session, so the ledger keeps the first
  decision (per mode: a decision made in observe or suggest is made again in act). No WIP
  commit once the task was claimed again since the exit (a commit would run under the next
  session; an alert and the resume note say the changes were left; a requeue alone is no
  claim, and the adapter refuses while a run is active),
  nor for a session that started before the oldest closed PR the snapshot lists
  (``Snapshot.closed_pull_requests_since``), nor onto a PR of the task that closed after the
  session started. A decision still waiting at ``act_within`` gets one alert.
  ``wip_without_pr_check`` skips the PR checks for a farm whose task branches never carry
  PRs. Not seen: a fork's PR, one in a repository ``[connectors.github] repos`` does not
  list, and one someone else opens between the last scan and the commit (up to
  ``poll_interval``). With ``wip_ci = "draft_pr"`` the draft PR of a first WIP commit refuses
  the next ones on that branch;
- **resume note**: when that exit sends the task back to the queue (it is queued again, or
  rule 1 is about to requeue it), the task gets one comment naming the WIP commit or the
  last commit and branch, with the progress note, so the next session continues instead
  of restarting. Not after a session that declared a block or a wait (``blocked``,
  ``waiting``): whoever unblocks the task sends it back, not a lost session.

WIP commits and resume notes are one-off actions (``Action.once``) and are only emitted
for ``act_within`` (default 24h) after the exit, which must stay shorter than the engine's
``once_window``. This rule runs first among the rules so a WIP commit decided on the exit's
tick precedes the requeue (rule 1); one still waiting for PR data when the task is claimed
again is not made at all (the next session finds the changes in the worktree), and one
still waiting at ``act_within`` gets one alert, so ``act_within`` plus 1h must stay shorter
than ``once_window``. The resume note goes out with the WIP commit, so it says where the
changes are if the commit failed.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Session, Snapshot, SpanKind, Status, Task
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._alerts import alert_params
from kelpie.rules._tasks import (
    DEFAULT_STALE_AFTER,
    Exit,
    finished_parent,
    is_live,
    last_exit,
    undeclared_exit,
)

NAME = "checkpoints"

_QUEUED = {Status.READY, Status.BACKLOG}
_PARKED = frozenset({Status.BLOCKED, Status.WAITING})  # declared by the session: no resume
GAVE_UP_WINDOW = timedelta(hours=1)  # after act_within: the one alert for a decision never made
_WIP_CI = ("skip_ci", "draft_pr")
_OUT_OF_HORIZON = "a PR closed during the session would no longer be listed"
_NOTE_LIMIT = 500


@dataclass(frozen=True, slots=True)
class Checkpoints:
    commit_every: timedelta = timedelta(minutes=30)
    max_changed_files: int = 20
    wip_ci: str = "skip_ci"
    session_stale_after: timedelta = DEFAULT_STALE_AFTER
    act_within: timedelta = timedelta(hours=24)
    commit_every_by_role: tuple[tuple[str, timedelta], ...] = ()
    tool_grace: timedelta = timedelta(hours=2)
    tool_long_after: timedelta = timedelta(minutes=5)
    wip_without_pr_check: bool = False
    wip_pr_margin: timedelta = timedelta(minutes=1)
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
            if end is None:
                continue  # someone is working on it, or nobody ever did
            age = snapshot.now - end.at
            if age >= self.act_within:
                # Kelpie stops acting on the exit: say once what became of the changes, unless
                # a decision was made already (the shared once-key keeps the first).
                if age < self.act_within + GAVE_UP_WINDOW and end.session.dirty_worktree:
                    verdict, why = self._wip_verdict(task, end, snapshot)
                    horizon = why == _OUT_OF_HORIZON and snapshot.pull_requests_at is None
                    if verdict == "defer" or horizon:
                        # The PR data never came: say so, not what the horizon now implies.
                        why = "the open PRs (or its branch) were never known in time"
                    actions.append(self._unsaved(task, end, why or "Kelpie stopped acting on it"))
                continue  # the exit is old
            unsaved = None
            if end.session.dirty_worktree:
                verdict, unsaved = self._wip_verdict(task, end, snapshot)
                if verdict == "defer":
                    continue  # the open PRs are not known since the exit yet: wait
                if verdict == "commit":
                    actions.append(self._wip_commit(task, end.session, self._branch(task, end)))
                else:  # refuse or skip
                    actions.append(self._unsaved(task, end, unsaved or ""))
            if finished_parent(task, snapshot) is None:  # a finished parent is closed, not resumed
                actions.extend(self._resume(task, end, snapshot, unsaved))
        return actions

    def _commit_every(self, task: Task, session: Session, snapshot: Snapshot) -> timedelta:
        """The task's role decides; without one, its owner's."""
        owner = snapshot.owner(session.owner_id)
        role = task.role or (owner.role if owner else None)
        return dict(self.commit_every_by_role).get(role or "", self.commit_every)

    def _tool_running(self, session: Session, snapshot: Snapshot) -> str | None:
        """The long tool run in progress in ``session`` that holds its reminder, if any:
        the adapter's ``running_tool`` (a build or simulator process, long by what it is)
        that started less than ``tool_grace`` ago, or an open TOOL span that started at
        least ``tool_long_after`` and less than ``tool_grace`` ago. One with no start time,
        or one that starts in the future, holds nothing."""
        now = snapshot.now
        held = [
            (s.start, s.tool or s.name)
            for s in snapshot.spans_for_session(session.id)
            if s.kind is SpanKind.TOOL
            and s.end is None
            and self.tool_long_after <= now - s.start < self.tool_grace
        ]
        since = session.running_tool_since
        if (
            session.running_tool
            and since is not None
            and timedelta(0) <= now - since < self.tool_grace
        ):
            held.append((since, session.running_tool))
        return max(held)[1] if held else None

    @staticmethod
    def _branch(task: Task, end: Exit) -> str | None:
        return end.session.branch or task.branch

    def _wip_verdict(self, task: Task, end: Exit, snapshot: Snapshot) -> tuple[str, str | None]:
        """``commit``; ``refuse`` or ``skip`` with why; or ``defer``. The WIP commit and the
        alert about changes left uncommitted share one once-key per session
        (``wip-decision:<session>``, ``once_per_key``), so the ledger keeps the first decision:
        a later tick that would decide otherwise is suppressed.

        Refuse: an open PR on a branch it may land on, or linked to the task; a PR of it that
        closed after the session started; or a session older than the closed PRs the
        snapshot lists (``Snapshot.closed_pull_requests_since``: such a PR would not be seen).
        Skip: the task was claimed again since the exit (a commit would run under the next
        session); a requeue alone is no skip. Defer: no branch known yet, or the open PRs
        not read in full at least ``wip_pr_margin`` after the exit (a PR the agent opened
        last may be missing)."""
        branches = {b for b in (end.session.branch, task.branch) if b}
        mine = [
            pr
            for pr in snapshot.pull_requests
            if not pr.fork and (pr.branch in branches or task.id in pr.task_ids)
        ]  # a fork's branch names are another repository's
        if mine:
            pr = mine[0]
            return "refuse", f"{pr.branch or 'its branch'} has an open PR, {pr.ref}"
        for pr in snapshot.closed_pull_requests:
            closed = pr.closed_at
            if (
                not pr.fork
                and (pr.branch in branches or task.id in pr.task_ids)
                and closed is not None
                and closed >= end.session.started_at
            ):
                return "refuse", f"its PR {pr.ref} was open after the session started"
        # Claimed since the exit: a commit would land under the next session. A requeue is
        # no claim: the platform's crash sweep requeues as it closes the run, often after
        # the run's last sign of life; the adapter refuses while a run is active.
        if task.status is Status.IN_PROGRESS and task.status_since > end.at:
            return "skip", "the task was claimed again before Kelpie could commit them"
        if not branches:
            return "defer", None  # the adapter may still report it
        if self.wip_without_pr_check:
            return "commit", None
        since = snapshot.closed_pull_requests_since
        if since is not None and end.session.started_at < since:
            return "refuse", _OUT_OF_HORIZON
        at = snapshot.pull_requests_at
        if (
            snapshot.pull_requests_complete is True
            and at is not None
            and at - end.at >= self.wip_pr_margin
        ):
            return "commit", None
        return "defer", None

    def _unsaved(self, task: Task, end: Exit, why: str) -> Action:
        """Uncommitted work Kelpie leaves alone: a person decides what becomes of it."""
        session = end.session
        branch = self._branch(task, end)
        where = f" on {branch}" if branch else ""
        return Action(
            ActionKind.ALERT,
            NAME,
            f"Session {session.id} of {session.owner_id} on task {task.id} ('{task.title}') "
            f"ended with uncommitted changes{where}, and Kelpie did not save them as a WIP "
            f"commit: {why}. They are only in that session's worktree: commit or drop them "
            "by hand.",
            task_id=task.id,
            severity=Severity.WARNING,
            key=f"wip-decision:{session.id}",  # shared with the WIP commit: the first stands
            once=True,
            once_per_key=True,
            params={
                "session_id": session.id,
                "branch": branch,
                "why": why,
                **alert_params(
                    f"Uncommitted work left on {task.id}{f' ({branch})' if branch else ''}, "
                    "not saved by Kelpie",
                    "Commit or drop the changes in the session's worktree by hand",
                ),
            },
        )

    def _remind(self, task: Task, session: Session, snapshot: Snapshot) -> Iterable[Action]:
        if not session.dirty_worktree and not session.changed_files:
            return
        # A fresh worktree's HEAD is the base commit, often older than the session.
        anchor_at = max(session.last_commit_at or session.started_at, session.started_at)
        since_commit = snapshot.now - anchor_at
        too_many = (session.changed_files or 0) > self.max_changed_files
        if since_commit < self._commit_every(task, session, snapshot) and not too_many:
            return
        if self._tool_running(session, snapshot) is not None:
            return  # a build or a simulator run: it commits when the call is over
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

    def _wip_commit(self, task: Task, session: Session, branch: str | None) -> Action:
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
            key=f"wip-decision:{session.id}",  # shared with the alert: the first decision stands
            once=True,
            once_per_key=True,
            params={
                "session_id": session.id,
                "branch": branch,  # the branch checked for open PRs
                "skip_ci": self.wip_ci == "skip_ci",
                "draft_pr": self.wip_ci == "draft_pr",
            },
        )

    def _resume(
        self, task: Task, end: Exit, snapshot: Snapshot, unsaved: str | None = None
    ) -> Iterable[Action]:
        if end.session.final_status in _PARKED:
            return  # it declared a block or a wait: the unblock sends the task back
        if task.status in _QUEUED:
            if task.status_since < end.session.started_at:
                return  # queued before this session: not sent back by it
        elif undeclared_exit(task, snapshot, self.session_stale_after) is None:
            return  # the session declared an end; nobody resumes from here
        session = end.session
        left = ""
        if session.dirty_worktree and unsaved is None:
            where = "the WIP commit Kelpie is saving"
            # Sent with the commit, not after it: the commit may still fail.
            left = (
                "\n\nIf git log does not show that commit, the changes are still in the last "
                "session's worktree."
            )
            commit = None
        else:
            if session.dirty_worktree:
                left = (
                    f"\n\nThe last session left uncommitted changes that Kelpie did not commit "
                    f"({unsaved}); look for them in its worktree."
                )
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
            f"Resume from {where} instead of starting over.{left}\n\n{note}".rstrip(),
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
    except (AttributeError, TypeError, ValueError) as exc:
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
    for key in ("tool_grace", "tool_long_after", "wip_pr_margin"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    default = Checkpoints()
    margin = kwargs.get("wip_pr_margin", default.wip_pr_margin)
    if margin >= kwargs.get("act_within", default.act_within):
        raise ValueError("wip_pr_margin must be shorter than act_within")
    if "commit_every_by_role" in config:
        roles = config.pop("commit_every_by_role")
        if not isinstance(roles, dict) or not all(isinstance(r, str) and r for r in roles):
            raise ValueError("commit_every_by_role must be a table of role = duration")
        kwargs["commit_every_by_role"] = tuple(
            (r, _duration(v, f"commit_every_by_role.{r}")) for r, v in sorted(roles.items())
        )
    if "wip_without_pr_check" in config:
        flag = config.pop("wip_without_pr_check")
        if not isinstance(flag, bool):
            raise ValueError("wip_without_pr_check must be true or false")
        kwargs["wip_without_pr_check"] = flag
    if "wip_ci" in config:
        mode = config.pop("wip_ci")
        if mode not in _WIP_CI:
            raise ValueError(f"wip_ci must be one of {', '.join(_WIP_CI)}")
        kwargs["wip_ci"] = mode
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return Checkpoints(**kwargs)
