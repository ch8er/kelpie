"""A large synthetic database for profiling the daily report (roadmap "code health", #248).

Deterministic (a seeded ``random.Random``), synthetic names only: spans of model and tool
calls spread over ``days`` (one session of ``per_session`` calls after another, on the
tasks of :func:`tests.perf.farm.large_farm`), and an action ledger of ``ledger_days``
(``actions_per_day`` entries a day from a mix of rules, kinds and outcomes, about one in
twenty with a state). Rows are written in large batches straight into the tables the
stores read, so a million spans take about a minute, not an hour.

``python -m tests.perf.profile_report`` builds one and profiles the report on it.
"""

from __future__ import annotations

import random
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from kelpie.core.ledger import to_json
from kelpie.core.model import Span

# The generator writes rows the way storage's writers do (their private helpers, on
# purpose): a million spans through the stores' own batching would take an hour.
from kelpie.core.storage import (
    _COLUMNS,
    _SPAN_COLUMNS,
    SqliteLedgerStore,
    SqliteSpanStore,
    _span_row,
    _ts,
)
from tests.perf.farm import ROLES, _session_spans

_RULES = (
    ("stale_blocks", "alert"),
    ("orphan_status", "alert"),
    ("wait_condition", "wake"),
    ("exit_contract", "requeue"),
    ("ping_pong", "escalate"),
    ("bottlenecks", "alert"),
    ("ci_economy", "cancel_build"),
    ("no_progress", "alert"),
    ("loop_breaker", "stop"),
    ("conflict_warning", "comment"),
    ("needs_you", "escalate"),
    ("checkpoints", "comment"),
)
_OUTCOMES = ("observed", "suggested", "executed", "suppressed", "dry_run")
BATCH = 20_000


def build(
    path: Path,
    end: datetime,
    spans: int = 1_000_000,
    days: int = 30,
    ledger_days: int = 182,
    actions_per_day: int = 3_000,
    tasks: int = 2_000,
    per_session: int = 40,
    seed: int = 11,
) -> tuple[int, int]:
    """Write the database at ``path`` (created and migrated by the stores); returns
    ``(spans, actions)`` written."""
    SqliteLedgerStore(path).close()  # the schema
    SqliteSpanStore(path).close()  # the span tables
    rng = random.Random(seed)
    db = sqlite3.connect(path)
    written = _spans(db, rng, end, spans, days, tasks, per_session)
    actions = _ledger(db, rng, end, ledger_days, actions_per_day, tasks)
    db.execute("ANALYZE")
    db.commit()
    db.close()
    return written, actions


def _spans(
    db: sqlite3.Connection,
    rng: random.Random,
    end: datetime,
    total: int,
    days: int,
    tasks: int,
    per_session: int,
) -> int:
    sql = (
        f"INSERT OR IGNORE INTO spans ({_SPAN_COLUMNS})"
        f" VALUES ({', '.join('?' * len(_SPAN_COLUMNS.split(',')))})"
    )
    sessions = max(1, total // per_session)
    span = timedelta(days=days) / sessions
    start = end - timedelta(days=days)
    rows: list[tuple[object, ...]] = []
    written = 0
    for k in range(sessions):
        at = start + k * span
        sid, tid = f"S-{k}", f"T-{rng.randrange(tasks)}"
        found: list[Span] = _session_spans(rng, sid, tid, at, per_session, until=end)
        received = _ts(at + timedelta(hours=1))
        rows += [_span_row(s, received, 1) for s in found]
        if len(rows) >= BATCH:
            written += _flush(db, sql, rows)
    written += _flush(db, sql, rows)
    # every token is counted once, as the store's writer decides for a plain trace
    db.execute("UPDATE spans SET counted = (kind = 'llm'), price_model = model")
    db.commit()
    return written


def _flush(db: sqlite3.Connection, sql: str, rows: list[tuple[object, ...]]) -> int:
    if not rows:
        return 0
    with db:
        n = db.executemany(sql, rows).rowcount
    rows.clear()
    return n


def _ledger(
    db: sqlite3.Connection,
    rng: random.Random,
    end: datetime,
    days: int,
    per_day: int,
    tasks: int,
) -> int:
    sql = f"INSERT INTO actions ({_COLUMNS}) VALUES ({', '.join('?' * len(_COLUMNS.split(',')))})"
    start = end - timedelta(days=days)
    step = timedelta(days=1) / per_day
    rows: list[tuple[object, ...]] = []
    written = 0
    for i in range(days * per_day):
        at = start + i * step
        rule, kind = _RULES[rng.randrange(len(_RULES))]
        task = f"T-{rng.randrange(tasks)}"
        outcome = _OUTCOMES[rng.randrange(len(_OUTCOMES))]
        params = {"role": ROLES[rng.randrange(len(ROLES))]}
        if rng.random() < 0.05:
            params["cost_hours"] = round(rng.random() * 5, 1)
        rows.append(
            (
                _ts(at),
                f"fp-{rule}-{task}-{i % 97}",
                task,
                rule,
                kind,
                "observe",
                outcome,
                f"synthetic {rule} {kind} on {task}",
                task,
                None,
                to_json(params),
                0,
                "cooldown" if outcome == "suppressed" else None,
                None,
            )
        )
        if len(rows) >= BATCH:
            written += _flush(db, sql, rows)
    written += _flush(db, sql, rows)
    return written


def counts(path: Path) -> dict[str, int]:
    db = sqlite3.connect(path)
    try:
        return {
            t: int(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0])
            for t in ("spans", "actions")
        }
    finally:
        db.close()
