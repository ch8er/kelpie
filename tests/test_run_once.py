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
    assert main(["run-once", "--dry-run"]) == 0
    assert "rule exit_contract failed: RuntimeError: bug" in capsys.readouterr().out
