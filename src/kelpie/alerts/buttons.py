"""Snoozing from the alert itself: Telegram buttons and the poller that takes presses (#176).

This is an inbound control channel, so it is built to do one small thing and refuse the
rest. Off by default (``buttons = true`` on a Telegram channel turns it on).

**What a press can do.** Snooze one task for 1, 3 or 7 days, nothing else: the only action
code is ``s`` (snooze) and the only durations are :data:`~kelpie.core.snooze.CHAT_DAYS`. A
press can never unblock, close, stop or requeue a task, or change a mode. The snooze is
stored like ``kelpie snooze``'s with ``source = "chat"``
(:func:`kelpie.core.snooze.chat_snooze`, written through the ``on_snooze`` callback the app
wires to its store), so it ends early when more work is blocked behind the task, and every
press is recorded in the ledger (who, what, until).

**Who may press.** A ``callback_query`` is taken only when its sender's user id is in
``allowed_user_ids`` **and** the message it came from is in the configured ``chat_id``.
Everything else is ignored and counted (``kelpie_chat_rejected_total{reason}``):
``unknown_user``, ``wrong_chat``, ``bad_ref`` (unknown, forged, truncated or for another
chat), ``expired_ref``, ``used_ref``, ``rate_limited`` (more than :data:`RATE_LIMIT` presses
by one user within :data:`RATE_WINDOW` seconds), ``malformed`` and ``not_callback`` (any
other update type, which Telegram should not even send: ``allowed_updates`` asks for
callback queries only). A rejected press gets the same neutral toast, "Not allowed.", or,
when rate limited, none: the answer never says why.

**The reference.** ``callback_data`` never carries task text, only a short signed
reference to a record Kelpie keeps (``refs``: the channel, chat, alert, task, item number,
when it was issued and the message it went out in)::

    s<days>.<record id, hex>.<HMAC-SHA256, first 16 bytes, base64url>
    s3.1f.Yj1u6p0mJ8b0KQ3x9qTgZw            # 25 bytes; at most 41 (Telegram allows 64)

The MAC covers a version tag, the chat id, the days and the record id, under a 32-byte key
Kelpie generates on first use and keeps in the button store (never in config, never
logged). It is compared with :func:`hmac.compare_digest`. Record ids are never reused
(``AUTOINCREMENT``), so an old reference can never name a newer record.

**Expiry.** A reference is good for :data:`REF_TTL` (24 hours, the longest escalation step)
after it was issued, and only while it is on the latest message of its alert in that chat:
a newer delivered message of the same alert (its next escalation step, or a change) makes
the older buttons expire. An alert is its long-lived state (``AlertEvent.state``, the same
across escalation steps), else its rule and subject. A reference is used once.

**Replay.** The poller uses ``getUpdates`` long polling only (no webhook) and persists the
update offset, moving it past an update *before* acting on it: a press is taken at most
once, also across restarts. An update below the stored offset is skipped.

**The store** is a small SQLite file next to Kelpie's database
(:func:`button_store_path`, ``kelpie.buttons.db`` beside ``kelpie.db``), created ``0600``
and never through a symlink: the HMAC key, the update offset and the references. Kept
apart from ``kelpie.db``, which other commands read and people share to debug.

**The acknowledgement.** After a press the poller answers the callback (a short toast) and
edits the alert: the message as Telegram holds it, with the same entities (so no markup is
parsed), plus a line "✅ 1. Snoozed T-101 until 2026-10-22 14:05 UTC by Alice", and the
pressed item's buttons removed. The person's name is untrusted text: it is cleaned and its
``@`` broken (:func:`kelpie.alerts.render.one_line`) and, being plain text with explicit
entities, never parsed as HTML. A message that would grow past Telegram's limit only loses
the buttons.

**The poller** runs in its own thread, never in the engine's tick: errors back off
exponentially (from 1 second to :data:`MAX_BACKOFF`, longer when Telegram says how long to
wait), and ``stop`` plus ``join`` hold a deadline: a long poll still in flight then is
abandoned and whatever it returns is ignored.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import sqlite3
import stat
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from kelpie.alerts import i18n, render
from kelpie.alerts.render import (
    DEFAULT_RENDERING,
    MAX_BUTTON,
    MAX_ID,
    MAX_TEXT,
    RenderSettings,
    SnoozeTarget,
    one_line,
    utf16_len,
)
from kelpie.core.alerts import AlertEvent
from kelpie.core.snooze import CHAT_DAYS

log = logging.getLogger(__name__)

REF_TTL = timedelta(hours=24)  # a reference's life: the longest escalation step (4h, 12h, 24h)
PRUNE_AFTER = timedelta(days=7)  # references kept after issue (expired ones say so till then)
RATE_LIMIT = 10  # presses one user may make...
RATE_WINDOW = 60.0  # ...within this many seconds; more are dropped without an answer
POLL_TIMEOUT = 25  # seconds Telegram holds a getUpdates open
UPDATES_LIMIT = 10  # updates per getUpdates
MAX_UPDATES_BYTES = 1024 * 1024
MAX_BACKOFF = 300.0
MAX_ALERT_KEY = 200
MAX_NAME = 40  # characters of a person's name shown in the acknowledgement
MAX_TOAST = 200  # Telegram's limit for an answerCallbackQuery text
ACTION = "s"  # the only action a button carries: snooze
_TAG = b"kelpie/telegram-snooze/v1"
_DATA = re.compile(rf"{ACTION}([0-9])\.([1-9a-f][0-9a-f]{{0,14}})\.([A-Za-z0-9_-]{{22}})")
_ENTITY_TYPES = frozenset(
    {"bold", "italic", "underline", "strikethrough", "code", "pre", "text_link", "url"}
)
OUTCOMES = ("snoozed", "rejected", "failed")
REASONS = (
    "unknown_user",
    "wrong_chat",
    "bad_ref",
    "expired_ref",
    "used_ref",
    "rate_limited",
    "malformed",
    "not_callback",
)


# --- the signed reference ------------------------------------------------------------------


def _mac(key: bytes, chat_id: str, days: int, ref_id: int) -> str:
    message = b"\n".join((_TAG, chat_id.encode(), str(days).encode(), str(ref_id).encode()))
    digest = hmac.new(key, message, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def callback_data(key: bytes, chat_id: str, days: int, ref_id: int) -> str:
    """The ``callback_data`` of a snooze button: ``s<days>.<id hex>.<mac>``, at most 41
    bytes."""
    if days not in CHAT_DAYS or ref_id < 1:
        raise ValueError("a snooze button is 1, 3 or 7 days on a stored reference")
    return f"{ACTION}{days}.{ref_id:x}.{_mac(key, chat_id, days, ref_id)}"


def parse_callback(data: object, key: bytes, chat_id: str) -> tuple[int, int] | None:
    """The reference id and the days of a button's ``callback_data``, or None when it is not
    one Kelpie signed for this chat (malformed, truncated, forged, another chat's)."""
    if not isinstance(data, str):
        return None
    m = _DATA.fullmatch(data)
    if m is None:
        return None
    days, ref_id = int(m[1]), int(m[2], 16)
    if days not in CHAT_DAYS:
        return None
    if not hmac.compare_digest(m[3].encode("ascii"), _mac(key, chat_id, days, ref_id).encode()):
        return None
    return ref_id, days


def _ref_id(data: object) -> int | None:
    """The record id of what looks like a Kelpie button (signature not checked)."""
    m = _DATA.fullmatch(data) if isinstance(data, str) else None
    return int(m[2], 16) if m else None


def alert_key(event: AlertEvent) -> str:
    """What identifies an alert across its escalation steps: its long-lived state, else its
    rule and subject."""
    key = event.state or f"{event.rule}|{event.subject}"
    return key[:MAX_ALERT_KEY]


# --- the store -----------------------------------------------------------------------------


def button_store_path(db_path: Path) -> Path:
    """Where the button store lives: beside Kelpie's database (``kelpie.buttons.db`` next to
    ``kelpie.db``)."""
    return db_path.with_name(f"{db_path.stem}.buttons.db")


@dataclass(frozen=True, slots=True)
class Ref:
    """A stored reference: what a button press would snooze."""

    id: int
    channel: str
    chat_id: str
    alert: str
    task_id: str
    item: int
    issued_at: datetime
    message_id: int | None
    used_at: datetime | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS refs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    channel    TEXT NOT NULL,
    chat_id    TEXT NOT NULL,
    alert      TEXT NOT NULL,
    task_id    TEXT NOT NULL,
    item       INTEGER NOT NULL,
    issued_at  TEXT NOT NULL,
    message_id INTEGER,
    used_at    TEXT
);
CREATE INDEX IF NOT EXISTS refs_alert ON refs (channel, chat_id, alert, issued_at);
CREATE INDEX IF NOT EXISTS refs_issued ON refs (issued_at);
"""
_KEY = "hmac_key"


def _ts(at: datetime) -> str:
    if at.tzinfo is None:
        raise ValueError("button store times must be timezone-aware")
    return at.astimezone(UTC).isoformat(timespec="microseconds")


def _private_file(path: Path) -> None:
    """Create ``path`` with mode 0600 (or tighten it), never through a symlink; ValueError
    when it is not a regular file."""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        mode = os.fstat(fd).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError(f"{path} is not a regular file")
        if mode & 0o077:
            os.fchmod(fd, 0o600)
            log.warning("button store %s was readable by others; set to 0600", path)
    finally:
        os.close(fd)


class ButtonStore:
    """The HMAC key, the update offsets and the references, in one SQLite file (or in
    memory with ``":memory:"``). Thread-safe: the alert worker issues references while the
    poller reads them."""

    def __init__(self, path: Path | str) -> None:
        if str(path) != ":memory:":
            _private_file(Path(path))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA busy_timeout = 5000")
        with self._db:
            self._db.executescript(_SCHEMA)
        self._key: bytes | None = None

    def __repr__(self) -> str:  # never the key
        return "ButtonStore()"

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def key(self) -> bytes:
        """The HMAC key, generated on first use (32 random bytes) and kept."""
        if self._key is None:
            with self._lock, self._db:
                self._db.execute(
                    "INSERT OR IGNORE INTO meta (name, value) VALUES (?, ?)",
                    (_KEY, secrets.token_hex(32)),
                )
                row = self._db.execute("SELECT value FROM meta WHERE name = ?", (_KEY,)).fetchone()
            self._key = bytes.fromhex(row["value"])
        return self._key

    def offset(self, channel: str) -> int:
        """The next update id to ask for (0: none taken yet)."""
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM meta WHERE name = ?", (f"offset:{channel}",)
            ).fetchone()
        try:
            return max(int(row["value"]), 0) if row is not None else 0
        except ValueError:
            return 0

    def set_offset(self, channel: str, offset: int) -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO meta (name, value) VALUES (?, ?)",
                (f"offset:{channel}", str(offset)),
            )

    def issue(
        self,
        channel: str,
        chat_id: str,
        alert: str,
        targets: Sequence[SnoozeTarget],
        at: datetime,
    ) -> list[int]:
        """Store a reference per target (issued ``at``) and return their ids; references
        issued more than :data:`PRUNE_AFTER` ago are deleted."""
        ids = []
        with self._lock, self._db:
            self._db.execute("DELETE FROM refs WHERE issued_at < ?", (_ts(at - PRUNE_AFTER),))
            for t in targets:
                cur = self._db.execute(
                    "INSERT INTO refs (channel, chat_id, alert, task_id, item, issued_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (channel, chat_id, alert, t.task_id, t.number, _ts(at)),
                )
                ids.append(int(cur.lastrowid or 0))
        return ids

    def bind(self, ids: Sequence[int], message_id: int) -> None:
        """The message the references went out in."""
        with self._lock, self._db:
            self._db.executemany(
                "UPDATE refs SET message_id = ? WHERE id = ? AND message_id IS NULL",
                [(message_id, i) for i in ids],
            )

    def get(self, ref_id: int) -> Ref | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM refs WHERE id = ?", (ref_id,)).fetchone()
        if row is None:
            return None
        return Ref(
            row["id"],
            row["channel"],
            row["chat_id"],
            row["alert"],
            row["task_id"],
            row["item"],
            datetime.fromisoformat(row["issued_at"]),
            row["message_id"],
            None if row["used_at"] is None else datetime.fromisoformat(row["used_at"]),
        )

    def superseded(self, ref: Ref) -> bool:
        """Whether a newer message of the same alert went out to the same chat since."""
        with self._lock:
            row = self._db.execute(
                "SELECT 1 FROM refs WHERE channel = ? AND chat_id = ? AND alert = ?"
                " AND issued_at > ? AND message_id IS NOT NULL"
                " AND message_id IS NOT ? LIMIT 1",
                (ref.channel, ref.chat_id, ref.alert, _ts(ref.issued_at), ref.message_id),
            ).fetchone()
        return row is not None

    def use(self, ref_id: int, at: datetime) -> bool:
        """Mark a reference used; False when it already was."""
        with self._lock, self._db:
            cur = self._db.execute(
                "UPDATE refs SET used_at = ? WHERE id = ? AND used_at IS NULL", (_ts(at), ref_id)
            )
        return cur.rowcount > 0


# --- the buttons on an alert ---------------------------------------------------------------


class Buttons:
    """Puts snooze buttons on a Telegram alert: one stored, signed reference per item a
    snooze would quiet, three buttons each (1, 3, 7 days)."""

    def __init__(
        self,
        store: ButtonStore,
        channel: str,
        chat_id: str,
        clock: Callable[[], datetime],
        language: str = i18n.DEFAULT,
    ) -> None:
        self.store = store
        self.channel = channel
        self.chat_id = chat_id
        self.clock = clock
        self.language = language

    def attach(
        self, event: AlertEvent, targets: Sequence[SnoozeTarget]
    ) -> tuple[dict[str, Any] | None, list[int]]:
        """The ``reply_markup`` for ``targets`` and the reference ids to bind once sent."""
        if not targets:
            return None, []
        ids = self.store.issue(self.channel, self.chat_id, alert_key(event), targets, self.clock())
        key = self.store.key()
        rows = [
            (t, [(d, callback_data(key, self.chat_id, d, i)) for d in CHAT_DAYS])
            for t, i in zip(targets, ids, strict=True)
        ]
        return render.keyboard(rows, self.language), ids

    def bind(self, ids: Sequence[int], message_id: int) -> None:
        self.store.bind(ids, message_id)


# --- the poller ----------------------------------------------------------------------------


class ApiError(Exception):
    """A Bot API call that failed. ``cause`` is a short metric label; the text is safe to
    log (never the token); ``retry_after`` is what Telegram asked for, in seconds."""

    def __init__(self, cause: str, detail: str = "", retry_after: float | None = None) -> None:
        super().__init__(detail or cause)
        self.cause = cause
        self.retry_after = retry_after


class BotApi(Protocol):
    """A Telegram Bot API method call: its ``result``, or :class:`ApiError`."""

    def call(
        self,
        method: str,
        body: Mapping[str, Any],
        timeout: float | None = None,
        max_bytes: int | None = None,
    ) -> Any: ...


class ChatObserver(Protocol):
    def chat_action(self, channel: str, outcome: str) -> None: ...

    def chat_rejected(self, channel: str, reason: str) -> None: ...

    def chat_api_error(self, channel: str, call: str, cause: str) -> None: ...


@dataclass(frozen=True, slots=True)
class Press:
    """An accepted press: snooze ``task_id`` for ``days``, by the person with ``user_id``
    (``name``: their chat name, cleaned)."""

    channel: str
    task_id: str
    days: int
    user_id: int
    name: str


# Writes the snooze and its ledger entry at ``now``; returns when it ends. Raises on failure.
SnoozeSink = Callable[[Press, datetime], datetime]


@dataclass(slots=True)
class ChatControl:
    """What the app hands the alert layer to take presses: the button store, the callback
    that writes a snooze (to Kelpie's store and ledger), the clock and the metrics."""

    store: ButtonStore
    on_snooze: SnoozeSink
    clock: Callable[[], datetime]
    observer: ChatObserver | None = None
    poll_timeout: int = POLL_TIMEOUT
    backoff: float = 1.0


class _RateLimit:
    """At most ``limit`` events per key within ``window`` seconds."""

    def __init__(
        self, limit: int, window: float, clock: Callable[[], float], max_keys: int = 1000
    ) -> None:
        self.limit, self.window, self.clock, self.max_keys = limit, window, clock, max_keys
        self._seen: dict[int, deque[float]] = {}

    def allow(self, key: int) -> bool:
        now = self.clock()
        seen = self._seen.setdefault(key, deque())
        while seen and now - seen[0] >= self.window:
            seen.popleft()
        if len(seen) >= self.limit:
            return False
        seen.append(now)
        if len(self._seen) > self.max_keys:  # forget the idle ones
            for k in [k for k, v in self._seen.items() if not v or now - v[-1] >= self.window]:
                del self._seen[k]
        return True


class _Rejected(Exception):
    def __init__(self, reason: str, answer: bool = True) -> None:
        super().__init__(reason)
        self.reason = reason
        self.answer = answer


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


class ButtonPoller:
    """Takes button presses for one Telegram channel with ``getUpdates`` long polling, in
    its own thread (see the module docstring for the rules it enforces)."""

    def __init__(
        self,
        api: BotApi,
        control: ChatControl,
        *,
        channel: str,
        chat_id: str,
        allowed: frozenset[int],
        rendering: RenderSettings = DEFAULT_RENDERING,
        request_timeout: float = 10.0,
        max_backoff: float = MAX_BACKOFF,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.control = control
        self.store = control.store
        self.channel = channel
        self.chat_id = chat_id
        self.allowed = allowed
        self.rendering = rendering
        self.request_timeout = request_timeout
        self.max_backoff = max_backoff
        self.limiter = _RateLimit(RATE_LIMIT, RATE_WINDOW, monotonic)
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self.abandoned = False

    def __repr__(self) -> str:
        return f"ButtonPoller(channel={self.channel!r}, chat_id={self.chat_id!r})"

    # --- lifecycle -------------------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, name=f"kelpie-buttons-{self.channel}", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Ask the poller to stop; it takes no new press from now on."""
        self._stopping.set()

    def join(self, deadline: float) -> None:
        """Wait for the thread until ``deadline`` (monotonic); a long poll still in flight
        then is abandoned and whatever it returns is ignored."""
        self._stopping.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(max(deadline - time.monotonic(), 0.0))
        if thread.is_alive():
            self.abandoned = True
            log.info("button poller %s: a long poll was still open at shutdown", self.channel)

    def close(self) -> None:
        self.store.close()

    def _run(self) -> None:
        failures = 0
        while not self._stopping.is_set():
            try:
                updates = self._fetch()
                failures = 0
                for update in updates:
                    if self._stopping.is_set():
                        return
                    self._take(update)
            except ApiError as exc:
                if self._stopping.is_set():
                    return
                failures += 1
                self._api_error("getUpdates", exc)
                wait = min(self.control.backoff * 2 ** min(failures - 1, 20), self.max_backoff)
                if exc.retry_after is not None:
                    wait = max(wait, min(exc.retry_after, 3600.0))
                self._stopping.wait(wait)
            except Exception as exc:  # a store failure or a bug: back off, never die quietly
                if self._stopping.is_set():
                    return
                failures += 1
                log.error("button poller %s failed: %s", self.channel, type(exc).__name__)
                self._notify("chat_api_error", self.channel, "poller", "error")
                self._stopping.wait(
                    min(self.control.backoff * 2 ** min(failures - 1, 20), self.max_backoff)
                )

    def _fetch(self) -> list[Mapping[str, Any]]:
        body: dict[str, Any] = {
            "timeout": self.control.poll_timeout,
            "allowed_updates": ["callback_query"],
            "limit": UPDATES_LIMIT,
        }
        offset = self.store.offset(self.channel)
        if offset:
            body["offset"] = offset
        timeout = self.control.poll_timeout + self.request_timeout
        result = self.api.call("getUpdates", body, timeout, MAX_UPDATES_BYTES)
        if not isinstance(result, list):
            raise ApiError("malformed", "getUpdates did not return a list")
        found = [u for u in result if isinstance(u, Mapping) and _int(u.get("update_id"))]
        return sorted(found, key=lambda u: u["update_id"])

    # --- one update ------------------------------------------------------------------------

    def _take(self, update: Mapping[str, Any]) -> None:
        update_id = update["update_id"]
        if self.abandoned or self._stopping.is_set():
            return  # shutting down: left for the next start, which asks for it again
        if update_id < self.store.offset(self.channel):
            return  # taken before (a replay): never twice
        # Past it before acting: a press is taken at most once, also across a restart.
        self.store.set_offset(self.channel, update_id + 1)
        query = update.get("callback_query")
        if not isinstance(query, Mapping):
            self._reject("not_callback")
            return
        query_id = query.get("id")
        try:
            press, ref, message = self._check(query)
        except _Rejected as exc:
            self._reject(exc.reason, query_id if exc.answer else None)
            return
        self._snooze(query_id, press, ref, message)

    def _check(self, query: Mapping[str, Any]) -> tuple[Press, Ref, Mapping[str, Any]]:
        """The press, its reference and its message, or :class:`_Rejected` with why."""
        sender = query.get("from")
        user_id = _int(sender.get("id")) if isinstance(sender, Mapping) else None
        if user_id is None or not isinstance(query.get("id"), str):
            raise _Rejected("malformed", answer=isinstance(query.get("id"), str))
        if not self.limiter.allow(user_id):
            raise _Rejected("rate_limited", answer=False)
        if user_id not in self.allowed:
            raise _Rejected("unknown_user")
        message = query.get("message")
        chat = message.get("chat") if isinstance(message, Mapping) else None
        if not isinstance(message, Mapping) or not isinstance(chat, Mapping):
            raise _Rejected("wrong_chat")
        if not self._our_chat(chat):
            raise _Rejected("wrong_chat")
        parsed = parse_callback(query.get("data"), self.store.key(), self.chat_id)
        if parsed is None:
            raise _Rejected("bad_ref")
        ref_id, days = parsed
        ref = self.store.get(ref_id)
        if ref is None or ref.channel != self.channel or ref.chat_id != self.chat_id:
            raise _Rejected("bad_ref")
        message_id = _int(message.get("message_id"))
        if ref.message_id is not None and ref.message_id != message_id:
            raise _Rejected("bad_ref")
        now = self.control.clock()
        if not ref.issued_at <= now + timedelta(minutes=5) or now - ref.issued_at > REF_TTL:
            raise _Rejected("expired_ref")
        if self.store.superseded(ref):
            raise _Rejected("expired_ref")
        if ref.used_at is not None:
            raise _Rejected("used_ref")
        assert isinstance(sender, Mapping)  # its id was read above
        name = _name(sender) or f"user {user_id}"
        return Press(self.channel, ref.task_id, days, user_id, name), ref, message

    def _our_chat(self, chat: Mapping[str, Any]) -> bool:
        chat_id = _int(chat.get("id"))
        if chat_id is not None and str(chat_id) == self.chat_id:
            return True
        username = chat.get("username")
        return (
            self.chat_id.startswith("@")
            and isinstance(username, str)
            and f"@{username}".casefold() == self.chat_id.casefold()
        )

    def _snooze(self, query_id: object, press: Press, ref: Ref, message: Mapping[str, Any]) -> None:
        now = self.control.clock()
        lang = self.rendering.language
        try:
            until = self.control.on_snooze(press, now)
            self.store.use(ref.id, now)
        except Exception as exc:
            log.error(
                "telegram button: snoozing %s for user id %d failed: %s",
                press.task_id,
                press.user_id,
                type(exc).__name__,
            )
            self._notify("chat_action", self.channel, "failed")
            self._answer(query_id, i18n.text(lang, "chat.failed"))
            return
        log.info(
            "telegram button: user id %d snoozed %s until %s",
            press.user_id,
            press.task_id,
            until.isoformat(),
        )
        self._notify("chat_action", self.channel, "snoozed")
        task = one_line(press.task_id, MAX_ID)
        span = render.snooze_span(lang, until, self.rendering.tz)
        self._answer(query_id, i18n.text(lang, "chat.toast", task=task, span=span))
        line = i18n.text(lang, "chat.snoozed", task=task, span=span, who=press.name)
        self._edit(message, ref, f"✅ {ref.item}. {line}")

    def _answer(self, query_id: object, text: str) -> None:
        if not isinstance(query_id, str):
            return
        body = {"callback_query_id": query_id, "text": text[:MAX_TOAST]}
        try:
            self.api.call("answerCallbackQuery", body)
        except ApiError as exc:
            self._api_error("answerCallbackQuery", exc)

    def _edit(self, message: Mapping[str, Any], ref: Ref, line: str) -> None:
        """The alert, acknowledged: the line added under its text (same entities, nothing
        parsed) and the pressed item's buttons gone. Too long a message only loses them."""
        chat = message["chat"]
        target = {"chat_id": chat["id"], "message_id": message.get("message_id")}
        markup = _remaining(message.get("reply_markup"), ref.id)
        text = message.get("text")
        if isinstance(text, str) and text and utf16_len(f"{text}\n{line}") <= MAX_TEXT:
            body: dict[str, Any] = {
                **target,
                "text": f"{text}\n{line}",
                "link_preview_options": {"is_disabled": True},
            }
            if entities := _entities(message.get("entities")):
                body["entities"] = entities
            if markup["inline_keyboard"]:
                body["reply_markup"] = markup
            method = "editMessageText"
        else:
            body = {**target, "reply_markup": markup}
            method = "editMessageReplyMarkup"
        try:
            self.api.call(method, body)
        except ApiError as exc:
            self._api_error(method, exc)

    # --- counting --------------------------------------------------------------------------

    def _reject(self, reason: str, query_id: object = None) -> None:
        log.info("telegram button press ignored (%s)", reason)
        self._notify("chat_action", self.channel, "rejected")
        self._notify("chat_rejected", self.channel, reason)
        if query_id is not None:
            self._answer(query_id, i18n.text(self.rendering.language, "chat.not_allowed"))

    def _api_error(self, call: str, exc: ApiError) -> None:
        log.warning("telegram %s failed for channel %s: %s", call, self.channel, exc)
        self._notify("chat_api_error", self.channel, call, exc.cause)

    def _notify(self, method: str, *args: str) -> None:
        observer = self.control.observer
        if observer is None:
            return
        try:
            getattr(observer, method)(*args)
        except Exception:
            log.exception("chat observer failed")


def _name(sender: Mapping[str, Any]) -> str:
    """The sender's name as the acknowledgement shows it: untrusted, so cleaned, with ``@``
    broken (no mention) and cut."""
    parts = [sender.get(k) for k in ("first_name", "last_name")]
    name = " ".join(p for p in parts if isinstance(p, str) and p.strip())
    if not name and isinstance(user := sender.get("username"), str):
        name = user
    return one_line(name, MAX_NAME) if name else ""


def _remaining(markup: object, ref_id: int) -> dict[str, Any]:
    """The message's keyboard without the buttons of reference ``ref_id``; only buttons
    that look like Kelpie's are kept."""
    rows = markup.get("inline_keyboard") if isinstance(markup, Mapping) else None
    out: list[list[dict[str, str]]] = []
    for row in rows if isinstance(rows, list) else ():
        kept = []
        for button in row if isinstance(row, list) else ():
            if not isinstance(button, Mapping):
                continue
            data, label = button.get("callback_data"), button.get("text")
            found = _ref_id(data)
            if found is None or found == ref_id or not isinstance(label, str):
                continue
            kept.append({"text": label[:MAX_BUTTON], "callback_data": str(data)})
        if kept:
            out.append(kept)
    return {"inline_keyboard": out}


def _entities(raw: object) -> list[dict[str, Any]]:
    """The message's formatting entities as Telegram reported them, only the kinds Kelpie's
    alerts use, with their known fields."""
    out = []
    for e in raw if isinstance(raw, list) else ():
        if not isinstance(e, Mapping) or e.get("type") not in _ENTITY_TYPES:
            continue
        offset, length = _int(e.get("offset")), _int(e.get("length"))
        if offset is None or length is None or offset < 0 or length <= 0:
            continue
        entity: dict[str, Any] = {"type": e["type"], "offset": offset, "length": length}
        if e["type"] == "text_link":
            url = render.plain_http_url(e.get("url"))
            if url is None:
                continue
            entity["url"] = url
        out.append(entity)
    return out
