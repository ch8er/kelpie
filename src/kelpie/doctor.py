"""``kelpie doctor``: is a first live run on a Hermes farm set up correctly?

One line per check, ``PASS|FAIL|SKIP  <check>: <detail>``, and a ``  fix: ...`` line under a
FAIL. Nothing is written anywhere: the kanban database and the span store are opened
read-only. This module may know about adapters and the app wiring; ``kelpie.core`` does not.
"""

from __future__ import annotations

import os
import sqlite3
import urllib.error
import urllib.request
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kelpie.adapters.hermes.adapter import HermesAdapter, HermesError, _age, _workspace
from kelpie.adapters.hermes.git_state import read_git_state
from kelpie.app import ConfigError, LoadedRules, Settings, load_rules, load_settings, utc_now
from kelpie.core.config import Mode

FRESH = timedelta(hours=1)  # newest spool line or span must be younger than this
MAX_WORKTREES = 20  # git is asked about at most this many workspaces
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


def check_board(adapter: HermesAdapter) -> Check:
    name = "kanban.db"
    fix = f"set db_path in [adapters.hermes] to the board's kanban.db (now {adapter.db_path})"
    try:
        with adapter._connect() as conn:
            count = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    except (HermesError, sqlite3.Error) as exc:
        return _fail(name, f"not readable: {exc}", fix)
    return _pass(name, f"readable, {count} tasks ({adapter.db_path})")


def check_hooks(adapter: HermesAdapter, now: datetime) -> Check:
    name = "hooks fire"
    spool = adapter.spool
    if spool is None:
        return _skip(name, "spool disabled")
    fix = (
        "install and enable the Kelpie plugin on the Hermes host (docs/quickstart-hermes.md), "
        "restart the dispatcher and run a task"
    )
    if not spool.exists():
        return _fail(name, f"spool missing ({spool})", fix)
    last = adapter._last_spool_at()
    if last is None:
        return _fail(name, f"spool has no readable lines ({spool})", fix)
    age = now - last
    if age > FRESH:
        return _fail(name, f"newest spool line is {_age(age)} old ({spool})", fix)
    return _pass(name, f"newest spool line {_age(age)} old ({spool})")


def check_spans(settings: Settings, now: datetime) -> Check:
    name = "OTLP spans arrive"
    if not settings.traces.enabled:
        return _skip(name, "[traces] disabled")
    path = settings.db_path
    if path is None:
        return _skip(name, "storage is in memory; nothing to read from outside")
    fix = (
        "point OTEL_EXPORTER_OTLP_TRACES_ENDPOINT at http://<kelpie>:9464/v1/traces (with "
        f"${settings.traces.token_env} as bearer token if set) and run a task; use the same "
        "KELPIE_DB for kelpie serve and kelpie doctor"
    )
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            newest = conn.execute("SELECT MAX(last_at) FROM spans").fetchone()[0]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return _fail(name, f"span store {path} not readable: {exc}", fix)
    if newest is None:
        return _fail(name, f"no spans in {path}", fix)
    age = now - datetime.fromisoformat(newest)
    if age > FRESH:
        return _fail(name, f"newest span is {_age(age)} old ({path})", fix)
    return _pass(name, f"newest span {_age(age)} old ({path})")


def check_worktrees(adapter: HermesAdapter) -> Check:
    name = "worktrees readable"
    if not adapter.git_state:
        return _skip(name, "git_state = false")
    try:
        with adapter._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM tasks WHERE status NOT IN ('done', 'archived')"
            ).fetchall()
    except (HermesError, sqlite3.Error) as exc:
        return _skip(name, f"board not readable: {exc}")
    paths = sorted({p for row in rows if (p := _workspace(row)) is not None})[:MAX_WORKTREES]
    if not paths:
        return _skip(name, "no live task has a worktree workspace on this host")
    timeout = adapter.git_timeout.total_seconds()
    problems: Counter[str] = Counter()
    for path in paths:
        state = read_git_state(path, timeout)
        if not (path / ".git").exists():
            problems["no .git in the workspace"] += 1
        elif state.problem is not None:
            problems[str(state.problem)] += 1
    ok = len(paths) - sum(problems.values())
    summary = f"{ok} ok, {sum(problems.values())} failed"
    if problems:
        summary += f" (most common: {problems.most_common(1)[0][0]})"
    if ok:
        return _pass(name, summary)
    return _fail(
        name,
        summary,
        "install git where Kelpie runs (docker: derive an image with git, run with init: true) "
        "and run Kelpie as the user that owns the workspaces; or set git_state = false",
    )


def check_prometheus(url: str | None, environ: dict[str, str] | os._Environ[str]) -> Check:
    name = "Prometheus reachable"
    explicit = url or environ.get(PROMETHEUS_ENV)
    base = (explicit or DEFAULT_PROMETHEUS).rstrip("/")
    try:
        with urllib.request.urlopen(f"{base}/-/ready", timeout=3) as response:
            status = response.status
        problem = "" if status == 200 else f"HTTP {status}"
    except (OSError, ValueError, urllib.error.URLError) as exc:
        problem = str(getattr(exc, "reason", exc))
    if not problem:
        return _pass(name, f"{base}/-/ready")
    if explicit is None:
        return _skip(name, f"nothing at {base}; pass --prometheus URL or set ${PROMETHEUS_ENV}")
    return _fail(
        name,
        f"{base}/-/ready: {problem}",
        "start Prometheus (docker compose up prometheus) or pass the right --prometheus URL",
    )


def run_checks(
    config: Path | None,
    prometheus: str | None = None,
    now: Callable[[], datetime] = utc_now,
    environ: dict[str, str] | os._Environ[str] = os.environ,
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
    if adapter is None:
        checks += [_skip("kanban.db", why), _skip("hooks fire", why)]
    else:
        checks += [check_board(adapter), check_hooks(adapter, at)]
    checks.append(check_spans(settings, at))
    checks.append(_skip("worktrees readable", why) if adapter is None else check_worktrees(adapter))
    checks.append(check_prometheus(prometheus, environ))
    return checks


def run(config: Path | None, prometheus: str | None = None) -> tuple[str, int]:
    """Output text and exit code (1 if any check failed)."""
    checks = run_checks(config, prometheus)
    text = "\n".join(c.render() for c in checks) + "\n"
    return text, int(any(c.status == "FAIL" for c in checks))
