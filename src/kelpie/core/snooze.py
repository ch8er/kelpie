"""Snoozes: a person's acknowledgement that a task waits on purpose until a date.

Pure functions only: the ``[snooze]`` settings, the ``--until`` value of ``kelpie snooze``,
the ``until:`` marker an agent may write into a block reason to record a person's decision,
and how a snooze is shown. The snoozes themselves are stored in Kelpie's database
(:class:`kelpie.core.storage.SqliteLedgerStore`) and put into ``Snapshot.snoozes`` when the
snapshot is assembled (:func:`with_snoozes`).

Times, for ``--until`` and markers alike: a bare date (``2026-10-25``) means through that
day, so the snooze ends at the start of the next day in ``[report] timezone``
(``SnoozeSettings.tz``); a date and time without an offset is in that timezone too; with an
offset (``Z``, ``+02:00``, ``-0500``) it is that instant.

Markers are off unless ``[snooze] marker = true``: any agent can write the words, so they
are trusted only where agents record people's decisions (as ``human_decisions`` is). A
marker is ``until:YYYY-MM-DD`` or ``until:YYYY-MM-DDTHH:MM`` (seconds allowed); with several,
the latest wins. Markers inside double quotes (``"..."``), typographic double or single
quotes (U+201C/U+201D, U+2018/U+2019) or backticks are someone else's words the agent
copied (an error message, a comment) and are not read. A marker that is not a valid date,
or ends more than ``marker_max_days`` after the block was set (``Block.since``), is ignored
for good and reported, never guessed. Rules must treat a marker snooze
(``source == "marker"``) as weaker than a person's: it may only hold the repeat human-wait
alerts (rules 13 and 4), never an agent-health rule, and it is always listed as "from
marker".
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, tzinfo
from types import MappingProxyType
from typing import Any

from kelpie.core.actions import ActionKind
from kelpie.core.config import Mode
from kelpie.core.ledger import LedgerEntry, Outcome, to_json
from kelpie.core.model import Snapshot, Snooze, Task
from kelpie.core.serde import parse_duration

DEFAULT_MAX_DAYS = 90  # how far ahead a snooze may reach, unless [snooze] max_days says less
MAX_DAYS_LIMIT = 365  # the highest [snooze] max_days accepted
DEFAULT_MARKER_MAX_DAYS = 30  # how long after its block a marker may end
# Expired snoozes stay in the snapshot this long, so a rule can tell "it was snoozed and has
# expired" from "never snoozed"; the store prunes them after PRUNE_AFTER.
EXPIRED_KEPT = timedelta(days=7)
PRUNE_AFTER = timedelta(days=30)
MAX_REASON = 500  # characters of a snooze reason that are kept
MAX_BY = 100  # ...and of who set it

_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_DATETIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?")
# A marker token: ``until:`` not glued to a word before it, then everything up to a space.
_MARKER = re.compile(r"(?<![\w:/.-])until:(\S+)", re.IGNORECASE)
_TRAILING = ".,;:!?)]}>'\"\u2019\u201d"  # punctuation a sentence puts after the marker
_QUOTED = re.compile('"[^"]*"|\u201c[^\u201d]*\u201d|\u2018[^\u2019]*\u2019|`[^`]*`')
_DURATION = re.compile(r"(?:\d+[dhms])+")


@dataclass(frozen=True, slots=True)
class SnoozeSettings:
    """``[snooze]``: ``max_days`` caps how far ahead ``kelpie snooze`` may reach;
    ``marker`` turns reading ``until:`` markers in block reasons on; ``marker_max_days``
    caps how long after its block a marker may end. ``tz`` is ``[report] timezone``: where a
    bare date or a time without an offset is read."""

    max_days: int = DEFAULT_MAX_DAYS
    marker: bool = False
    marker_max_days: int = DEFAULT_MARKER_MAX_DAYS
    tz: tzinfo = UTC

    @property
    def max_ahead(self) -> timedelta:
        return timedelta(days=self.max_days)

    @property
    def marker_max(self) -> timedelta:
        return timedelta(days=self.marker_max_days)


def _days(raw: Mapping[str, Any], key: str, default: int, top: int) -> int:
    value = raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"[snooze] {key} must be a whole number of days")
    if not 1 <= value <= top:
        raise ValueError(f"[snooze] {key} must be between 1 and {top}")
    return value


def load_snooze(raw: Any, tz: tzinfo = UTC) -> SnoozeSettings:
    """``[snooze]`` from the config (``tz``: ``[report] timezone``); ValueError naming the
    key on a bad value."""
    if raw is None:
        return SnoozeSettings(tz=tz)
    if not isinstance(raw, Mapping):
        raise ValueError("[snooze] must be a table")
    if extra := set(raw) - {"max_days", "marker", "marker_max_days"}:
        raise ValueError(f"unknown [snooze] keys: {sorted(extra)}")
    max_days = _days(raw, "max_days", DEFAULT_MAX_DAYS, MAX_DAYS_LIMIT)
    marker_max_days = _days(
        raw, "marker_max_days", min(DEFAULT_MARKER_MAX_DAYS, max_days), max_days
    )
    marker = raw.get("marker", False)
    if not isinstance(marker, bool):
        raise ValueError("[snooze] marker must be true or false")
    return SnoozeSettings(max_days, marker, marker_max_days, tz)


def _resolve(value: str, tz: tzinfo) -> datetime:
    """A date (through that day: the start of the next one) or a date and time, in ``tz``
    unless it has an offset. ValueError or OverflowError when it is not one."""
    at = datetime.fromisoformat(value)
    if _DATE.fullmatch(value):
        # The start of the next day on the wall clock there, whatever a DST change does.
        at = (at + timedelta(days=1)).replace(tzinfo=tz)
    elif at.tzinfo is None:
        at = at.replace(tzinfo=tz)
    at.astimezone(UTC)  # out of range (year 9999 with an offset) raises here, not later
    return at


def parse_until(text: str, now: datetime, tz: tzinfo = UTC) -> datetime:
    """``--until`` of ``kelpie snooze``: a duration from ``now`` (``3d``, ``1d12h``), a date
    (through that day) or an ISO date and time; without an offset in ``tz``. ValueError
    when it is none of these or out of range."""
    value = text.strip()
    try:
        if _DURATION.fullmatch(value):
            return now + parse_duration(value)
        return _resolve(value, tz)
    except OverflowError:
        raise ValueError(f"--until {text!r} is out of range") from None
    except ValueError:
        raise ValueError(
            f"--until must be a date (2026-10-25: through that day), a date and time "
            f"(2026-10-25T09:00) or a duration (3d, 12h), got {text!r}"
        ) from None


def is_date(text: str) -> bool:
    """Whether ``--until`` (or a marker) is a bare date, which means through that day."""
    return _DATE.fullmatch(text.strip()) is not None


def _past_cap(
    until: datetime, start: datetime, days: int, through_day: bool, tz: tzinfo
) -> str | None:
    """None when ``until`` is at most ``days`` after ``start``, else the last allowed date
    (``through_day``: a bare date, compared by date in ``tz``, so the date exactly ``days``
    ahead is allowed) or time, as shown to a person."""
    if through_day:
        last = until.astimezone(tz).date() - timedelta(days=1)  # the day it runs through
        limit = start.astimezone(tz).date() + timedelta(days=days)
        return None if last <= limit else f"the last date allowed is {limit.isoformat()}"
    latest = start + timedelta(days=days)
    return None if until <= latest else f"the latest allowed is {format_when(latest, tz)}"


def until_problem(
    until: datetime, now: datetime, settings: SnoozeSettings, through_day: bool = False
) -> str | None:
    """Why ``kelpie snooze`` cannot reach ``until``, or None: it must lie in the future and
    at most ``max_days`` ahead. ``through_day``: ``--until`` was a bare date, so the date
    exactly ``max_days`` after today (in ``[report] timezone``) is still allowed."""
    if until <= now:
        return "is not in the future"
    if over := _past_cap(until, now, settings.max_days, through_day, settings.tz):
        return f"is more than {settings.max_days} days ahead ([snooze] max_days); {over}"
    return None


def format_when(at: datetime, tz: tzinfo = UTC) -> str:
    """How a snooze's time is shown everywhere (``kelpie why``, ``kelpie snoozes``): in UTC,
    and first in ``tz`` when that is not UTC there."""
    utc = at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    local = at.astimezone(tz)
    if local.utcoffset() == timedelta(0):
        return utc
    return f"{local:%Y-%m-%d %H:%M %Z} ({utc})"


def snooze_end(at: datetime, tz: tzinfo = UTC) -> tuple[str, bool]:
    """How a snooze's end reads to a person (alerts, the daily report), and whether it is a
    whole day: at midnight in ``tz`` it is the last day it covered, as ``--until
    2026-10-25`` takes a date (``("2026-10-25", True)``: through that day); else the time to
    the minute, with the zone (``("2026-10-25 09:00 UTC", False)``)."""
    local = at.astimezone(tz)
    if (local.hour, local.minute, local.second, local.microsecond) == (0, 0, 0, 0):
        return (local.date() - timedelta(days=1)).isoformat(), True
    zone = "UTC" if local.utcoffset() == timedelta(0) else local.tzname() or local.strftime("%z")
    return f"{local:%Y-%m-%d %H:%M} {zone}", False


def snooze_json(s: Snooze, now: datetime, tz: tzinfo = UTC) -> dict[str, Any]:
    """A snooze as ``--json`` shows it (``kelpie why``, ``kelpie snoozes``)."""
    return {
        "task_id": s.task_id,
        "until": s.until.isoformat(),
        "until_text": format_when(s.until, tz),
        "active": s.active(now),
        "reason": s.reason,
        "by": s.by,
        "source": s.source,
        "created_at": s.created_at.isoformat(),
        "behind_at_snooze": s.behind_at_snooze,
        "grown_at": None if s.grown_at is None else s.grown_at.isoformat(),
        "grown_by": s.grown_by,
    }


def _marker_time(token: str, tz: tzinfo) -> datetime | None:
    if not (_DATE.fullmatch(token) or _DATETIME.fullmatch(token)):
        return None
    if token.endswith("Z"):
        token = token[:-1] + "+00:00"
    try:
        return _resolve(token, tz)
    except (ValueError, OverflowError):
        return None


def _markers(reason: str, tz: tzinfo) -> tuple[tuple[datetime, str] | None, tuple[str, ...]]:
    """The latest valid marker (its time and its token as written) and the invalid tokens."""
    text = _QUOTED.sub(" ", reason)
    latest: tuple[datetime, str] | None = None
    invalid: list[str] = []
    for m in _MARKER.finditer(text):
        token = m[1].rstrip(_TRAILING)
        at = _marker_time(token, tz)
        if at is None:
            invalid.append(f"until:{token}")
        elif latest is None or at > latest[0]:
            latest = (at, token)
    return latest, tuple(invalid)


def parse_marker(reason: str, tz: tzinfo = UTC) -> tuple[datetime | None, tuple[str, ...]]:
    """The latest valid ``until:`` time in a block reason (None: no valid marker), and the
    marker tokens that are not a valid date. Quoted text is skipped."""
    latest, invalid = _markers(reason, tz)
    return (None if latest is None else latest[0]), invalid


@dataclass(frozen=True, slots=True)
class MarkerProblem:
    """A marker that was ignored: ``token`` as written, ``why`` it was ignored."""

    task_id: str
    token: str
    why: str


def marker_snooze(
    task: Task, now: datetime, settings: SnoozeSettings
) -> tuple[Snooze | None, tuple[MarkerProblem, ...]]:
    """The snooze a task's block reason records, if any (whether or not markers are on). A
    marker ending more than ``marker_max_days`` after the block was set is never honoured (a
    bare date is compared by date, so the date exactly ``marker_max_days`` after the block's
    is allowed); one that expired more than :data:`EXPIRED_KEPT` ago is dropped quietly, like
    an old stored snooze."""
    if task.block is None:
        return None, ()
    latest, invalid = _markers(task.block.reason, settings.tz)
    problems = [MarkerProblem(task.id, t, "not a valid date") for t in invalid]
    snooze = None
    if latest is not None:
        until, token = latest
        days = settings.marker_max_days
        if over := _past_cap(until, task.block.since, days, is_date(token), settings.tz):
            why = (
                f"ends more than {days} days after the block was set "
                f"([snooze] marker_max_days); {over}"
            )
            problems.append(MarkerProblem(task.id, f"until:{token}", why))
        elif until > now - EXPIRED_KEPT:
            snooze = Snooze(
                task_id=task.id,
                until=until,
                reason=None,
                by=None,
                created_at=task.block.since,
                source="marker",
            )
    return snooze, tuple(problems)


def marker_snoozes(
    snapshot: Snapshot, settings: SnoozeSettings
) -> tuple[dict[str, Snooze], tuple[MarkerProblem, ...]]:
    """The snoozes the board's block reasons record, by task id, and the ignored markers;
    nothing when ``[snooze] marker`` is off."""
    found: dict[str, Snooze] = {}
    problems: list[MarkerProblem] = []
    if not settings.marker:
        return found, ()
    for task in snapshot.tasks:
        snooze, bad = marker_snooze(task, snapshot.now, settings)
        problems += bad
        if snooze is not None:
            found[task.id] = snooze
    return found, tuple(problems)


def with_snoozes(
    snapshot: Snapshot, stored: Mapping[str, Snooze], settings: SnoozeSettings
) -> tuple[Snapshot, tuple[MarkerProblem, ...]]:
    """``snapshot`` with ``Snapshot.snoozes`` filled: the stored snoozes (``kelpie snooze``,
    chat) that are active or expired within :data:`EXPIRED_KEPT`, and the markers of the
    block reasons. A stored snooze wins over a marker for the same task, expired or not: a
    person's own word is the later decision."""
    markers, problems = marker_snoozes(snapshot, settings)
    kept = snapshot.now - EXPIRED_KEPT
    merged = {**markers, **{k: s for k, s in stored.items() if s.until > kept}}
    return replace(snapshot, snoozes=MappingProxyType(merged)), problems


# --- a snooze from a chat button (#176) ------------------------------------------------------

CHAT_DAYS = (1, 3, 7)  # the buttons a chat offers
CHAT_MAX_DAYS = 7  # the longest snooze a chat button can set, whatever [snooze] max_days says
CHAT_RULE = "chat"  # the ledger's rule for a press: a person acted, no rule did


def chat_snooze(
    task_id: str,
    days: int,
    by: str,
    user_id: int,
    channel: str,
    now: datetime,
    settings: SnoozeSettings,
    dry_run: bool = False,
) -> tuple[Snooze, LedgerEntry]:
    """The snooze a chat button sets and the ledger entry that records the press: who
    (``by``, the person's chat name, and ``user_id``), what (the task) and until when.
    ``days`` is one of :data:`CHAT_DAYS`, capped at :data:`CHAT_MAX_DAYS` and ``[snooze]
    max_days``; the snooze runs from ``now``. Stored like ``kelpie snooze``'s (one per task,
    replacing the old one) with ``source="chat"``, so ``kelpie serve`` records its growth
    baseline as for any stored snooze and it ends early when more work is blocked behind
    the task. The entry (kind ``snooze``, rule ``chat``, outcome ``executed``) counts
    against no task's rate limit: its subject is the channel. A person's snooze is stored
    under the global dry-run too, as ``kelpie snooze``'s is (it only quiets alerts); with
    ``dry_run`` the entry says so (``params["dry_run"]`` and its reason)."""
    if days not in CHAT_DAYS:
        raise ValueError(f"a chat snooze is one of {CHAT_DAYS} days, got {days!r}")
    task_id = task_id.strip()
    if not task_id:
        raise ValueError("the task id is empty")
    until = now + timedelta(days=min(days, CHAT_MAX_DAYS, settings.max_days))
    who = by.strip()[:MAX_BY] or f"user {user_id}"
    snooze = Snooze(
        task_id=task_id, until=until, reason=None, by=who, created_at=now, source="chat"
    )
    params = {
        "until": until.isoformat(),
        "days": days,
        "by": who,
        "user_id": user_id,
        "channel": channel,
        "source": "chat",
    }
    reason = (
        f"{who} (user id {user_id}) snoozed {task_id} until {until.isoformat()} "
        f"from {channel} ({days}d button)."
    )
    if dry_run:
        params["dry_run"] = True
        reason += " Stored under the global dry-run, as kelpie snooze is: it only quiets alerts."
    raw = to_json(["chat_snooze", channel, task_id, user_id, now.isoformat()])
    entry = LedgerEntry(
        at=now,
        fingerprint=hashlib.sha256(raw.encode()).hexdigest()[:32],
        subject=f"chat:{channel}",
        rule=CHAT_RULE,
        kind=ActionKind.SNOOZE,
        mode=Mode.ACT,
        outcome=Outcome.EXECUTED,
        reason=reason,
        task_id=task_id,
        executor=channel,
        params=params,
    )
    return snooze, entry
