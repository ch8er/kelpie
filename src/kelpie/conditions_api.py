"""``POST /conditions``: an agent registers the condition its task waits for (rule 2).

Off by default. With ``[conditions] enabled = true`` the main listener of ``kelpie serve``
(loopback by default, behind the ``[server]`` Host and connection guards) takes::

    POST /conditions
    Authorization: Bearer <$KELPIE_CONDITIONS_TOKEN>
    Content-Type: application/json

    {"task_id": "T-1", "kind": "pr_merged", "params": {"repo": "owner/name", "pr": 12}}

**Attach-only.** A registered condition attaches to its task inside Kelpie: each snapshot
carries it in ``Snapshot.conditions`` with ``task_id`` set, the connectors evaluate it like
any other (``pr_merged`` and ``ci_status`` by the GitHub connector, ``task_status`` from the
board, ...), and the rules read it in their own modes (rule 2 wakes a waiting task once it
holds, rule 4 clears a block). Registering never parks the task or changes anything on the
board: the agent still sets its own status before it exits.

**Checks, before the body is read:** the bearer token (``token_env``, compared in constant
time; a missing or wrong one is 401), no ``Origin`` header (a web page, not an agent:
403), ``Content-Length`` present and at most :data:`MAX_BODY` (411/413), JSON media type
(415). Then the body: an object with ``task_id`` (1 to 200 characters, no control
characters), ``kind`` (a :class:`~kelpie.core.model.ConditionKind`) and ``params`` (an object
of at most :data:`MAX_PARAMS` scalar values, keys and strings at most 200 characters); 400
otherwise.

**Idempotent.** A registration is identified by its task, kind and params (a hash of their
canonical JSON): the same one again is ``200`` with ``"duplicate": true`` and keeps its first
registration time; a new one is ``201``. At most ``max_registered`` live registrations are
kept (507 beyond, nothing stored). A registration is dropped once its task is done or
cancelled on the board, or ``max_age`` after it was made.

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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.core.files import prepare_db
from kelpie.core.model import Condition, ConditionKind, Snapshot
from kelpie.core.serde import parse_duration
from kelpie.traces.config import weak_token

log = logging.getLogger(__name__)

PATH = "/conditions"
DEFAULT_TOKEN_ENV = "KELPIE_CONDITIONS_TOKEN"
DEFAULT_MAX_AGE = timedelta(days=7)
MAX_MAX_AGE = timedelta(days=90)
DEFAULT_MAX_REGISTERED = 1000
MAX_REGISTERED = 100_000
MAX_BODY = 16 * 1024
MAX_PARAMS = 20
MAX_TEXT = 200
_KEYS = frozenset({"enabled", "token_env", "max_age", "max_registered"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_TERMINAL = frozenset({"done", "cancelled"})
_SCHEMA = """
CREATE TABLE IF NOT EXISTS registered_conditions (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    kind          TEXT NOT NULL,
    params        TEXT NOT NULL,
    registered_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS registered_conditions_at ON registered_conditions (registered_at);
"""


@dataclass(frozen=True, slots=True)
class ConditionSettings:
    """``[conditions]``."""

    enabled: bool = False
    token_env: str = DEFAULT_TOKEN_ENV
    max_age: timedelta = DEFAULT_MAX_AGE
    max_registered: int = DEFAULT_MAX_REGISTERED

    def token(self, environ: Mapping[str, str] = os.environ) -> str:
        """The token; ValueError naming the variable (never the value) when it is unset or
        weak."""
        token = environ.get(self.token_env, "").strip()
        if not token:
            raise ValueError(f"[conditions] enabled needs ${self.token_env} set")
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
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= MAX_REGISTERED:
        raise ValueError(f"[conditions] max_registered must be 1 to {MAX_REGISTERED}")
    return ConditionSettings(enabled, env, max_age, count)


@dataclass(frozen=True, slots=True)
class Answer:
    status: int
    body: dict[str, Any]

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
        kinds = ", ".join(k.value for k in ConditionKind)
        raise BadRequest(f"kind must be one of {kinds}") from None
    params = data.get("params", {})
    if not isinstance(params, dict) or len(params) > MAX_PARAMS:
        raise BadRequest(f"params must be an object of at most {MAX_PARAMS} entries")
    for key, value in params.items():
        scalar = isinstance(value, bool | int | float) or value is None or _text(value)
        if not _text(key) or not scalar or (isinstance(value, float) and not math.isfinite(value)):
            raise BadRequest(f"params values must be numbers, booleans or short text ({key!r})")
    return str(task), kind, dict(params)


def _text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_TEXT and not _CONTROL.search(value)


def registration_id(task: str, kind: ConditionKind, params: Mapping[str, Any]) -> str:
    canonical = json.dumps([task, kind.value, params], sort_keys=True, separators=(",", ":"))
    return "registered:" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


@dataclass(slots=True)
class ConditionRegistry:
    """The endpoint and the snapshot step that attaches what was registered."""

    settings: ConditionSettings
    path: Path | str
    clock: Callable[[], datetime]
    token: str = field(repr=False, default="")
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _db: sqlite3.Connection | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.token:
            raise ValueError("a condition registry needs its token")

    def _conn(self) -> sqlite3.Connection:
        if self._db is None:
            prepare_db(self.path)
            db = sqlite3.connect(str(self.path), check_same_thread=False)
            db.execute("PRAGMA busy_timeout = 5000")
            db.executescript(_SCHEMA)
            self._db = db
        return self._db

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    # --- the endpoint -----------------------------------------------------------------

    def authorized(self, header: str | None) -> bool:
        scheme, _, credentials = (header or "").strip().partition(" ")
        given = credentials.strip() if scheme.lower() == "bearer" else ""
        # Compared in constant time whatever was sent (an empty one too).
        return hmac.compare_digest(given.encode(), self.token.encode()) and bool(given)

    def precheck(self, headers: Any) -> Answer | None:
        """What the headers alone decide, before the body is read; None: read it."""
        if not self.authorized(headers.get("Authorization")):
            return Answer(401, {"error": "missing or invalid bearer token"})
        if headers.get("Origin") is not None:
            return Answer(403, {"error": "requests from a web page are refused"})
        raw = headers.get("Content-Length")
        if raw is None:
            return Answer(411, {"error": "Content-Length required"})
        try:
            length = int(raw)
        except ValueError:
            return Answer(400, {"error": "bad Content-Length"})
        if length < 0:
            return Answer(400, {"error": "bad Content-Length"})
        if length > MAX_BODY:
            return Answer(413, {"error": f"body over {MAX_BODY} bytes"})
        media = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if media != "application/json":
            return Answer(415, {"error": "use Content-Type: application/json"})
        return None

    def register(self, body: bytes) -> Answer:
        """Store one registration (the headers were checked)."""
        try:
            task, kind, params = parse(body)
        except BadRequest as exc:
            return Answer(400, {"error": str(exc)})
        ident = registration_id(task, kind, params)
        now = self.clock()
        with self._lock:
            db = self._conn()
            with db:
                if db.execute(
                    "SELECT 1 FROM registered_conditions WHERE id = ?", (ident,)
                ).fetchone():
                    return Answer(200, {"id": ident, "duplicate": True})
                live = db.execute("SELECT COUNT(*) FROM registered_conditions").fetchone()[0]
                if live >= self.settings.max_registered:
                    return Answer(507, {"error": "too many registered conditions"})
                db.execute(
                    "INSERT INTO registered_conditions VALUES (?, ?, ?, ?, ?)",
                    (ident, task, kind.value, json.dumps(params, sort_keys=True), _ts(now)),
                )
        log.info("condition %s registered for task %s (%s)", ident, task[:MAX_TEXT], kind)
        return Answer(201, {"id": ident, "duplicate": False})

    # --- the snapshot step ------------------------------------------------------------

    def __call__(self, snapshot: Snapshot) -> Snapshot:
        """Attach the live registrations to their tasks (after dropping those of finished
        tasks and those past ``max_age``); never raises: a database it cannot read attaches
        nothing this tick."""
        try:
            rows = self._live(snapshot)
        except (OSError, sqlite3.Error) as exc:
            log.warning("registered conditions not read: %s", exc)
            return snapshot
        known = {c.id for c in snapshot.conditions}
        found = [
            Condition(
                id=ident,
                kind=ConditionKind(kind),
                task_id=task,
                params=json.loads(params),
                registered_at=datetime.fromisoformat(at),
            )
            for ident, task, kind, params, at in rows
            if ident not in known and kind in ConditionKind._value2member_map_
        ]
        if not found:
            return snapshot
        return replace(snapshot, conditions=(*snapshot.conditions, *found))

    def _live(self, snapshot: Snapshot) -> list[tuple[str, str, str, str, str]]:
        finished = [t.id for t in snapshot.tasks if t.status.value in _TERMINAL]
        oldest = _ts(snapshot.now - self.settings.max_age)
        with self._lock:
            db = self._conn()
            with db:
                db.execute("DELETE FROM registered_conditions WHERE registered_at < ?", (oldest,))
                for i in range(0, len(finished), 500):
                    chunk = finished[i : i + 500]
                    marks = ", ".join("?" * len(chunk))
                    db.execute(
                        f"DELETE FROM registered_conditions WHERE task_id IN ({marks})", chunk
                    )
            return db.execute(
                "SELECT id, task_id, kind, params, registered_at FROM registered_conditions"
                " ORDER BY registered_at, id"
            ).fetchall()


def _ts(at: datetime) -> str:
    return at.astimezone(UTC).isoformat(timespec="microseconds")
