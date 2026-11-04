"""Per-agent ingest tokens: who a ``[[traces.tokens]]`` token may report spans for (#302).

A span names its session and task itself, so a token every agent holds lets any agent
report for any other (audit M1: forged identical calls made rule 10 stop another agent's
run). A scoped token may report only for the owners, roles, sessions and boards its entry
lists; a span is in its scope when any of them covers it:

- ``owners``: the span's session (or, without one, its task) belongs to one of these owners;
- ``roles``: that owner has one of these roles;
- ``sessions``: the span's session id matches one of these patterns (``fnmatch``: ``*``,
  ``?``, ``[...]``), whether or not the board shows the session yet;
- ``boards``: the span's task is on one of these boards (Hermes named boards, #198).

Who a session or task belongs to comes from the board, not from the span: the
:class:`ScopeIndex` holds what the last snapshots showed. Ingest rejects a span whose
session or task the index places outside the token's scope (counted as
``out_of_scope``). A span the index cannot place yet (a session too new to be on the board)
is accepted, marked with :data:`TOKEN_ATTRIBUTE`, and checked again on every snapshot until
the board places it: once it falls outside the scope it is dropped from the snapshot and
deleted from the span store (:class:`ScopeGuard`), before any rule or budget sees it.

The shared ``[traces] token_env`` token (and an ingest with no token) stays unscoped: it
may report for every session, as before.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from kelpie.core.model import Snapshot, Span
from kelpie.core.spans import SpanKey, span_key

log = logging.getLogger(__name__)

# Set by ingest on every span a scoped token sent: the token's name. Any value a sender put
# there is replaced (scoped token) or removed (shared token), so no span can claim a scope.
TOKEN_ATTRIBUTE = "kelpie.ingest.token"
OUT_OF_SCOPE = "out_of_scope"  # spans_rejected reason
INDEX_KEEP = timedelta(hours=24)  # a session or task the board stopped showing is kept this long
MAX_INDEX = 200_000  # entries per map; the oldest go first
LOG_EVERY = timedelta(minutes=1)


@dataclass(frozen=True, slots=True)
class Place:
    """Where the board puts a session or task: its owner, the owner's role, its board."""

    owner: str | None = None
    role: str | None = None
    board: str | None = None


@dataclass(frozen=True, slots=True)
class TokenScope:
    """What one ``[[traces.tokens]]`` entry may report for."""

    name: str
    owners: frozenset[str] = frozenset()
    roles: frozenset[str] = frozenset()
    sessions: tuple[str, ...] = ()  # fnmatch patterns on session ids
    boards: frozenset[str] = frozenset()

    def session_matches(self, session_id: str | None) -> bool:
        return session_id is not None and any(
            fnmatch.fnmatchcase(session_id, p) for p in self.sessions
        )

    def covers(self, place: Place) -> bool:
        return (
            (place.owner is not None and place.owner in self.owners)
            or (place.role is not None and place.role in self.roles)
            or (place.board is not None and place.board in self.boards)
        )

    def describe(self) -> str:
        parts = [
            f"{label} {', '.join(sorted(values))}"
            for label, values in (
                ("owners", self.owners),
                ("roles", self.roles),
                ("sessions", self.sessions),
                ("boards", self.boards),
            )
            if values
        ]
        return "; ".join(parts)


class Verdict(StrEnum):
    IN = "in"
    OUT = "out"
    UNKNOWN = "unknown"  # the board does not place the span (yet)


class ScopeIndex:
    """Session id and task id -> :class:`Place`, from the snapshots the control loop builds;
    read by ingest on the HTTP threads. Entries the board no longer shows are kept for
    :data:`INDEX_KEEP` (finished sessions still report their last spans)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, tuple[Place, datetime]] = {}
        self._tasks: dict[str, tuple[Place, datetime]] = {}

    def update(self, snapshot: Snapshot) -> None:
        roles = {o.id: o.role for o in snapshot.owners}
        tasks: dict[str, Place] = {}
        for task in snapshot.tasks:
            owner = task.owner_id
            tasks[task.id] = Place(owner, roles.get(owner) if owner else None, task.board)
        sessions: dict[str, Place] = {}
        for s in snapshot.sessions:
            board = tasks[s.task_id].board if s.task_id in tasks else None
            sessions[s.id] = Place(s.owner_id, roles.get(s.owner_id), board)
        now = snapshot.now
        with self._lock:
            for table, fresh in ((self._sessions, sessions), (self._tasks, tasks)):
                for key, place in fresh.items():
                    table.pop(key, None)  # re-inserted last: the newest at the end
                    table[key] = (place, now)
                stale = [k for k, (_, seen) in table.items() if now - seen > INDEX_KEEP]
                for key in stale:
                    del table[key]
                while len(table) > MAX_INDEX:
                    del table[next(iter(table))]

    def place(self, span: Span) -> Place | None:
        """Where the board puts ``span``: by its session, else by its task; None when it
        names neither or the board shows neither."""
        with self._lock:
            if span.session_id is not None:
                found = self._sessions.get(span.session_id)
                if found is not None:
                    return found[0]
            if span.task_id is not None:
                found = self._tasks.get(span.task_id)
                if found is not None:
                    return found[0]
        return None


def verdict(scope: TokenScope, span: Span, index: ScopeIndex) -> Verdict:
    if scope.session_matches(span.session_id):
        return Verdict.IN
    place = index.place(span)
    if place is None:
        return Verdict.UNKNOWN
    return Verdict.IN if scope.covers(place) else Verdict.OUT


def stamp(span: Span, token: str | None) -> Span:
    """``span`` with :data:`TOKEN_ATTRIBUTE` set to ``token`` (a scoped token's name), or
    removed (``None``: the shared token)."""
    if token is None and TOKEN_ATTRIBUTE not in span.attributes:
        return span
    attributes = {k: v for k, v in span.attributes.items() if k != TOKEN_ATTRIBUTE}
    if token is not None:
        attributes[TOKEN_ATTRIBUTE] = token
    return replace(span, attributes=attributes)


@dataclass(slots=True)
class _Throttle:
    last: dict[str, datetime] = field(default_factory=dict)

    def due(self, key: str, now: datetime) -> bool:
        seen = self.last.get(key)
        if seen is not None and timedelta(0) <= now - seen < LOG_EVERY:
            return False
        self.last[key] = now
        return True


@dataclass(slots=True)
class ScopeGuard:
    """Checks spans of scoped tokens against the board on every snapshot.

    ``check`` refreshes the index from the merged snapshot, then drops the spans a scoped
    token sent that the board now places outside its scope (a span of a token no longer
    configured counts as outside), deletes them from the span store through ``discard``
    and counts them through ``rejected``. Spans of the shared token and hook spans carry no
    token and are left alone."""

    scopes: Mapping[str, TokenScope]
    index: ScopeIndex = field(default_factory=ScopeIndex)
    discard: Callable[[Collection[SpanKey]], int] | None = None
    rejected: Callable[[str, int], None] | None = None
    _throttle: _Throttle = field(default_factory=_Throttle)

    def check(self, snapshot: Snapshot) -> tuple[Snapshot, int]:
        """The snapshot without out-of-scope spans, and how many were dropped."""
        self.index.update(snapshot)
        out = self.outside(snapshot.spans, snapshot.now)
        if not out:
            return snapshot, 0
        keys = {span_key(s) for s in out}
        if self.discard is not None:
            try:
                self.discard(keys)
            except Exception as exc:  # dropped from this tick anyway; retried next tick
                log.error("cannot delete %d out-of-scope spans: %s", len(keys), exc)
        if self.rejected is not None:
            self.rejected(OUT_OF_SCOPE, len(out))
        kept = tuple(s for s in snapshot.spans if span_key(s) not in keys)
        return replace(snapshot, spans=kept), len(out)

    def outside(self, spans: Iterable[Span], now: datetime) -> list[Span]:
        out: list[Span] = []
        for span in spans:
            token = span.attributes.get(TOKEN_ATTRIBUTE)
            if not isinstance(token, str):
                continue
            scope = self.scopes.get(token)
            if scope is None or verdict(scope, span, self.index) is Verdict.OUT:
                out.append(span)
                if self._throttle.due(token, now):
                    log.warning(
                        "dropped a span of traces token %r for session %r: outside its scope",
                        token[:64],
                        (span.session_id or "")[:80],
                    )
        return out

    def ingest_filter(self, token: str, spans: Sequence[Span], now: datetime) -> list[Span]:
        """The spans token ``token`` may report (stamped with its name); logs the first
        rejection per token and minute. The caller counts the rest as rejected."""
        scope = self.scopes[token]
        kept: list[Span] = []
        for span in spans:
            if verdict(scope, span, self.index) is Verdict.OUT:
                if self._throttle.due(token, now):
                    log.warning(
                        "traces token %r sent a span for session %r outside its scope "
                        "(%s): rejected",
                        token,
                        (span.session_id or "")[:80],
                        scope.describe(),
                    )
                continue
            kept.append(stamp(span, token))
        return kept
