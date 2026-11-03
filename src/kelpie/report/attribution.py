"""Which task and role a model call belongs to, for the report (#197 items 1, 6 and 7).

A span names its task when its platform sends one; most do not (Hermes spans carry a
session id only). The session's task then comes from, in order: the board's sessions as
reported now, what the span store remembers of finished sessions
(:class:`kelpie.core.spans.Attribution`), and the platform's own usage records
(``Snapshot.usage``, #271: on Hermes each profile's ``state.db``, which names the session's
task and role under both Kelpie's session id and the platform's own).

A call's role is the role of the task the span itself names (the task's own, else its
owner's), else its session owner's, else the role its usage record names, else
:data:`kelpie.core.stages.UNKNOWN_ROLE`: the order the throughput section has always priced
roles by (a session's task is never used for its role: a task in review often carries the
reviewer's role by then), so a line that moves spend away from a role moves it from the
same role. The session's task only finds the call's task, as an outage needs. Pure and
deterministic.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from kelpie.core.model import Owner, Session, SessionUsage, Span, Task
from kelpie.core.spans import Attribution, TokenUsage
from kelpie.core.stages import UNKNOWN_ROLE, task_roles


@dataclass(frozen=True, slots=True)
class CallOwners:
    """Session -> task and role, and task -> role, for attributing model calls."""

    session_tasks: Mapping[str, str]
    session_roles: Mapping[str, str]
    task_roles: Mapping[str, str]

    def task_of(self, task_id: str | None, session_id: str | None) -> str | None:
        """A call's task: its own, else its session's."""
        if task_id:
            return task_id
        return self.session_tasks.get(session_id) if session_id else None

    def role_of(self, task_id: str | None, session_id: str | None) -> str:
        """A call's role: the role of the task the span names (``task_id``: the span's
        own, never its session's), else its session owner's, else unknown."""
        role = self.task_roles.get(task_id) if task_id else None
        if role is None and session_id:
            role = self.session_roles.get(session_id)
        return role or UNKNOWN_ROLE

    def span_task(self, span: Span) -> str | None:
        return self.task_of(span.task_id, span.session_id)

    def row_role(self, row: TokenUsage) -> str:
        return self.role_of(row.task_id, row.session_id)


def call_owners(
    tasks: Sequence[Task],
    owners: Sequence[Owner],
    sessions: Sequence[Session],
    session_roles: Mapping[str, str],
    remembered: Attribution | None = None,
    usage: Sequence[SessionUsage] = (),
) -> CallOwners:
    """``session_roles``: the role of each session's owner
    (:func:`kelpie.traces.outputs.session_context`, as the report's ``roles``)."""
    of_session: dict[str, str] = {}
    roles: dict[str, str] = {}
    # The weakest source first, later ones overwrite it: for the task, usage records, then
    # remembered sessions, then the board's; for the role, usage records, then the session
    # owners' roles.
    for u in usage:
        for sid in (u.platform_session_id, u.session_id):
            if u.task_id:
                of_session[sid] = u.task_id
            if u.role:
                roles[sid] = u.role
    if remembered is not None:
        of_session.update(remembered.session_tasks)
    of_session.update({s.id: s.task_id for s in sessions if s.task_id})
    roles.update(session_roles)
    return CallOwners(of_session, roles, task_roles(tasks, owners))
