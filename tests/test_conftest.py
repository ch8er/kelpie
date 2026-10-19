"""The suite's own guards (tests/conftest.py), in a pytest run of their own."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from kelpie.app import load_settings
from tests.conftest import _cwd_db_state, cwd_db_leaks

pytest_plugins = ["pytester"]
CONFTEST = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")


@pytest.fixture
def run(pytester: pytest.Pytester) -> pytest.Pytester:
    pytester.makeconftest(CONFTEST)
    return pytester


def test_a_test_that_writes_the_cwd_db_fails_and_leaves_nothing(run: pytest.Pytester) -> None:
    run.makepyfile(
        """
        import sqlite3

        def test_leaks():
            sqlite3.connect("kelpie.db").execute("CREATE TABLE t (x)")

        def test_clean():
            pass
        """
    )
    result = run.runpytest_subprocess()
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*test_leaks*wrote kelpie.db*tmp_db*"])
    assert not (run.path / "kelpie.db").exists()


def test_a_session_fixture_teardown_leak_fails_the_run(run: pytest.Pytester) -> None:
    run.makepyfile(
        """
        import sqlite3
        import pytest

        @pytest.fixture(scope="session")
        def late():
            yield
            sqlite3.connect("kelpie.db").execute("CREATE TABLE t (x)")

        def test_ok(late):
            pass
        """
    )
    result = run.runpytest_subprocess()
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*the run wrote kelpie.db in the cwd*"])
    assert not (run.path / "kelpie.db").exists()


def test_an_interrupted_run_keeps_its_exit_code(run: pytest.Pytester) -> None:
    run.makepyfile(
        """
        import sqlite3
        import pytest

        @pytest.fixture(scope="session")
        def late():
            yield
            sqlite3.connect("kelpie.db").execute("CREATE TABLE t (x)")

        def test_stop(late):
            pytest.exit("stop", returncode=2)
        """
    )
    result = run.runpytest_subprocess()
    assert result.ret == 2
    assert not (run.path / "kelpie.db").exists()  # caught after the fixture's teardown


def test_an_existing_database_is_never_removed(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    keep = sqlite3.connect(db)
    keep.execute("PRAGMA journal_mode=WAL")
    keep.execute("PRAGMA wal_autocheckpoint=0")
    keep.execute("CREATE TABLE t (x)")
    keep.commit()
    keep.close()  # a clean shutdown: no -wal, no -shm
    (tmp_path / "kelpie.db-wal").unlink(missing_ok=True)
    (tmp_path / "kelpie.db-shm").unlink(missing_ok=True)
    before = _cwd_db_state(str(tmp_path))
    open_ = sqlite3.connect(db)  # a developer's kelpie serve, say
    open_.execute("PRAGMA wal_autocheckpoint=0")
    open_.execute("INSERT INTO t VALUES (2)")
    open_.commit()
    touched = cwd_db_leaks(str(tmp_path), before)
    assert "kelpie.db-wal" in touched  # reported...
    assert (tmp_path / "kelpie.db-wal").exists()  # ...never removed
    assert sqlite3.connect(db).execute("SELECT COUNT(*) FROM t").fetchone() == (1,)
    open_.close()


def test_a_new_cwd_db_is_reported_and_removed(tmp_path: Path) -> None:
    before = _cwd_db_state(str(tmp_path))
    (tmp_path / "kelpie.db").write_bytes(b"x")
    (tmp_path / "kelpie.db-wal").write_bytes(b"x")
    assert cwd_db_leaks(str(tmp_path), before) == ["kelpie.db", "kelpie.db-wal"]
    assert not (tmp_path / "kelpie.db").exists() and not (tmp_path / "kelpie.db-wal").exists()


def test_tmp_db_is_where_settings_put_the_database(
    tmp_path: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kelpie.toml").write_text(f'[storage]\npath = "{tmp_path / "x.db"}"\n')
    assert load_settings(tmp_path / "kelpie.toml").db_path == tmp_db  # wins over the config
    assert tmp_db.parent != tmp_path


def test_the_shell_kelpie_variables_are_unset() -> None:
    import os

    assert not [k for k in os.environ if k.startswith("KELPIE_")]
