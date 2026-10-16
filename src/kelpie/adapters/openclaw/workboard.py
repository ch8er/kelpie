"""OpenClaw Workboard cards as Kelpie tasks (read only).

OpenClaw's bundled Workboard plugin keeps a Kanban board for agent work in its own SQLite
database, ``<state dir>/plugins/workboard/workboard.sqlite`` (``~/.openclaw`` by default).
Kelpie opens it strictly read-only, one short-lived connection and read transaction per
snapshot, like Hermes' ``kanban.db``; OpenClaw writes to it concurrently (WAL mode). Changes go
through OpenClaw's own ``openclaw workboard`` CLI, never through this file.

The schema is the plugin's (schema version 3 in OpenClaw 2026.10.4, ``sqlite-store-schema.ts``),
not a published interface: :func:`missing_columns` names what Kelpie needs, so a changed
schema shows up in ``kelpie doctor`` and the log instead of as wrong data.

Status mapping (``workboard_cards.status`` -> :class:`~kelpie.core.model.Status`)::

    triage, backlog, todo, scheduled -> BACKLOG  (triage and scheduled are pre-work: only a
                                                  person or a planning agent moves them on)
    ready                            -> READY
    running                          -> IN_PROGRESS
    review                           -> REVIEW
    blocked                          -> BLOCKED
    done                             -> DONE
    archived cards (``archived_at``) -> (left out)
    anything else                    -> (left out, logged once)

``status_since`` is the time of the latest status event (``created``, or ``moved`` to the
card's current status), else ``started_at`` for a running card, ``completed_at`` for a done
one, else ``created_at``. Workboard keeps only recent events per card, so an old status falls
back to those columns. Done cards completed before ``lookback`` are left out.

Owners. ``agent_id`` is the agent the card is assigned to (``Task.assignee`` and the role); the
owner is whoever holds the card's claim (``claim_json.ownerId``) while the claim has not
expired, else the assigned agent while the card is ``running`` (a run started from the card
without a claim), else none. The claim token in ``claim_json`` is never read into Kelpie.

Blocks. A ``blocked`` card carries a :class:`~kelpie.core.model.Block` whose reason is the
error of the card's latest ``blocked`` attempt, else its latest comment written at or after
the block (Workboard writes the block reason as a comment), else empty.

Dependencies: a ``parent`` or ``blocked_by`` link makes the card depend on the target card (a
child stays ``todo`` until every parent is ``done``); ``child`` links are the card's
``subtasks``. ``blocks`` and ``relates_to`` links say nothing about this card's start.

Sessions. A card names the OpenClaw session working on it (``session_key``, else
``execution_session_key``): :attr:`Card.session_key` lets the adapter tie hook sessions to the
card. Dispatched workers use ``agent:<agentId>:subagent:workboard-<boardId>-<cardId>``.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kelpie.core.model import Block, Dependency, Status, Task

BUSY_TIMEOUT_MS = 5000
MAX_TITLE = 200
MAX_BODY = 8000  # characters of a card's notes kept in the snapshot
MAX_REASON = 500
MAX_FIELD = 128
MAX_CARDS = 20000  # cards read per snapshot (newest first); a board this size is unusual
PREWORK = frozenset({"triage", "scheduled"})

STATUS_MAP: Mapping[str, Status] = {
    "triage": Status.BACKLOG,
    "backlog": Status.BACKLOG,
    "todo": Status.BACKLOG,
    "scheduled": Status.BACKLOG,
    "ready": Status.READY,
    "running": Status.IN_PROGRESS,
    "review": Status.REVIEW,
    "blocked": Status.BLOCKED,
    "done": Status.DONE,
}
DEPENDS = frozenset({"parent", "blocked_by"})  # link types the card waits on
CHILD = "child"

# What Kelpie reads, per table; anything missing means the plugin's schema moved.
COLUMNS: Mapping[str, frozenset[str]] = {
    "workboard_cards": frozenset(
        {
            "id",
            "board_id",
            "title",
            "notes",
            "status",
            "agent_id",
            "session_key",
            "run_id",
            "created_at",
            "updated_at",
            "started_at",
            "completed_at",
            "execution_status",
            "execution_session_key",
            "execution_run_id",
            "claim_json",
            "archived_at",
        }
    ),
    "workboard_card_labels": frozenset({"card_id", "ordinal", "label"}),
    "workboard_card_events": frozenset({"card_id", "kind", "at", "from_status", "to_status"}),
    "workboard_card_links": frozenset({"card_id", "type", "target_card_id"}),
    "workboard_card_attempts": frozenset({"card_id", "status", "started_at", "ended_at", "error"}),
    "workboard_card_comments": frozenset({"card_id", "body", "created_at"}),
}


class WorkboardError(RuntimeError):
    """The Workboard database cannot be read."""


def _ms(value: Any) -> datetime | None:
    """Workboard stores times as epoch milliseconds."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        return datetime.fromtimestamp(value / 1000, UTC)
    except (ValueError, OverflowError, OSError):
        return None


def _text(value: Any, limit: int = MAX_FIELD) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:limit] or None


@dataclass(frozen=True, slots=True)
class Card:
    """One card as Kelpie needs it. ``task`` is the Kelpie view of it."""

    task: Task
    session_key: str | None  # the OpenClaw session linked to the card
    run_id: str | None  # the run linked to the card


@dataclass(frozen=True, slots=True)
class Board:
    cards: tuple[Card, ...]
    dependencies: tuple[Dependency, ...]


def connect(path: Path) -> sqlite3.Connection:
    """A read-only connection; raises :class:`WorkboardError`."""
    if not path.is_file():
        raise WorkboardError(f"Workboard database not found: {path}")
    try:
        conn = sqlite3.connect(
            path.resolve().as_uri() + "?mode=ro",
            uri=True,
            timeout=BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,  # the read transaction is explicit
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA query_only=1")
    except sqlite3.Error as exc:
        raise WorkboardError(f"cannot open Workboard database {path}: {exc}") from exc
    return conn


def missing_columns(conn: sqlite3.Connection) -> list[str]:
    """``table.column`` names Kelpie reads that the database lacks (empty: all there)."""
    missing: list[str] = []
    for table, wanted in COLUMNS.items():
        have = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not have:
            missing.append(table)
            continue
        missing += [f"{table}.{c}" for c in sorted(wanted - have)]
    return missing


def read_board(path: Path, cutoff: datetime, now: datetime, source: str) -> Board:
    """Cards and dependencies as of ``now``; raises :class:`WorkboardError`."""
    conn = connect(path)
    try:
        conn.execute("BEGIN")  # one read transaction: a consistent view
        if missing := missing_columns(conn):
            raise WorkboardError(
                f"Workboard database {path} lacks {', '.join(missing)}: "
                "an OpenClaw version Kelpie does not know"
            )
        rows = conn.execute(
            "SELECT * FROM workboard_cards WHERE archived_at IS NULL "
            "ORDER BY updated_at DESC LIMIT ?",
            (MAX_CARDS,),
        ).fetchall()
        rows = [
            r
            for r in rows
            if r["status"] != "done" or (_ms(r["completed_at"] or r["updated_at"]) or now) >= cutoff
        ]
        ids = [r["id"] for r in rows]
        labels = _grouped(conn, "SELECT card_id, label FROM workboard_card_labels", ids, "ordinal")
        events = _grouped(
            conn,
            "SELECT card_id, kind, at, from_status, to_status FROM workboard_card_events",
            ids,
            "ordinal",
        )
        links = _grouped(
            conn, "SELECT card_id, type, target_card_id FROM workboard_card_links", ids
        )
        attempts = _grouped(
            conn,
            "SELECT card_id, status, started_at, ended_at, error FROM workboard_card_attempts",
            ids,
        )
        comments = _grouped(
            conn, "SELECT card_id, body, created_at FROM workboard_card_comments", ids
        )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        raise WorkboardError(f"cannot read Workboard database {path}: {exc}") from exc
    finally:
        conn.close()
    cards: list[Card] = []
    for row in rows:
        card = _card(row, labels, events, links, attempts, comments, now, source)
        if card is not None:
            cards.append(card)
    kept = {c.task.id for c in cards}
    dependencies = [
        Dependency(task_id=r["card_id"], depends_on=r["target_card_id"])
        for card_id in kept
        for r in links.get(card_id, ())
        if r["type"] in DEPENDS and r["target_card_id"] in kept and r["target_card_id"] != card_id
    ]
    return Board(cards=tuple(cards), dependencies=tuple(dict.fromkeys(dependencies)))


def _grouped(
    conn: sqlite3.Connection, select: str, ids: Sequence[str], order: str | None = None
) -> dict[str, list[sqlite3.Row]]:
    """Rows of a child table per card id, for the given cards only."""
    found: dict[str, list[sqlite3.Row]] = {}
    for start in range(0, len(ids), 500):  # SQLite's host parameter limit
        chunk = ids[start : start + 500]
        marks = ",".join("?" * len(chunk))
        sql = f"{select} WHERE card_id IN ({marks})"
        if order is not None:
            sql += f" ORDER BY {order}"
        for row in conn.execute(sql, chunk):
            found.setdefault(row["card_id"], []).append(row)
    return found


def _claim_owner(raw: Any, now: datetime) -> str | None:
    """Who holds the card's claim, if it has not expired; the token is never kept."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        claim = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(claim, dict):
        return None
    expires = _ms(claim.get("expiresAt"))
    if expires is not None and expires <= now:
        return None
    return _text(claim.get("ownerId"))


def _status_since(row: sqlite3.Row, events: Iterable[sqlite3.Row]) -> datetime:
    status = row["status"]
    latest: datetime | None = None
    for e in events:
        moved = e["kind"] == "moved" and e["from_status"] != e["to_status"]
        if (moved or e["kind"] == "created") and e["to_status"] == status:
            at = _ms(e["at"])
            if at is not None and (latest is None or at > latest):
                latest = at
    if latest is not None:
        return latest
    fallback = {"running": row["started_at"], "done": row["completed_at"]}.get(status)
    return _ms(fallback) or _ms(row["created_at"]) or datetime.fromtimestamp(0, UTC)


def _block(
    since: datetime, attempts: Iterable[sqlite3.Row], comments: Iterable[sqlite3.Row]
) -> Block:
    reason: str | None = None
    latest: datetime | None = None
    for a in attempts:
        at = _ms(a["ended_at"]) or _ms(a["started_at"])
        if a["status"] == "blocked" and at is not None and (latest is None or at >= latest):
            latest, reason = at, _text(a["error"], MAX_REASON)
    if reason is None:
        written = [
            (at, c["body"])
            for c in comments
            if (at := _ms(c["created_at"])) is not None and at >= since
        ]
        if written:
            reason = _text(max(written, key=lambda x: x[0])[1], MAX_REASON)
    return Block(reason=reason or "", since=since)


def _card(
    row: sqlite3.Row,
    labels: Mapping[str, list[sqlite3.Row]],
    events: Mapping[str, list[sqlite3.Row]],
    links: Mapping[str, list[sqlite3.Row]],
    attempts: Mapping[str, list[sqlite3.Row]],
    comments: Mapping[str, list[sqlite3.Row]],
    now: datetime,
    source: str,
) -> Card | None:
    card_id = row["id"]
    raw = row["status"]
    status = STATUS_MAP.get(raw)
    if status is None:
        return None
    since = _status_since(row, events.get(card_id, ()))
    agent = _text(row["agent_id"])
    owner = _claim_owner(row["claim_json"], now)
    if owner is None and raw == "running":
        owner = agent
    children = tuple(
        r["target_card_id"]
        for r in links.get(card_id, ())
        if r["type"] == CHILD and isinstance(r["target_card_id"], str)
    )
    task = Task(
        id=card_id,
        title=(_text(row["title"], MAX_TITLE) or ""),
        status=status,
        status_since=since,
        role=owner or agent,
        owner_id=owner,
        source=source,
        block=_block(since, attempts.get(card_id, ()), comments.get(card_id, ()))
        if status is Status.BLOCKED
        else None,
        labels=tuple(lbl for r in labels.get(card_id, ()) if (lbl := _text(r["label"]))),
        body=_text(row["notes"], MAX_BODY),
        assignee=agent,
        subtasks=children,
        prework=raw in PREWORK,
        platform_status=raw,
    )
    return Card(
        task=task,
        session_key=_text(row["session_key"]) or _text(row["execution_session_key"]),
        run_id=_text(row["run_id"]) or _text(row["execution_run_id"]),
    )


def statuses(cards: Iterable[Card]) -> dict[str, int]:
    """Cards per platform status (for ``kelpie status`` and doctor)."""
    counts: dict[str, int] = {}
    for c in cards:
        key = c.task.platform_status or ""
        counts[key] = counts.get(key, 0) + 1
    return counts
