"""Per-agent ingest tokens: who a ``[[traces.tokens]]`` token may report spans for (#302).

A span names its session and task itself, so a token every agent holds lets any agent
report for any other (audit M1: forged identical calls made rule 10 stop another agent's
run). A scoped token may report only for the owners, roles, sessions and boards its entry
lists. Who a session or task belongs to comes from the board (and, for sessions the board
no longer shows, from the owners the span store remembers for budgets), never from the
span. Every id a span names is checked on its own:

- **a session the board places** must be in scope: its owner is one of ``owners``, has one
  of ``roles``, its task is on one of ``boards``, or its id matches one of the ``sessions``
  patterns (``fnmatch``). A task the span names must then be the session's own task;
- **a task the board places** (named without a session the board places) must be in scope
  the same way, or be the task of a session of the token's scope;
- **an id the board does not place yet** (a session too new to be on the board, a task not
  on it) is *bound* to the first token that reports it, and only that token may report for
  it. Every tick checks the bound ids against the board: once the board places one inside
  the token's scope the binding settles; once it places one outside, the binding is revoked
  and every stored span of that token naming it is deleted. The check runs alone: an ingest
  request judges and stores its spans with no check in between (:class:`Barrier`), so no
  span judged before a revocation is stored after it. With only ``sessions`` patterns, an
  unplaced session must match one. A binding lives for the span retention after its last
  use, and bindings rebuild from the store after a restart.

A span inherits a missing session or task only from a parent sent with the same scoped
token (:data:`kelpie.core.spans.INGEST_MARK`), so it cannot take another agent's ids through
a parent. Spans out of scope are rejected at ingest, or deleted from the store by the guard
before rules, budgets and totals see them; both are counted as ``out_of_scope``. The shared
``[traces] token_env`` token (and an ingest with no token) stays unscoped: it may report for
every session, as before.
"""

from __future__ import annotations

import fnmatch
import logging
import threading
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from enum import StrEnum

from kelpie.core.model import Snapshot, Span
from kelpie.core.spans import INGEST_MARK, Attribution, SpanKey, SpanStore, span_key

log = logging.getLogger(__name__)

# Set by ingest on every span a scoped token sent: the token's name. Any value a sender put
# there is replaced (scoped token) or removed (shared token), so no span can claim a scope.
TOKEN_ATTRIBUTE = INGEST_MARK
OUT_OF_SCOPE = "out_of_scope"  # spans_rejected reason
SCOPE_BACKLOG = "scope_backlog"  # spans_rejected reason: a token bound too many unplaced ids
INDEX_KEEP = timedelta(hours=24)  # a session or task the board stopped showing is kept this long
UNPLACED_ACTIVE = timedelta(hours=24)  # an unplaced id counts toward the cap this long after use
MAX_BINDINGS = 1_000_000  # all tokens; past it the token holding the most loses its oldest
MAX_INDEX = 200_000  # entries per map; the oldest go first
MAX_UNPLACED = 10_000  # ids one token may hold bound while the board does not place them
LOG_EVERY = timedelta(minutes=1)
MAX_FUTURE = timedelta(hours=1)  # how far ahead ingest takes span times (clock skew)
REMOVED_TOKEN = "<not configured>"  # the log name of every token no longer configured

SESSION, TASK = "session_id", "task_id"  # binding kinds: the span column they name


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


@dataclass(frozen=True, slots=True)
class _Seen:
    place: Place
    task_id: str | None  # a session's task (a task's own id)
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
        self._local: dict[str, str | None] = {}  # id on its board -> task id; None: ambiguous
        self._alias: dict[str, str] = {}  # task id -> its id on its board
        self._task_sessions: dict[str, list[Place]] = {}  # task id -> places of its sessions
        self._roles: dict[str, str] = {}
        self._remembered = Attribution()

    def update(self, snapshot: Snapshot, remembered: Attribution | None = None) -> None:
        now = snapshot.now
        roles = {o.id: o.role for o in snapshot.owners}
        tasks: dict[str, _Seen] = {}
        local: dict[str, str | None] = {}
        alias: dict[str, str] = {}
        for t in snapshot.tasks:
            role = (roles.get(t.owner_id) if t.owner_id else None) or t.role
            tasks[t.id] = _Seen(Place(t.owner_id, role, t.board), t.id, now)
            if t.board and (lid := t.local_id) is not None:
                key = f"{t.id[: len(t.id) - len(lid) - len(t.board) - 1]}{lid}"
                local[key] = t.id if key not in local else None
                alias[t.id] = key
        sessions: dict[str, _Seen] = {}
        of_task: dict[str, list[Place]] = {}
        for s in snapshot.sessions:
            task = tasks.get(s.task_id)
            role = roles.get(s.owner_id) or (task.place.role if task is not None else None)
            board = task.place.board if task is not None else None
            place = Place(s.owner_id, role, board)
            sessions[s.id] = _Seen(place, s.task_id, now)
            of_task.setdefault(s.task_id, []).append(place)
        with self._lock:
            self._roles = roles
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
            self._alias.update(alias)  # kept: a finished session's task by its local id
            while len(self._alias) > MAX_INDEX:
                del self._alias[next(iter(self._alias))]
            self._task_sessions = of_task

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

    def same_task(self, named: str, task_id: str) -> bool:
        """Whether ``named`` (a span's task id) is the task ``task_id``, by its id or by
        its id on its board."""
        with self._lock:
            return named == task_id or self._alias.get(task_id) == named

    def task_sessions(self, task_id: str) -> list[Place]:
        with self._lock:
            return list(self._task_sessions.get(task_id, ()))


@dataclass(slots=True)
class _Binding:
    token: str
    last: datetime  # last reported (or bound)
    settled: bool = False  # the board placed it inside the token's scope
    revoked: datetime | None = None  # the board placed it outside: its spans were deleted


class Barrier:
    """Many ingest requests at once (``shared``), or the guard's check alone
    (``exclusive``): a request judges and stores its spans with no check in between, so a
    span judged before a revocation is stored before the revocation deletes."""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting = 0

    @contextmanager
    def shared(self) -> Iterator[None]:
        with self._cond:
            while self._writer or self._waiting:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if not self._readers:
                    self._cond.notify_all()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._cond:
            self._waiting += 1
            try:
                while self._writer or self._readers:
                    self._cond.wait()
            finally:
                self._waiting -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


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
    """The scopes of ``[[traces.tokens]]`` and what enforces them: the
    :class:`ScopeIndex` and the bindings of ids the board did not place when a token first
    reported them.

    Ingest calls :meth:`ingest_filter` per request. The control loop calls :meth:`check` on
    every merged snapshot, after hook spans were folded in: it refreshes the index, settles
    or revokes bindings against the board (deleting a revoked id's spans of that token from
    ``store``), deletes the snapshot's spans of scoped tokens that are now out of scope (a
    folded span takes the hook's session), counts what it deleted through ``rejected`` and
    calls ``on_discard`` (cached totals and usage are stale then). A span of a token no
    longer configured is kept until the board places its session or task, then deleted."""

    scopes: Mapping[str, TokenScope]
    store: SpanStore | None = None
    rejected: Callable[[str, int], None] | None = None
    on_discard: Callable[[], None] | None = None
    retention: timedelta = timedelta(days=7)
    index: ScopeIndex = field(default_factory=ScopeIndex)
    barrier: Barrier = field(default_factory=Barrier)
    _bound: dict[tuple[str, str], _Binding] = field(default_factory=dict)
    # per token: the ids bound to it, least recently used first (eviction is per token, so
    # one token's flood evicts its own bindings, never another token's: #374)
    _owned: dict[str, OrderedDict[tuple[str, str], None]] = field(default_factory=dict)
    # per token: unsettled ids it used within UNPLACED_ACTIVE, least recently used first
    _active: dict[str, OrderedDict[tuple[str, str], datetime]] = field(default_factory=dict)
    _logged: dict[str, datetime] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _due(self, token: str, now: datetime) -> bool:
        with self._lock:
            seen = self._logged.get(token)
            if seen is not None and timedelta(0) <= now - seen < LOG_EVERY:
                return False
            if len(self._logged) > len(self.scopes):  # slots past their minute are free
                for name, at in list(self._logged.items()):
                    if not timedelta(0) <= now - at < LOG_EVERY:
                        del self._logged[name]
            self._logged[token] = now
            return True

    def _own(self, key: tuple[str, str], binding: _Binding) -> None:
        """Bind ``key`` (or refresh its use); with the lock held."""
        self._bound[key] = binding
        owned = self._owned.setdefault(binding.token, OrderedDict())
        owned[key] = None
        owned.move_to_end(key)

    def _drop(self, key: tuple[str, str]) -> _Binding | None:
        """Unbind ``key``; with the lock held."""
        binding = self._bound.pop(key, None)
        if binding is not None:
            owned = self._owned.get(binding.token)
            if owned is not None:
                owned.pop(key, None)
                if not owned:
                    del self._owned[binding.token]
            self._active.get(binding.token, {}).pop(key, None)
        return binding

    def _evict(self) -> None:
        """Past :data:`MAX_BINDINGS`: the token holding the most bindings loses its least
        recently used ones, so a token that floods ids evicts its own; with the lock held."""
        while len(self._bound) > MAX_BINDINGS:
            largest = max(self._owned.values(), key=len)
            self._drop(next(iter(largest)))

    def _holder(self, kind: str, value: str) -> _Binding | None:
        with self._lock:
            return self._bound.get((kind, value))

    def judge(self, token: str, span: Span) -> tuple[Verdict, list[tuple[str, str]]]:
        """Whether ``token`` may report ``span``, and the ids the board does not place that
        it names (to bind, or to note their use)."""
        scope = self.scopes.get(token)
        if scope is None:
            return Verdict.OUT, []
        unplaced: list[tuple[str, str]] = []
        session = None
        if (sid := span.session_id) is not None:
            session = self.index.session(sid)
            if session is not None:
                if not (scope.covers(session[0]) or scope.session_matches(sid)):
                    return Verdict.OUT, []
            else:
                held = self._holder(SESSION, sid)
                if held is not None and (held.token != token or held.revoked is not None):
                    return Verdict.OUT, []
                if scope.sessions_only and not scope.session_matches(sid):
                    return Verdict.OUT, []
                unplaced.append((SESSION, sid))  # bound now, or its use noted
        if (tid := span.task_id) is not None:
            if session is not None and session[1] is not None:
                if not self.index.same_task(tid, session[1]):
                    return Verdict.OUT, []  # a task the board does not give this session
            elif (task := self.index.task(tid)) is not None:
                if not self._task_in_scope(scope, task):
                    return Verdict.OUT, []
            else:
                held = self._holder(TASK, tid)
                if held is not None and (held.token != token or held.revoked is not None):
                    return Verdict.OUT, []
                unplaced.append((TASK, tid))
        return Verdict.IN, unplaced

    def _task_in_scope(self, scope: TokenScope, task: tuple[Place, str]) -> bool:
        place, task_id = task
        return scope.covers(place) or any(
            scope.covers(p) for p in self.index.task_sessions(task_id)
        )

    # --- ingest ---------------------------------------------------------------------------

    def ingest_filter(
        self, token: str, spans: Sequence[Span], now: datetime
    ) -> tuple[list[Span], Counter[str]]:
        """The spans token ``token`` may report (stamped with its name), and why the others
        were rejected. Ids the board does not place yet are bound to ``token`` (at most
        :data:`MAX_UNPLACED` per token)."""
        kept: list[Span] = []
        rejected: Counter[str] = Counter()
        for span in spans:
            found, unplaced = self.judge(token, span)
            if found is Verdict.IN and unplaced and (why := self._bind(token, unplaced, now)):
                found = Verdict.OUT
                rejected[why] += 1
            elif found is Verdict.OUT:
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
            if found is Verdict.IN:
                kept.append(stamp(span, token))
        return kept, rejected

    def _bind(self, token: str, ids: list[tuple[str, str]], now: datetime) -> str | None:
        """Bind ``ids`` to ``token`` and note their use, all or none; why not, when another
        token holds one (out of scope) or the token would hold more than
        :data:`MAX_UNPLACED` unplaced ids used within :data:`UNPLACED_ACTIVE` (backlog)."""
        with self._lock:
            held = [self._bound.get(k) for k in ids]
            if any(b is not None and (b.token != token or b.revoked) for b in held):
                return OUT_OF_SCOPE
            active = self._active.setdefault(token, OrderedDict())
            while active and now - next(iter(active.values())) > UNPLACED_ACTIVE:
                active.popitem(last=False)
            # every unsettled id not used lately counts, an old binding of its own included
            new = sum(
                1
                for k, b in zip(ids, held, strict=True)
                if (b is None or not b.settled) and k not in active
            )
            if new and len(active) + new > MAX_UNPLACED:
                return SCOPE_BACKLOG
            for key, b in zip(ids, held, strict=True):
                if b is None:
                    b = _Binding(token, now)
                else:
                    b.last = now
                self._own(key, b)  # least recently used first
                if not b.settled:
                    active[key] = now
                    active.move_to_end(key)
            self._evict()
            return None

    def restore(self, after: datetime, now: datetime) -> int:
        """After a restart: bind the ids of the stored spans of scoped tokens (last time at
        or after ``after``) to the token whose span was stored first, so the next tick checks
        them against the board again; a token no longer configured covers nothing, so its
        spans are deleted once the board places their session or task. Returns how many ids
        were bound."""
        if self.store is None:
            return 0
        bound = 0
        with self._lock:
            for session_id, task_id, token in self.store.marked_ids(after, TOKEN_ATTRIBUTE):
                for kind, value in ((SESSION, session_id), (TASK, task_id)):
                    if value is not None and (kind, value) not in self._bound:
                        self._own((kind, value), _Binding(token, now))
                        bound += 1
            self._evict()
        return bound

    # --- every tick -----------------------------------------------------------------------

    def check(
        self, snapshot: Snapshot, remembered: Attribution | None = None
    ) -> tuple[Snapshot, int]:
        """The snapshot without spans out of their token's scope, and how many spans were
        deleted from the store. Runs alone: no ingest request is between judging and storing
        its spans meanwhile (:class:`Barrier`)."""
        with self.barrier.exclusive():
            return self._check(snapshot, remembered)

    def _check(self, snapshot: Snapshot, remembered: Attribution | None) -> tuple[Snapshot, int]:
        self.index.update(snapshot, remembered)
        now = snapshot.now
        deleted = 0
        revoke: list[tuple[str, str, str]] = []  # kind, id, token
        with self._lock:
            bindings = list(self._bound.items())
        for (kind, value), held in bindings:
            # A span may be dated up to MAX_FUTURE ahead: the binding outlives it by as much,
            # so a revocation always finds the binding its spans were stored under (#374).
            if now - held.last > self.retention + MAX_FUTURE:
                with self._lock:
                    if self._bound.get((kind, value)) is held:
                        self._drop((kind, value))
                continue
            if held.settled or held.revoked is not None:
                continue
            scope = self.scopes.get(held.token)
            verdict = self._placed(kind, value, scope)
            if verdict is not None:
                with self._lock:
                    self._active.get(held.token, {}).pop((kind, value), None)
            if verdict is Verdict.OUT:
                with self._lock:
                    held.revoked = now
                revoke.append((kind, value, held.token))
                self._log(held.token, f"{kind.removesuffix('_id')} {_short(value)!r}", now)
            elif verdict is Verdict.IN:
                with self._lock:
                    held.settled = True
        if self.store is not None:
            for kind, value, token in revoke:
                try:
                    deleted += self.store.discard_marked(kind, value, TOKEN_ATTRIBUTE, token)
                except Exception as exc:
                    log.error("cannot delete out-of-scope spans: %s", exc)
                    with self._lock:  # tried again next tick
                        if (held := self._bound.get((kind, value))) is not None:
                            held.revoked = None
        out: set[SpanKey] = set()
        for span in snapshot.spans:
            token = span.attributes.get(TOKEN_ATTRIBUTE)
            if not isinstance(token, str):
                continue
            if token not in self.scopes:  # no longer configured: history, until placed
                if not self._placed_anywhere(span):
                    continue
            elif self.judge(token, span)[0] is not Verdict.OUT:
                continue
            out.add(span_key(span))
            self._log(token, f"session {_short(span.session_id)!r}", now)
        if out and self.store is not None:
            try:
                deleted += self.store.discard(out)
            except Exception as exc:  # dropped from this tick anyway
                log.error("cannot delete %d out-of-scope spans: %s", len(out), exc)
        if deleted and self.rejected is not None:
            self.rejected(OUT_OF_SCOPE, deleted)
        if (deleted or out) and self.on_discard is not None:
            self.on_discard()
        if not out:
            return snapshot, deleted
        kept = tuple(s for s in snapshot.spans if span_key(s) not in out)
        return replace(snapshot, spans=kept), deleted + len(out)

    def _placed_anywhere(self, span: Span) -> bool:
        return (
            span.session_id is not None and self.index.session(span.session_id) is not None
        ) or (span.task_id is not None and self.index.task(span.task_id) is not None)

    def _placed(self, kind: str, value: str, scope: TokenScope | None) -> Verdict | None:
        """IN or OUT once the board places a bound id; None while it does not. A token no
        longer configured (``scope`` None) covers nothing."""
        if kind == SESSION:
            session = self.index.session(value)
            if session is None:
                return None
            if scope is None:
                return Verdict.OUT
            ok = scope.covers(session[0]) or scope.session_matches(value)
            return Verdict.IN if ok else Verdict.OUT
        task = self.index.task(value)
        if task is None:
            return None
        if scope is None:
            return Verdict.OUT
        return Verdict.IN if self._task_in_scope(scope, task) else Verdict.OUT

    def _log(self, token: str, what: str, now: datetime) -> None:
        # names no longer configured share one log slot: stored marks could hold many
        if self._due(token if token in self.scopes else REMOVED_TOKEN, now):
            log.warning(
                "deleted spans of traces token %r for %s: outside its scope", token[:64], what
            )

    def bound(self, kind: str, value: str) -> str | None:
        """The token ``value`` is bound to (and not revoked), or None."""
        held = self._holder(kind, value)
        return held.token if held is not None and held.revoked is None else None
