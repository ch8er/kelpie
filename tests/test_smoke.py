"""Smoke test: the app a user gets from the quickstart config, with no explicit engine values.

A bug once hid in a default (a slots dataclass default read from the class gave a member
descriptor, so the state store failed on every tick). Unit tests pass explicit values, so
this one builds the app exactly as ``kelpie serve`` does from the documented minimal config
and runs real ticks over a synthetic Hermes board.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import build_app, find_config, load_rules, load_settings
from kelpie.cli import main
from kelpie.core.config import EngineConfig
from kelpie.core.ledger import Outcome
from tests.adapters.kanban import Board

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
TICK = timedelta(minutes=2)

# The config of docs/quickstart-hermes.md step 2, paths pointed at tmp files.
QUICKSTART = """
[engine]
dry_run = true
default_mode = "observe"

[adapters.hermes]
db_path = {db}
spool = {spool}

[engine.rules.exit_contract]
session_stale_after = "60m"
[engine.rules.orphan_status]
session_stale_after = "60m"
[engine.rules.wait_condition]
session_stale_after = "60m"
"""


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


@pytest.fixture
def farm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A working directory with the minimal kelpie.toml and a synthetic Hermes board."""
    monkeypatch.delenv("KELPIE_CONFIG", raising=False)
    monkeypatch.delenv("KELPIE_DB", raising=False)
    monkeypatch.chdir(tmp_path)  # kelpie.toml and kelpie.db are looked up in the cwd
    board = Board(tmp_path / "kanban.db")
    spool = tmp_path / "spool.jsonl"

    # A live worker: fresh heartbeat.
    board.task("t_live", "running", ago(days=1), assignee="dev", started=ago(hours=1))
    board.event("t_live", "claimed", ago(hours=1))
    live = board.run("t_live", "dev", ago(hours=1), heartbeat=ago(minutes=1))
    board.update("t_live", current_run_id=live, last_heartbeat_at=ago(minutes=1))
    spool.write_text(
        json.dumps({"type": "tool", "at": ago(minutes=1).timestamp(), "task_id": "t_live"}) + "\n"
    )

    # A worker that vanished: stale heartbeat, claim expired, nobody reaped it.
    board.task("t_stale", "running", ago(days=1), assignee="dev", started=ago(hours=5))
    board.event("t_stale", "claimed", ago(hours=5))
    stale = board.run("t_stale", "dev", ago(hours=5), heartbeat=ago(hours=3))
    board.update(
        "t_stale", current_run_id=stale, last_heartbeat_at=ago(hours=3), claim_expires=ago(hours=2)
    )

    # A task blocked for hours on an untyped reason, with nothing Kelpie can check.
    board.task("t_blocked", "blocked", ago(days=1), assignee="dev")
    board.event("t_blocked", "claimed", ago(hours=7))
    board.run(
        "t_blocked", "dev", ago(hours=7), status="blocked", ended=ago(hours=6), outcome="blocked"
    )
    board.event("t_blocked", "blocked", ago(hours=6), {"reason": "waiting for something"})
    board.close()

    (tmp_path / "kelpie.toml").write_text(
        QUICKSTART.format(db=json.dumps(str(tmp_path / "kanban.db")), spool=json.dumps(str(spool)))
    )
    return tmp_path


def test_quickstart_config_runs_ticks_without_errors(
    farm: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    clock = [NOW]
    settings = load_settings(find_config(None))  # as `kelpie serve` does
    load_rules(settings.engine)
    app = build_app(settings, clock=lambda: clock[0])
    try:
        for _ in range(3):
            app.loop.tick_once()
            clock[0] += TICK

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not errors, "\n".join(f"{r.levelname} {r.name}: {r.getMessage()}" for r in errors)

        assert "kelpie_engine_ticks_total 3" in app.metrics.render()
        assert app.metrics.tick_failures == 0 and not any(app.metrics.adapter_errors.values())

        # The state store works: the long-blocked task is an open alert state that was
        # opened at the first tick and has been seen on the later ones.
        open_states = app.engine.states.store.open_states()
        assert {s.task_id for s in open_states} == {"t_blocked"}
        assert all(s.opened_at == NOW and s.last_seen > NOW for s in open_states)

        # The ledger has what the rules proposed, and nothing was executed (dry-run).
        entries = app.store.since(NOW - timedelta(minutes=1))
        assert not [e for e in entries if e.outcome is Outcome.EXECUTED]
        proposed = {e.task_id for e in entries}
        assert {"t_stale", "t_blocked"} <= proposed
        assert "t_live" not in proposed
    finally:
        app.close()


def test_empty_engine_table_equals_the_defaults() -> None:
    parsed = EngineConfig.from_mapping({})
    defaults = EngineConfig()
    for f in dataclasses.fields(EngineConfig):
        assert getattr(parsed, f.name) == getattr(defaults, f.name), f.name


def test_status_runs_on_the_minimal_config(farm: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["status"]) == 0
    assert "observe" in capsys.readouterr().out
