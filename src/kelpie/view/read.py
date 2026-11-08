"""What the farm view reads from Kelpie's database, without writing to it (not even a
migration): open alert states, the latest ledger entries per rule and, when no board is
read, the last status ``kelpie serve`` observed per task."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from kelpie.core.farmview import PER_RULE
from kelpie.core.ledger import LedgerEntry
from kelpie.core.states import OpenState
from kelpie.core.storage import (
    SchemaOutdated,
    open_readonly,
    read_last_statuses,
    read_latest_actions,
    read_open_states,
)
from kelpie.core.transitions import StatusChange


@dataclass(frozen=True, slots=True)
class Stored:
    states: list[OpenState] = field(default_factory=list)
    entries: list[LedgerEntry] = field(default_factory=list)
    statuses: dict[str, StatusChange] | None = None  # asked for and read
    problems: list[str] = field(default_factory=list)


def read_stored(db: Path | None, statuses: bool = False, per_rule: int = PER_RULE) -> Stored:
    """Read-only. ``statuses``: also the last observed status per task."""
    if db is None:
        return Stored(problems=["ledger in memory: no alerts or actions outside kelpie serve"])
    if not db.exists():
        return Stored(problems=[f"no database at {db} yet: kelpie serve has not run"])
    try:
        conn = open_readonly(db, 5000)
    except (SchemaOutdated, RuntimeError, sqlite3.Error) as exc:
        return Stored(problems=[f"alerts and actions left out: {exc}"])
    try:
        return Stored(
            states=read_open_states(conn),
            entries=read_latest_actions(conn, per_rule),
            statuses=read_last_statuses(conn) if statuses else None,
        )
    except sqlite3.Error as exc:
        return Stored(problems=[f"alerts and actions left out: {exc}"])
    finally:
        conn.close()
