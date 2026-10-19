"""Suite-wide fixtures."""

from __future__ import annotations

import contextlib
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


_SHELL_KELPIE: dict[str, str] = {}


def pytest_configure(config: pytest.Config) -> None:
    """No test reads or writes what the developer's shell points Kelpie at (``KELPIE_DB``
    would replace every database path, ``KELPIE_CONFIG`` the config): every ``KELPIE_*``
    variable is unset for the run, collection included; a test sets what it needs."""
    _SHELL_KELPIE.update({k: v for k, v in os.environ.items() if k.startswith("KELPIE_")})
    for key in _SHELL_KELPIE:
        del os.environ[key]


def pytest_unconfigure(config: pytest.Config) -> None:
    os.environ.update(_SHELL_KELPIE)


_CWD_DB = ("kelpie.db", "kelpie.db-wal", "kelpie.db-shm")
_MAIN = _CWD_DB[0]


def _cwd_db_state(cwd: str) -> dict[str, tuple[int, int, int] | None]:
    found: dict[str, tuple[int, int, int] | None] = {}
    for name in _CWD_DB:
        try:
            st = os.stat(os.path.join(cwd, name))
        except FileNotFoundError:
            found[name] = None
        else:
            # mtime may be coarse (1-2s on HFS+, FAT): a same-size rewrite can be missed
            found[name] = (st.st_ino, st.st_mtime_ns, st.st_size)
    return found


def cwd_db_leaks(cwd: str, before: dict[str, tuple[int, int, int] | None]) -> list[str]:
    """The ``./kelpie.db`` files written in ``cwd`` since ``before``. When the database
    itself is new they are removed, so one leak does not break the tests after it; files
    of a database that was there before (a developer's, maybe open) are never removed."""
    after = _cwd_db_state(cwd)
    touched = [n for n in _CWD_DB if after[n] is not None and after[n] != before[n]]
    if before[_MAIN] is None:
        for name in touched:
            if before[name] is None:
                with contextlib.suppress(OSError):  # still open (Windows): reported anyway
                    os.remove(os.path.join(cwd, name))
    return touched


@pytest.fixture(autouse=True)
def _no_cwd_db(request: pytest.FixtureRequest) -> Iterator[None]:
    """Fails a test that creates or writes ``./kelpie.db`` (the default storage path): a
    file left there outlives the run and breaks later ones on another schema. A test that
    builds the app from a config gets a temporary database (``tmp_db``). It watches the
    directory pytest runs in: run the suite where no Kelpie uses ``./kelpie.db``."""
    cwd = os.getcwd()
    before = _cwd_db_state(cwd)
    yield
    if touched := cwd_db_leaks(cwd, before):
        pytest.fail(
            f"{request.node.nodeid} (or a thread left by an earlier test) wrote "
            f"{', '.join(touched)} in {cwd}: give it a temporary database (the tmp_db "
            "fixture)",
            pytrace=False,
        )


_SESSION_DB: dict[str, tuple[int, int, int] | None] = {}


def pytest_sessionstart(session: pytest.Session) -> None:
    _SESSION_DB.update(_cwd_db_state(str(session.startpath)))


@pytest.hookimpl(trylast=True)  # after session-scoped fixtures are torn down
def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """A backstop for writes no single test caught (a thread that outlived its test, a
    session fixture's teardown)."""
    if not _SESSION_DB or not (touched := cwd_db_leaks(str(session.startpath), _SESSION_DB)):
        return
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.ensure_newline()
        reporter.write_sep("!", f"the run wrote {', '.join(touched)} in the cwd", red=True)
    if session.exitstatus in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED):
        session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture
def tmp_db(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A database path under a temporary directory, also as ``KELPIE_DB``, which Kelpie
    uses instead of any ``[storage] path``: settings loaded in the test put their
    database there."""
    path = tmp_path_factory.mktemp("db") / "kelpie.db"
    monkeypatch.setenv("KELPIE_DB", str(path))
    return path
