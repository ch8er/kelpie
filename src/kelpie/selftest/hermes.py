"""``kelpie selftest stop --adapter hermes``: does rule 10's stop work on this host, with this
Hermes and this Kelpie configuration, before it is trusted in ``act`` on a real farm?

Everything happens in a throwaway ``HERMES_HOME`` (a new temporary directory): the user's
Hermes home, boards, profiles and gateway are never read or written. In it:

1. a dummy profile whose model is :class:`~kelpie.selftest.mock_llm.MockLLM`, a local
   OpenAI-compatible server that always asks for the same ``terminal`` call (zero real
   tokens: Hermes gets no provider key, the profile's model is the mock), with Hermes' own
   loop guard off so the loop runs until something stops it, and Kelpie's hooks plugin
   writing a throwaway spool;
2. a throwaway board with one task assigned to the profile, dispatched once
   (``hermes kanban dispatch``); the dummy profile is the only one in
   ``kanban.dispatch_profiles``;
3. rule 10 (``loop_breaker``, with the thresholds of the user's config) runs in ``act``
   against that board and spool, through the Hermes adapter built from the user's
   ``[adapters.hermes]`` (``cli``, ``cli_timeout``, ``end_worker``, ``worker_grace``,
   ``author``), until it stops the run;
4. checks: the task is blocked for a human, the block is signed by Kelpie, the run's worker
   processes are gone and the model gets no more calls;
5. cleanup: every process still running with that ``HERMES_HOME`` (or working in the
   throwaway directory) is ended, the gateway Hermes started for it included, and the
   directory is removed (unless ``keep``).

Each step prints one line, ``PASS|FAIL  <step>: <detail>``. Alerts are printed, not sent.
"""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.adapters.hermes import adapter as hermes_adapter
from kelpie.adapters.hermes.adapter import HermesAdapter
from kelpie.adapters.hermes.stop import ENV_EXACT, ENV_PREFIXES
from kelpie.adapters.hermes.worker import PROC, Proc, _same, host_table, run_worker
from kelpie.adapters.hermes.worker import end as end_processes
from kelpie.alerts.telegram import format_message
from kelpie.app import ConfigError, HermesSettings, Settings, load_rules
from kelpie.core.actions import ActionKind
from kelpie.core.alerts import AlertEvent, format_age
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.model import Snapshot
from kelpie.core.rule import Rule
from kelpie.rules import REGISTRY
from kelpie.selftest.mock_llm import MODEL, MockLLM

PROFILE = "kelpie-selftest"
BOARD = "kelpie-selftest"
RULE = "loop_breaker"
COMMAND = "sleep 1; echo kelpie-selftest"  # the runaway's call, the same every time
PLUGIN = Path(hermes_adapter.__file__).parent / "plugin.py"
CLI_TIMEOUT = 120.0  # one Hermes command (a first dispatch starts a gateway)
WORKER_TIMEOUT = 120.0  # from dispatch until the agent is bound to the run
STOP_TIMEOUT = 300.0  # from the agent's start until rule 10 stops the run
QUIET = 5.0  # seconds without a model call after the stop
CLEANUP_GRACE = 5.0  # SIGTERM to SIGKILL for leftovers
TICK = 1.0


class Failed(Exception):
    """A step failed; the message says what and, after a newline, what to do."""


@dataclass
class Report:
    out: Callable[[str], None]
    failed: bool = False
    lines: list[str] = field(default_factory=list)

    def ok(self, step: str, detail: str) -> None:
        self._line(f"PASS  {step}: {detail}")

    def fail(self, step: str, detail: str) -> None:
        self.failed = True
        what, _, fix = detail.partition("\n")
        self._line(f"FAIL  {step}: {what}")
        if fix:
            self._line(f"      fix: {fix}")

    def note(self, text: str) -> None:
        self._line(f"      {text}")

    def _line(self, text: str) -> None:
        self.lines.append(text)
        self.out(text)


class CaptureAlerts:
    """The alerter of the self-test: alerts are printed, never sent."""

    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    def send(self, event: AlertEvent) -> None:
        self.events.append(event)


def hermes_env(base: Mapping[str, str], home: Path, spool: Path, user: Path) -> dict[str, str]:
    """Hermes' environment: the allow-list Kelpie gives its CLI, minus every ``HERMES_*`` of
    the user's own (their home, profile, board), plus the throwaway home and spool, and a
    throwaway ``HOME`` (``user``, and no ``XDG_*_HOME``): no provider key in the environment
    and no credential file of the user's (``~/.codex``, ``~/.config/gh``, ...) for Hermes to
    discover."""
    env = {
        k: v
        for k, v in base.items()
        if (k in ENV_EXACT or k.startswith(ENV_PREFIXES))
        and not k.startswith("HERMES_")
        and k not in _XDG_HOMES
    }
    env["HERMES_HOME"] = str(home)
    env["HOME"] = str(user)  # and no XDG_*_HOME: the XDG defaults are under it
    env["KELPIE_HERMES_SPOOL"] = str(spool)
    # Hermes renames its processes (setproctitle), which blanks /proc/<pid>/environ unless
    # this is set: the cleanup finds Hermes' processes by their HERMES_HOME.
    env["SPT_NOENV"] = "1"
    return env


_XDG_HOMES = frozenset({"XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"})


def profile_config(base_url: str) -> str:
    return f"""\
# kelpie selftest: a dummy profile whose model is Kelpie's local mock (zero real tokens)
model:
  provider: custom
  default: {MODEL}
  base_url: {base_url}
  api_key: kelpie-selftest-no-key
  context_length: 131072
  streaming: false
fallback_providers: []
kanban:
  dispatch_in_gateway: false
plugins:
  enabled: [kelpie]
# Hermes' own loop guard would end the turn after a few identical calls; off, the loop runs
# until Kelpie stops it.
tool_loop_guardrails:
  hard_stop_enabled: false
  non_interactive_hard_stop_enabled: false
"""


ROOT_CONFIG = f"""\
# kelpie selftest: a throwaway Hermes home
kanban:
  dispatch_in_gateway: false
  dispatch_profiles: [{PROFILE}]
"""

PLUGIN_YAML = """\
name: kelpie
version: 0.1.0
description: Records tool-call metadata for Kelpie (kelpie selftest).
"""


@dataclass
class Hermes:
    """Runs the Hermes CLI against the throwaway home."""

    exe: str
    env: dict[str, str]
    cwd: Path

    def run(self, *args: str, timeout: float = CLI_TIMEOUT) -> str:
        try:
            proc = subprocess.run(
                [self.exe, *args],
                env=self.env,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise Failed(f"hermes {' '.join(args[:3])} timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise Failed(f"cannot run {self.exe}: {exc}") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout).strip()[-400:]
            raise Failed(f"hermes {' '.join(args[:3])} failed (exit {proc.returncode}): {tail}")
        return proc.stdout

    def json(self, *args: str) -> dict[str, Any]:
        """The first JSON object in the command's output (Hermes may print lines before it)."""
        out = self.run(*args)
        decoder = json.JSONDecoder()
        at = out.find("{")
        while at != -1:
            try:
                value, _ = decoder.raw_decode(out, at)
            except ValueError:
                at = out.find("{", at + 1)
                continue
            if isinstance(value, dict):
                return value
            at = out.find("{", at + 1)
        raise Failed(f"hermes {' '.join(args[:4])} printed no JSON object: {out[-200:]!r}")


def s6_container(proc: Path = PROC, run: Path = Path("/run/s6/basedir")) -> bool:
    """Is this Hermes' s6 container (s6-svscan as pid 1)? There Hermes sends ``gateway`` and
    profile commands to the container's supervisor whatever ``HERMES_HOME`` says, so a
    throwaway home is not throwaway (as ``hermes_cli.service_manager._s6_running``)."""
    try:
        comm = (proc / "1" / "comm").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return comm == "s6-svscan" and run.is_dir()


class _Interrupted(BaseException):
    """SIGTERM while the self-test runs: unwind through the cleanup, then exit."""


def _on_sigterm(signum: int, frame: object) -> None:
    raise _Interrupted


_UNSET = object()


@contextlib.contextmanager
def _signals_ignored() -> Iterator[None]:
    """While cleaning up: a second SIGTERM or Ctrl-C must not cut the cleanup short."""
    saved = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(ValueError):  # not the main thread
            saved.append((sig, signal.signal(sig, signal.SIG_IGN)))
    try:
        yield
    finally:
        for sig, handler in saved:
            signal.signal(sig, handler if handler is not None else signal.SIG_DFL)


def run(settings: Settings, out: Callable[[str], None], *, keep: bool = False) -> int:
    """Run the self-test; 0 when every step passed."""
    report = Report(out)
    hermes = settings.hermes or HermesSettings()
    exe = shutil.which(hermes.cli)
    if exe is None:
        report.fail(
            "Hermes CLI",
            f"{hermes.cli!r} not found\nset [adapters.hermes] cli, or run on the Hermes host",
        )
        return 1
    if s6_container():
        report.fail(
            "host",
            "this is Hermes' s6 container: Hermes would act on the container's gateway, not on "
            "a throwaway one\nrun the self-test on a host (or VM) with Hermes installed",
        )
        return 1
    if (why := host_table().unavailable()) is not None:
        report.fail("host", f"{why}: Kelpie cannot end Hermes workers on this system")
        return 1
    try:
        rule = _rule(settings)
    except (ConfigError, ValueError, TypeError) as exc:
        report.fail("rule 10", f"its configuration does not load: {exc}")
        return 1
    previous: Any = _UNSET
    tmp: Path | None = None
    try:
        with contextlib.suppress(ValueError):  # not the main thread
            previous = signal.signal(signal.SIGTERM, _on_sigterm)
        tmp = Path(tempfile.mkdtemp(prefix="kelpie-selftest-")).resolve()
        home, spool = tmp / "home", tmp / "spool.jsonl"
        home.mkdir(mode=0o700)
        (tmp / "user").mkdir(mode=0o700)
        cli = Hermes(exe, hermes_env(os.environ, home, spool, tmp / "user"), tmp)
        with MockLLM(COMMAND) as llm:
            try:
                _steps(report, cli, hermes, rule, llm, home, spool, tmp)
            except Failed as exc:
                what, _, fix = str(exc).partition("\n")
                report.fail("selftest", f"{what}\n{fix}" if fix else what)
            except (_Interrupted, KeyboardInterrupt):
                report.fail("selftest", "interrupted; cleaning up")
            except Exception as exc:
                report.fail("selftest", f"unexpected {type(exc).__name__}: {exc}")
            finally:
                with _signals_ignored():  # while the mock still answers: no fallback hunting
                    _cleanup(report, tmp)
    except (_Interrupted, KeyboardInterrupt):  # before the steps started
        report.fail("selftest", "interrupted")
    finally:
        if previous is not _UNSET:
            signal.signal(signal.SIGTERM, previous if previous is not None else signal.SIG_DFL)
        if tmp is not None and keep:
            report.note(f"kept {tmp} (Hermes home, spool, logs under home/logs)")
        elif tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
    out("")
    if report.failed:
        out("selftest stop: FAILED (see above); rule 10 is not ready for act on this host")
        return 1
    out("selftest stop: passed")
    return 0


def _steps(
    report: Report,
    cli: Hermes,
    hermes: HermesSettings,
    rule: tuple[Rule, str],
    llm: MockLLM,
    home: Path,
    spool: Path,
    tmp: Path,
) -> None:
    version = cli.run("--version").strip().splitlines()
    report.ok("Hermes CLI", f"{cli.exe} ({version[0] if version else 'version unknown'})")

    cli.run("profile", "create", "--no-alias", "--no-skills", PROFILE)
    profile = home / "profiles" / PROFILE
    if not profile.is_dir():  # e.g. a `cli` that runs Hermes elsewhere (docker exec ...)
        raise Failed(
            f"hermes profile create did not create {profile}: this Hermes does not use the "
            "throwaway HERMES_HOME\nset [adapters.hermes] cli to the hermes executable itself"
        )
    (profile / "config.yaml").write_text(profile_config(llm.base_url), encoding="utf-8")
    (home / "config.yaml").write_text(ROOT_CONFIG, encoding="utf-8")
    plugin = profile / "plugins" / "kelpie"
    plugin.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(PLUGIN, plugin / "__init__.py")
    (plugin / "plugin.yaml").write_text(PLUGIN_YAML, encoding="utf-8")
    report.ok(
        "throwaway Hermes home",
        f"{home}: profile {PROFILE} (model: local mock at {llm.base_url}), Kelpie's plugin",
    )

    cli.run("kanban", "boards", "create", BOARD)
    db = home / "kanban" / "boards" / BOARD / "kanban.db"
    created = cli.json(
        "kanban", "--board", BOARD, "create", "kelpie selftest: a runaway loop",
        "--assignee", PROFILE, "--json",
    )  # fmt: skip
    task = str(created.get("id") or "")
    if not task or not db.exists():
        raise Failed(f"no task or board after hermes kanban create (board {db})")
    dispatched = cli.json("kanban", "--board", BOARD, "dispatch", "--json")
    spawned = {str(s.get("task_id")) for s in dispatched.get("spawned") or ()}
    if task not in spawned:
        if task in {str(s) for s in _ids(dispatched.get("skipped_nonspawnable"))}:
            raise Failed(
                f"Hermes did not spawn {task}: skipped_nonspawnable\n"
                f"kanban.dispatch_profiles must list {PROFILE}"
            )
        raise Failed(f"Hermes did not spawn {task}: {json.dumps(dispatched)[:300]}")
    report.ok("task dispatched", f"{task} on board {BOARD}, assigned to {PROFILE}")

    rid, recorded = _wait_bound(db, task)
    report.ok(
        "worker running",
        f"run {rid}: submitter pid {recorded.submitter}, agent pid {recorded.agent}",
    )

    loop_breaker, thresholds = rule
    key = tmp / "wait.key"  # signs this stop only; tmp is private (mkdtemp: 0700)
    key.write_bytes(secrets.token_bytes(32))
    key.chmod(0o600)
    adapter = replace(
        hermes,
        db_path=db,
        spool=spool,
        git_state=False,
        enabled=True,
        wait_key=key,
    ).build()
    adapter.cli_environ = cli.env  # the block runs against the throwaway home too
    alerts = CaptureAlerts()
    engine = Engine(
        [loop_breaker],
        adapters=[adapter],
        alerter=alerts,
        config=EngineConfig(dry_run=False, modes={RULE: Mode.ACT}),
    )
    started = time.monotonic()
    stop = None
    while stop is None:
        if time.monotonic() - started > STOP_TIMEOUT:
            raise Failed(
                f"rule 10 did not stop run {rid} within {STOP_TIMEOUT:g}s ({_calls(spool, rid)} "
                f"tool calls of the run reached Kelpie; {thresholds})\n"
                "check that Kelpie's plugin loads in this Hermes (kelpie selftest stop --keep, "
                "then the profile's logs)"
            )
        results = engine.tick(lambda: _snapshot(adapter))
        stop = next((r for r in results if r.action.kind is ActionKind.STOP), None)
        if stop is None:
            time.sleep(TICK)
    stopped_at = time.monotonic()
    if stop.outcome is not Outcome.EXECUTED:
        raise Failed(f"rule 10's stop of run {rid} was {stop.outcome.value}: {stop.detail}")
    report.ok(
        "rule 10 stopped the run",
        f"after {_calls(spool, rid)} identical tool calls ({thresholds}), "
        f"{stopped_at - started:.0f}s after the agent started: {stop.detail}",
    )
    for event in alerts.events:
        report.note("the alert, as Telegram would show it:")
        for line in format_message(event).splitlines():
            report.note(f"| {line}")

    _check_board(report, db, task, adapter)
    _check_processes(report, recorded, hermes.end_worker)
    _check_quiet(report, llm, stopped_at)


def _ids(value: object) -> list[object]:
    if not isinstance(value, list):
        return []
    return [v.get("task_id") if isinstance(v, dict) else v for v in value]


def _read(db: Path, sql: str, params: Sequence[object] = ()) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(sql, tuple(params)).fetchall()
    finally:
        conn.close()


def _wait_bound(db: Path, task: str) -> tuple[int, Any]:
    deadline = time.monotonic() + WORKER_TIMEOUT
    while time.monotonic() < deadline:
        rows = _read(
            db,
            "SELECT r.id, r.status, r.error FROM tasks t JOIN task_runs r ON r.task_id = t.id"
            " WHERE t.id = ? ORDER BY r.id DESC LIMIT 1",
            (task,),
        )
        if rows and rows[0]["status"] != "running":
            raise Failed(
                f"run {rows[0]['id']} of {task} ended before it was stopped "
                f"({rows[0]['status']}: {rows[0]['error']})\n"
                "run with --keep and read the profile's logs/agent.log and errors.log"
            )
        if rows:
            conn = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
            try:
                recorded = run_worker(conn, int(rows[0]["id"]))
            finally:
                conn.close()
            if recorded is not None and recorded.agent is not None:
                return int(rows[0]["id"]), recorded
        time.sleep(0.5)
    raise Failed(
        f"no agent started for {task} within {WORKER_TIMEOUT:g}s\n"
        "run with --keep and read home/logs (the gateway) and the profile's logs"
    )


def _rule(settings: Settings) -> tuple[Rule, str]:  # raises ConfigError
    """Rule 10 as the user's config sets it up, whatever its mode there."""
    loaded = load_rules(settings.engine)
    rule = next((r for r in loaded.rules if r.name == RULE), None)
    if rule is None:
        rule = REGISTRY[RULE](settings.engine.rules.get(RULE, {}))
    thresholds = getattr(rule, "thresholds", None)
    calls = getattr(thresholds, "identical_calls", "?")
    window = getattr(thresholds, "identical_window", None)
    within = f" within {format_age(window)}" if isinstance(window, timedelta) else ""
    return rule, f"limit {calls}{within}"


def _snapshot(adapter: HermesAdapter) -> Snapshot:
    return adapter.snapshot(datetime.now(UTC))


def _calls(spool: Path, rid: int) -> int:
    n = 0
    with contextlib.suppress(OSError):
        for line in spool.read_text(encoding="utf-8", errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                entry = json.loads(line)
                n += entry.get("type") == "tool" and str(entry.get("run_id")) == str(rid)
    return n


def _check_board(report: Report, db: Path, task: str, adapter: HermesAdapter) -> None:
    row = _read(db, "SELECT status, block_kind FROM tasks WHERE id = ?", (task,))
    if not row or (row[0]["status"], row[0]["block_kind"]) != ("blocked", "needs_input"):
        state = dict(row[0]) if row else "missing"
        report.fail("task blocked for a human", f"{task} is {state}")
    else:
        report.ok("task blocked for a human", f"{task}: blocked (needs_input)")
    authors = [
        r["author"]
        for r in _read(
            db, "SELECT author FROM task_comments WHERE task_id = ? ORDER BY id", (task,)
        )
    ]
    if authors and all(a == adapter.author for a in authors):
        report.ok("block signed by Kelpie", f"comment author {adapter.author!r}")
    else:
        report.fail(
            "block signed by Kelpie",
            f"comment authors {authors} (expected {adapter.author!r})\n"
            "this Hermes does not sign with $HERMES_PROFILE_NAME; tell the Kelpie maintainers",
        )


def _check_processes(report: Report, recorded: Any, end_worker: bool) -> None:
    pids = [p for p in (recorded.submitter, recorded.agent) if p is not None]
    table = host_table()
    alive = [p for p in pids if (q := table.read(p)) is not None and q.state not in "ZXx"]
    if alive:
        fix = (
            "set [adapters.hermes] end_worker = true"
            if not end_worker
            else "see Kelpie's log line 'run N: worker ...' above"
        )
        report.fail("worker ended", f"still running: pid {alive}\n{fix}")
    else:
        report.ok("worker ended", f"submitter and agent (pid {pids}) are gone")


def _check_quiet(report: Report, llm: MockLLM, stopped_at: float) -> None:
    before = llm.calls
    time.sleep(QUIET)
    after = llm.calls
    last = llm.last_call
    if after > before or (last is not None and last > stopped_at):
        report.fail("no model calls after the stop", f"{after - before} more in {QUIET:g}s")
    else:
        report.ok(
            "no model calls after the stop",
            f"{after} model calls, all answered by the local mock",
        )


def _cleanup(report: Report, tmp: Path) -> None:
    """End every process still running for the throwaway setup (only processes this self-test
    started can carry its ``HERMES_HOME`` or work in its directory): the gateway Hermes started
    for it, a runaway left by a failed step. SIGTERM, then SIGKILL, each re-checked against the
    process's start time (a pid reused meanwhile is never signalled); again until none is left,
    for children started in between. No ``hermes gateway stop``: under a service manager it may
    reach a real gateway."""
    killed = False
    for _ in range(3):
        left = _processes_of(tmp)
        if not left:
            break
        killed = end_processes(left, grace=CLEANUP_GRACE).killed or killed
    if killed:
        report.note("cleanup: some processes of the throwaway home needed SIGKILL")
    if left := _processes_of(tmp):
        pids = [p.pid for p in left]
        report.fail("cleanup", f"processes of the throwaway home still running: pid {pids}")


def _processes_of(tmp: Path) -> list[Proc]:
    """Processes of this user whose ``HERMES_HOME`` is in ``tmp`` or that work in it (Linux:
    ``/proc``; macOS: ``KERN_PROCARGS2`` and libproc). The start time is read before and after
    the environment, so a pid reused in between never counts."""
    table = host_table()
    home = f"HERMES_HOME={tmp}".encode()
    found: list[Proc] = []
    me = os.getpid()
    for pid in table.pids():
        if pid == me or (before := table.read(pid)) is None:
            continue
        env = table.environ(pid) or []
        ours = any(v == home or v.startswith(home + b"/") for v in env)
        if not ours and (cwd := table.cwd(pid)) is not None:
            ours = cwd == tmp or tmp in cwd.parents
        if ours and _same(before, table):
            found.append(before)
    return found
