"""Private files (#304, audit #275 L5): Kelpie's database, its -wal/-shm and the report
files are created for its own user only; existing files keep their mode, and doctor names
the ones others may read."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from kelpie.app import Settings
from kelpie.core.files import create_private, db_files, loose, private_dir, write_private
from kelpie.core.storage import SqliteLedgerStore
from kelpie.doctor import PRIVATE_CHECK, check_private_files
from kelpie.report.config import ReportSettings
from kelpie.report.fixes import FixStore
from kelpie.report.run import write
from kelpie.report.schedule import RunLog

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def umask(request: pytest.FixtureRequest) -> Iterator[int]:
    value = getattr(request, "param", 0o022)
    old = os.umask(value)
    try:
        yield value
    finally:
        os.umask(old)


@pytest.mark.parametrize("umask", [0o022, 0o002, 0o000, 0o077], indirect=True)
def test_a_new_database_is_private(tmp_path: Path, umask: int) -> None:
    db = tmp_path / "kelpie.db"
    store = SqliteLedgerStore(db)
    try:
        store._db.execute("CREATE TABLE IF NOT EXISTS probe (x)")  # the WAL now exists
        store._db.execute("INSERT INTO probe VALUES (1)")
        store._db.commit()
        files = [p for p in db_files(db) if p.exists()]
        assert len(files) == 3  # kelpie.db, -wal, -shm
        assert {mode(p) for p in files} == {0o600}
    finally:
        store.close()


def test_a_missing_database_folder_is_not_created(tmp_path: Path) -> None:
    """A mistyped [storage] path fails loudly, as before, instead of starting empty."""
    with pytest.raises(sqlite3.OperationalError):
        SqliteLedgerStore(tmp_path / "typo" / "kelpie.db")
    assert not (tmp_path / "typo").exists()


@pytest.mark.parametrize("umask", [0o277], indirect=True)
def test_a_stricter_umask_is_respected(tmp_path: Path, umask: int) -> None:
    target = tmp_path / "key"
    assert create_private(target)
    assert mode(target) == 0o400


def test_an_existing_folder_and_database_keep_their_mode(tmp_path: Path, umask: int) -> None:
    folder = tmp_path / "shared"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    db = folder / "kelpie.db"
    db.touch()
    db.chmod(0o644)
    SqliteLedgerStore(db).close()
    assert mode(folder) == 0o755
    assert mode(db) == 0o644  # never chmodded: doctor names it instead
    assert loose(db_files(db)) == [p for p in db_files(db) if p.exists()]


def test_the_fix_log_creates_a_private_database(tmp_path: Path, umask: int) -> None:
    db = tmp_path / "kelpie.db"
    FixStore(db).close()
    assert mode(db) == 0o600


def test_the_report_run_log_recreates_a_private_database(tmp_path: Path, umask: int) -> None:
    db = tmp_path / "kelpie.db"
    RunLog(db).mark(date(2026, 11, 2), datetime(2026, 11, 3, 1, tzinfo=UTC))
    assert mode(db) == 0o600


def test_report_files_are_private(tmp_path: Path, umask: int) -> None:
    settings = ReportSettings(dir=tmp_path / "reports")
    path = write("# day\n", settings, date(2026, 11, 2))
    assert path is not None
    assert mode(path) == 0o600
    assert mode(tmp_path / "reports") == 0o700
    assert path.read_text(encoding="utf-8") == "# day\n"
    # A report written again keeps its file (and whatever mode the operator gave it).
    path.chmod(0o640)
    write("# day, again\n", settings, date(2026, 11, 2))
    assert mode(path) == 0o640
    assert path.read_text(encoding="utf-8") == "# day, again\n"


def test_a_report_is_never_written_into_a_fifo(tmp_path: Path) -> None:
    fifo = tmp_path / "report.md"
    os.mkfifo(fifo)
    with pytest.raises(OSError):
        write_private(fifo, "x")  # at once: no reader needed


def test_a_report_is_never_written_through_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text("keep\n")
    link = tmp_path / "report.md"
    link.symlink_to(target)
    with pytest.raises(OSError):
        write_private(link, "x")
    assert target.read_text() == "keep\n"


def test_private_dir_leaves_existing_folders_alone(tmp_path: Path, umask: int) -> None:
    tmp_path.chmod(0o755)
    private_dir(tmp_path / "a" / "b")
    assert mode(tmp_path) == 0o755
    assert mode(tmp_path / "a") == 0o700
    private_dir(tmp_path / "a" / "b")  # again: nothing to do
    (tmp_path / "c").mkdir(mode=0o755)
    (tmp_path / "c").chmod(0o755)
    private_dir(tmp_path / "c" / "d")  # an existing parent is kept as it is
    assert mode(tmp_path / "c") == 0o755
    assert mode(tmp_path / "c" / "d") == 0o700


def test_loose_follows_symlinks_and_skips_folders(tmp_path: Path) -> None:
    target = tmp_path / "t"
    target.write_text("x")
    target.chmod(0o644)
    link = tmp_path / "l"
    link.symlink_to(target)
    folder = tmp_path / "d"
    folder.mkdir(mode=0o755)
    assert loose([target, link, folder, tmp_path / "missing"]) == [target, link]


def test_doctor_names_loose_files_and_changes_nothing(tmp_path: Path, umask: int) -> None:
    db = tmp_path / "kelpie.db"
    SqliteLedgerStore(db).close()
    reports = tmp_path / "reports"
    settings = Settings(db_path=db, report=ReportSettings(dir=reports))
    write("# day\n", settings.report, date(2026, 11, 2))
    check = check_private_files(settings)
    assert (check.status, check.name) == ("PASS", PRIVATE_CHECK)

    report = reports / "kelpie-report-2026-11-02.md"
    report.chmod(0o644)
    db.chmod(0o640)
    check = check_private_files(settings)
    assert check.status == "WARN"
    assert str(db) in check.detail and str(report) in check.detail
    assert "chmod 600" in (check.fix or "")
    assert mode(db) == 0o640 and mode(report) == 0o644  # doctor never chmods


def test_doctor_skips_without_files(tmp_path: Path) -> None:
    check = check_private_files(Settings(db_path=tmp_path / "none.db"))
    assert check.status == "SKIP"
    assert check_private_files(Settings(db_path=None)).status == "SKIP"
