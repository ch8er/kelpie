"""``kelpie run-once --dry-run``: the actions every loaded rule asks for on one snapshot.

The snapshot is built as ``kelpie why`` builds it (:func:`kelpie.why.read_snapshot`): the
platform adapters, the connector steps that read only the board, and the span store opened
read-only; ``--refresh`` adds the network connectors (GitHub), as one tick of ``kelpie
serve`` would. The open alert states are read from the database without writing to it, so
rules that keep a state open on a lower bar (hysteresis) see what serve sees. Every loaded
rule is evaluated (rules are pure) and its actions are printed with the mode the rule runs
in; nothing is dispatched, nothing is written: no ledger entry, no alert state, no action
on a board. The engine's ledger would hold back some of these (cooldowns, alerts sent on
transitions only), so serve does not necessarily act on all of them on its next tick.

Only ``--dry-run`` exists: running the actions once is ``kelpie serve``'s job. Exit codes:
0; 2 without ``--dry-run`` or on a config error; 3 when a platform adapter failed to read its
board (the actions printed lack its tasks).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from kelpie.alerts.render import one_line
from kelpie.app import LoadedRules, Settings, load_rules, utc_now
from kelpie.core.actions import Action
from kelpie.core.config import Mode
from kelpie.core.storage import SchemaOutdated, open_readonly
from kelpie.why import read_snapshot

SCHEMA_VERSION = 1
REASON_CHARS = 160


def open_state_ids(db: Path | None) -> tuple[tuple[str, ...], list[str]]:
    """The ids of the open alert states, read-only, and what could not be read."""
    if db is None or not db.exists():
        return (), []
    try:
        conn = open_readonly(db, 5000)
    except (SchemaOutdated, RuntimeError, sqlite3.Error) as exc:
        return (), [f"open alert states left out: {exc}"]
    try:
        rows = conn.execute("SELECT id FROM states WHERE closed_at IS NULL ORDER BY id")
        return tuple(r[0] for r in rows.fetchall()), []
    except sqlite3.Error as exc:
        return (), [f"open alert states left out: {exc}"]
    finally:
        conn.close()


def evaluate(loaded: LoadedRules, snapshot: Any) -> tuple[list[tuple[Action, Mode]], list[str]]:
    """Every loaded rule's actions with the mode they run in. A rule that raises is skipped
    and named, as the engine skips it."""
    modes = {s.name: s.mode for s in loaded.statuses}
    found: list[tuple[Action, Mode]] = []
    problems: list[str] = []
    for rule in loaded.rules:
        try:
            actions = list(rule.evaluate(snapshot))
        except Exception as exc:
            problems.append(f"rule {rule.name} failed: {type(exc).__name__}: {exc}")
            continue
        for a in actions:
            found.append(
                (a, Mode.OBSERVE if a.observe_only else modes.get(rule.name, Mode.OBSERVE))
            )
    return found, problems


def _mode(mode: Mode, dry_run: bool) -> str:
    return f"{mode} (dry-run)" if dry_run and mode is not Mode.OBSERVE else str(mode)


def _as_json(
    now: datetime,
    found: list[tuple[Action, Mode]],
    dry_run: bool,
    counts: dict[str, int],
    problems: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "now": now.isoformat(),
        "dispatched": False,
        "dry_run": dry_run,
        "snapshot": counts,
        "actions": [
            {
                "rule": a.rule,
                "kind": str(a.kind),
                "mode": str(mode),
                "severity": str(a.severity),
                "task_id": a.task_id,
                "executor": a.executor,
                "state": a.state,
                "reason": a.reason,
                "params": a.params,
            }
            for a, mode in found
        ],
        "problems": problems,
    }


def run(
    settings: Settings,
    dry_run: bool,
    as_json: bool = False,
    refresh: bool = False,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[str, str, int]:
    """Stdout, stderr and the exit code."""
    if not dry_run:
        return (
            "",
            "kelpie: run-once only runs with --dry-run (it prints the actions and dispatches "
            "nothing); kelpie serve carries actions out\n",
            2,
        )
    loaded = load_rules(settings.engine)
    read = read_snapshot(settings, clock, refresh)
    ids, state_problems = open_state_ids(settings.db_path)
    snapshot = replace(read.snapshot, open_states=ids)
    found, rule_problems = evaluate(loaded, snapshot)
    problems = [*read.adapter_failures, *read.problems, *state_problems, *rule_problems]
    code = 3 if read.adapter_failures else 0
    counts = {
        "tasks": len(snapshot.tasks),
        "sessions": len(snapshot.sessions),
        "conditions": len(snapshot.conditions),
        "open_states": len(ids),
    }
    dry = settings.engine.dry_run
    if as_json:
        body = _as_json(snapshot.now, found, dry, counts, problems)
        return json.dumps(body, indent=2, default=str) + "\n", "", code
    lines = [
        f"run-once at {snapshot.now.isoformat(timespec='seconds')}: nothing dispatched, "
        "nothing written",
        f"snapshot: {counts['tasks']} tasks, {counts['sessions']} sessions, "
        f"{counts['conditions']} conditions, {counts['open_states']} open alert states",
        "",
    ]
    for a, mode in found:
        task = f" {one_line(a.task_id, 60)}" if a.task_id else ""
        lines.append(
            f"  {a.rule:<17} {a.kind!s:<12} {_mode(mode, dry):<16}{task}: "
            f"{one_line(a.reason, REASON_CHARS)}"
        )
    rules = len({a.rule for a, _ in found})
    lines.append(f"{len(found)} action(s) from {rules} rule(s)" if found else "no actions")
    if found:
        lines.append(
            "note: kelpie serve's ledger holds some back (cooldowns, alerts on transitions "
            "only), so its next tick may carry out fewer"
        )
    lines += [f"note: {one_line(p, 300)}" for p in problems]
    return "\n".join(lines) + "\n", "", code
