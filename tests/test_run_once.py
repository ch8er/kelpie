"""``kelpie run-once --dry-run``: the actions of one snapshot, printed; nothing dispatched or
written."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kelpie.app import build_app, find_config, load_settings
from kelpie.cli import main
from kelpie.run_once import run
from tests.test_smoke import NOW, TICK, farm  # noqa: F401  (the fixture)


def _files(root: Path) -> dict[str, tuple[int, int]]:
    return {
        p.name: (os.stat(p).st_mtime_ns, os.stat(p).st_size)
        for p in root.iterdir()
        if p.is_file() and not p.name.endswith(("-wal", "-shm"))  # SQLite's own, on a read
    }


def test_without_dry_run_it_refuses(farm: Path, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    assert main(["run-once"]) == 2
    out, err = capsys.readouterr()
    assert not out and "only runs with --dry-run" in err
    assert not (farm / "kelpie.db").exists()


def test_prints_the_actions_and_writes_nothing(
    farm: Path,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    before = _files(farm)
    out, err, code = run(load_settings(find_config(None)), True, clock=lambda: NOW)
    assert code == 0 and not err
    assert "nothing dispatched, nothing written" in out
    assert "exit_contract" in out and "t_stale" in out and "requeue" in out
    assert "t_live" not in out
    assert _files(farm) == before  # no database, no spool change, nothing


def test_json(farm: Path, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    assert main(["run-once", "--dry-run", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1 and data["dispatched"] is False
    assert data["snapshot"]["tasks"] == 3
    kinds = {(a["rule"], a["task_id"], a["kind"]) for a in data["actions"]}
    assert ("exit_contract", "t_stale", "requeue") in kinds
    assert all(a["mode"] == "observe" for a in data["actions"])


def test_open_states_are_read_and_left_alone(
    farm: Path,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    clock = [NOW]
    app = build_app(load_settings(find_config(None)), clock=lambda: clock[0])
    try:
        app.loop.tick_once()
        clock[0] += TICK
    finally:
        app.close()
    db = farm / "kelpie.db"
    before = (os.stat(db).st_mtime_ns, os.stat(db).st_size)
    assert main(["run-once", "--dry-run", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["snapshot"]["open_states"] == 1
    assert (os.stat(db).st_mtime_ns, os.stat(db).st_size) == before


def test_a_quiet_board_has_no_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kelpie.toml").write_text("[engine]\ndry_run = true\n")
    assert main(["run-once", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "no actions" in out and "0 tasks" in out


def test_a_failing_rule_is_named_not_fatal(
    farm: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from kelpie.rules.exit_contract import ExitContract

    def boom(self: object, snapshot: object) -> list[object]:
        raise RuntimeError("bug")

    monkeypatch.setattr(ExitContract, "evaluate", boom)
    assert main(["run-once", "--dry-run"]) == 3  # the list is incomplete
    assert "rule exit_contract failed: RuntimeError: bug" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("mode", "dry_run", "shown"),
    [
        ("act", "true", "act (dry-run)"),
        ("suggest", "true", "suggest (dry-run)"),
        ("suggest", "false", "suggest "),
    ],
)
def test_the_rule_mode_is_shown(farm: Path, mode: str, dry_run: str, shown: str) -> None:  # noqa: F811
    config = farm / "kelpie.toml"
    text = config.read_text().replace("dry_run = true", f"dry_run = {dry_run}")
    text = text.replace(
        "[engine.rules.exit_contract]\n", f'[engine.rules.exit_contract]\nmode = "{mode}"\n'
    )
    config.write_text(text)
    out, _, code = run(load_settings(find_config(None)), True, clock=lambda: NOW)
    assert code == 0
    line = next(x for x in out.splitlines() if "exit_contract" in x and "t_stale" in x)
    assert shown in line


def test_observe_only_and_escaping() -> None:
    from types import SimpleNamespace

    from kelpie.core.actions import Action, ActionKind
    from kelpie.core.config import Mode
    from kelpie.core.model import Snapshot
    from kelpie.run_once import evaluate

    class Rule:
        name = "stub"

        def evaluate(self, snapshot: Snapshot) -> list[Action]:
            return [
                Action(ActionKind.ALERT, "stub", "\x1b[31mred\x1b[0m\nline @everyone", "T-1\x07"),
                Action(ActionKind.ALERT, "stub", "new check", observe_only=True),
            ]

    status = SimpleNamespace(name="stub", mode=Mode.ACT)
    loaded = SimpleNamespace(rules=[Rule()], statuses=[status])
    found, problems = evaluate(loaded, Snapshot(now=NOW))  # type: ignore[arg-type]
    assert not problems and [m for _, m in found] == [Mode.ACT, Mode.OBSERVE]
    from kelpie.alerts.render import one_line

    shown = one_line(found[0][0].reason, 160) + one_line(found[0][0].task_id or "", 60)
    assert "\x1b" not in shown and "\n" not in shown and "\x07" not in shown


def test_an_unreadable_state_db_is_a_note(farm: Path) -> None:  # noqa: F811
    import sqlite3

    db = farm / "kelpie.db"
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA user_version = 1")
    out, _, code = run(load_settings(find_config(None)), True, clock=lambda: NOW)
    assert code == 0 and "open alert states left out" in out
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_an_adapter_failure_exits_3(farm: Path) -> None:  # noqa: F811
    (farm / "kanban.db").write_bytes(b"not a database")
    _, _, code = run(load_settings(find_config(None)), True, clock=lambda: NOW)
    assert code == 3


def test_sources_only_serve_reads_are_named() -> None:
    from dataclasses import replace

    from kelpie.app import Settings
    from kelpie.run_once import left_out

    assert left_out(Settings()) == []
    (note,) = left_out(replace(Settings(), hub=object(), llm_quotas=object()))  # type: ignore[arg-type]
    assert "provider quotas, the hub's remote boards" in note
