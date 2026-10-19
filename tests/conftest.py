"""Suite-wide fixtures."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _private_home(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """No test writes to the developer's home: Kelpie's defaults live under ``~`` (the wait
    key that signs parks and stops is created on first use)."""
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path_factory.mktemp("home"))
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


_CWD_DB = ("kelpie.db", "kelpie.db-wal", "kelpie.db-shm")


def _cwd_db_state(cwd: str) -> dict[str, tuple[int, int, int] | None]:
    found: dict[str, tuple[int, int, int] | None] = {}
    for name in _CWD_DB:
        try:
            st = os.stat(os.path.join(cwd, name))
        except FileNotFoundError:
            found[name] = None
        else:
            found[name] = (st.st_ino, st.st_mtime_ns, st.st_size)
    return found


def cwd_db_leaks(cwd: str, before: dict[str, tuple[int, int, int] | None]) -> list[str]:
    """The ``./kelpie.db`` files written in ``cwd`` since ``before``; those that did not
    exist then are removed, so one leak does not break the tests after it."""
    after = _cwd_db_state(cwd)
    touched = [n for n in _CWD_DB if after[n] is not None and after[n] != before[n]]
    for name in touched:
        if before[name] is None:
            os.remove(os.path.join(cwd, name))
    return touched


@pytest.fixture(autouse=True)
def _no_cwd_db(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fails a test that creates or writes ``./kelpie.db`` (the default storage path): a
    file left there outlives the run and breaks later ones on another schema. A test
    that builds the app from a config gets a database under ``tmp_path`` (``tmp_db``)."""
    cwd = os.getcwd()
    before = _cwd_db_state(cwd)
    yield
    if touched := cwd_db_leaks(cwd, before):
        pytest.fail(
            f"{request.node.nodeid} wrote {', '.join(touched)} in {cwd}: give it a database "
            "under tmp_path (the tmp_db fixture)",
            pytrace=False,
        )


_SESSION_DB: dict[str, tuple[int, int, int] | None] = {}


def pytest_sessionstart(session: pytest.Session) -> None:
    _SESSION_DB.update(_cwd_db_state(os.getcwd()))


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """A backstop for writes no single test caught (a thread that outlived its test)."""
    if _SESSION_DB and (touched := cwd_db_leaks(os.getcwd(), _SESSION_DB)):
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line(f"ERROR: the run wrote {', '.join(touched)} in the cwd")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def tmp_db(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A database path under a temporary directory, also as ``KELPIE_DB`` so that settings
    loaded from a config without ``[storage] path`` use it instead of ``./kelpie.db``."""
    path = tmp_path_factory.mktemp("db") / "kelpie.db"
    monkeypatch.setenv("KELPIE_DB", str(path))
    return path
