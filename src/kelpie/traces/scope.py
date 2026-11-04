"""Per-agent ingest tokens: who a ``[[traces.tokens]]`` token may report spans for (#302).

A span names its session and task itself, so a token every agent holds lets any agent
report for any other (audit M1: forged identical calls made rule 10 stop another agent's
run). A scoped token may report only for the owners, roles, sessions and boards its entry
lists. Who a session or task belongs to comes from the board (and, for sessions the board
no longer shows, from the owners the span store remembers for budgets), never from the
span:

- a span whose session the board places is in scope when the session's owner is one of
  ``owners``, has one of ``roles``, its task is on one of ``boards``, or its id matches one
  of the ``sessions`` patterns (``fnmatch``);
- a span that names a task the board places must be in scope for that task too, unless it
  is the task the board gives the span's (in scope) session; a task that contradicts the
  board's task of the session is out of scope;
- a span the board cannot place yet (a session too new to be on the board, or a span that
  names no session or task and may inherit them from its parent later) is accepted and
  kept *pending*: every tick reads it again from the store and checks it against the
  board, and deletes it once it falls outside the scope (:class:`ScopeGuard`). A session
  the board does not know is bound to the first token that reports it: another token's
  spans for it are out of scope. When the token lists ``sessions`` patterns and nothing
  else, an unplaced session must match one.

Spans out of scope are rejected at ingest, or deleted from the store by the guard before
rules, budgets and totals see them; both are counted as ``out_of_scope``. The shared
``[traces] token_env`` token (and an ingest with no token) stays unscoped: it may report
for every session, as before.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from kelpie.core.model import Snapshot, Span
from kelpie.core.spans import Attribution, SpanKey, SpanStore, span_key

log = logging.getLogger(__name__)

# Set by ingest on every span a scoped token sent: the token's name. Any value a sender put
# there is replaced (scoped token) or removed (shared token), so no span can claim a scope.
TOKEN_ATTRIBUTE = "kelpie.ingest.token"
OUT_OF_SCOPE = "out_of_scope"  # spans_rejected reason
SCOPE_BACKLOG = "scope_backlog"  # spans_rejected reason: too many spans wait to be placed
INDEX_KEEP = timedelta(hours=24)  # a session or task the board stopped showing is kept this long
PENDING_KEEP = timedelta(hours=24)  # an unplaced span is checked this long, then left as it is
MAX_INDEX = 200_000  # entries per map; the oldest go first
MAX_PENDING = 200_000  # unplaced spans checked every tick; more are refused at ingest
MAX_BINDINGS = 200_000
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

    @property
    def sessions_only(self) -> bool:
        return bool(self.sessions) and not (self.owners or self.roles or self.boards)

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


@dataclass(frozen=True, slots=True)
class _Seen:
    place: Place
    task_id: str | None  # a session's task
    at: datetime


class ScopeIndex:
    """Session id and task id -> :class:`Place`, from the snapshots the control loop builds
    (read by ingest on the HTTP threads). Entries the board no longer shows are kept for
    :data:`INDEX_KEEP`; after that a session is still placed by the owner the span store
    remembers for it (``remembered``, kept for the span retention when budgets are on).
    Hermes tasks are also found by their id on their board (``t_1a`` for ``qr:t_1a``),
    as OTLP from Hermes names them, when exactly one board has that id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, _Seen] = {}
        self._tasks: dict[str, _Seen] = {}
        self._local: dict[str, str | None] = {}  # local id -> task id; None: ambiguous
        self._roles: dict[str, str] = {}
        self._remembered = Attribution()
        self.ready = False  # a snapshot was read

    def update(self, snapshot: Snapshot, remembered: Attribution | None = None) -> None:
        now = snapshot.now
        roles = {o.id: o.role for o in snapshot.owners}
        tasks: dict[str, _Seen] = {}
        local: dict[str, str | None] = {}
        for t in snapshot.tasks:
            role = (roles.get(t.owner_id) if t.owner_id else None) or t.role
            tasks[t.id] = _Seen(Place(t.owner_id, role, t.board), t.id, now)
            if t.board and (lid := t.local_id) is not None:
                key = f"{t.id[: len(t.id) - len(lid) - len(t.board) - 1]}{lid}"
                local[key] = t.id if key not in local else None
        sessions: dict[str, _Seen] = {}
        for s in snapshot.sessions:
            task = tasks.get(s.task_id)
            role = roles.get(s.owner_id) or (task.place.role if task is not None else None)
            board = task.place.board if task is not None else None
            sessions[s.id] = _Seen(Place(s.owner_id, role, board), s.task_id, now)
        with self._lock:
            self._roles.update(roles)
            if remembered is not None:
                self._remembered = remembered
            for table, fresh in ((self._sessions, sessions), (self._tasks, tasks)):
                for key, seen in fresh.items():
                    table.pop(key, None)  # re-inserted last: the newest at the end
                    table[key] = seen
                for key in [k for k, v in table.items() if now - v.at > INDEX_KEEP]:
                    del table[key]
                while len(table) > MAX_INDEX:
                    del table[next(iter(table))]
            self._local = {k: v for k, v in local.items() if k not in tasks}
            self.ready = True

    def session(self, session_id: str) -> tuple[Place, str | None] | None:
        """Where the board puts the session, and its task; None when it does not."""
        with self._lock:
            seen = self._sessions.get(session_id)
            if seen is not None:
                return seen.place, seen.task_id
            owner = self._remembered.owners.get(session_id)
            if owner is None:
                return None
            task_id = self._remembered.session_tasks.get(session_id)
            task = self._tasks.get(task_id) if task_id is not None else None
            board = task.place.board if task is not None else None
            role = self._roles.get(owner) or (task.place.role if task is not None else None)
            return Place(owner, role, board), task_id

    def task(self, task_id: str) -> tuple[Place, str] | None:
        """Where the board puts the task (by its id, or its id on its board), and its id."""
        with self._lock:
            seen = self._tasks.get(task_id)
            if seen is None and (full := self._local.get(task_id)) is not None:
                seen = self._tasks.get(full)
            return (seen.place, seen.task_id or task_id) if seen is not None else None


def verdict(
    scope: TokenScope,
    span: Span,
    index: ScopeIndex,
    bound: Callable[[str], str | None] = lambda _: None,
) -> Verdict:
    """Whether ``scope`` may report ``span``. ``bound`` gives the token an unplaced session
    is bound to (the first that reported it)."""
    session = index.session(span.session_id) if span.session_id is not None else None
    task = index.task(span.task_id) if span.task_id is not None else None
    session_ok = False
    if session is not None:
        place, session_task = session
        session_ok = scope.covers(place) or scope.session_matches(span.session_id)
        if not session_ok:
            return Verdict.OUT
        named = task[1] if task is not None else span.task_id
        if named is not None and session_task is not None and named != session_task:
            return Verdict.OUT  # a task the board does not give this session
    if task is not None:
        own = session_ok and session is not None and session[1] == task[1]
        if not (own or scope.covers(task[0])):
            return Verdict.OUT
    if session is not None or task is not None:
        return Verdict.IN
    if span.session_id is not None:
        owner = bound(span.session_id)
        if owner is not None and owner != scope.name:
            return Verdict.OUT
        if scope.sessions_only and not scope.session_matches(span.session_id):
            return Verdict.OUT
    return Verdict.UNKNOWN


def stamp(span: Span, token: str | None) -> Span:
    """``span`` with :data:`TOKEN_ATTRIBUTE` set to ``token`` (a scoped token's name), or
    removed (``None``: the shared token)."""
    if token is None and TOKEN_ATTRIBUTE not in span.attributes:
        return span
    attributes = {k: v for k, v in span.attributes.items() if k != TOKEN_ATTRIBUTE}
    if token is not None:
        attributes[TOKEN_ATTRIBUTE] = token
    return replace(span, attributes=attributes)


def _short(text: str | None) -> str:
    return (text or "")[:80]


@dataclass(slots=True)
class ScopeGuard:
    """The scopes of ``[[traces.tokens]]`` and what is needed to enforce them: the
    :class:`ScopeIndex`, the sessions bound to a token, and the spans waiting to be placed.

    Ingest calls :meth:`ingest_filter` per request; the control loop calls :meth:`check`
    on every merged snapshot, after hook spans were folded in. ``check`` refreshes the index,
    reads every pending span again from ``store`` (its session and task may have been filled
    in from its parent since) and checks it, checks the snapshot's stamped spans too (a
    folded span takes the hook's session), deletes the spans now out of scope from the store,
    counts them through ``rejected`` and calls ``on_discard`` (cached totals and usage are
    stale then). A span of a token no longer configured counts as out of scope."""

    scopes: Mapping[str, TokenScope]
    store: SpanStore | None = None
    rejected: Callable[[str, int], None] | None = None
    on_discard: Callable[[], None] | None = None
    index: ScopeIndex = field(default_factory=ScopeIndex)
    _pending: dict[SpanKey, tuple[str, datetime]] = field(default_factory=dict)
    _bound: dict[str, str] = field(default_factory=dict)
    _logged: dict[str, datetime] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def bound(self, session_id: str) -> str | None:
        with self._lock:
            return self._bound.get(session_id)

    def _bind(self, session_id: str, token: str) -> None:
        with self._lock:
            if session_id not in self._bound:
                self._bound[session_id] = token
                while len(self._bound) > MAX_BINDINGS:
                    del self._bound[next(iter(self._bound))]

    def _due(self, token: str, now: datetime) -> bool:
        with self._lock:
            seen = self._logged.get(token)
            if seen is not None and timedelta(0) <= now - seen < LOG_EVERY:
                return False
            self._logged[token] = now
            return True

    def verdict(self, token: str, span: Span) -> Verdict:
        scope = self.scopes.get(token)
        if scope is None:
            return Verdict.OUT
        return verdict(scope, span, self.index, self.bound)

    # --- ingest ---------------------------------------------------------------------------

    def ingest_filter(
        self, token: str, spans: Sequence[Span], now: datetime
    ) -> tuple[list[Span], Counter[str]]:
        """The spans token ``token`` may report (stamped with its name), and why the others
        were rejected. Unplaced spans are accepted, bound and kept pending, while there is
        room."""
        kept: list[Span] = []
        rejected: Counter[str] = Counter()
        for span in spans:
            found = self.verdict(token, span)
            if found is Verdict.OUT:
                rejected[OUT_OF_SCOPE] += 1
                if self._due(token, now):
                    scope = self.scopes.get(token)
                    log.warning(
                        "traces token %r sent a span for session %r, task %r outside its "
                        "scope (%s): rejected",
                        token,
                        _short(span.session_id),
                        _short(span.task_id),
                        scope.describe() if scope is not None else "not configured",
                    )
                continue
            if found is Verdict.UNKNOWN:
                with self._lock:
                    full = len(self._pending) >= MAX_PENDING
                    if not full:
                        self._pending[span_key(span)] = (token, now)
                if full:
                    rejected[SCOPE_BACKLOG] += 1
                    continue
                if span.session_id is not None:
                    self._bind(span.session_id, token)
            kept.append(stamp(span, token))
        return kept, rejected

    def restore(self, after: datetime, now: datetime) -> int:
        """After a restart: the stored spans of scoped tokens (last time at or after
        ``after``) are pending again, and their sessions bound to their tokens, so a span
        accepted before the restart is still checked against the board. Returns how many."""
        if self.store is None:
            return 0
        found = self.store.spans_with_attribute(after, TOKEN_ATTRIBUTE)
        with self._lock:
            for span in sorted(found, key=lambda s: s.start):
                token = span.attributes.get(TOKEN_ATTRIBUTE)
                if not isinstance(token, str):
                    continue
                if len(self._pending) < MAX_PENDING:
                    self._pending[span_key(span)] = (token, now)
                if span.session_id is not None:
                    self._bound.setdefault(span.session_id, token)
        return len(found)

    # --- every tick -----------------------------------------------------------------------

    def check(self, snapshot: Snapshot, remembered: Attribution | None = None) -> Snapshot:
        """The snapshot without spans out of their token's scope; those are deleted."""
        self.index.update(snapshot, remembered)
        now = snapshot.now
        out: set[SpanKey] = set()
        with self._lock:
            pending = dict(self._pending)
        if pending and self.store is not None:
            try:
                stored = {span_key(s): s for s in self.store.spans_by_key(list(pending))}
            except Exception as exc:  # checked again next tick
                log.error("cannot read spans waiting for their scope check: %s", exc)
                stored = None
            if stored is not None:
                settled: list[SpanKey] = []
                for key, (token, since) in pending.items():
                    span = stored.get(key)
                    if span is None:  # pruned or replaced
                        settled.append(key)
                        continue
                    found = self.verdict(token, span)
                    if found is Verdict.OUT:
                        out.add(key)
                        self._log_drop(token, span, now)
                    if found is not Verdict.UNKNOWN or now - since > PENDING_KEEP:
                        settled.append(key)
                with self._lock:
                    for key in settled:
                        self._pending.pop(key, None)
        for span in snapshot.spans:
            token = span.attributes.get(TOKEN_ATTRIBUTE)
            if isinstance(token, str) and self.verdict(token, span) is Verdict.OUT:
                out.add(span_key(span))
                self._log_drop(token, span, now)
        if not out:
            return snapshot
        if self.store is not None:
            try:
                self.store.discard(out)
            except Exception as exc:  # dropped from this tick anyway; retried next tick
                log.error("cannot delete %d out-of-scope spans: %s", len(out), exc)
                with self._lock:
                    for key in out:
                        self._pending.setdefault(key, ("", now))
        if self.rejected is not None:
            self.rejected(OUT_OF_SCOPE, len(out))
        if self.on_discard is not None:
            self.on_discard()
        return replace(snapshot, spans=tuple(s for s in snapshot.spans if span_key(s) not in out))

    def _log_drop(self, token: str, span: Span, now: datetime) -> None:
        if self._due(token, now):
            log.warning(
                "deleted a span of traces token %r for session %r, task %r: outside its scope",
                token[:64],
                _short(span.session_id),
                _short(span.task_id),
            )

    def pending(self) -> int:
        with self._lock:
            return len(self._pending)
