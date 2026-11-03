"""Rule 2 — wait for a condition instead of waiting inside a session (incident 10).

An agent that needs something external (CI, a merge, a quota reset, a human) registers a
condition and exits; the task becomes ``waiting``. This rule:

- alerts at once when a condition can never be met (flagged ``params.terminal`` by its
  connector: a PR closed unmerged, a human's task that failed), seen since the wait began;
- wakes a waiting task when every condition of its *current* wait holds, on fresh evidence
  (checked since the wait began and within ``max_condition_age``);
- alerts when a task waits past ``max_wait``, or (after a short grace) waits on nothing;
- spots agents that wait *inside* a live session by polling a known check, and tells them
  to register a condition and exit;
- spots sessions that waited inside themselves (``sleep``, a CI watch, a loop over them, as
  their platform recorded the commands: ``Snapshot.waits``), however the session ended.

Polling detection only looks at calls matching ``poll_patterns`` (condition kind → command
substrings), which is configuration, never code: Kelpie knows no particular CI. Without
patterns it stays silent. Generic loops are rule 10's (``loop_breaker``).

**Waits inside sessions** (issue #197). A session that waited ``session_waits`` times or more
within ``session_wait_lookback``, or at least ``session_wait_time`` in all (the lower bounds
of the waits' buckets), wasted its context on waiting even when it then blocked the task as
it should: on a live farm a reviewer slept hundreds of times polling CI before each block.
The adapter recognises the waits (the Hermes adapter from each profile's ``state.db``) and
keeps only their shape and duration bucket. Only waits of ``MIN_WAIT`` or more, or of unknown
length (a watch, a loop), count: a two-second sleep while a server starts is no polling. A
session of an unfinished task gets one comment on the task, once, under the key of the live
polling check's comment (``poll:<session>``): block on a condition for what it waits for and
exit. After a polling comment it is not sent (the ledger has the key within its once window);
after it, a polling comment waits out only the usual cooldown. Sessions not on an open task
Kelpie knows of (an orchestrator's, a finished task's) are counted per role, in one info
alert for the role's
owner to act on (its instructions are theirs: Kelpie never edits them). The Hermes adapter
reads the last 6 hours: a longer ``session_wait_lookback`` sees no more there.

The can-never-be-met and waits-on-nothing alerts list the tasks stuck behind the waiting one
(``params.stuck_ids``, as rule 3 reads chains), so rule 3 does not alert on the same dead end.

**Snoozed human tasks** (issue #176, :mod:`kelpie.rules._snoozes`). A task waiting past
``max_wait`` only on ``human_task`` conditions, each known unmet (``satisfied`` false, fresh
evidence, no ``params.error``: :func:`_known_unmet`), whose human tasks a snooze holds (a
person deferred them on purpose) gets no overdue alert while the snooze holds: it would repeat a
wait the person knows about. It alerts again once a snooze expires or more work is blocked
behind the human task; a dead end still alerts at once. A snooze on the waiting task itself changes
nothing here. A wait Kelpie cannot check (a condition never checked, stale, or one the connector
reports an error for) is never held: it keeps alerting.

**Waits rule 13 lists are rule 13's** (issue #213, as rule 4's blocks since #169). While
rule 13 reaches people, this rule gets its settings (``needs_you``, set by ``kelpie.app``:
rule 13's config section, else ``false``) and leaves it the overdue alert of every waiting
task :func:`~kelpie.rules.needs_you.inbox_task_ids` lists (its time thresholds ignored, so
``max_wait`` still applies): a task parked on a human task the person can move now, by a
``human_task`` condition (or a dispatch gate's ``task_status`` on it) with no other unmet
condition, every one of them known unmet. The person hears about that wait once, in their
digest, under the human task; under the hand-off rule 13's snooze handling decides (a
snoozed item is in its footer, one whose snooze expired or whose work grew is back in the
digest). A listed wait whose conditions Kelpie cannot check keeps this rule's overdue alert,
worded as a connector problem ("C-1 was not checked on this pass: is the
connector running?", ``params.unchecked_conditions``): the digest cannot say what Kelpie
does not know, and a snooze holds nothing for it. Rule 13 lists no waiting task with a
dead end or a condition Kelpie cannot see, and none that waits on something else too (a PR,
CI), so those alert here as before, and so does a wait on a human task the person cannot
move yet. Every other alert is this rule's whoever lists the task: a dead end, a condition
Kelpie cannot see, no condition registered, a poll inside a session, and the wake. Without
rule 13 reaching people (it is not loaded, or it runs in ``observe`` while this rule does
not), the overdue alert is sent as before. :meth:`WaitCondition.resolved` says so when a
wait moves to the digest while it is open.

Keeping the dispatcher from declaring a waiting task dead is the adapter's job (see
:class:`kelpie.core.ports.PlatformAdapter`); this rule never requeues waiting tasks.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cache, partial
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import (
    Condition,
    ConditionKind,
    Session,
    SessionWait,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.core.serde import parse_duration
from kelpie.rules import needs_you, register
from kelpie.rules._alerts import alert_params, check_connectors, count
from kelpie.rules._calls import (
    CallKey,
    call_key,
    error_streak,
    parse_poll_patterns,
    poll_kind,
    tool_calls,
)
from kelpie.rules._chains import behind_lookup, behind_text
from kelpie.rules._snoozes import checker
from kelpie.rules._tasks import (
    DEFAULT_MAX_CONDITION_AGE,
    DEFAULT_STALE_AFTER,
    dead_end_text,
    dead_ends,
    fresh,
    is_live,
    linked_conditions,
)

NAME = "wait_condition"
MIN_WAIT = timedelta(seconds=30)  # a shorter wait inside a session does not count


@dataclass(frozen=True, slots=True)
class WaitCondition:
    max_wait: timedelta = timedelta(hours=24)
    no_condition_grace: timedelta = timedelta(minutes=10)
    # condition kind -> substrings of a polled command
    poll_patterns: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Defaults are the AgentWatchdog thresholds: 8 repeats in 10 minutes, 5 errors in a row.
    poll_min_repeats: int = 8
    poll_window: timedelta = timedelta(minutes=10)
    poll_error_streak: int = 5
    # Waits inside a session (Snapshot.waits): this many, or this long in all, in the lookback.
    session_waits: int = 3  # 0: off
    session_wait_time: timedelta = timedelta(minutes=10)
    session_wait_lookback: timedelta = timedelta(hours=6)
    max_condition_age: timedelta = DEFAULT_MAX_CONDITION_AGE
    session_stale_after: timedelta = DEFAULT_STALE_AFTER
    # Rule 13 as it reaches people (kelpie.app sets it from needs_you's config while it
    # does), or None: the waits it lists are left to it, with no overdue alert from here.
    inbox: needs_you.NeedsYou | None = None
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        actions: list[Action] = []
        behind = behind_lookup(snapshot, self.session_stale_after)
        listed = self._listed(snapshot)
        for task in snapshot.tasks:
            if task.status is Status.WAITING:
                actions.extend(self._check_waiting(task, snapshot, behind, listed))
        polled: set[str] = set()
        if self.poll_patterns:
            waiting = {t.id for t in snapshot.tasks if t.status is Status.WAITING}
            for session in snapshot.sessions:
                if session.task_id in waiting:
                    continue  # already did the right thing; the wake will follow
                if is_live(session, snapshot.now, self.session_stale_after):
                    found = list(self._check_polling(session, snapshot))
                    if found:
                        polled.add(session.id)
                    actions.extend(found)
        if self.session_waits:
            actions.extend(self._check_session_waits(snapshot, polled))
        return actions

    def _listed(self, snapshot: Snapshot) -> Callable[[], frozenset[str]] | None:
        """The tasks rule 13 lists, worked out when first asked (only an overdue wait needs
        it, so wakes, dead ends and a farm without overdue waits never walk rule 13's graph);
        None without the hand-off."""
        inbox = self.inbox
        if inbox is None:
            return None
        return cache(lambda: needs_you.inbox_task_ids(snapshot, inbox))

    def resolved(
        self, state_id: str, snapshot: Snapshot, last_key: str | None = None
    ) -> str | None:
        """The resolve notice of a wait that moved to rule 13's digest while it was open (this
        rule alerted on it, and would without the hand-off): where it is listed now, and that
        a snooze holds it there, not that the wait is gone. None otherwise: the engine's
        general notice, also when the last alert was about the connector (it recovered)."""
        if self.inbox is None or (last_key or "").startswith("overdue-unchecked:"):
            return None
        task = next(
            (
                t
                for t in snapshot.tasks
                if t.status is Status.WAITING
                and state_id == f"{NAME}:{t.id}:{t.status_since.isoformat()}"
            ),
            None,
        )
        if task is None:
            return None
        behind = behind_lookup(snapshot, self.session_stale_after)
        listed = self._listed(snapshot)
        assert listed is not None
        if any(a.state == state_id for a in self._check_waiting(task, snapshot, behind, listed)):
            return None  # still reported
        if not any(a.state == state_id for a in self._check_waiting(task, snapshot, behind)):
            return None  # it would be gone anyway: the wait's own news, not the hand-off's
        return needs_you.digest_notice(snapshot, self.inbox, task.id, "wait")

    # --- waiting tasks ------------------------------------------------------------------

    def _check_waiting(
        self,
        task: Task,
        snapshot: Snapshot,
        behind: Callable[[str], list[str]],
        listed: Callable[[], frozenset[str]] | None = None,
    ) -> Iterable[Action]:
        waited = snapshot.now - task.status_since
        since = task.status_since.isoformat()
        conditions, missing = linked_conditions(task, snapshot)
        if not conditions:
            if waited < self.no_condition_grace:
                return  # the connector may not have synced the new condition yet
            what = (
                f"points at condition {missing}, which Kelpie cannot see"
                if missing
                else "is waiting without any registered condition"
            )
            stuck = behind(task.id)
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"Task {task.id} {what}, so nothing will ever wake it." + behind_text(stuck),
                task_id=task.id,
                severity=Severity.WARNING,
                key=f"no-condition:{task.id}:{since}",
                state=f"{NAME}:{task.id}:{since}",
                since=task.status_since,
                params={
                    "stuck_ids": stuck,
                    **alert_params(
                        f"Nothing will wake {task.id}: "
                        + (
                            f"its condition {missing} is not visible"
                            if missing
                            else "no condition registered"
                        ),
                        "Fix the condition the task points at, or move the task on"
                        if missing
                        else "Register a condition for the task, or move it on",
                        len(stuck),
                    ),
                },
                about_wait=not missing,  # unexplained, not broken: a CI outage may explain it
            )
            return
        ended = dead_ends(conditions, task.status_since)
        if ended:
            stuck = behind(task.id)
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"Task {task.id} is waiting, but {dead_end_text(ended)}: "
                "nothing will ever wake it." + behind_text(stuck),
                task_id=task.id,
                severity=Severity.WARNING,
                key=f"dead-end:{task.id}:{since}",
                state=f"{NAME}:{task.id}:{since}",
                since=task.status_since,
                params={
                    "ended_conditions": [c.id for c in ended],
                    "stuck_ids": stuck,
                    **alert_params(
                        f"Nothing will wake {task.id}: "
                        + (
                            f"its condition {ended[0].id} can never be met"
                            if len(ended) == 1
                            else f"{len(ended)} of its conditions can never be met"
                        ),
                        "Replace the condition, or move the task on",
                        len(stuck),
                    ),
                },
            )
            return
        if all(
            c.satisfied and fresh(c, task.status_since, snapshot.now, self.max_condition_age)
            for c in conditions
        ):
            done = ", ".join(f"{c.id} ({c.kind})" for c in conditions)
            yield Action(
                ActionKind.WAKE,
                NAME,
                f"Every condition the task was waiting for now holds: {done}.",
                task_id=task.id,
                key=f"wake:{task.id}:{since}",
                params={"conditions": [c.id for c in conditions]},
            )
            return
        if waited >= self.max_wait:
            pending = [c for c in conditions if not c.satisfied]
            known = partial(
                _known_unmet,
                since=task.status_since,
                now=snapshot.now,
                max_age=self.max_condition_age,
            )
            if _snoozed_people(pending, snapshot, known):
                return  # deferred on purpose by the person it waits on: no reminder
            unchecked: list[Condition] = []
            if listed is not None and pending and task.id in listed():
                # In the person's "needs you" digest: no alert of ours says the same again,
                # unless Kelpie cannot tell what the wait is on (the digest cannot say it).
                unchecked = [c for c in pending if not known(c)]
                if not unchecked:
                    return
            yield self._overdue(task, snapshot, behind, pending, unchecked, known)

    def _overdue(
        self,
        task: Task,
        snapshot: Snapshot,
        behind: Callable[[str], list[str]],
        pending: Sequence[Condition],
        unchecked: Sequence[Condition],
        known: Callable[[Condition], bool],
    ) -> Action:
        waited = snapshot.now - task.status_since
        since = task.status_since.isoformat()
        what = ", ".join(f"{c.id} ({c.kind})" for c in pending)
        headline = f"{task.id} has waited {_hours(waited)} for its condition" + (
            "s" if len(pending) > 1 else ""
        )
        reason = (
            f"Task {task.id} has waited {_hours(waited)} (limit {_hours(self.max_wait)}) "
            f"for {what}."
        )
        needed = "Check why the condition has not cleared"
        params: dict[str, Any] = {
            "pending_conditions": [c.id for c in pending],
            **_snooze_target(pending, known),
        }
        if unchecked:
            # A wait the person's digest lists, but Kelpie cannot check: say what is wrong
            # with the checking, which only an operator can fix.
            cause = "; ".join(
                _unchecked_text(c, task.status_since, snapshot.now) for c in unchecked
            )
            reason = (
                f"{reason[:-1]}, and Kelpie cannot tell whether it still waits on a person: "
                f"{cause}. Is the connector running?"
            )
            more = f" and {len(unchecked) - 1} more" if len(unchecked) > 1 else ""
            first = _unchecked_text(unchecked[0], task.status_since, snapshot.now)
            headline = f"{task.id} has waited {_hours(waited)}: {first}{more}"
            needed = check_connectors(sorted({c.kind for c in unchecked}))
            params["unchecked_conditions"] = [c.id for c in unchecked]
        return Action(
            ActionKind.ALERT,
            NAME,
            reason,
            task_id=task.id,
            severity=Severity.WARNING,
            # its own key: the connector wording is news, announced as a change of the state
            key=f"{'overdue-unchecked' if unchecked else 'overdue'}:{task.id}:{since}",
            state=f"{NAME}:{task.id}:{since}",
            since=task.status_since,
            params={**params, **alert_params(headline, needed, len(behind(task.id)))},
            about_wait=True,  # a CI outage that explains the wait folds it
        )

    # --- polling inside a session -------------------------------------------------------

    def _check_polling(self, session: Session, snapshot: Snapshot) -> Iterable[Action]:
        spans = snapshot.spans_for_session(session.id)
        calls = tool_calls(spans, after=snapshot.now - self.poll_window)
        found = (
            self._repeated_check(calls)
            or self._failing_check(calls)
            or self._blocking_check(spans, snapshot)
        )
        if found is None:
            return
        what, kind = found
        yield Action(
            ActionKind.COMMENT,
            NAME,
            f"This session {what}, which looks like waiting inside the session. Please "
            f"register a `{kind}` condition and exit: the task will show as waiting rather "
            "than dead, and Kelpie will wake it when the condition holds.",
            task_id=session.task_id,
            key=f"poll:{session.id}",
            params={"session_id": session.id, "condition_kind": kind},
        )

    def _repeated_check(self, calls: Sequence[Span]) -> tuple[str, str] | None:
        """The same known check called often within the window, other calls in between."""
        groups: dict[CallKey, list[Span]] = defaultdict(list)
        for span in calls:
            key = call_key(span)
            if key is not None and self._kind_for(span):
                groups[key].append(span)
        if not groups:
            return None
        best = max(groups.values(), key=len)
        if len(best) < self.poll_min_repeats:
            return None
        kind = self._kind_for(best[-1])
        assert kind is not None
        return f"ran the same check {len(best)} times within {_minutes(self.poll_window)}", kind

    def _failing_check(self, calls: Sequence[Span]) -> tuple[str, str] | None:
        """The same known check failing over and over (e.g. "still pending" exit codes)."""
        errors = error_streak(calls)
        if len(errors) < self.poll_error_streak:
            return None
        keys = {call_key(s) for s in errors}
        kind = self._kind_for(errors[-1])
        if len(keys) != 1 or None in keys or kind is None:
            return None
        return f"failed the same check {len(errors)} times in a row", kind

    def _blocking_check(self, spans: Sequence[Span], snapshot: Snapshot) -> tuple[str, str] | None:
        """One known check that has been blocking (still running) for a whole window."""
        for span in spans:
            if span.kind is not SpanKind.TOOL or span.end is not None:
                continue
            running = snapshot.now - span.start
            if running >= self.poll_window and (kind := self._kind_for(span)):
                return f"has been blocked on one check for {_minutes(running)}", kind
        return None

    def _kind_for(self, span: Span) -> str | None:
        return poll_kind(self.poll_patterns, span)

    # --- waits inside sessions (Snapshot.waits) -----------------------------------------

    def _check_session_waits(self, snapshot: Snapshot, polled: set[str]) -> Iterable[Action]:
        since = snapshot.now - self.session_wait_lookback
        sessions: dict[str, list[SessionWait]] = defaultdict(list)
        for w in snapshot.waits:
            if w.waited is not None and w.waited < MIN_WAIT:
                continue
            if since < w.at <= snapshot.now and w.session_id not in polled:
                sessions[w.session_id].append(w)
        by_role: dict[str, list[list[SessionWait]]] = defaultdict(list)
        for sid, waits in sorted(sessions.items()):
            waits.sort(key=lambda w: w.at)
            if len(waits) < self.session_waits and _waited(waits) < self.session_wait_time:
                continue
            task_id = next((w.task_id for w in reversed(waits) if w.task_id), None)
            task = snapshot.task(task_id) if task_id is not None else None
            if task is None or task.status.is_terminal:
                by_role[waits[-1].role or "unknown"].append(waits)
                continue
            yield Action(
                ActionKind.COMMENT,
                NAME,
                f"This task's session waited inside itself {_waits_text(waits)}. Waiting in a "
                "session re-reads its whole context on every check and holds its slot, even "
                "when the task is blocked afterwards: block the task on a condition for what "
                "it waits for (CI, a merge, a review) and exit; Kelpie wakes it when the "
                "condition holds.",
                task_id=task.id,
                key=f"poll:{sid}",  # the live polling check's: one comment or the other
                once=True,
                params=_waits_params(waits) | {"session_id": sid},
            )
        for role, found in sorted(by_role.items()):
            waits = [w for ws in found for w in ws]
            yield Action(
                ActionKind.ALERT,
                NAME,
                f"{len(found)} {role} session(s) waited inside themselves "
                f"{_waits_text(waits)} in the last {_hours(self.session_wait_lookback)}, "
                "not on an open task Kelpie knows of. Waiting in a session re-reads its context on "
                f"every check: {role} agents should block on a condition and exit instead "
                "(their instructions are yours to change; Kelpie does not edit them).",
                key=f"session-waits:{role}",
                state=f"{NAME}:session-waits:{role}",
                since=min(w.at for w in waits),
                params=_waits_params(waits)
                | {"role": role, "session_ids": sorted({w.session_id for w in waits})}
                | alert_params(f"{role} agents waited inside {count(len(found), 'session')}"),
            )


def _waited(waits: Sequence[SessionWait]) -> timedelta:
    """At least how long ``waits`` waited: the sum of their buckets' lower bounds."""
    return sum((w.waited for w in waits if w.waited is not None), timedelta(0))


def _waits_text(waits: Sequence[SessionWait]) -> str:
    shapes: dict[str, int] = defaultdict(int)
    for w in waits:
        shapes[w.shape] += 1
    parts = ", ".join(f"{shape} {n}" for shape, n in sorted(shapes.items(), key=lambda x: -x[1]))
    total = _waited(waits)
    least = f", at least {_minutes(total)} in all" if total >= timedelta(minutes=1) else ""
    times = "once" if len(waits) == 1 else f"{len(waits)} times"
    return f"{times} ({parts}){least}"


def _waits_params(waits: Sequence[SessionWait]) -> dict[str, Any]:
    shapes: dict[str, int] = defaultdict(int)
    for w in waits:
        shapes[w.shape] += 1
    return {
        "waits": len(waits),
        "waited_s": int(_waited(waits).total_seconds()),
        "shapes": dict(sorted(shapes.items())),
    }


def _known_unmet(
    c: Condition, since: datetime, now: datetime, max_age: timedelta = DEFAULT_MAX_CONDITION_AGE
) -> bool:
    """Whether Kelpie knows the condition does not hold: checked unmet since the wait began
    and recently (:func:`~kelpie.rules._tasks.fresh`, as every rule reads evidence), with no
    error from the connector. An unchecked one, one the connector cannot check, or one whose
    connector stopped checking is not known."""
    return c.satisfied is False and "error" not in c.params and fresh(c, since, now, max_age)


def _unchecked_text(c: Condition, since: datetime, now: datetime) -> str:
    """Why a condition is not known unmet, for the connector problem an alert names."""
    error = c.params.get("error")
    if error:
        return f"the connector reports {error} for {c.id}"
    if c.satisfied is False and c.checked_at is not None and c.checked_at >= since:
        return f"{c.id} was last checked {_hours(now - c.checked_at)} ago"
    return f"{c.id} was not checked on this pass"


def _snoozed_people(
    pending: Sequence[Condition], snapshot: Snapshot, known: Callable[[Condition], bool]
) -> bool:
    """Whether every unmet condition is a human task that a snooze holds."""
    held = checker(snapshot)
    for c in pending:
        ref = c.params.get("task")
        if c.kind is not ConditionKind.HUMAN_TASK or not isinstance(ref, str):
            return False
        if not known(c):
            return False  # unchecked, or Kelpie cannot check it: it alerts
        found = held(ref)
        if found is None or not found.holds:
            return False
    return bool(pending)


def _snooze_target(
    pending: Sequence[Condition], known: Callable[[Condition], bool]
) -> dict[str, Any]:
    """``snooze_task`` for an overdue wait on one person's human task (#176), only where a
    snooze on that task would hold this alert: every pending condition a ``human_task`` one
    on that same task and known unmet (``known``: :func:`_known_unmet`, exactly what
    :func:`_snoozed_people` requires). Nothing for any other wait: a snooze would hold
    nothing there, so a chat offers none."""
    refs: set[object] = set()
    for c in pending:
        ref = c.params.get("task")
        holdable = c.kind is ConditionKind.HUMAN_TASK and isinstance(ref, str) and known(c)
        refs.add(ref if holdable else None)
    if len(refs) != 1:
        return {}
    (ref,) = refs
    return {"snooze_task": ref} if ref else {}


def _hours(d: timedelta) -> str:
    return f"{d.total_seconds() / 3600:.1f}h"


def _minutes(d: timedelta) -> str:
    return f"{d.total_seconds() / 60:.0f} minutes"


def _duration(value: Any, name: str) -> timedelta:
    try:
        result = parse_duration(value)
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if result <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return result


def _positive_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


@register(NAME)
def build(config: dict[str, Any]) -> WaitCondition:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    for key in (
        "max_wait",
        "no_condition_grace",
        "poll_window",
        "max_condition_age",
        "session_stale_after",
    ):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    for key in ("poll_min_repeats", "poll_error_streak"):
        if key in config:
            kwargs[key] = _positive_int(config.pop(key), key)
    if "session_waits" in config:
        value = config.pop("session_waits")
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError("session_waits must be a non-negative integer (0: off)")
        kwargs["session_waits"] = value
    for key in ("session_wait_time", "session_wait_lookback"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    if "poll_patterns" in config:
        kwargs["poll_patterns"] = parse_poll_patterns(config.pop("poll_patterns"))
    if "needs_you" in config:
        kwargs["inbox"] = needs_you.hand_off(config.pop("needs_you"))
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return WaitCondition(**kwargs)
