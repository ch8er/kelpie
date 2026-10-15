"""``kelpie doctor``: is a first live run on a Hermes farm set up correctly?

One line per check, ``PASS|FAIL|SKIP  <check>: <detail>``, and a ``  fix: ...`` line under a
FAIL. Nothing is written anywhere: the kanban database and the span store are opened
read-only. This module may know about adapters and the app wiring; ``kelpie.core`` does not.
"""

from __future__ import annotations

import os
import sqlite3
import time
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from kelpie.adapters.hermes.adapter import HermesAdapter, HermesError, format_age
from kelpie.adapters.hermes.git_state import read_git_state
from kelpie.app import ConfigError, LoadedRules, Settings, load_rules, load_settings, utc_now
from kelpie.core.config import Mode

FRESH = timedelta(hours=1)  # newest spool line or span must be younger than this
MAX_WORKTREES = 20  # git is asked about at most this many workspaces
GIT_DEADLINE = 15.0  # seconds for all git calls together
DEFAULT_PROMETHEUS = "http://localhost:9090"
PROMETHEUS_ENV = "KELPIE_PROMETHEUS_URL"


@dataclass(frozen=True, slots=True)
class Check:
    status: str  # PASS | FAIL | SKIP
    name: str
    detail: str
    fix: str = ""

    def render(self) -> str:
        line = f"{self.status}  {self.name}: {self.detail}"
        return f"{line}\n  fix: {self.fix}" if self.status == "FAIL" and self.fix else line


def _pass(name: str, detail: str) -> Check:
    return Check("PASS", name, detail)


def _skip(name: str, detail: str) -> Check:
    return Check("SKIP", name, detail)


def _fail(name: str, detail: str, fix: str) -> Check:
    return Check("FAIL", name, detail, fix)


def _quiet_or_fail(name: str, detail: str, fix: str, running: int | None) -> Check:
    """A signal is missing: fine on an idle farm, a failure while tasks are running."""
    if running == 0:
        return _skip(name, f"{detail}; no task is running, start one and run doctor again")
    return _fail(name, detail, fix)


def check_dry_run(settings: Settings, loaded: LoadedRules) -> Check:
    name = "observe only"
    acting = sorted(s.name for s in loaded.statuses if s.implemented and s.mode is not Mode.OBSERVE)
    problems = []
    if not settings.engine.dry_run:
        problems.append("dry_run is off")
    if acting:
        problems.append(f"rules not in observe: {', '.join(acting)}")
    if problems:
        return _fail(
            name,
            "; ".join(problems),
            'in [engine] set dry_run = true and default_mode = "observe", and remove '
            "mode = ... from [engine.rules.*]",
        )
    return _pass(name, "dry_run on, every rule in observe")


def check_board(adapter: HermesAdapter) -> tuple[Check, int | None]:
    """The check, and the number of running tasks (None when the board cannot be read)."""
    name = "kanban.db"
    fix = f"set db_path in [adapters.hermes] to the board's kanban.db (now {adapter.db_path})"
    try:
        total, running = adapter.board_counts()
    except (HermesError, sqlite3.Error) as exc:
        return _fail(name, f"not readable: {exc}", fix), None
    detail = f"readable, {total} tasks, {running} running ({adapter.db_path})"
    return _pass(name, detail), running


def check_hooks(adapter: HermesAdapter, now: datetime, running: int | None) -> Check:
    name = "hooks fire"
    spool = adapter.spool
    if spool is None:
        return _skip(name, "spool disabled")
    fix = (
        "install and enable the Kelpie plugin on the Hermes host (docs/quickstart-hermes.md), "
        "restart the dispatcher and run a task"
    )
    if not spool.exists():
        return _quiet_or_fail(name, f"spool missing ({spool})", fix, running)
    last = adapter.last_spool_at()
    if last is None:
        return _quiet_or_fail(name, f"spool has no readable lines ({spool})", fix, running)
    age = now - last
    if age > FRESH:
        detail = f"newest spool line is {format_age(age)} old ({spool})"
        return _quiet_or_fail(name, detail, fix, running)
    return _pass(name, f"newest spool line {format_age(age)} old ({spool})")


def _newest_span(path: Path) -> str | None:
    """``last_at`` of the newest stored span, None if none ever arrived; read-only."""
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        return conn.execute("SELECT MAX(last_at) FROM spans").fetchone()[0]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    finally:
        conn.close()


def check_spans(settings: Settings, now: datetime, running: int | None) -> Check:
    name = "OTLP spans arrive"
    if not settings.traces.enabled:
        return _skip(name, "[traces] disabled")
    path = settings.db_path
    if path is None:
        return _skip(name, "storage is in memory; nothing to read from outside")
    fix = (
        "check the OTLP backend of hermes-otel (endpoint http://<kelpie>:9464/v1/traces, "
        f"Authorization bearer token = ${settings.traces.token_env}); run doctor with the same "
        "KELPIE_DB as kelpie serve"
    )
    try:
        newest = _newest_span(path)
    except sqlite3.Error as exc:
        return _fail(name, f"span store {path} not readable: {exc}", fix)
    if newest is None:
        return _skip(name, f"no span has arrived in {path}; OTLP is optional (quickstart step 4)")
    age = now - datetime.fromisoformat(newest)
    if age > FRESH:  # spans used to arrive and stopped
        return _quiet_or_fail(name, f"newest span is {format_age(age)} old ({path})", fix, running)
    return _pass(name, f"newest span {format_age(age)} old ({path})")


def check_worktrees(adapter: HermesAdapter) -> Check:
    name = "worktrees readable"
    if not adapter.git_state:
        return _skip(name, "git_state = false")
    try:
        paths = adapter.live_workspaces()[:MAX_WORKTREES]
    except (HermesError, sqlite3.Error) as exc:
        return _skip(name, f"board not readable: {exc}")
    if not paths:
        return _skip(name, "no live task has a worktree workspace on this host")
    timeout = adapter.git_timeout.total_seconds()
    deadline = time.monotonic() + GIT_DEADLINE
    problems: Counter[str] = Counter()
    for path in paths:
        state = read_git_state(path, timeout, deadline=deadline)
        if not (path / ".git").exists():
            problems["no .git in the workspace"] += 1
        elif state.problem is not None:
            problems[str(state.problem)] += 1
    failed = sum(problems.values())
    summary = f"{len(paths) - failed} ok, {failed} failed"
    if not failed:
        return _pass(name, summary)
    return _fail(
        name,
        f"{summary} (most common: {problems.most_common(1)[0][0]})",
        "install git where Kelpie runs (docker: derive an image with git, run with init: true) "
        "and run Kelpie as the user that owns the workspaces; or set git_state = false",
    )


def check_prometheus(url: str | None, environ: Mapping[str, str]) -> Check:
    name = "Prometheus reachable"
    explicit = url or environ.get(PROMETHEUS_ENV)
    base = (explicit or DEFAULT_PROMETHEUS).rstrip("/")
    fix = "start Prometheus (docker compose up prometheus) or pass the right --prometheus URL"
    if urlsplit(base).scheme not in ("http", "https"):
        return _fail(name, f"{base}: only http and https URLs are supported", fix)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
    try:
        with opener.open(f"{base}/-/ready", timeout=3) as response:
            problem = "" if response.status == 200 else f"HTTP {response.status}"
    except (OSError, ValueError) as exc:
        problem = str(getattr(exc, "reason", exc))
    if not problem:
        return _pass(name, f"{base}/-/ready")
    if explicit is None:
        return _skip(name, f"nothing at {base}; pass --prometheus URL or set ${PROMETHEUS_ENV}")
    return _fail(name, f"{base}/-/ready: {problem}", fix)


def run_checks(
    config: Path | None,
    prometheus: str | None = None,
    now: Callable[[], datetime] = utc_now,
    environ: Mapping[str, str] = os.environ,
) -> list[Check]:
    try:
        settings = load_settings(config)
        loaded = load_rules(settings.engine)
    except ConfigError as exc:
        fix = "fix the config; kelpie status shows what it parsed"
        return [_fail("config loads", str(exc), fix)]
    at = now().astimezone(UTC)
    checks = [
        _pass("config loads", f"{settings.source or 'defaults'}, {len(loaded.rules)} rules"),
        check_dry_run(settings, loaded),
    ]
    hermes = settings.hermes
    adapter = hermes.build() if hermes is not None and hermes.enabled else None
    why = "no [adapters.hermes] section" if hermes is None else "adapter disabled"
    running: int | None = None
    if adapter is None:
        checks += [_skip("kanban.db", why), _skip("hooks fire", why)]
    else:
        board, running = check_board(adapter)
        checks += [board, check_hooks(adapter, at, running)]
    checks.append(check_spans(settings, at, running))
    checks.append(_skip("worktrees readable", why) if adapter is None else check_worktrees(adapter))
    checks.append(check_prometheus(prometheus, environ))
    return checks


def run(config: Path | None, prometheus: str | None = None) -> tuple[str, int]:
    """Output text and exit code (1 if any check failed)."""
    checks = run_checks(config, prometheus)
    text = "\n".join(c.render() for c in checks) + "\n"
    return text, int(any(c.status == "FAIL" for c in checks))
