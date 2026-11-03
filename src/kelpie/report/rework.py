"""Tokens per return (#197 item 7): what the farm spent redoing work that came back.

A task sent back (``Task.returns``: from review, a merge, a failed check) is reworked from
then on. Its rework is every counted model call made for it from its first return within
the day to the day's end, by the role that made the call (its session's, else the task's;
:mod:`kelpie.report.attribution`: a call names its task itself, else through its session,
on Hermes the profile's ``state.db``).
The reviewer's next look counts too: it is part of what the return cost. A lower bound,
like the rework rate it sits beside: a return before the day is not counted, and calls
that name no task cannot be told apart. Pure and deterministic.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from kelpie.core.model import Span, Task
from kelpie.core.spans import SpanKey, TokenUsage, last_at, span_key, usage
from kelpie.report.attribution import CallOwners


@dataclass(frozen=True, slots=True)
class ReworkTask:
    """One task returned within the day: how often, from when, and its calls since then
    per role (token usage, priced by the caller)."""

    task_id: str
    returns: int
    since: datetime  # its first return of the day
    per_role: Mapping[str, tuple[TokenUsage, ...]] = field(default_factory=dict)

    def rows(self) -> tuple[TokenUsage, ...]:
        return tuple(u for rows in self.per_role.values() for u in rows)


def rework_tokens(
    tasks: Iterable[Task],
    spans: Sequence[Span],
    counted: Mapping[SpanKey, str | None],
    owners: CallOwners,
    start: datetime,
    end: datetime,
) -> tuple[ReworkTask, ...]:
    """Each task with a return in ``[start, end)`` and its rework calls, by task id; a
    task returned but with no call after it is listed with none."""
    first: dict[str, tuple[datetime, int]] = {}
    for task in tasks:
        back = sorted(r.at for r in task.returns if start <= r.at < end)
        if back:
            first[task.id] = (back[0], len(back))
    if not first:
        return ()
    calls: dict[str, dict[str, list[Span]]] = defaultdict(lambda: defaultdict(list))
    for span in spans:
        if span_key(span) not in counted:
            continue
        task = owners.span_task(span)
        if task is None or task not in first:
            continue
        at = last_at(span)
        if first[task][0] <= at < end:
            calls[task][owners.doer_of(task, span.session_id)].append(span)
    out = []
    for task_id in sorted(first):
        since, n = first[task_id]
        per_role = {
            role: usage(found, {span_key(s): counted[span_key(s)] for s in found})
            for role, found in sorted(calls.get(task_id, {}).items())
        }
        out.append(ReworkTask(task_id, n, since, per_role))
    return tuple(out)
