"""Rule 1 — exit contract (incident 2).

When an agent's session ends, it must leave the task in a declared state: a final status,
a hand-off to the next stage, or a block with an allowed reason. Otherwise nobody works on
the task and nobody knows why it stopped, so it goes back to the queue with an explanation.

Only what the *last session* left behind is judged:

- the task is still ``in_progress`` (or still ``review``/``accepted`` when a reviewer or
  merger took it and left without a verdict) → requeue;
- the session left the task ``blocked`` with no allowed reason and no registered
  condition → requeue.

A condition attached during snapshot assembly from the block reason (``[[block_conditions]]``,
e.g. "waiting for CI/merge of #123" resolved on GitHub) counts as registered: the agent
declared a wait Kelpie can check, so the block is rule 4's to clear, not a reason to requeue.

Blocks set by people or the platform outside an agent session are rule 4's business. A
session that never reported its end counts as ended once its heartbeat is older than
``session_stale_after``: crashed sessions are exactly the ones that skip the end hook.

**"Done" needs evidence** (P1, off until ``done_evidence`` is set). Tasks were marked done
without test output, green CI or a live check, then reopened. Each ``done_evidence`` entry
names one kind of evidence: an artifact of one of the task's sessions since it was last
reopened, whose path matches a glob (``artifacts``, case-insensitive, on the whole path or
the file name: test output, a live-check report), or a satisfied condition of a kind linked
to the task (``condition``, e.g. ``ci_status``; it needs a connector that keeps such a
condition on done tasks, which none does yet). A task an agent completed (a session ended
declaring done when the task became done, within ``session_exit_grace``, which is also the
time the platform gets to record the evidence) within ``done_evidence_within`` (24h) without
one of them (``done_evidence_require = "any"``) or without all of them (``"all"``) goes
back to the queue with the missing evidence in the reason (``done_evidence_action =
"alert"`` only alerts, once). A person's task, one a person moved to done, and a parent
(its evidence is its subtasks') are not judged. A platform that cannot requeue a done task
(Hermes) tells a human instead. The reopened-after-done rate per role
(``Task.reopened_at``) is on ``/metrics`` and in the daily report.

**People's acceptance criteria need their own evidence** (incident 18; off until
``acceptance_patterns`` is set). A person asked for "20 runs in a row, 20 of 20" before
automerge; the reviewer confirmed green CI and a clean merge only, and the PR merged without
it. The lines of a person's comments (and of the description, when a person created the
task) that match ``acceptance_patterns`` are its criteria (:mod:`kelpie.rules._acceptance`).
When the work is about to land or has landed, each criterion needs evidence written after
it that addresses it, naming its reference (``AC-`` and six hex digits) or quoting it, and
reports its result there (:mod:`kelpie.rules._acceptance`: a comment, a session summary, an
artifact, a CI build of the task's branch named for it that passed). While a task with a
criterion younger than ``ANNOUNCE_WITHIN`` (or a task that entered that status since) is
ready, in progress or in review and not landing yet, the criterion is announced on it once
(a comment with the reference, so the agent knows how to report it). Landing means, within
``done_evidence_within``: an open PR of the task that carries one of
``acceptance_merge_labels`` (it merges by itself once checks pass: critical), a PR of it
merged (critical, whoever closed the task), a mergeable PR of a task in review or accepted,
the task accepted, or an agent completing it. Parents are not judged, nor a task a person
moved to done without a merge. Each criterion without evidence is
alerted once per severity, naming it, the person who asked and the roles to tell
(``acceptance_notify_roles``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from fnmatch import fnmatchcase
from pathlib import PurePosixPath
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import (
    ConditionKind,
    KelpieMark,
    OwnerKind,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.core.serde import parse_duration
from kelpie.rules import register
from kelpie.rules._acceptance import ANNOUNCEMENT, Criterion, unanswered
from kelpie.rules._tasks import (
    DEFAULT_STALE_AFTER,
    Exit,
    finished_parent,
    last_exit,
    linked_conditions,
    undeclared_exit,
)

NAME = "exit_contract"
# A criterion is announced on its task this long after it was stated (or the task entered
# its status), at most: within the ledger's once_window (7d by default; keep it longer), so
# a criterion is not announced again and again.
ANNOUNCE_WITHIN = timedelta(hours=72)
_ANNOUNCED_IN = frozenset({Status.READY, Status.IN_PROGRESS, Status.REVIEW})


@dataclass(frozen=True, slots=True)
class Evidence:
    """One kind of evidence that a task is done: a session artifact whose path matches one
    of ``artifacts`` (globs), or a satisfied condition of kind ``condition``."""

    name: str
    artifacts: tuple[str, ...] = ()
    condition: ConditionKind | None = None

    def describe(self) -> str:
        if self.condition is not None:
            return f"{self.name} (a satisfied {self.condition} condition)"
        return f"{self.name} (an artifact matching {', '.join(self.artifacts)})"

    def found(self, task: Task, snapshot: Snapshot) -> bool:
        """Evidence of the current completion: since the task was last reopened."""
        if self.condition is not None:
            return any(
                c.task_id == task.id and c.kind is self.condition and c.satisfied is True
                for c in snapshot.conditions
            )
        since = max(task.reopened_at, default=None)
        paths = [
            a.path
            for s in snapshot.sessions_for(task.id)
            for a in s.artifacts
            if since is None or a.at > since
        ]
        return any(
            fnmatchcase(p.lower(), g.lower())
            or fnmatchcase(PurePosixPath(p).name.lower(), g.lower())
            for p in paths
            for g in self.artifacts
        )


DEFAULT_ALLOWED_BLOCK_REASONS = (
    "guard",
    "waiting_ci",
    "waiting_pr_merge",
    "waiting_human",
    "quota",
)


@dataclass(frozen=True, slots=True)
class ExitContract:
    """``allowed_block_reasons`` match the whole reason, or its first word followed by
    ``:`` or a space, ignoring case ("guard: wait for CI" is a ``guard`` block). A block
    tied to a registered condition (named by the block or linked to the task) is always
    allowed. ``session_exit_grace`` gives the platform time to record a status after an
    exit, and applies to blocks left behind too."""

    allowed_block_reasons: tuple[str, ...] = DEFAULT_ALLOWED_BLOCK_REASONS
    session_exit_grace: timedelta = timedelta(minutes=10)
    session_stale_after: timedelta = DEFAULT_STALE_AFTER
    done_evidence: tuple[Evidence, ...] = ()  # empty: "done" is not judged
    done_evidence_require: str = "any"  # "any" or "all"
    done_evidence_action: str = "requeue"  # or "alert"
    done_evidence_within: timedelta = timedelta(hours=24)
    acceptance_patterns: tuple[re.Pattern[str], ...] = ()  # empty: criteria are not read
    acceptance_merge_labels: tuple[str, ...] = ("automerge",)
    acceptance_notify_roles: tuple[str, ...] = ("reviewer",)
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        if self.done_evidence:
            humans = {o.id for o in snapshot.owners if o.kind is OwnerKind.HUMAN}
            for task in snapshot.tasks:
                if task.status is Status.DONE and (task.owner_id or task.assignee) not in humans:
                    actions.extend(self._check_done(task, snapshot))
        if self.acceptance_patterns:
            for task in snapshot.tasks:
                actions.extend(self._check_acceptance(task, snapshot))
        for task in snapshot.tasks:
            if finished_parent(task, snapshot) is not None:
                continue  # its subtasks did the work: rule 3 asks to close it, not resume it
            if task.status is Status.BLOCKED:
                actions.extend(self._check_block(task, snapshot))
                continue
            end = undeclared_exit(task, snapshot, self.session_stale_after)
            if end is not None and self._past_grace(end, snapshot):
                actions.append(self._requeue_exit(task, end))
            elif end is None:
                actions.extend(self._check_unapplied(task, snapshot))
        return actions

    def _check_done(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        """Done without the configured evidence: back to the queue (or an alert)."""
        age = snapshot.now - task.status_since
        if age > self.done_evidence_within or age < self.session_exit_grace:
            return  # long done, or the platform may still be recording the evidence
        if task.subtasks:
            return  # a parent: the evidence is its subtasks'
        if not self._agent_completed(task, snapshot):
            return  # a person (or the platform) moved it to done: not an agent's claim
        have = {e.name for e in self.done_evidence if e.found(task, snapshot)}
        need_all = self.done_evidence_require == "all"
        if (need_all and len(have) == len(self.done_evidence)) or (not need_all and have):
            return
        missing = [e for e in self.done_evidence if e.name not in have]
        wanted = "all of" if need_all else "one of"
        reason = (
            f"Task {task.id} ('{task.title}') was moved to done without evidence that it "
            f"works: missing {'; '.join(e.describe() for e in missing)}. Done needs "
            f"{wanted}: {', '.join(e.name for e in self.done_evidence)}."
        )
        params = {
            "pattern": "done-without-evidence",
            "missing": [e.name for e in missing],
            "required": self.done_evidence_require,
        }
        key = f"evidence:{task.id}:{task.status_since.isoformat()}"
        if self.done_evidence_action == "alert":
            yield Action(
                ActionKind.ALERT,
                NAME,
                reason,
                task_id=task.id,
                severity=Severity.WARNING,
                key=key,
                once=True,  # about one completion: not a lasting state that "resolves"
                params=params,
            )
            return
        yield Action(
            ActionKind.REQUEUE,
            NAME,
            f"{reason} It goes back to the queue until it has it.",
            task_id=task.id,
            severity=Severity.WARNING,
            key=key,
            once=True,
            params=params,
        )

    def _landing(self, task: Task, snapshot: Snapshot) -> tuple[str, bool, datetime] | None:
        """How the task's work is landing or landed, whether that happens without a person
        (a merge by itself, or a merge done), and when; None when it is not yet. A parent is
        not judged (its evidence is its subtasks'). A merged PR is judged whoever closed the
        task; without one, a task a person moved to done is not."""
        if task.subtasks:
            return None
        labels = {label.casefold() for label in self.acceptance_merge_labels}
        mine = [pr for pr in snapshot.pull_requests if task.id in pr.task_ids and not pr.draft]
        for pr in mine:
            # Set to merge by itself: it merges the moment checks pass, so say it now.
            if labels.intersection(x.casefold() for x in pr.labels):
                return f"its PR {pr.ref} is set to merge by itself", True, snapshot.now
        within = self.done_evidence_within
        for pr in snapshot.closed_pull_requests:
            merged = pr.merged_at
            if task.id in pr.task_ids and merged is not None and snapshot.now - merged <= within:
                # Whoever closed the task (trackers do it on merge), the merge landed it.
                return f"its PR {pr.ref} was merged", True, merged
        if task.status.is_terminal and not self._agent_completed(task, snapshot):
            return None  # a person closed it: their call
        handed_on = task.status in (Status.REVIEW, Status.ACCEPTED)
        for pr in mine:
            if pr.mergeable is True and handed_on:
                return f"its PR {pr.ref} can be merged now", False, snapshot.now
        age = snapshot.now - task.status_since
        if task.status is Status.ACCEPTED and age <= within:
            return "it was accepted", False, task.status_since
        if task.status is Status.DONE and self.session_exit_grace <= age <= within:
            return "it was moved to done", False, task.status_since
        return None

    def _check_acceptance(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        """Each criterion a person stated is announced on the task once, with the reference
        evidence must name; one with no evidence when the work lands is alerted."""
        landing = self._landing(task, snapshot)
        if landing is None:
            # Not landing yet: announce young criteria of a task being worked on, once. Not
            # while landing: the alert names the reference, and comments sent first would
            # use up the task's action budget before it.
            if task.status in _ANNOUNCED_IN:
                yield from (
                    self._announce(task, c)
                    for c in unanswered(task, snapshot, self.acceptance_patterns)
                    # From when it was stated, or when work on the task began, if later.
                    if snapshot.now - max(c.at, task.status_since) <= ANNOUNCE_WITHIN
                )
            return
        left = unanswered(task, snapshot, self.acceptance_patterns)
        how, merges, at = landing
        for c in left:
            if c.at > at:
                continue  # asked after it landed: for the next change, not this one
            notify = [c.asked_by, *self.acceptance_notify_roles]
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"Task {task.id} ('{task.title}'): {how}, but nothing shows the acceptance "
                f'criterion {c.asked_by} stated was met: "{c.text}" ({c.ref}). Tell '
                f'{", ".join(notify)}: evidence is a comment that starts with "{c.ref}: met" '
                f"and gives the result (or a CI job named {c.ref} that passed)"
                f"{' before this lands' if at == snapshot.now else ''}.",
                task_id=task.id,
                severity=Severity.CRITICAL if merges else Severity.WARNING,
                # The severity in the key: a step up to critical (a merge label put on
                # later) is told again.
                key=f"acceptance:{task.id}:{c.key}:{'critical' if merges else 'warning'}",
                once=True,  # about one criterion of one task, not a lasting state
                params={
                    "pattern": "acceptance-criterion-missing",
                    "criterion": c.text,
                    "ref": c.ref,
                    "asked_by": c.asked_by,
                    "asked_at": c.at.isoformat(),
                    "notify": notify,
                    "landing": how,
                },
            )

    @staticmethod
    def _announce(task: Task, c: Criterion) -> Action:
        """Tell whoever works on the task how to report the criterion, once."""
        return Action(
            ActionKind.COMMENT,
            NAME,
            f'{ANNOUNCEMENT}{c.ref[3:]} from {c.asked_by}: "{c.text}". Before this lands, '
            f'report it in a comment of its own that starts with "{c.ref}: met", then the '
            f'result with its numbers and a link, for example "{c.ref}: met, 20/20 passed, '
            f'<link to the log or CI run>". Nothing else counts as its evidence, and a '
            f"failure, a retry or a plan in that comment means not met.",
            task_id=task.id,
            key=f"acceptance-ask:{task.id}:{c.key}",
            once=True,
            params={"pattern": "acceptance-criterion-stated", "ref": c.ref, "criterion": c.text},
        )

    def _agent_completed(self, task: Task, snapshot: Snapshot) -> bool:
        """An agent's session ended declaring done when the task became done (within
        ``session_exit_grace``)."""
        grace = self.session_exit_grace
        return any(
            s.final_status is Status.DONE
            and s.ended_at is not None
            and abs(s.ended_at - task.status_since) <= grace
            for s in snapshot.sessions_for(task.id)
        )

    def _past_grace(self, end: Exit, snapshot: Snapshot) -> bool:
        return snapshot.now - end.at >= self.session_exit_grace

    def _check_block(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        end = last_exit(task, snapshot, self.session_stale_after)
        if end is None or task.status_since > end.at or not self._past_grace(end, snapshot):
            return
        last = end.session
        # A status stamped at the same moment as the exit was set by the session itself.
        if task.status_since < last.started_at and last.final_status is not Status.BLOCKED:
            return  # the block predates the session: not this session's doing
        # Tied to a condition (via the block or the task) means something will clear it.
        conditions, missing = linked_conditions(task, snapshot)
        block = task.block
        if conditions or missing or (block is not None and self._allowed(block.reason)):
            return
        if block is not None and block.kelpie is KelpieMark.STOP:
            return  # Kelpie stopped this run: requeueing it would undo the stop
        yield self._requeue_block(task, last)

    def _check_unapplied(self, task: Task, snapshot: Snapshot) -> Iterable[Action]:
        """The session declared an end, but the platform never applied it."""
        if task.status is not Status.IN_PROGRESS:
            return
        end = last_exit(task, snapshot, self.session_stale_after)
        if end is None or task.status_since > end.at or not self._past_grace(end, snapshot):
            return
        declared = end.session.final_status
        if declared is None or declared is task.status:
            return
        yield Action(
            ActionKind.ALERT,
            NAME,
            f"Session {end.session.id} ended declaring '{declared}', but the task is still "
            f"'{task.status}' {_hours(snapshot.now - end.at)} later: the platform did not "
            "apply the declared status.",
            task_id=task.id,
            severity=Severity.WARNING,
            key=f"unapplied:{end.session.id}",
            state=f"{NAME}:unapplied:{end.session.id}",
            since=end.at,
            params={"session_id": end.session.id, "declared": str(declared)},
        )

    def _requeue_exit(self, task: Task, end: Exit) -> Action:
        s = end.session
        when = f"{end.at.astimezone(UTC):%Y-%m-%d %H:%M} UTC"
        how = (
            f"stopped sending heartbeats at {when}"
            if end.silent
            else f"ended at {when} ({s.exit_reason or 'no exit reason'})"
        )
        params: dict[str, Any] = {"session_id": s.id, "owner_id": s.owner_id}
        note = ""
        if s.dirty_worktree:
            params["dirty_worktree"] = True
            note = " The worktree has uncommitted changes: commit them as WIP before reassigning."
        return Action(
            ActionKind.REQUEUE,
            NAME,
            f"Session {s.id} of {s.owner_id} {how} without a final status, leaving the task "
            f"'{task.status}' with nobody on it; it belongs back in the queue.{note}",
            task_id=task.id,
            severity=Severity.WARNING,
            key=f"exit:{s.id}",
            params=params,
        )

    def _requeue_block(self, task: Task, session: Session) -> Action:
        block = task.block
        got = f"'{block.reason}'" if block is not None and block.reason else "no reason"
        return Action(
            ActionKind.REQUEUE,
            NAME,
            f"Session {session.id} of {session.owner_id} left the task blocked with {got}, "
            f"which is not an allowed block reason ({', '.join(self.allowed_block_reasons)}) "
            "and has no registered condition. Nothing would ever unblock it; it belongs back in "
            "the queue.",
            task_id=task.id,
            severity=Severity.WARNING,
            key=f"block:{task.id}:{task.status_since.isoformat()}",
            params={
                "session_id": session.id,
                "owner_id": session.owner_id,
                "block_reason": block.reason if block is not None else None,
            },
        )

    def _allowed(self, reason: str) -> bool:
        head = reason.strip().lower()
        return any(
            head == r or head.startswith(f"{r}:") or head.startswith(f"{r} ")
            for r in (a.strip().lower() for a in self.allowed_block_reasons)
        )


def _hours(d: timedelta) -> str:
    return f"{d.total_seconds() / 3600:.1f}h"


def _duration(value: Any, name: str) -> timedelta:
    try:
        return parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc


def _evidence(raw: Any) -> Evidence:
    if not isinstance(raw, Mapping):
        raise ValueError("done_evidence entries must be tables")
    raw = dict(raw)
    name = raw.pop("name", None)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("done_evidence: name must be a non-empty string")
    artifacts = raw.pop("artifacts", None)
    condition = raw.pop("condition", None)
    if raw:
        raise ValueError(f"done_evidence.{name}: unknown keys {sorted(raw)}")
    if (artifacts is None) == (condition is None):
        raise ValueError(f"done_evidence.{name}: set exactly one of artifacts or condition")
    if artifacts is not None:
        if (
            not isinstance(artifacts, list | tuple)
            or not artifacts
            or not all(isinstance(g, str) and g for g in artifacts)
        ):
            raise ValueError(f"done_evidence.{name}: artifacts must be a list of globs")
        return Evidence(name, artifacts=tuple(artifacts))
    try:
        kind = ConditionKind(condition)
    except ValueError:
        raise ValueError(
            f"done_evidence.{name}: condition must be one of {[k.value for k in ConditionKind]}"
        ) from None
    return Evidence(name, condition=kind)


@register(NAME)
def build(config: dict[str, Any]) -> ExitContract:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    if "allowed_block_reasons" in config:
        reasons = config.pop("allowed_block_reasons")
        if not isinstance(reasons, list | tuple) or not all(
            isinstance(r, str) and r.strip() for r in reasons
        ):
            raise ValueError("allowed_block_reasons must be a list of non-empty strings")
        kwargs["allowed_block_reasons"] = tuple(reasons)
    for key in ("session_exit_grace", "session_stale_after", "done_evidence_within"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    within = kwargs.get("done_evidence_within")
    if within is not None and not timedelta(0) < within <= timedelta(days=7):
        raise ValueError("done_evidence_within must be positive and at most 7d (once_window)")
    if "done_evidence" in config:
        raw = config.pop("done_evidence")
        if not isinstance(raw, list | tuple):
            raise ValueError("done_evidence must be a list of tables ([[...done_evidence]])")
        found = tuple(_evidence(e) for e in raw)
        if len({e.name for e in found}) != len(found):
            raise ValueError("done_evidence: duplicate name")
        kwargs["done_evidence"] = found
    for key, allowed in (
        ("done_evidence_require", ("any", "all")),
        ("done_evidence_action", ("requeue", "alert")),
    ):
        if key in config:
            value = config.pop(key)
            if value not in allowed:
                raise ValueError(f"{key} must be one of {list(allowed)}")
            kwargs[key] = value
    if "acceptance_patterns" in config:
        patterns = config.pop("acceptance_patterns")
        if not isinstance(patterns, list) or not all(
            isinstance(p, str) and p.strip() for p in patterns
        ):
            raise ValueError("acceptance_patterns must be a list of regular expressions")
        try:
            compiled = tuple(re.compile(p, re.IGNORECASE) for p in patterns)
        except re.error as exc:
            raise ValueError(f"acceptance_patterns: invalid pattern: {exc}") from exc
        if any(p.search("") for p in compiled):
            raise ValueError("acceptance_patterns: a pattern matches an empty line (every line)")
        kwargs["acceptance_patterns"] = compiled
    for key in ("acceptance_merge_labels", "acceptance_notify_roles"):
        if key in config:
            values = config.pop(key)
            if not isinstance(values, list) or not all(
                isinstance(v, str) and v.strip() for v in values
            ):
                raise ValueError(f"{key} must be a list of names")
            kwargs[key] = tuple(v.strip() for v in values)
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return ExitContract(**kwargs)
