"""What a snooze changes for the rules that honour it (issue #176).

A person who knows a task waits on purpose snoozes it until a date (``kelpie snooze``, a
chat reply, or, where agents record people's decisions, an ``until:`` marker in the block
reason; :mod:`kelpie.core.snooze`). Three rules honour a snooze, and only for the alerts
that repeat a human wait:

- rule 13 (``needs_you``): a snoozed item leaves the person's digest (its state key, its
  counts and cost) and is listed in the digest's footer instead, unless its block can never
  clear (a dead end, a condition Kelpie cannot see): that is a fact, said at once;
- rule 4 (``stale_blocks``): no no-condition and no overdue alert on a snoozed block. Its
  dead-end, unseen-condition and stale-evidence alerts still fire: those are facts about
  the block or Kelpie's plumbing, not reminders;
- rule 2 (``wait_condition``): no overdue alert on a waiting task whose only unmet
  conditions are human tasks a snooze holds; a dead end still fires.

Agent-health rules (``loop_breaker``, ``no_progress``, ``exit_contract``,
``orphan_status``, ...) never read snoozes. A marker snooze (``source == "marker"``) holds
no more than a person's, only human waits (rule 4 checks that the block asks a person or
waits on human tasks only), and is always shown as "from marker".

A task alerts again when its snooze **expired** (still in ``Snapshot.snoozes``, which keeps
expired snoozes for :data:`~kelpie.core.snooze.EXPIRED_KEPT`, but no longer active) or when
the blocked work **grew**: more tasks wait behind it now (:func:`behind_count`) than when it
was snoozed (``behind_at_snooze``). ``kelpie serve`` records that baseline with the same
count at the first complete tick after the snooze (``kelpie.app.SnoozeReader``); until then
it is None, and a snooze without a baseline (markers always) holds and never re-alerts on
growth. Growth is latched: the first time ``kelpie serve`` sees more work behind the task
it stores ``grown_at`` and ``grown_by`` with the snooze, and from then on the snooze holds
nothing until it is replaced or expires, whatever the count does next (conditions that
come and go with a dependent's status would otherwise make it flap). Without a store the
current count decides. Pure: reads the snapshot only.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kelpie.core.alerts import SnoozeState
from kelpie.core.model import ConditionKind, Snapshot, Snooze
from kelpie.core.snooze import EXPIRED_KEPT

_TASK_REFS = frozenset({ConditionKind.TASK_STATUS, ConditionKind.HUMAN_TASK})


def behind_counter(snapshot: Snapshot) -> Callable[[str], int]:
    """:func:`behind_count` for many tasks of one snapshot: the graph is built once, on
    first use."""
    waiters: dict[str, set[str]] | None = None

    def build() -> dict[str, set[str]]:
        open_ids = {t.id for t in snapshot.tasks if not t.status.is_terminal}
        found: dict[str, set[str]] = {}
        for d in snapshot.dependencies:
            if d.task_id in open_ids and d.task_id != d.depends_on:
                found.setdefault(d.depends_on, set()).add(d.task_id)
        named = {
            t.block.condition_id: t.id
            for t in snapshot.tasks
            if t.id in open_ids and t.block is not None and t.block.condition_id
        }
        for c in snapshot.conditions:
            ref = c.params.get("task")
            if c.kind not in _TASK_REFS or not isinstance(ref, str) or not ref:
                continue
            for waiter in {c.task_id, named.get(c.id)}:
                if waiter is not None and waiter in open_ids and waiter != ref:
                    found.setdefault(ref, set()).add(waiter)
        return found

    def count(task_id: str) -> int:
        nonlocal waiters
        if waiters is None:
            waiters = build()
        seen: set[str] = set()
        queue = deque([task_id])
        while queue:
            for w in waiters.get(queue.popleft(), ()):
                if w != task_id and w not in seen:
                    seen.add(w)
                    queue.append(w)
        return len(seen)

    return count


def behind_count(snapshot: Snapshot, task_id: str) -> int:
    """How much work a snooze's task holds up, for growth: the open tasks (not in a final
    status) behind it, transitively, through dependencies and the ``human_task`` and
    ``task_status`` conditions that name it, whatever their status or sessions (unlike
    :func:`~kelpie.rules._chains.behind_lookup`, which follows only tasks that wait now). A
    dependent moving between the backlog and work, or its session starting or stopping,
    does not change it; a new dependent or a task finishing does.

    Known case, kept on purpose: a waiter linked only by a condition its block reason
    attaches (``[[block_conditions]]``) counts only while it is blocked, since the condition
    exists only then and nothing else on the board names the task (an unblocked task has no
    block reason left to read). A baseline taken while such a waiter is briefly unblocked is
    one short, and its next block reads as growth: one alert, latched, then nothing more.
    Counting it otherwise would mean remembering past blocks, which a pure count over one
    snapshot cannot do."""
    return behind_counter(snapshot)(task_id)


@dataclass(frozen=True, slots=True)
class SnoozeCheck:
    """A task's snooze as it bears on its alerts now: ``state`` held (active, the work
    behind it has not grown), expired or grown (``grown``: how many more tasks are blocked
    behind it than when it was snoozed)."""

    snooze: Snooze
    state: SnoozeState
    grown: int = 0

    @property
    def holds(self) -> bool:
        """Whether the snooze holds the task's repeat human-wait alerts."""
        return self.state is SnoozeState.HELD

    @property
    def marker(self) -> bool:
        return self.snooze.source == "marker"

    def params(self) -> dict[str, Any]:
        """The ``snooze`` an alert's item carries (``kelpie.core.engine`` reads it)."""
        out: dict[str, Any] = {
            "state": self.state.value,
            "until": self.snooze.until.isoformat(),
            "reason": self.snooze.reason,
            "by": self.snooze.by,
            "source": self.snooze.source,
        }
        if self.state is SnoozeState.GROWN:
            out["grown"] = self.grown
            out["behind_at_snooze"] = self.snooze.behind_at_snooze
        return out

    def text(self) -> str:
        """A short account for an alert's reason (and the ledger)."""
        until = self.snooze.until.isoformat()
        source = " (from marker)" if self.marker else ""
        if self.state is SnoozeState.EXPIRED:
            return f"its snooze{source} expired at {until}"
        if self.state is SnoozeState.GROWN:
            return f"{self.grown} more task(s) blocked since its snooze{source} (until {until})"
        return f"snoozed{source} until {until}"


def check(snapshot: Snapshot, task_id: str, blocking: Callable[[], int]) -> SnoozeCheck | None:
    """The task's snooze as it bears on its alerts, or None (never snoozed, or expired more
    than :data:`~kelpie.core.snooze.EXPIRED_KEPT` ago). ``blocking`` counts the tasks
    behind it now (:func:`behind_count`), called only for an active snooze with a
    baseline: without one (not recorded yet, or a marker) it holds."""
    snooze = snapshot.snoozes.get(task_id)
    if snooze is None:
        return None
    if not snooze.active(snapshot.now):
        if snapshot.now - snooze.until > EXPIRED_KEPT:
            return None
        return SnoozeCheck(snooze, SnoozeState.EXPIRED)
    if snooze.grown_at is not None and snooze.grown_at <= snapshot.now:
        return SnoozeCheck(snooze, SnoozeState.GROWN, snooze.grown_by or 0)  # latched
    if snooze.behind_at_snooze is not None:
        now = blocking()
        if now > snooze.behind_at_snooze:
            return SnoozeCheck(snooze, SnoozeState.GROWN, now - snooze.behind_at_snooze)
    return SnoozeCheck(snooze, SnoozeState.HELD)


def checker(snapshot: Snapshot) -> Callable[[str], SnoozeCheck | None]:
    """:func:`check` for many tasks of one snapshot, growth by :func:`behind_count`."""
    count = behind_counter(snapshot)
    return lambda task_id: check(snapshot, task_id, lambda: count(task_id))


def describe(snooze: Snooze, title: str | None = None) -> str:
    """One snoozed wait for a notice: "'title' (t_1) is snoozed until <ISO> (reason, by
    who)", "from marker" for a marker."""
    name = f"'{title}' ({snooze.task_id})" if title else snooze.task_id
    if snooze.source == "marker":
        extra = ["from marker"]
    else:
        extra = [x for x in (snooze.reason, f"by {snooze.by}" if snooze.by else None) if x]
    tail = f" ({', '.join(extra)})" if extra else ""
    return f"{name} is snoozed until {snooze.until.isoformat()}{tail}"
