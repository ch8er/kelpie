"""``POST /conditions``: an agent registers the condition its task waits for (rule 2).

Off by default. With ``[conditions] enabled = true`` the main listener of ``kelpie serve``
(loopback by default, behind the ``[server]`` Host and connection guards) takes::

    POST /conditions
    Authorization: Bearer <the agent's [[traces.tokens]] token>
    Content-Type: application/json

    {"task_id": "T-1", "kind": "pr_merged", "params": {"repo": "owner/name", "pr": 12}}

**Attach-only.** A registered condition attaches to its task inside Kelpie: each snapshot
carries it in ``Snapshot.conditions`` with ``task_id`` set while the task is on the board,
in progress or waiting (:data:`ATTACH_TO`; never a blocked task, whose block rule 4 would
read it as ending, nor a ready one, which the dispatch gate would park), the connectors
evaluate it like any other (``pr_merged`` and ``ci_status`` by the GitHub connector,
``task_status`` from the board, ...), and the rules read it in their own modes (rule 2
wakes a waiting task once it holds). Registering never parks the task or changes anything
on the board: the agent sets its own status. A registration made at most
:data:`REGISTER_GRACE` before the task's wait began counts from that wait (register, then
set waiting and exit), and is bound to it: a later wait needs it registered again. An older
one is left over from an earlier wait, as for any condition. Only the kinds an evaluator
reads are taken (:data:`REGISTRABLE`), and params never set the keys evaluators write
(:data:`RESERVED`).

**Trust: a token per agent (#442).** Each agent registers with its own
``[[traces.tokens]]`` token, and only for its own tasks: the tasks the board places in that
entry's scope (its owners, roles or boards, or a session of the task in scope, the same
scoping as its spans, :func:`kelpie.traces.scope.own_task`). A registration for any other
task, or for a task the board does not place, is 403 with the same answer either way (it
does not tell whether the task exists), a ledger line (:data:`AUDIT_RULE`, at most one an
hour per token and task, repeats counted on it; past :data:`AUDIT_PER_TOKEN` an hour, one
for all the token's tasks) and a counter. Scopes come from the last tick: until the first
one after a start a per-agent token gets 503 with ``Retry-After``. A registration attaches only
while its token is still configured and the task still in its scope, so removing an agent's
entry (or moving its task to another agent) also drops what it registered. Each token has
its own rate (``max_per_minute``, 429 beyond) and its own share of live registrations
(``max_per_token``, 507 beyond), so a flood from one token leaves the others alone. The
shared ``token_env`` token (deprecated) still registers for any task, as before; its
registrations (and those older than #442, which name no token) attach only while a shared
token is set (``shared_rows``), and are deleted once it is not.

**Checks, before the body is read:** the bearer token (every token compared, in constant
time each; a missing or wrong one is 401), no ``Origin`` header (a web page, not an agent:
403), a per-agent token's rate (429), ``Content-Length`` present and at most
:data:`MAX_BODY` (411/413), JSON media type (415). Then the body: an object with
``task_id`` (1 to 200 characters, no control characters), ``kind`` (a
:class:`~kelpie.core.model.ConditionKind`) and ``params`` (an object of at most
:data:`MAX_PARAMS` scalar values, keys and strings at most 200 characters); 400 otherwise.
Then a per-agent token's scope (403).

**Idempotent.** A registration is identified by its task, kind and params (a hash of their
canonical JSON): the same one again is ``200`` with ``"duplicate": true`` and counts from
now (a later wait on the same thing); a new one is ``201``. At most ``max_registered`` live
registrations are kept (507 beyond, nothing stored), and at most ``max_per_token`` of one
per-agent token. A registration is dropped once its task finishes on the board, or
``max_age`` after it was made.

Registrations live in Kelpie's database (table ``registered_conditions``, created when
missing; no schema version), so a restart keeps them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import threading
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from kelpie.core.actions import ActionKind
from kelpie.core.config import Mode
from kelpie.core.files import prepare_db
from kelpie.core.ledger import LedgerEntry, Outcome, to_json
from kelpie.core.model import Condition, ConditionKind, Snapshot, Status
from kelpie.core.serde import parse_duration
from kelpie.traces.auth import TokenCheck
from kelpie.traces.config import weak_token
from kelpie.traces.ingest import ScopedToken
from kelpie.traces.scope import ScopeIndex, TokenScope, own_task

log = logging.getLogger(__name__)

PATH = "/conditions"
DEFAULT_TOKEN_ENV = "KELPIE_CONDITIONS_TOKEN"
DEFAULT_MAX_AGE = timedelta(days=7)
MAX_MAX_AGE = timedelta(days=90)
DEFAULT_MAX_REGISTERED = 1000
MAX_REGISTERED = 100_000
DEFAULT_MAX_PER_TOKEN = 100  # live registrations of one per-agent token
DEFAULT_MAX_PER_MINUTE = 60  # requests of one per-agent token (a burst of as many)
MAX_PER_MINUTE = 10_000
MAX_BODY = 16 * 1024
MAX_PARAMS = 20
MAX_TEXT = 200
_KEYS = frozenset(
    {"enabled", "token_env", "max_age", "max_registered", "max_per_token", "max_per_minute"}
)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
# The kinds a connector or the board evaluates; ``time`` and ``custom`` nothing does, so a
# registration of them could only ever stall a wake.
REGISTRABLE = frozenset(
    {
        ConditionKind.PR_MERGED,
        ConditionKind.CI_STATUS,
        ConditionKind.TASK_STATUS,
        ConditionKind.QUOTA_RESET,
        ConditionKind.HUMAN_TASK,
    }
)
# Params the evaluators write or trust as their own results, never a client's.
RESERVED = frozenset(
    {
        "terminal",
        "terminal_ok",
        "error",
        "satisfied",
        "source",
        "pr_state",
        "ci_state",
        "ci_source",
        "head_sha",
    }
)
# Statuses a registration attaches to: a task working, about to wait, or waiting. Never a
# blocked one: rule 4 would read a registered condition as the end of its block.
# Not a ready one either: the dispatch gate would park it on an unmet registration.
ATTACH_TO = frozenset({Status.IN_PROGRESS, Status.WAITING})
# The usual order is: register, then set the waiting status and exit. A registration made at
# most this long before the task's current status counts from that status, as made for it.
REGISTER_GRACE = timedelta(minutes=15)
PRUNE_EVERY = timedelta(minutes=5)  # finished tasks' and old registrations are deleted so often
MAX_INT = 2**53
_SCHEMA = """
CREATE TABLE IF NOT EXISTS registered_conditions (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    params        TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    bound_to      TEXT,  -- the task's status_since the registration was taken for (once)
    token         TEXT   -- the [[traces.tokens]] entry that registered it; NULL: shared
);
CREATE INDEX IF NOT EXISTS registered_conditions_at ON registered_conditions (registered_at);
CREATE INDEX IF NOT EXISTS registered_conditions_task ON registered_conditions (task_id);
"""
# A table made before per-agent tokens (#442) gets the column; then its index.
_TOKEN_INDEX = (
    "CREATE INDEX IF NOT EXISTS registered_conditions_token ON registered_conditions (token)"
)
_COLUMNS = "id, task_id, kind, params, registered_at, bound_to, token"

AUDIT_RULE = "conditions"  # the ledger's rule for a refused registration: no rule acted
AUDIT_EVERY = timedelta(hours=1)  # one ledger line per token and task this often
MAX_AUDITED = 10_000  # (token, task) pairs remembered for that
# Ledger lines of one token's refusals an hour; beyond, they fold into one line per token, so
# a token naming made-up task ids cannot fill the ledger and the log.
AUDIT_PER_TOKEN = 20
NOT_READ_YET = 30  # seconds: Retry-After while no tick has read the board since the start
# kelpie_conditions_refused_total reasons
OUT_OF_SCOPE = "out_of_scope"
RATE_LIMITED = "rate_limited"
TOKEN_CAP = "token_cap"
FOREIGN = "this token may not register conditions for that task"


@dataclass(frozen=True, slots=True)
class ConditionSettings:
    """``[conditions]``."""

    enabled: bool = False
    token_env: str = DEFAULT_TOKEN_ENV
    max_age: timedelta = DEFAULT_MAX_AGE
    max_registered: int = DEFAULT_MAX_REGISTERED
    token_env_explicit: bool = False  # set in the config: the variable must then be set
    max_per_token: int = DEFAULT_MAX_PER_TOKEN
    max_per_minute: int = DEFAULT_MAX_PER_MINUTE

    def shared_token(self, environ: Mapping[str, str] = os.environ) -> str | None:
        """The shared token (deprecated, #442), or None when its variable is unset;
        ValueError naming the variable (never the value) when it is weak, or unset although
        ``token_env`` is in the config."""
        token = environ.get(self.token_env, "").strip()
        if not token:
            if self.token_env_explicit:
                raise ValueError(f"[conditions] token_env: ${self.token_env} is not set")
            return None
        if (problem := weak_token(token)) is not None:
            raise ValueError(f"[conditions] ${self.token_env} {problem}")
        return token


def load_conditions(raw: Any) -> ConditionSettings:
    if raw is None:
        return ConditionSettings()
    if not isinstance(raw, Mapping):
        raise ValueError("[conditions] must be a table")
    if extra := set(raw) - _KEYS:
        raise ValueError(f"unknown [conditions] keys: {sorted(extra)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("[conditions] enabled must be true or false")
    env = raw.get("token_env", DEFAULT_TOKEN_ENV)
    if not isinstance(env, str) or not _ENV_NAME.fullmatch(env):
        raise ValueError("[conditions] token_env must be an environment variable name")
    try:
        max_age = parse_duration(raw.get("max_age", DEFAULT_MAX_AGE))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"[conditions] max_age: {exc}") from None
    if not timedelta(minutes=1) <= max_age <= MAX_MAX_AGE:
        raise ValueError("[conditions] max_age must be 1m to 90d")
    count = raw.get("max_registered", DEFAULT_MAX_REGISTERED)
    if not _count(count, MAX_REGISTERED):
        raise ValueError(f"[conditions] max_registered must be 1 to {MAX_REGISTERED}")
    per_token = raw.get("max_per_token", min(DEFAULT_MAX_PER_TOKEN, count))
    if not _count(per_token, count):
        raise ValueError(f"[conditions] max_per_token must be 1 to max_registered ({count})")
    per_minute = raw.get("max_per_minute", DEFAULT_MAX_PER_MINUTE)
    if not _count(per_minute, MAX_PER_MINUTE):
        raise ValueError(f"[conditions] max_per_minute must be 1 to {MAX_PER_MINUTE}")
    return ConditionSettings(
        enabled,
        env,
        max_age,
        count,
        token_env_explicit="token_env" in raw,
        max_per_token=per_token,
        max_per_minute=per_minute,
    )


def _count(value: Any, most: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= most


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: dict[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)

    def encoded(self) -> bytes:
        return (json.dumps(self.body, sort_keys=True) + "\n").encode()


class BadRequest(ValueError):
    pass


def parse(body: bytes) -> tuple[str, ConditionKind, dict[str, Any]]:
    """The task, kind and params of a registration; :class:`BadRequest` when it is not one."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        raise BadRequest("the body must be a JSON object") from None
    if not isinstance(data, dict) or set(data) - {"task_id", "kind", "params"}:
        raise BadRequest("the body must be an object with task_id, kind and params only")
    task = data.get("task_id")
    if not _text(task):
        raise BadRequest(f"task_id must be 1 to {MAX_TEXT} characters, no control characters")
    try:
        kind = ConditionKind(data.get("kind"))
    except ValueError:
        kind = None
    if kind not in REGISTRABLE:
        kinds = ", ".join(sorted(k.value for k in REGISTRABLE))
        raise BadRequest(f"kind must be one of {kinds}") from None
    params = data.get("params", {})
    if not isinstance(params, dict) or len(params) > MAX_PARAMS:
        raise BadRequest(f"params must be an object of at most {MAX_PARAMS} entries")
    for key, value in params.items():
        if key in RESERVED or str(key).startswith("observed_"):
            raise BadRequest(f"params may not set {key!r}: the evaluators write it")
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > MAX_INT:
            raise BadRequest(f"params numbers must be within 2**53 ({key!r})")
        scalar = isinstance(value, bool | int | float) or value is None or _text(value)
        if not _text(key) or not scalar or (isinstance(value, float) and not math.isfinite(value)):
            raise BadRequest(f"params values must be numbers, booleans or short text ({key!r})")
    return str(task), kind, dict(params)


def _text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_TEXT and not _CONTROL.search(value)


def registration_id(task: str, kind: ConditionKind, params: Mapping[str, Any]) -> str:
    canonical = json.dumps([task, kind.value, params], sort_keys=True, separators=(",", ":"))
    return "registered:" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


@dataclass(frozen=True, slots=True)
class Caller:
    """Who sent a registration: a ``[[traces.tokens]]`` entry's name, or None for the
    shared ``token_env`` token."""

    token: str | None = None


SHARED = Caller()


class AuditLog(Protocol):
    """Where refused registrations are recorded (the action ledger's store)."""

    def append(self, entry: LedgerEntry) -> None: ...

    def bump_suppressed(self, fingerprint: str, mode: Mode, after: datetime) -> bool: ...


@dataclass(slots=True)
class ConditionRegistry:
    """The endpoint and the snapshot step that attaches what was registered."""

    settings: ConditionSettings
    path: Path | str
    clock: Callable[[], datetime]
    token: str = field(repr=False, default="")  # the shared token (deprecated); "": none
    readonly: bool = False  # kelpie why and the report: attach, never delete or register
    # Per-agent tokens (#442): the [[traces.tokens]] tokens, and every entry's scope (by
    # name; ``readonly`` needs only the scopes, to attach what serve would).
    scoped: Sequence[ScopedToken] = ()
    scopes: Mapping[str, TokenScope] = field(default_factory=dict)
    audit: AuditLog | None = None  # the ledger's store: refused registrations
    observer: Any = None  # Metrics: conditions_refused(reason)
    index: ScopeIndex = field(default_factory=ScopeIndex)  # the board, from each snapshot
    # Whether the shared token's registrations (and those from before #442, which carry no
    # token) attach: only while a shared token is configured. None: when ``token`` is set.
    shared_rows: bool | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _db: sqlite3.Connection | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _pruned_at: datetime | None = field(default=None, init=False, repr=False)
    _has_token: bool = field(default=True, init=False, repr=False)  # the table's column
    _rate_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _buckets: dict[str, tuple[float, datetime]] = field(
        default_factory=dict, init=False, repr=False
    )
    _audited: OrderedDict[str, datetime] = field(
        default_factory=OrderedDict, init=False, repr=False
    )
    _audit_counts: dict[str, tuple[datetime, int]] = field(
        default_factory=dict, init=False, repr=False
    )
    _read_board: bool = field(default=False, init=False, repr=False)  # a tick ran

    def __post_init__(self) -> None:
        if not self.token and not self.scoped and not self.readonly:
            raise ValueError("a condition registry needs a token")
        if any(t.name not in self.scopes for t in self.scoped):
            raise ValueError("every per-agent conditions token needs its scope")
        if self.shared_rows is None:
            self.shared_rows = bool(self.token)

    def _conn(self) -> sqlite3.Connection:
        if self._closed:
            raise sqlite3.ProgrammingError("the condition registry is closed")
        if self._db is None:
            if self.readonly:
                uri = Path(self.path).resolve().as_uri() + "?mode=ro"
                db = sqlite3.connect(uri, uri=True, check_same_thread=False)
            else:
                prepare_db(self.path)
                db = sqlite3.connect(str(self.path), check_same_thread=False)
            try:
                db.execute("PRAGMA busy_timeout = 5000")
                if not self.readonly:
                    db.executescript(_SCHEMA)
                columns = {r[1] for r in db.execute("PRAGMA table_info(registered_conditions)")}
                self._has_token = "token" in columns or not columns
                if not self.readonly and not self._has_token:
                    with db:
                        db.execute("ALTER TABLE registered_conditions ADD COLUMN token TEXT")
                    self._has_token = True
                if not self.readonly:
                    db.execute(_TOKEN_INDEX)
            except sqlite3.Error:
                db.close()
                raise
            self._db = db
        return self._db

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._db is not None:
                self._db.close()
                self._db = None

    # --- the endpoint -----------------------------------------------------------------

    def authenticate(self, header: str | None) -> Caller | None:
        """Who sent the request (None: no token of ours). Every token is compared, in
        constant time each, whatever was sent; a per-agent token is matched first (a
        rotated-out one within its grace too), as ingest does."""
        scheme, _, credentials = (header or "").strip().partition(" ")
        given = credentials.strip() if scheme.lower() == "bearer" else ""
        found: Caller | None = None
        for scoped in self.scoped:
            verdict = scoped.check(given)
            if verdict is not TokenCheck.INVALID and given and found is None:
                found = Caller(scoped.name)
        shared = hmac.compare_digest(given.encode(), self.token.encode())
        if found is not None:
            return found
        return SHARED if shared and given and self.token else None

    def precheck(self, headers: Any) -> Answer | Caller:
        """What the headers alone decide, before the body is read; a :class:`Caller`:
        read it."""
        caller = self.authenticate(headers.get("Authorization"))
        if caller is None:
            return Answer(401, {"error": "missing or invalid bearer token"})
        if headers.get("Origin") is not None:
            return Answer(403, {"error": "requests from a web page are refused"})
        if caller.token is not None and (wait := self._throttle(caller.token)) is not None:
            self._refused(RATE_LIMITED)
            return Answer(
                429,
                {"error": "too many registrations from this token, retry later"},
                {"Retry-After": str(wait)},
            )
        raw = headers.get("Content-Length")
        if raw is None:
            return Answer(411, {"error": "Content-Length required"})
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 12:
            return Answer(400, {"error": "bad Content-Length"})
        length = int(raw)
        if length > MAX_BODY:
            return Answer(413, {"error": f"body over {MAX_BODY} bytes"})
        media = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if media != "application/json":
            return Answer(415, {"error": "use Content-Type: application/json"})
        return caller

    def _throttle(self, name: str) -> int | None:
        """Take one request from the token's bucket (``max_per_minute`` a minute, a burst
        of as many); the seconds to wait when it is empty."""
        rate = self.settings.max_per_minute
        now = self.clock()
        with self._rate_lock:
            level, at = self._buckets.get(name, (float(rate), now))
            elapsed = max((now - at).total_seconds(), 0.0)
            level = min(float(rate), level + elapsed * rate / 60)
            if level < 1:
                self._buckets[name] = (level, now)
                return max(1, math.ceil((1 - level) * 60 / rate))
            self._buckets[name] = (level - 1, now)
            return None

    def _refused(self, reason: str) -> None:
        hook = getattr(self.observer, "conditions_refused", None)
        if callable(hook):
            hook(reason)

    def register(self, body: bytes, caller: Caller = SHARED) -> Answer:
        """Store one registration (the headers were checked, and told who sent it)."""
        if self.readonly:
            return Answer(503, {"error": "registrations are read-only here"})
        try:
            task, kind, params = parse(body)
        except BadRequest as exc:
            return Answer(400, {"error": str(exc)})
        now = self.clock()
        if caller.token is not None:
            if not self._read_board:  # just started: no scope is known yet, refuse nothing
                return Answer(
                    503,
                    {"error": "the board is not read yet, retry later"},
                    {"Retry-After": str(NOT_READ_YET)},
                )
            scope = self.scopes.get(caller.token)
            owned = own_task(self.index, scope, task) if scope is not None else None
            if owned is None:  # another agent's task, or none the board shows: one answer
                self._refuse_foreign(caller.token, task, kind, now)
                return Answer(403, {"error": FOREIGN})
            task = owned
        ident = registration_id(task, kind, params)
        oldest = _ts(now - self.settings.max_age)
        with self._lock:
            db = self._conn()
            with db:
                if db.execute(
                    "SELECT 1 FROM registered_conditions WHERE id = ?", (ident,)
                ).fetchone():
                    # The same again, for this wait: it counts from now, bound to no wait yet,
                    # and is the latest sender's.
                    db.execute(
                        "UPDATE registered_conditions SET registered_at = ?, bound_to = NULL,"
                        " token = ? WHERE id = ?",
                        (_ts(now), caller.token, ident),
                    )
                    return Answer(200, {"id": ident, "duplicate": True})
                if caller.token is not None:
                    mine = db.execute(
                        "SELECT COUNT(*) FROM registered_conditions"
                        " WHERE token = ? AND registered_at >= ?",
                        (caller.token, oldest),
                    ).fetchone()[0]
                    if mine >= self.settings.max_per_token:
                        self._refused(TOKEN_CAP)
                        return Answer(
                            507, {"error": "too many registered conditions for this token"}
                        )
                live = db.execute(
                    "SELECT COUNT(*) FROM registered_conditions WHERE registered_at >= ?",
                    (oldest,),
                ).fetchone()[0]
                if live >= self.settings.max_registered:
                    return Answer(507, {"error": "too many registered conditions"})
                db.execute(
                    f"INSERT INTO registered_conditions ({_COLUMNS})"
                    " VALUES (?, ?, ?, ?, ?, NULL, ?)",
                    (
                        ident,
                        task,
                        kind.value,
                        json.dumps(params, sort_keys=True),
                        _ts(now),
                        caller.token,
                    ),
                )
        log.info("condition %s registered for task %s (%s)", ident, task[:MAX_TEXT], kind)
        return Answer(201, {"id": ident, "duplicate": False})

    def _refuse_foreign(self, name: str, task: str, kind: ConditionKind, now: datetime) -> None:
        """Count a registration refused for a task that is not the token's own, and write
        its ledger line: one per token and task an hour, later ones counted on it; past
        :data:`AUDIT_PER_TOKEN` lines of a token in an hour, one line for all its tasks."""
        self._refused(OUT_OF_SCOPE)
        with self._rate_lock:
            start, count = self._audit_counts.get(name, (now, 0))
            if not timedelta(0) <= now - start < AUDIT_EVERY:
                start, count = now, 0
            folded = count >= AUDIT_PER_TOKEN
            shown = "(several tasks)" if folded else task
            raw = to_json([AUDIT_RULE, OUT_OF_SCOPE, name, None if folded else task])
            fingerprint = hashlib.sha256(raw.encode()).hexdigest()[:32]
            last = self._audited.get(fingerprint)
            fresh = last is None or not timedelta(0) <= now - last < AUDIT_EVERY
            if fresh:
                self._audited[fingerprint] = now
                self._audited.move_to_end(fingerprint)
                while len(self._audited) > MAX_AUDITED:
                    self._audited.popitem(last=False)
                if not folded:
                    self._audit_counts[name] = (start, count + 1)
        if fresh:
            log.warning(
                "conditions: token %r asked to register for task %r, not one of its tasks: refused",
                name,
                shown[:80],
            )
        if self.audit is None:
            return
        entry = LedgerEntry(
            at=now,
            fingerprint=fingerprint,
            subject=f"token:{name}",
            rule=AUDIT_RULE,
            kind=ActionKind.REGISTER,
            mode=Mode.OBSERVE,
            outcome=Outcome.OBSERVED,
            reason=(
                f"Per-agent token {name!r} asked to register a {kind.value} condition for "
                f"{shown}, which is not one of its tasks (or not on the board): refused (403)."
                + (" Its further refusals this hour are counted on this line." if folded else "")
            ),
            task_id=None if folded else task,
            params={"token": name, "kind": kind.value, "status": 403},
        )
        try:
            if fresh or not self.audit.bump_suppressed(
                fingerprint, Mode.OBSERVE, now - AUDIT_EVERY
            ):
                self.audit.append(entry)
        except Exception as exc:  # the refusal stands; the ledger line is best effort
            log.error("conditions: cannot write the ledger line of a refused registration: %s", exc)

    # --- the snapshot step ------------------------------------------------------------

    def __call__(self, snapshot: Snapshot) -> Snapshot:
        """Attach the live registrations to their tasks (after dropping those of finished
        tasks and those past ``max_age``); never raises: a database it cannot read attaches
        nothing this tick. A per-agent token's registration attaches only while the token is
        configured and the task in its scope. Each call also reads the board into the index
        the endpoint judges scopes against."""
        self.index.update(snapshot)
        self._read_board = True
        try:
            rows = self._live(snapshot)
        except (OSError, sqlite3.Error) as exc:
            if "no such table" not in str(exc):
                log.warning("registered conditions not read: %s", exc)
            return snapshot
        known = {c.id for c in snapshot.conditions}
        tasks = {t.id: t for t in snapshot.tasks}
        found = []
        bind: list[tuple[str, str]] = []
        for ident, task_id, kind, params, at, bound, token in rows:
            task = tasks.get(task_id)
            if ident in known or task is None or task.status not in ATTACH_TO:
                continue  # not on the board now, or not a task a registration attaches to
            if token is None and not self.shared_rows:
                continue  # the shared token's (or older): only while a shared token is set
            if token is not None and not self._still_owned(token, task_id):
                continue  # its token was removed, or the task is another agent's now
            try:
                registered = datetime.fromisoformat(at)
                condition = Condition(
                    id=ident,
                    kind=ConditionKind(kind),
                    task_id=task_id,
                    params=dict(json.loads(params)),
                    registered_at=registered,
                )
            except (ValueError, TypeError) as exc:  # a row someone else wrote
                log.warning("registered condition %s skipped: %s", ident[:64], exc)
                continue
            since = _ts(task.status_since)
            fresh = task.status_since > registered >= task.status_since - REGISTER_GRACE
            if bound == since or (bound is None and fresh and task.status is Status.WAITING):
                # Registered, then the wait set: made for this wait, and for no later one.
                condition = replace(condition, registered_at=task.status_since)
                if bound is None:
                    bind.append((since, ident))
            found.append(condition)
        if bind and not self.readonly:
            try:
                with self._lock:
                    db = self._conn()
                    with db:
                        db.executemany(
                            "UPDATE registered_conditions SET bound_to = ? WHERE id = ?", bind
                        )
            except sqlite3.Error as exc:  # counted for this tick; bound on the next
                log.warning("registered conditions not bound: %s", exc)
        if not found:
            return snapshot
        return replace(snapshot, conditions=(*snapshot.conditions, *found))

    def _still_owned(self, token: str, task_id: str) -> bool:
        scope = self.scopes.get(token)
        return scope is not None and own_task(self.index, scope, task_id) == task_id

    def _live(
        self, snapshot: Snapshot
    ) -> list[tuple[str, str, str, str, str, str | None, str | None]]:
        """The rows within ``max_age``; every :data:`PRUNE_EVERY` (not with ``readonly``)
        those of finished tasks and older ones are deleted first."""
        oldest = _ts(snapshot.now - self.settings.max_age)
        with self._lock:
            db = self._conn()
            due = self._pruned_at is None or not (
                timedelta(0) <= snapshot.now - self._pruned_at < PRUNE_EVERY
            )
            if due and not self.readonly:
                self._pruned_at = snapshot.now
                self._prune(db, snapshot, oldest)
            token = "token" if self._has_token else "NULL"  # a table older than #442
            return db.execute(
                f"SELECT id, task_id, kind, params, registered_at, bound_to, {token}"
                " FROM registered_conditions"
                " WHERE registered_at >= ? ORDER BY registered_at, id",
                (oldest,),
            ).fetchall()

    def _prune(self, db: sqlite3.Connection, snapshot: Snapshot, oldest: str) -> None:
        stored = {r[0] for r in db.execute("SELECT DISTINCT task_id FROM registered_conditions")}
        finished = [t.id for t in snapshot.tasks if t.status.is_terminal and t.id in stored]
        old = db.execute(
            "SELECT 1 FROM registered_conditions WHERE registered_at < ? LIMIT 1", (oldest,)
        ).fetchone()
        # Rows of tokens no longer configured (and the shared token's, once none is set)
        # never attach again: they go, and stop counting toward max_registered.
        tokens = {r[0] for r in db.execute("SELECT DISTINCT token FROM registered_conditions")}
        gone = [t for t in tokens if t is not None and t not in self.scopes]
        shared_gone = None in tokens and not self.shared_rows
        if not finished and old is None and not gone and not shared_gone:
            return  # nothing to delete: no write transaction
        with db:
            db.execute("DELETE FROM registered_conditions WHERE registered_at < ?", (oldest,))
            if shared_gone:
                db.execute("DELETE FROM registered_conditions WHERE token IS NULL")
            for i in range(0, len(gone), 500):
                part = gone[i : i + 500]
                marks = ", ".join("?" * len(part))
                db.execute(f"DELETE FROM registered_conditions WHERE token IN ({marks})", part)
            for i in range(0, len(finished), 500):
                chunk = finished[i : i + 500]
                marks = ", ".join("?" * len(chunk))
                db.execute(f"DELETE FROM registered_conditions WHERE task_id IN ({marks})", chunk)


def _ts(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="microseconds")
