"""The suite's own guards (tests/conftest.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kelpie.app import load_settings
from tests.conftest import _cwd_db_state, cwd_db_leaks


def test_a_new_cwd_db_is_reported_and_removed(tmp_path: Path) -> None:
    before = _cwd_db_state(str(tmp_path))
    (tmp_path / "kelpie.db").write_bytes(b"x")
    (tmp_path / "kelpie.db-wal").write_bytes(b"x")
    assert cwd_db_leaks(str(tmp_path), before) == ["kelpie.db", "kelpie.db-wal"]
    assert not (tmp_path / "kelpie.db").exists() and not (tmp_path / "kelpie.db-wal").exists()


def test_a_written_cwd_db_is_reported_and_kept(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    db.write_bytes(b"x")
    before = _cwd_db_state(str(tmp_path))
    db.write_bytes(b"xy")  # someone's own database: reported, never deleted
    assert cwd_db_leaks(str(tmp_path), before) == ["kelpie.db"]
    assert db.exists()
    assert cwd_db_leaks(str(tmp_path), _cwd_db_state(str(tmp_path))) == []


def test_tmp_db_is_where_settings_put_the_database(
    tmp_path: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kelpie.toml").write_text("")
    assert load_settings(tmp_path / "kelpie.toml").db_path == tmp_db
    assert tmp_db.parent != tmp_path
