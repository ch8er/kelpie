"""``[doctor]``: settings that only ``kelpie doctor`` reads.

``pr_block_acks`` acknowledges a reviewed exception of the "PR blocks have conditions" check
per task (#417): a block whose reason names a PR and reads as waiting on it, but that an
operator checked and knows waits on something no ``[[block_conditions]]`` pattern should
resolve. Each entry names the task, an expiry (``until``: a date, through that day, or a
date and time; without an offset in ``[report] timezone``) and optionally a note. An entry
past its expiry, or ending more than ``MAX_ACK_DAYS`` after the doctor run, acknowledges
nothing and doctor says so, so a reviewed WARN comes back instead of staying hidden.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

MAX_ACK_DAYS = 90  # an acknowledgement ending later than this after the run counts for nothing
MAX_ACKS = 200
MAX_TASK = 200  # characters of a task id
MAX_NOTE = 300  # characters of a note
_ACK_KEYS = frozenset({"task", "until", "note"})


@dataclass(frozen=True, slots=True)
class PrBlockAck:
    """A reviewed PR block on ``task``, acknowledged until ``until`` (aware)."""

    task: str
    until: datetime
    note: str = ""


@dataclass(frozen=True, slots=True)
class DoctorSettings:
    pr_block_acks: tuple[PrBlockAck, ...] = ()


def _until(value: Any, tz: tzinfo, where: str) -> datetime:
    """A TOML date (through that day: the start of the next one in ``tz``), a TOML date and
    time, or the same as an ISO string; without an offset in ``tz``."""
    if isinstance(value, str):
        text = value.strip()
        try:
            value = date.fromisoformat(text) if len(text) == 10 else datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(
                f"{where} until must be a date (2026-12-01: through that day) or a date and "
                f"time (2026-12-01T09:00), got {value!r}"
            ) from None
    if isinstance(value, datetime):
        at = value if value.tzinfo is not None else value.replace(tzinfo=tz)
    elif isinstance(value, date):
        at = datetime.combine(value + timedelta(days=1), datetime.min.time(), tzinfo=tz)
    else:
        raise ValueError(f"{where} until must be a date or a date and time")
    try:
        at.astimezone(UTC)
    except OverflowError:
        raise ValueError(f"{where} until is out of range") from None
    return at


def load_doctor(raw: Any, tz: tzinfo = UTC) -> DoctorSettings:
    """``[doctor]`` from the config (``tz``: ``[report] timezone``); ValueError naming the
    key on a bad value."""
    if raw is None:
        return DoctorSettings()
    if not isinstance(raw, Mapping):
        raise ValueError("[doctor] must be a table")
    if extra := set(raw) - {"pr_block_acks"}:
        raise ValueError(f"unknown [doctor] keys: {sorted(extra)}")
    entries = raw.get("pr_block_acks", [])
    if not isinstance(entries, list):
        raise ValueError("[doctor] pr_block_acks must be a list of tables")
    if len(entries) > MAX_ACKS:
        raise ValueError(f"[doctor] pr_block_acks takes at most {MAX_ACKS} entries")
    acks: dict[str, PrBlockAck] = {}
    for n, entry in enumerate(entries):
        where = f"[doctor] pr_block_acks[{n}]"
        if not isinstance(entry, Mapping):
            raise ValueError(f"{where} must be a table")
        if extra := set(entry) - _ACK_KEYS:
            raise ValueError(f"unknown {where} keys: {sorted(extra)}")
        task = entry.get("task")
        if not isinstance(task, str) or not task.strip() or len(task.strip()) > MAX_TASK:
            raise ValueError(f"{where} task must be a task id (1-{MAX_TASK} characters)")
        if "until" not in entry:
            raise ValueError(f"{where} needs until: an acknowledgement always expires")
        note = entry.get("note", "")
        if not isinstance(note, str) or len(note) > MAX_NOTE:
            raise ValueError(f"{where} note must be text of at most {MAX_NOTE} characters")
        task = task.strip()
        if task in acks:
            raise ValueError(f"{where} repeats task {task!r}")
        acks[task] = PrBlockAck(task, _until(entry["until"], tz, where), note.strip())
    return DoctorSettings(tuple(acks.values()))


def ack_problem(ack: PrBlockAck, now: datetime) -> str | None:
    """Why ``ack`` acknowledges nothing at ``now``, or None when it holds."""
    if ack.until <= now:
        return f"expired {ack.until.isoformat(timespec='minutes')}"
    if ack.until > now + timedelta(days=MAX_ACK_DAYS):
        return f"ends more than {MAX_ACK_DAYS} days ahead"
    return None
