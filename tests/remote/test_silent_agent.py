"""A silent agent does not make its tasks look abandoned (#210), on a clock the test drives.

The scenario of ``tests/e2e/test_multi_host.py``, in one process and deterministic: the
central ``kelpie serve`` (``build_app`` with ``[hub]``, the real HTTP hub) and two real
``kelpie agent`` (:class:`~kelpie.remote.agent.Agent`) on Hermes boards of their own. Every
tick moves a fake clock one second, writes the live runs' heartbeats at that time, has each
running agent report and runs one control-loop tick: no sleeps, no wall clock, so the
messages in a window of silence are exactly the expected ones.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import App, build_adapters, build_app, load_settings
from kelpie.core.alerts import AlertEvent, Transition
from kelpie.remote.agent import Agent
from kelpie.remote.config import AgentSettings
from kelpie.remote.hub import HubService
from kelpie.sim import toml
from kelpie.sim.board import Board, connect, epoch
from tests.remote.test_wiring import free_port

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
HOSTS = ("laptop", "vm-1")
TOKENS = {"laptop": "sim-token-laptop-" + "x" * 32, "vm-1": "sim-token-vm-1-" + "y" * 32}
_METRIC = re.compile(r"^(?P<key>[a-zA-Z_:][\w:]*(?:\{[^}]*\})?) (?P<value>\S+)$")


def token_env(host: str) -> str:
    return f"KELPIE_TEST_AGENT_{host.upper().replace('-', '_')}"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Recorder:
    """The alert channel: every event the engine hands over, in order."""

    def __init__(self) -> None:
        self.events: list[AlertEvent] = []

    def send(self, event: AlertEvent) -> None:
        self.events.append(event)


def about(event: AlertEvent, *needles: str) -> bool:
    text = repr(event)
    return all(n in text for n in needles)


class Host:
    """One machine: a Hermes board with an unowned review blocking two tasks, a live run,
    and a ``kelpie agent`` that reports it."""

    def __init__(self, root: Path, name: str, clock: Clock, server: str) -> None:
        self.name = name
        self.clock = clock
        self.server = server
        self.dir = root / name
        self.dir.mkdir()
        self.board = Board(self.dir / "kanban.db")
        b = self.board
        b.task("t_auth", "todo", NOW - timedelta(hours=6), title="Shared auth", assignee="dev")
        b.claim("t_auth", "dev", NOW - timedelta(hours=5))
        b.hand_off("t_auth", NOW - timedelta(hours=3))
        b.update("t_auth", assignee=None)
        for i in range(2):
            b.task(f"t_dep{i}", "todo", NOW - timedelta(hours=6), title=f"Protect endpoint {i}")
            b.link("t_auth", f"t_dep{i}")
        b.task("t_live", "todo", NOW - timedelta(hours=2), title="Paginate", assignee="ops")
        self.run_id = b.claim(
            "t_live",
            "ops",
            NOW - timedelta(minutes=30),
            heartbeat=NOW,
            claim_expires=NOW + timedelta(hours=2),
        )
        config = self.dir / "kelpie-agent.toml"
        hermes = {"db_path": str(b.path), "spool": False, "git_state": False, "lookback": "24h"}
        storage = {"path": str(self.dir / "agent.db")}
        config.write_text(toml.dumps({"adapters": {"hermes": hermes}, "storage": storage}))
        self.adapters = build_adapters(load_settings(config))
        self.agent: Agent | None = None
        self.start()

    def start(self) -> None:
        """A new ``kelpie agent`` process (a new boot id)."""
        settings = AgentSettings(
            name=self.name,
            server=self.server,
            allow_plain_http=True,
            interval=timedelta(seconds=1),
        )
        self.agent = Agent(settings, self.adapters, TOKENS[self.name], clock=self.clock)

    def kill(self) -> None:
        self.agent = None

    def beat(self) -> None:
        """Hermes keeps heartbeating its live run, whether or not the agent is up."""
        at = epoch(self.clock.now)
        with connect(self.board.path) as conn:
            conn.execute(
                "UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (at, self.run_id)
            )
            conn.execute("UPDATE tasks SET last_heartbeat_at = ? WHERE id = 't_live'", (at,))

    def report(self) -> None:
        if self.agent is not None:
            self.agent.report()


class Farm:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        for host in HOSTS:
            monkeypatch.setenv(token_env(host), TOKENS[host])
        self.clock = Clock()
        port = free_port()
        central = root / "central"
        central.mkdir()
        config = central / "kelpie.toml"
        stale = {"session_stale_after": "20s", "session_exit_grace": "0s"}
        config.write_text(
            toml.dumps(
                {
                    "engine": {
                        "interval": "1s",
                        "dry_run": False,
                        "default_mode": "observe",
                        # Without the freeze, a state its rule stops reporting resolves in 2s.
                        "state_resolve_after": "2s",
                        "rules": {
                            "orphan_status": {"mode": "suggest", "session_stale_after": "60m"},
                            "exit_contract": {"mode": "suggest", **stale},
                        },
                    },
                    "storage": {"path": str(central / "kelpie.db")},
                    "hub": {
                        "listen": f"127.0.0.1:{port}",
                        "allow_plain_http": True,
                        "silent_after": "10s",
                        "action_timeout": "10s",
                        "agents": [{"name": h, "token_env": token_env(h)} for h in HOSTS],
                    },
                }
            )
        )
        self.app: App = build_app(load_settings(config), clock=self.clock)
        (self.service,) = [s for s in self.app.services if isinstance(s, HubService)]
        self.service.start()
        self.alerts = Recorder()
        self.app.engine.alerter = self.alerts
        server = f"http://127.0.0.1:{port}"
        self.hosts = {name: Host(root, name, self.clock, server) for name in HOSTS}

    def tick(self, n: int = 1) -> None:
        """``n`` seconds: heartbeats, each running agent's report, one control-loop tick."""
        for _ in range(n):
            self.clock.now += timedelta(seconds=1)
            for host in self.hosts.values():
                host.beat()
                host.report()
            self.app.loop.tick_once()

    def metrics(self) -> dict[str, float]:
        out = {}
        for line in self.app.metrics.render().splitlines():
            if (m := _METRIC.match(line)) is not None:
                out[m["key"]] = float(m["value"])
        return out

    def open_states(self, rule: str) -> float:
        return self.metrics().get(f'kelpie_open_states{{rule="{rule}"}}', 0)

    def task_count(self) -> float:
        return sum(v for k, v in self.metrics().items() if k.startswith("kelpie_tasks{"))

    def close(self) -> None:
        self.service.stop()
        self.app.close()


@pytest.fixture
def farm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Farm]:
    f = Farm(tmp_path, monkeypatch)
    try:
        yield f
    finally:
        f.close()


def test_a_silent_agent_does_not_make_its_tasks_look_abandoned(farm: Farm) -> None:
    farm.tick(3)
    orphans = [e for e in farm.alerts.events if e.rule == "orphan_status"]
    assert [about(e, "laptop:t_auth") for e in orphans] == [True, False]
    assert [about(e, "vm-1:t_auth") for e in orphans] == [False, True]
    assert farm.open_states("orphan_status") == 2
    before = len(farm.alerts.events)

    farm.hosts["laptop"].kill()
    farm.tick(15)  # past [hub] silent_after (10s)
    (silent,) = farm.alerts.events[before:]
    assert silent.rule == "kelpie" and "agent laptop silent" in silent.subject
    assert "not evaluated" in silent.reason and silent.transition is Transition.NEW
    m = farm.metrics()
    assert m['kelpie_agent_up{agent="laptop"}'] == 0 and m['kelpie_agent_up{agent="vm-1"}'] == 1
    assert farm.task_count() == 4  # only vm-1's board is evaluated

    # The other machine carries on: vm-1's review gets an owner and its alert resolves.
    farm.hosts["vm-1"].board.update("t_auth", assignee="dev")
    farm.tick(5)
    resolved = farm.alerts.events[-1]
    assert about(resolved, "vm-1:t_auth") and resolved.transition is Transition.RESOLVED
    # Long enough for a stale laptop board to show a dead session (20s) and for any laptop
    # state to resolve (2s): nothing about the laptop's tasks.
    farm.tick(30)
    assert farm.alerts.events[before:] == [silent, resolved]
    assert farm.open_states("orphan_status") == 1  # the laptop's, held

    farm.hosts["laptop"].start()
    farm.tick(1)
    back = farm.alerts.events[-1]
    assert "agent laptop silent" in back.subject and back.transition is Transition.RESOLVED
    farm.tick(5)
    after = farm.alerts.events[before:]
    assert after == [silent, resolved, back]  # nothing about the laptop's tasks
    assert len([e for e in farm.alerts.events if about(e, "orphan_status", "laptop:t_auth")]) == 1
    assert farm.task_count() == 8
    assert farm.metrics()['kelpie_agent_up{agent="laptop"}'] == 1
