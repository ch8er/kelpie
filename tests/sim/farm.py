"""One simulated farm: a Hermes board, its spool and CLI, fake GitHub and alert channels,
and the ``kelpie serve`` that watches them.

Times on the board are relative to :attr:`Farm.now`, taken when the farm is built: Kelpie
reads the wall clock, so "blocked 3 hours ago" must be three hours before the real now.

:meth:`Farm.config` writes a complete config: a 1 second control loop, dry-run off, every
rule in ``observe`` unless the scenario sets its mode, the Hermes adapter on this board with
the fake CLI, both alert channels and (with ``github=True``) the GitHub connector on the fake
API. Scenarios pass rule sections and any extra sections to merge in.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from tests.sim import hermes_cli, toml
from tests.sim.board import Board
from tests.sim.channels import Channels
from tests.sim.github import TOKEN as GITHUB_TOKEN
from tests.sim.github import FakeGitHub
from tests.sim.kelpie import KelpieServe
from tests.sim.spool import Spool

REPO_ROOT = Path(__file__).resolve().parents[2]
GITHUB_TOKEN_ENV = "KELPIE_SIM_GITHUB_TOKEN"
REPO = "example/repo"
# Recommended on Hermes (README): Hermes tolerates heartbeats up to an hour old.
HERMES_STALE = {"session_stale_after": "60m"}


def merge(base: dict[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-merge ``extra`` into a copy of ``base`` (tables merge, everything else replaces)."""
    out = dict(base)
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = merge(dict(out[key]), value)
        else:
            out[key] = value
    return out


class Farm:
    def __init__(self, root: Path, channels: Channels, github: FakeGitHub) -> None:
        self.root = root
        self.now = datetime.now(UTC).replace(microsecond=0)
        hermes_home = root / "hermes"
        hermes_home.mkdir(parents=True)
        self.board = Board(hermes_home / "kanban.db")
        self.spool = Spool(hermes_home / "kelpie" / "spool.jsonl")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        self.hermes_path = hermes_cli.install(bin_dir, REPO_ROOT)
        self.channels = channels
        self.github = github
        self.kelpie: KelpieServe | None = None
        # Log lines a scenario expects at ERROR level (each is a regex).
        self.allowed_errors: list[re.Pattern[str]] = []

    def ago(self, **kw: float) -> datetime:
        return self.now - timedelta(**kw)

    # --- config ------------------------------------------------------------------------

    def config(
        self,
        rules: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        github: bool = False,
        human_owners: Sequence[str] = (),
        extra: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        hermes: dict[str, Any] = {
            "db_path": str(self.board.path),
            "spool": str(self.spool.path),
            "cli": str(self.hermes_path),
            "cli_timeout": "10s",
            "git_state": False,
            "lookback": "24h",
        }
        if human_owners:
            hermes["human_owners"] = list(human_owners)
        config: dict[str, Any] = {
            "engine": {
                "interval": "1s",
                "dry_run": False,
                "default_mode": "observe",
                "rules": {name: dict(section) for name, section in (rules or {}).items()},
            },
            "storage": {"path": str(self.root / "kelpie.db")},
            "adapters": {"hermes": hermes},
            "alerts": self.channels.config(),
        }
        if github:
            config["connectors"] = {
                "github": {
                    "repos": [REPO],
                    "api_url": self.github.url,
                    "token_env": GITHUB_TOKEN_ENV,
                    "cache_ttl": "30s",
                    "timeout": "5s",
                }
            }
        return merge(config, extra or {})

    def env(self) -> dict[str, str]:
        return {**self.channels.env(), GITHUB_TOKEN_ENV: GITHUB_TOKEN}

    # --- kelpie --------------------------------------------------------------------------

    def serve(self, config: Mapping[str, Any]) -> KelpieServe:
        """Write ``config`` and start ``kelpie serve`` on it."""
        assert self.kelpie is None, "one kelpie serve per farm"
        self.kelpie = KelpieServe(self.root, toml.dumps(config), self.env())
        self.kelpie.start()
        return self.kelpie

    def hermes_calls(self) -> list[dict[str, Any]]:
        return hermes_cli.calls(self.board.path.parent)

    def close(self) -> None:
        """Stop ``kelpie serve``; fail on a bad exit or any unexpected ERROR in its log."""
        kelpie = self.kelpie
        if kelpie is None:
            return
        code = kelpie.stop()
        problems = kelpie.problems(self.allowed_errors)
        if code != 0 or problems:
            raise AssertionError(
                f"kelpie serve exited with {code}; unexpected errors:\n"
                + "\n".join(problems)
                + f"\n--- full log: {kelpie.log_path}\n{kelpie.log_text()[-6000:]}"
            )
