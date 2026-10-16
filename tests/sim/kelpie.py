"""Runs the real ``kelpie`` CLI as a subprocess and watches it from outside.

``kelpie serve`` gets a generated config, a free loopback port, a clean environment (only
what the scenario passes: no tokens of the machine running the tests leak in) and its own
``HOME``. Its output goes to a log file; :meth:`KelpieServe.problems` returns every
``ERROR``/``CRITICAL`` line, every traceback and every WARNING that reports a failure (an
action or an alert delivery that failed, a connector read that failed: Kelpie logs those as
warnings because it keeps going) so a scenario can fail on them. Ticks are counted on
``/metrics`` (``kelpie_engine_ticks_total``).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

# "%(asctime)s %(levelname)s %(name)s %(message)s", asctime like "2026-10-16 01:02:03,123"
_LEVEL = re.compile(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d+ (?P<level>[A-Z]+) ")
_PROBLEM_LEVELS = {"ERROR", "CRITICAL"}
# WARNING lines that mean something failed (kelpie.core.engine, kelpie.alerts.delivery,
# kelpie.connectors.github, kelpie.app).
FAILURE_WARNINGS = tuple(
    re.compile(p)
    for p in (
        r"action failed: ",
        r"cannot execute; alerting instead",
        r"no executor for action",
        r"no alerter configured; dropped",
        r"alert channel .*: (delivery failed|dropped an alert|an alert was dropped)",
        r"alerts: unknown channels",
        r"block condition for task .* failed",
        r"condition .* could not be checked",
        r"runs not read",
        r"control loop still busy",
    )
)
_METRIC = re.compile(r"^(?P<name>[a-zA-Z_:][\w:]*)(?P<labels>\{[^}]*\})? (?P<value>\S+)$")


def kelpie_command() -> list[str]:
    """The installed ``kelpie`` entry point of this environment."""
    script = Path(sys.executable).with_name("kelpie")
    if script.exists():
        return [str(script)]
    found = shutil.which("kelpie")
    return [found] if found else [sys.executable, "-m", "kelpie.cli"]


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def base_env(home: Path, extra: Mapping[str, str]) -> dict[str, str]:
    """A clean environment: PATH, a private HOME, UTF-8, and the scenario's variables."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PYTHONUNBUFFERED": "1",
    }
    if "VIRTUAL_ENV" in os.environ:
        env["VIRTUAL_ENV"] = os.environ["VIRTUAL_ENV"]
    env.update(extra)
    return env


def wait_for[T](
    what: str, probe: Callable[[], T | None], timeout: float = 20.0, every: float = 0.1
) -> T:
    """Poll ``probe`` until it returns something truthy; ``AssertionError`` on timeout."""
    deadline = time.monotonic() + timeout
    while True:
        result = probe()
        if result:
            return result
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {timeout:g}s waiting for {what}")
        time.sleep(every)


def problems_in(
    lines: Sequence[str],
    allowed: Sequence[re.Pattern[str]] = (),
    patterns: Sequence[re.Pattern[str]] = (),
) -> list[str]:
    """Log lines at ERROR or above, failure WARNINGs (:data:`FAILURE_WARNINGS` and
    ``patterns``) and tracebacks that no ``allowed`` pattern matches. A traceback right
    after an allowed line belongs to it."""
    found = []
    excused = False  # inside the traceback of an allowed line
    for line in lines:
        m = _LEVEL.match(line)
        if m is None:
            if line.startswith("Traceback (most recent call last)") and not excused:
                found.append(line)
            continue
        bad = m["level"] in _PROBLEM_LEVELS or (
            m["level"] == "WARNING" and any(p.search(line) for p in (*FAILURE_WARNINGS, *patterns))
        )
        excused = bad and any(p.search(line) for p in allowed)
        if bad and not excused:
            found.append(line)
    return found


@dataclass
class CliResult:
    code: int
    stdout: str
    stderr: str


class KelpieServe:
    """One ``kelpie serve`` process for a scenario."""

    def __init__(self, workdir: Path, config: str, env: Mapping[str, str]) -> None:
        self.workdir = workdir
        self.config_path = workdir / "kelpie.toml"
        self.config_path.write_text(config, encoding="utf-8")
        self.home = workdir / "home"
        self.home.mkdir(exist_ok=True)
        self.env = base_env(self.home, env)
        self.port = free_port()
        self.log_path = workdir / "kelpie-serve.log"
        self.proc: subprocess.Popen[bytes] | None = None
        self.returncode: int | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def cli(self, *args: str, timeout: float = 60) -> CliResult:
        """Run another ``kelpie`` command (``report``, ``status``...) on the same config."""
        proc = subprocess.run(
            [*kelpie_command(), "--config", str(self.config_path), *args],
            cwd=self.workdir,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CliResult(proc.returncode, proc.stdout, proc.stderr)

    def start(self, attempts: int = 3) -> None:
        """Start ``kelpie serve`` and wait for ``/healthz``. The port is picked free just
        before, so another process can take it in between: then try another one."""
        for attempt in range(attempts):
            try:
                self._spawn()
                return
            except AssertionError:
                taken = "Address already in use" in self.log_text()
                if not taken or attempt == attempts - 1:
                    raise
                self.port = free_port()

    def _spawn(self) -> None:
        log = self.log_path.open("wb")
        try:
            self.proc = subprocess.Popen(
                [
                    *kelpie_command(),
                    "--config",
                    str(self.config_path),
                    "--log-level",
                    "INFO",
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                ],
                cwd=self.workdir,
                env=self.env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        finally:
            log.close()
        wait_for("kelpie serve to answer /healthz", self._healthy, timeout=30)

    def _healthy(self) -> bool:
        assert self.proc is not None
        if self.proc.poll() is not None:
            raise AssertionError(
                f"kelpie serve exited with {self.proc.returncode}:\n{self.log_text()}"
            )
        try:
            with urllib.request.urlopen(f"{self.url}/healthz", timeout=1) as r:
                return r.status == 200
        except OSError:
            return False

    def metrics(self) -> dict[str, float]:
        """``/metrics`` as ``{"name{labels}": value}``."""
        with urllib.request.urlopen(f"{self.url}/metrics", timeout=5) as r:
            text = r.read().decode()
        out: dict[str, float] = {}
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            m = _METRIC.match(line)
            if m:
                out[m["name"] + (m["labels"] or "")] = float(m["value"])
        return out

    def ticks(self) -> int:
        return int(self.metrics().get("kelpie_engine_ticks_total", 0))

    def _ticks_or_none(self) -> int | None:
        try:
            return self.ticks()
        except OSError:  # one slow or refused read: the next poll tries again
            return None

    def wait_ticks(self, n: int, timeout: float = 60) -> int:
        """Wait until ``n`` more ticks have completed than when called."""
        (now,) = wait_for(
            "a /metrics answer",
            lambda: (t := self._ticks_or_none()) is not None and (t,),
            timeout,
        )
        target = now + n
        return wait_for(
            f"{n} control-loop ticks",
            lambda: (t := self._ticks_or_none()) is not None and t >= target and t,
            timeout,
        )

    def stop(self, timeout: float = 15) -> int:
        """SIGTERM, as a container runtime sends it; the exit code."""
        if self.proc is None:
            return 0
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
                raise AssertionError(
                    f"kelpie serve did not stop within {timeout}s:\n{self.log_text()[-6000:]}"
                ) from None
        self.returncode = self.proc.returncode
        return self.returncode

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def log_lines(self) -> list[str]:
        return self.log_text().splitlines()

    def problems(self, allowed: Sequence[re.Pattern[str]] = ()) -> list[str]:
        return problems_in(self.log_lines(), allowed)
