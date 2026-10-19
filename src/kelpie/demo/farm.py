"""The demo's farm: the packaged simulator (:mod:`kelpie.sim`) and a real Kelpie watching it.

Everything runs in this process on a virtual clock, so a working day passes in seconds and
every run prints the same story: the board, the hooks spool and the fake ``hermes`` CLI read
their times from the clock, and Kelpie (built by :func:`kelpie.app.build_app` from a
generated config, as ``kelpie serve`` builds it) gets the clock instead of the wall clock.
Its control loop does not run on its own: :meth:`DemoFarm.tick` runs one engine tick, so the
story decides when Kelpie looks.

Nothing leaves the machine. GitHub is a fake on loopback with a fake token; there are no
alert channels: an alerter here keeps what Kelpie would send to Telegram or Slack, for the
narration. Nothing of a real install is touched either: Kelpie is built with the fakes'
tokens in its environment and without ``KELPIE_DB`` (which would point it at the real
database), and the environment is restored right after.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from collections.abc import Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.alerts import telegram
from kelpie.app import App, build_app, load_settings
from kelpie.core.alerts import AlertEvent
from kelpie.core.ledger import Dispatched
from kelpie.sim import github, hermes_cli, otlp, toml
from kelpie.sim.board import Board
from kelpie.sim.spool import Spool

REPO = "example/shop"
GITHUB_TOKEN_ENV = "KELPIE_DEMO_GITHUB_TOKEN"
TRACES_TOKEN_ENV = "KELPIE_DEMO_TRACES_TOKEN"
TRACES_TOKEN = "demo-traces-token"  # what the simulated agents send; not a secret
CONFIG = "kelpie.toml"
LOG = "kelpie-demo.log"


@dataclass(frozen=True, slots=True)
class Message:
    """An alert as the engine handed it over, and its text as Telegram would carry it."""

    event: AlertEvent

    @property
    def rule(self) -> str:
        return self.event.rule

    @property
    def severity(self) -> str:
        return self.event.severity.value

    @property
    def text(self) -> str:
        return telegram.format_message(self.event)

    @property
    def did(self) -> str | None:
        """What Kelpie did, as the engine worded it (None: it only sent the alert)."""
        return self.event.did_text

    def mentions(self, *needles: str) -> bool:
        return all(n in self.text for n in needles)


class Inbox:
    """The alerter Kelpie's engine sends to (``kelpie.core.ports.Alerter``)."""

    def __init__(self) -> None:
        self.messages: list[Message] = []

    def send(self, event: AlertEvent) -> None:
        self.messages.append(Message(event))


class DemoFarm:
    """A temporary farm directory with a Hermes board, its spool and CLI, a fake GitHub and,
    once :meth:`start` is called, Kelpie. Close it to stop everything."""

    def __init__(self, now: datetime, root: Path | None = None) -> None:
        self._stack = ExitStack()
        self._owned = root is None
        self.root = Path(tempfile.mkdtemp(prefix="kelpie-demo-")) if root is None else root
        self.root.mkdir(parents=True, exist_ok=True)
        self.now = now
        hermes_home = self.root / "hermes"
        hermes_home.mkdir(exist_ok=True)
        (self.root / "bin").mkdir(exist_ok=True)
        self._clock_file = self.root / "clock"
        self._write_clock()
        self.board = Board(hermes_home / "kanban.db")
        self.spool = Spool(hermes_home / "spool.jsonl")
        self.hermes_log = hermes_home / hermes_cli.CALLS
        self.cli = hermes_cli.install(self.root / "bin", self.hermes_log, self._clock_file)
        self.github = self._stack.enter_context(github.serve())
        self.github.repo(REPO)
        self.inbox = Inbox()
        self.app: App | None = None
        self.results: list[Dispatched] = []

    # --- time ---------------------------------------------------------------------------

    def ago(self, **kw: float) -> datetime:
        return self.now - timedelta(**kw)

    def advance(self, **kw: float) -> datetime:
        self.now += timedelta(**kw)
        self._write_clock()
        return self.now

    def at(self, when: datetime) -> datetime:
        if when < self.now:
            raise ValueError("the demo clock only moves forward")
        self.now = when
        self._write_clock()
        return self.now

    def _write_clock(self) -> None:
        self._clock_file.write_text(f"{self.now.timestamp():.0f}\n", encoding="utf-8")

    # --- kelpie -------------------------------------------------------------------------

    def config(self, rules: Mapping[str, Mapping[str, Any]], **extra: Any) -> dict[str, Any]:
        return {
            "engine": {
                "interval": "1m",
                "dry_run": False,
                "default_mode": "observe",
                "rules": {name: dict(section) for name, section in rules.items()},
            },
            "storage": {"path": str(self.root / "kelpie.db")},
            "traces": {"enabled": True, "token_env": TRACES_TOKEN_ENV},
            "adapters": {
                "hermes": {
                    "db_path": str(self.board.path),
                    "spool": str(self.spool.path),
                    "cli": str(self.cli),
                    "git_state": False,
                    "lookback": "24h",
                    **extra.pop("hermes", {}),
                }
            },
            "connectors": {
                "github": {
                    "repos": [REPO],
                    "api_url": self.github.url,
                    "token_env": GITHUB_TOKEN_ENV,
                    "cache_ttl": "30s",
                    "timeout": "5s",
                    **extra.pop("github", {}),
                }
            },
            **extra,
        }

    def start(self, config: Mapping[str, Any]) -> App:
        """Write ``config`` next to the board and build Kelpie from it."""
        path = self.root / CONFIG
        path.write_text(toml.dumps(config), encoding="utf-8")
        with _environment({GITHUB_TOKEN_ENV: github.TOKEN, TRACES_TOKEN_ENV: TRACES_TOKEN}):
            app = build_app(load_settings(path), clock=lambda: self.now)
        app.engine.alerter = self.inbox
        self.app = app
        self._stack.callback(app.close)
        return app

    def tick(self) -> list[Dispatched]:
        """One control-loop tick at the current time; what the engine did with each action."""
        assert self.app is not None, "start Kelpie first"
        results = self.app.engine.tick(self.app.loop.snapshot_source)
        self.results.extend(results)
        return results

    def export(self, calls: Sequence[otlp.ModelCall]) -> None:
        """An agent's OTLP exporter sends these model calls to Kelpie's ``/v1/traces``."""
        assert self.app is not None and self.app.ingest is not None, "start Kelpie first"
        body = otlp.request(calls)
        headers = {
            "Content-Type": otlp.CONTENT_TYPE,
            "Content-Length": str(len(body)),
            "Authorization": f"Bearer {TRACES_TOKEN}",
        }
        reply = self.app.ingest.handle(headers, io.BytesIO(body).read)
        if reply.status != 200:
            raise RuntimeError(f"Kelpie refused the spans: {reply.status} {reply.body!r}")

    def hermes_calls(self) -> list[dict[str, object]]:
        return hermes_cli.calls(self.hermes_log)

    # --- lifetime -----------------------------------------------------------------------

    def close(self, keep: bool = False) -> None:
        """Stop Kelpie and the fake GitHub; remove the farm directory unless ``keep``."""
        self._stack.close()
        if not keep:
            self.remove()

    def remove(self) -> None:
        """Remove the farm directory, if this farm made it."""
        if self._owned:
            shutil.rmtree(self.root, ignore_errors=True)


# Environment variables that would point the demo's Kelpie at a real install.
_REAL = ("KELPIE_DB",)


@contextmanager
def _environment(values: Mapping[str, str]) -> Iterator[None]:
    """``values`` set and :data:`_REAL` unset while Kelpie is built, then all as before."""
    saved = {name: os.environ.get(name) for name in (*values, *_REAL)}
    try:
        for name in _REAL:
            os.environ.pop(name, None)
        os.environ.update(values)
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
