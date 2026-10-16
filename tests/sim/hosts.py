"""A farm on two machines: each host has its own Hermes board, spool and CLI and runs the
real ``kelpie agent``; one central ``kelpie serve`` with ``[hub]`` merges them.

Both hosts and the server share loopback, so "two machines" means two boards, two CLIs and
two agent processes that know nothing of each other; the hub tells them apart only by
token. Host boards use the same task ids on purpose: namespacing has to keep them apart.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from tests.sim import toml
from tests.sim.channels import Channels
from tests.sim.farm import Farm, merge
from tests.sim.github import FakeGitHub
from tests.sim.kelpie import KelpieServe, base_env, free_port, kelpie_command, problems_in, wait_for

HOSTS = ("laptop", "vm-1")


def token_env(host: str) -> str:
    return f"KELPIE_SIM_AGENT_{host.upper().replace('-', '_')}"


def token(host: str) -> str:
    return f"sim-token-{host}-" + "x" * 32


class AgentHost:
    """One machine with agents: a Hermes board (a :class:`Farm`'s) and a ``kelpie agent``."""

    def __init__(self, name: str, root: Path, channels: Channels, github: FakeGitHub) -> None:
        self.name = name
        self.root = root / name
        self.farm = Farm(self.root, channels, github)  # board, spool and CLI only
        self.log_path = self.root / "kelpie-agent.log"
        self.proc: subprocess.Popen[bytes] | None = None
        self.home = self.root / "home"
        self.home.mkdir()
        self.runs = 0

    def config(self, server: str) -> dict[str, Any]:
        hermes = self.farm.config()["adapters"]["hermes"]
        return {
            "agent": {
                "name": self.name,
                "server": server,
                "token_env": token_env(self.name),
                "interval": "1s",
                "allow_plain_http": True,
                "relay": False,
            },
            "adapters": {"hermes": hermes},
            "storage": {"path": str(self.root / "agent.db")},  # the Hermes park key lives here
        }

    def start(self, server: str) -> None:
        path = self.root / "kelpie-agent.toml"
        path.write_text(toml.dumps(self.config(server)), encoding="utf-8")
        self.runs += 1
        log = self.log_path.open("ab")
        try:
            self.proc = subprocess.Popen(
                [*kelpie_command(), "--config", str(path), "--log-level", "INFO", "agent"],
                cwd=self.root,
                env=base_env(self.home, {token_env(self.name): token(self.name)}),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        finally:
            log.close()

    def kill(self) -> None:
        """The machine goes away: no goodbye."""
        if self.proc is not None and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()

    def stop(self, timeout: float = 15) -> int:
        if self.proc is None:
            return 0
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
                raise AssertionError(f"kelpie agent {self.name} did not stop") from None
        return self.proc.returncode

    def log_text(self) -> str:
        try:
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""


class MultiHostFarm:
    """Two :class:`AgentHost` and the central ``kelpie serve`` with a hub."""

    def __init__(self, root: Path, channels: Channels, github: FakeGitHub) -> None:
        self.root = root
        self.channels = channels
        self.hosts = {name: AgentHost(name, root, channels, github) for name in HOSTS}
        self.hub_port = free_port()
        self.central = root / "central"
        self.central.mkdir()
        self.kelpie: KelpieServe | None = None

    @property
    def hub_url(self) -> str:
        return f"http://127.0.0.1:{self.hub_port}"

    def config(
        self,
        rules: Mapping[str, Mapping[str, Any]] | None = None,
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        config: dict[str, Any] = {
            "engine": {
                "interval": "1s",
                "dry_run": False,
                "default_mode": "observe",
                "rules": {name: dict(section) for name, section in (rules or {}).items()},
            },
            "storage": {"path": str(self.central / "kelpie.db")},
            "alerts": self.channels.config(),
            "hub": {
                "listen": f"127.0.0.1:{self.hub_port}",
                "allow_plain_http": True,
                "silent_after": "10s",
                "action_timeout": "10s",
                "agents": [{"name": n, "token_env": token_env(n)} for n in HOSTS],
            },
        }
        return merge(config, extra or {})

    def serve(self, config: Mapping[str, Any]) -> KelpieServe:
        env = {**self.channels.env(), **{token_env(n): token(n) for n in HOSTS}}
        self.kelpie = KelpieServe(self.central, toml.dumps(config), env)
        self.kelpie.start()
        for host in self.hosts.values():
            host.start(self.hub_url)
        for name in HOSTS:
            self.wait_up(name)
        return self.kelpie

    def up(self, host: str) -> bool:
        assert self.kelpie is not None
        return self.kelpie.metrics().get(f'kelpie_agent_up{{agent="{host}"}}') == 1

    def wait_up(self, host: str, timeout: float = 30) -> None:
        wait_for(f"agent {host} to report", lambda: self.up(host), timeout)

    def messages(self, *needles: str) -> list[str]:
        return [t for t in self.channels.telegram_texts() if all(n in t for n in needles)]

    def wait_message(self, *needles: str, timeout: float = 30) -> str:
        return wait_for(
            f"a Telegram message with {needles}",
            lambda: next(iter(self.messages(*needles)), None),
            timeout,
        )

    def close(self) -> None:
        problems: list[str] = []
        for host in self.hosts.values():
            code = host.stop()
            lines = host.log_text().splitlines()
            # A killed agent's server-side counterpart logs nothing bad; the agent itself
            # may log the hub going away during shutdown.
            found = problems_in(lines)
            if code not in (0, -9) or found:
                problems.append(f"agent {host.name} exited with {code}: {found}")
        if self.kelpie is not None:
            code = self.kelpie.stop()
            found = self.kelpie.problems()
            if code != 0 or found:
                problems.append(f"kelpie serve exited with {code}: {found}")
        if problems:
            raise AssertionError("\n".join(problems))
