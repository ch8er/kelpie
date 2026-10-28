"""Wires the engine for ``kelpie serve`` and ``kelpie status``.

This module sits outside ``kelpie.core`` on purpose: it may know about rules (and, later,
adapters and connectors) and it may read the clock. Rules never do.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from kelpie.adapters.claude_code.config import ClaudeCodeSettings, load_claude_code
from kelpie.adapters.hermes import adapter as hermes
from kelpie.adapters.hermes.git_state import valid_base_ref
from kelpie.adapters.hermes.park import AUTHOR, DEFAULT_WAIT_KEY, WAIT_KEY_NAME, board_key
from kelpie.adapters.hermes.processes import (
    DEFAULT_TIMEOUT as DEFAULT_TOOL_TIMEOUT,
)
from kelpie.adapters.hermes.processes import SlotPool, ToolProcesses
from kelpie.adapters.hermes.worker import DEFAULT_GRACE
from kelpie.adapters.issues.config import (
    GitHubIssuesSettings,
    LinearSettings,
    load_github_issues,
    load_linear,
)
from kelpie.adapters.openclaw.adapter import OpenClawAdapter
from kelpie.adapters.openclaw.config import OpenClawSettings, load_openclaw
from kelpie.alerts.buttons import (
    AlreadySnoozed,
    ButtonStore,
    ChatControl,
    Press,
    button_store_path,
)
from kelpie.alerts.config import AlertSettings, load_alerts
from kelpie.alerts.delivery import Delivery
from kelpie.connectors.cost_reports import CostReportsSettings, load_cost_reports
from kelpie.connectors.github import (
    BlockConditionPattern,
    ConditionSync,
    GitHubSettings,
    load_block_conditions,
    load_github,
)
from kelpie.connectors.github.billing import KEEP_FOR
from kelpie.connectors.github.pulls import PullRequestSync
from kelpie.connectors.human_tasks import (
    HumanTaskConnector,
    HumanTaskSettings,
    load_human_tasks,
)
from kelpie.connectors.llm_quotas import LlmQuotaSettings, load_llm_quotas
from kelpie.core.actions import ActionKind
from kelpie.core.budgets import BudgetConfig, BudgetStatus, BudgetUnit, evaluate, load_budgets
from kelpie.core.conditions import check_task_status
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import LedgerStore, MemoryLedgerStore
from kelpie.core.model import (
    Owner,
    OwnerKind,
    Reconciliation,
    ReportedCost,
    Snapshot,
    Snooze,
    SpanKind,
)
from kelpie.core.ports import (
    ActionConnector,
    CostReportReader,
    PlatformAdapter,
    ProviderReader,
    ResourceReader,
    SnapshotSource,
    SupportsClose,
    SupportsReassign,
    SupportsStop,
    SupportsWait,
)
from kelpie.core.prices import CHECKED, canonical_model, platform_wrapped
from kelpie.core.rule import Rule
from kelpie.core.serde import parse_duration
from kelpie.core.snooze import (
    MarkerProblem,
    SnoozeSettings,
    chat_snooze,
    load_snooze,
    with_snoozes,
)
from kelpie.core.spans import MemorySpanStore, SpanStore
from kelpie.core.spend import (
    Estimate,
    ProviderMode,
    SpendConfig,
    SpendSettings,
    load_spend,
    reconcile,
)
from kelpie.core.states import MemoryStateStore, StateStore
from kelpie.core.storage import (
    SchemaOutdated,
    SqliteCostReports,
    SqliteLedgerStore,
    SqliteSpanStore,
)
from kelpie.metrics import Metrics
from kelpie.remote.config import AgentSettings, HubSettings, load_agent, load_hub
from kelpie.remote.farm import RemoteFarm
from kelpie.remote.hub import Hub, HubServer, HubService, server_context
from kelpie.report.config import ReportSettings, load_report
from kelpie.resources.config import ResourceSettings, load_resources
from kelpie.rules import PLANNED, REGISTRY, PlannedRule, RuleFactory
from kelpie.rules._snoozes import behind_counter
from kelpie.rules.ci_economy import CiEconomy
from kelpie.rules.conflict_warning import ConflictWarning
from kelpie.rules.no_progress import NoProgress
from kelpie.rules.resources import Resources
from kelpie.traces.auth import TokenFile, TokenFileError, loose_mode
from kelpie.traces.config import TraceSettings, load_traces
from kelpie.traces.forward import Forwarder
from kelpie.traces.hooks import merge_hook_spans
from kelpie.traces.ingest import IGNORED, TraceIngest
from kelpie.traces.store import OffenderWatch, SpanRetention, SpanWindow, merge_spans

log = logging.getLogger(__name__)

# [adapters.hermes] author: a name as Hermes writes profile names.
_AUTHOR = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# Names a Hermes home already writes comments as: the default profile, the CLI's fallback
# author, and the names Hermes reserves for profiles.
_RESERVED_AUTHORS = frozenset({"default", "user", "hermes", "test", "tmp", "root", "sudo"})

DEFAULT_CONFIG = Path("kelpie.toml")
DEFAULT_DB = Path("kelpie.db")


class ConfigError(ValueError):
    """The configuration cannot be used; Kelpie refuses to start."""


# [[adapters.hermes.boards]] name: the prefix of the board's task ids (``<name>:t_1a``).
_BOARD_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")


@dataclass(frozen=True, slots=True)
class HermesBoardSettings:
    """``[[adapters.hermes.boards]]``: one Kanban board of the Hermes home. Profiles, the
    spool, the CLI and the host's tool processes belong to the home, so they stay in
    ``[adapters.hermes]``; a board has its database and its own park-signing key."""

    name: str  # prefixes the board's task and session ids: <name>:t_1a
    db_path: Path
    # Signs Kelpie's parks and stops on this board; None: hermes-wait-<name>.key next to
    # the ledger (see _with_wait_key).
    wait_key: Path | None = None


@dataclass(frozen=True, slots=True)
class HermesSettings:
    """``[adapters.hermes]``: where the Kanban board (or boards) and the hooks spool live."""

    db_path: Path = hermes.DEFAULT_DB
    spool: Path | None = hermes.DEFAULT_SPOOL  # None: spool reading disabled
    lookback: timedelta = hermes.DEFAULT_LOOKBACK
    enabled: bool = True
    git_state: bool = True  # read git state of the workspaces of live tasks
    git_timeout: timedelta = hermes.DEFAULT_GIT_TIMEOUT  # per git call
    base_refs: tuple[str, ...] = hermes.DEFAULT_BASE_REFS  # task files: changes since these
    cli: str = hermes.DEFAULT_CLI  # Hermes executable that carries out ``stop``
    cli_timeout: timedelta = timedelta(seconds=hermes.CLI_TIMEOUT)  # one stop command
    human_owners: frozenset[str] = frozenset()  # assignees that are people, not agents
    roles: tuple[tuple[str, str], ...] = ()  # profile -> role ([adapters.hermes.roles])
    role_from_name: bool = True  # role of other profiles: the name without a trailing -N
    # Signs Kelpie's parks of Hermes tasks; None: next to the ledger (see wait_key_path).
    wait_key: Path | None = None
    # Hermes statuses before any work starts (rule 3's cards stuck before work)
    prework_statuses: tuple[str, ...] = hermes.DEFAULT_PREWORK
    # A person's decisions agents record: (marker, relay profiles, person or None),
    # [[adapters.hermes.human_decisions]]
    human_decisions: tuple[tuple[str, tuple[str, ...], str | None], ...] = ()
    # After a stop, end the run's worker processes at once; SIGKILL after ``worker_grace``.
    end_worker: bool = True
    worker_grace: timedelta = timedelta(seconds=DEFAULT_GRACE)
    # Long tool runs and tool slots read from the host's processes ([adapters.hermes.tools])
    tools: ToolProcesses | None = None
    author: str = AUTHOR  # the name Kelpie signs its blocks, parks, wakes and comments with
    # [[adapters.hermes.boards]]: several boards of this Hermes home, each with its own
    # db_path (then ``db_path`` and ``wait_key`` above are unused). (): one board, db_path.
    boards: tuple[HermesBoardSettings, ...] = ()

    def build(self) -> hermes.HermesAdapter:
        return hermes.HermesAdapter(
            db_path=self.db_path,
            spool=self.spool,
            lookback=self.lookback,
            git_state=self.git_state,
            git_timeout=self.git_timeout,
            base_refs=self.base_refs,
            cli=self.cli,
            cli_timeout=self.cli_timeout.total_seconds(),
            human_owners=self.human_owners,
            human_decisions=tuple(
                hermes.HumanDecision(m, frozenset(r), p) for m, r, p in self.human_decisions
            ),
            roles=dict(self.roles),
            role_from_name=self.role_from_name,
            wait_key=self.wait_key or DEFAULT_WAIT_KEY,
            prework_statuses=self.prework_statuses,
            end_worker=self.end_worker,
            worker_grace=self.worker_grace.total_seconds(),
            tools=self.tools,
            author=self.author,
        )


@dataclass(frozen=True, slots=True)
class Settings:
    engine: EngineConfig = field(default_factory=EngineConfig)
    hermes: HermesSettings | None = None  # None: no [adapters.hermes] section
    claude_code: ClaudeCodeSettings | None = None  # None: no [adapters.claude_code] section
    openclaw: OpenClawSettings | None = None  # None: no [adapters.openclaw] section
    github_issues: GitHubIssuesSettings | None = None  # None: no [adapters.github_issues]
    linear: LinearSettings | None = None  # None: no [adapters.linear]
    db_path: Path | None = DEFAULT_DB  # None keeps the ledger in memory
    source: Path | None = None  # the file the settings came from, if any
    traces: TraceSettings = field(default_factory=TraceSettings)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)  # [[budgets]], [prices]
    spend_settings: SpendSettings = field(default_factory=SpendSettings)  # [spend]
    resources: ResourceSettings = field(default_factory=ResourceSettings)
    github: GitHubSettings | None = None  # None: no [connectors.github] section
    block_conditions: tuple[BlockConditionPattern, ...] = ()
    human_tasks: HumanTaskSettings | None = None  # None: no [connectors.human_tasks]
    llm_quotas: LlmQuotaSettings | None = None  # [connectors.llm_quotas]
    cost_reports: CostReportsSettings | None = None  # [connectors.cost_reports]
    alerts: AlertSettings = field(default_factory=AlertSettings)  # [alerts]
    report: ReportSettings = field(default_factory=ReportSettings)  # [report]
    agent: AgentSettings | None = None  # [agent]: this host runs ``kelpie agent``
    hub: HubSettings | None = None  # [hub]: ``kelpie serve`` takes reports from agents
    snooze: SnoozeSettings = field(default_factory=SnoozeSettings)  # [snooze]

    @property
    def spend(self) -> SpendConfig:
        """``[spend]`` with what decides each provider's spend mode (budgets, prices)."""
        reports = self.cost_reports.providers if self.cost_reports is not None else frozenset()
        return SpendConfig(self.spend_settings, self.budgets.prices, self.budgets.budgets, reports)


def find_config(explicit: str | None) -> Path | None:
    """``--config``, else ``$KELPIE_CONFIG``, else ``./kelpie.toml`` if it exists."""
    if explicit:
        return Path(explicit)
    if env := os.environ.get("KELPIE_CONFIG"):
        return Path(env)
    return DEFAULT_CONFIG if DEFAULT_CONFIG.exists() else None


def load_settings(path: Path | None) -> Settings:
    data: Mapping[str, Any] = {}
    if path is not None:
        try:
            with path.open("rb") as f:
                data = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read config {path}: {exc}") from exc
    unknown = set(data) - {
        "engine",
        "storage",
        "adapters",
        "traces",
        "budgets",
        "prices",
        "spend",
        "resources",
        "connectors",
        "block_conditions",
        "alerts",
        "report",
        "agent",
        "hub",
        "snooze",
    }
    if unknown:
        raise ConfigError(f"unknown config sections: {sorted(unknown)}")
    try:
        engine = EngineConfig.from_mapping(data.get("engine", {}))
    except (ValueError, TypeError) as exc:
        raise ConfigError(str(exc)) from exc
    storage = data.get("storage", {})
    if not isinstance(storage, Mapping):
        raise ConfigError("[storage] must be a table")
    if extra := set(storage) - {"path"}:
        raise ConfigError(f"unknown [storage] keys: {sorted(extra)}")
    raw_db = os.environ.get("KELPIE_DB") or storage.get("path", str(DEFAULT_DB))
    if not isinstance(raw_db, str) or not raw_db.strip():
        raise ConfigError("storage path must be a non-empty string")
    db_path = None if raw_db == ":memory:" else Path(raw_db)
    try:
        traces = load_traces(data.get("traces"))
        resources = load_resources(data.get("resources"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    try:
        spend_settings = load_spend(data.get("spend"))
        budgets = load_budgets(
            data.get("budgets"),
            data.get("prices"),
            traces.retention,
            default_prices=spend_settings.default_prices,
        )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if budgets.budgets and not traces.enabled:
        raise ConfigError("[[budgets]] read spend from traces: they need [traces] enabled")
    github, block_conditions, human_tasks, llm_quotas, cost_reports = _load_connectors(data)
    try:
        report = load_report(data.get("report"))
        alerts = load_alerts(data.get("alerts"), report.task_url, report.tz)
        agent = load_agent(data.get("agent"))
        hub = load_hub(data.get("hub"))
        snooze = load_snooze(data.get("snooze"), report.tz)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return Settings(
        github=github,
        human_tasks=human_tasks,
        block_conditions=block_conditions,
        engine=_with_condition_reasons(engine, block_conditions),
        hermes=_with_wait_key(_load_hermes(data.get("adapters", {})), db_path),
        claude_code=_load_claude_code(data.get("adapters", {})),
        openclaw=_load_openclaw(data.get("adapters", {})),
        github_issues=_load_github_issues(data.get("adapters", {}), github),
        linear=_load_linear(data.get("adapters", {})),
        db_path=db_path,
        source=path,
        traces=traces,
        budgets=budgets,
        spend_settings=spend_settings,
        resources=resources,
        llm_quotas=llm_quotas,
        cost_reports=cost_reports,
        alerts=alerts,
        report=report,
        agent=agent,
        hub=hub,
        snooze=snooze,
    )


def _load_connectors(
    data: Mapping[str, Any],
) -> tuple[
    GitHubSettings | None,
    tuple[BlockConditionPattern, ...],
    HumanTaskSettings | None,
    LlmQuotaSettings | None,
    CostReportsSettings | None,
]:
    """``[connectors.github]`` (with ``[[block_conditions]]``), ``[connectors.human_tasks]``,
    ``[connectors.llm_quotas]`` and ``[connectors.cost_reports]``."""
    connectors = data.get("connectors", {})
    if not isinstance(connectors, Mapping):
        raise ConfigError("[connectors] must be a table")
    if extra := set(connectors) - {"github", "human_tasks", "llm_quotas", "cost_reports"}:
        raise ConfigError(f"unknown [connectors] entries: {sorted(extra)}")
    try:
        github = load_github(connectors["github"]) if "github" in connectors else None
        repos = github.repos if github is not None else None
        patterns = load_block_conditions(data.get("block_conditions"), repos)
        human_tasks = (
            load_human_tasks(connectors["human_tasks"]) if "human_tasks" in connectors else None
        )
        llm_quotas = load_llm_quotas(connectors.get("llm_quotas"))
        cost_reports = load_cost_reports(connectors.get("cost_reports"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return github, patterns, human_tasks, llm_quotas, cost_reports


def _load_hermes(adapters: Any) -> HermesSettings | None:
    if not isinstance(adapters, Mapping):
        raise ConfigError("[adapters] must be a table")
    if extra := set(adapters) - {"hermes", "claude_code", "openclaw", "github_issues", "linear"}:
        raise ConfigError(f"unknown [adapters] entries: {sorted(extra)}")
    if "hermes" not in adapters:
        return None
    section = adapters["hermes"]
    if not isinstance(section, Mapping):
        raise ConfigError("[adapters.hermes] must be a table")
    if extra := set(section) - {
        "db_path",
        "spool",
        "lookback",
        "enabled",
        "git_state",
        "git_timeout",
        "base_refs",
        "cli",
        "cli_timeout",
        "human_owners",
        "human_decisions",
        "roles",
        "role_from_name",
        "prework_statuses",
        "wait_key",
        "end_worker",
        "worker_grace",
        "tools",
        "author",
        "boards",
    }:
        raise ConfigError(f"unknown [adapters.hermes] keys: {sorted(extra)}")
    boards = _hermes_boards(section)
    paths: dict[str, Path | None] = {}
    for key, default in (("db_path", hermes.DEFAULT_DB), ("spool", hermes.DEFAULT_SPOOL)):
        value = section.get(key, str(default))
        if key == "spool" and value is False:
            paths[key] = None  # spool = false: no hook data
        elif not isinstance(value, str) or not value.strip():
            allowed = "a non-empty string or false" if key == "spool" else "a non-empty string"
            raise ConfigError(f"[adapters.hermes] {key} must be {allowed}")
        else:
            paths[key] = Path(value)
    db_path = paths["db_path"]
    assert db_path is not None
    try:
        lookback = parse_duration(section.get("lookback", hermes.DEFAULT_LOOKBACK))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigError(f"[adapters.hermes] lookback: {exc}") from exc
    if lookback <= timedelta(0):
        raise ConfigError("[adapters.hermes] lookback must be positive")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ConfigError("[adapters.hermes] enabled must be true or false")
    git_state = section.get("git_state", True)
    if not isinstance(git_state, bool):
        raise ConfigError("[adapters.hermes] git_state must be true or false")
    try:
        git_timeout = parse_duration(section.get("git_timeout", hermes.DEFAULT_GIT_TIMEOUT))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigError(f"[adapters.hermes] git_timeout: {exc}") from exc
    if git_timeout <= timedelta(0):
        raise ConfigError("[adapters.hermes] git_timeout must be positive")
    base_refs = section.get("base_refs", list(hermes.DEFAULT_BASE_REFS))
    if (
        not isinstance(base_refs, list)
        or not all(isinstance(r, str) and valid_base_ref(r) for r in base_refs)
        or len(set(base_refs)) != len(base_refs)
    ):
        raise ConfigError(
            "[adapters.hermes] base_refs must be a list of distinct branch names "
            '(e.g. ["origin/main", "main"])'
        )
    cli = section.get("cli", hermes.DEFAULT_CLI)
    if not isinstance(cli, str) or not cli.strip():
        raise ConfigError("[adapters.hermes] cli must be a non-empty string")
    try:
        cli_timeout = parse_duration(section.get("cli_timeout", f"{hermes.CLI_TIMEOUT:g}s"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigError(f"[adapters.hermes] cli_timeout: {exc}") from exc
    if not timedelta(0) < cli_timeout <= timedelta(minutes=1):
        raise ConfigError("[adapters.hermes] cli_timeout must be positive and at most 60s")
    end_worker = section.get("end_worker", True)
    if not isinstance(end_worker, bool):
        raise ConfigError("[adapters.hermes] end_worker must be true or false")
    try:
        worker_grace = parse_duration(section.get("worker_grace", f"{DEFAULT_GRACE:g}s"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigError(f"[adapters.hermes] worker_grace: {exc}") from exc
    if not timedelta(0) < worker_grace <= timedelta(seconds=30):
        raise ConfigError("[adapters.hermes] worker_grace must be positive and at most 30s")
    humans = section.get("human_owners", [])
    if (
        not isinstance(humans, list)
        or not all(isinstance(h, str) and h.strip() for h in humans)
        or len({h.strip().casefold() for h in humans}) != len(humans)
    ):
        raise ConfigError(
            "[adapters.hermes] human_owners must be a list of distinct non-empty strings"
        )
    author = section.get("author", AUTHOR)
    if not isinstance(author, str) or not _AUTHOR.fullmatch(author):
        raise ConfigError(
            "[adapters.hermes] author must be 1-64 lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit"
        )
    if author in {h.strip().casefold() for h in humans}:
        raise ConfigError(f"[adapters.hermes] author {author!r} is also in human_owners")
    if author in _RESERVED_AUTHORS:
        raise ConfigError(
            f"[adapters.hermes] author {author!r} is a name Hermes itself uses (its default "
            "profile or CLI author): Kelpie would hide those comments as its own"
        )
    decisions = section.get("human_decisions", [])
    if not isinstance(decisions, list) or not all(
        isinstance(d, Mapping)
        and {"marker", "relays"} <= set(d) <= {"marker", "relays", "person"}
        and isinstance(d["marker"], str)
        and d["marker"].strip()
        and isinstance(d["relays"], list)
        and d["relays"]
        and all(isinstance(r, str) and r.strip() for r in d["relays"])
        and (isinstance(d.get("person", "x"), str) and d.get("person", "x").strip())
        for d in decisions
    ):
        raise ConfigError(
            "[[adapters.hermes.human_decisions]] entries need a marker (the note an agent "
            'writes, e.g. "решение CEO") and relays (the agent profiles trusted to record '
            "it), optionally the person whose decisions they are, and nothing else"
        )
    people = {h.strip().casefold() for h in humans}
    seen: set[str] = set()
    for d in decisions:
        relays = {r.strip().casefold() for r in d["relays"]}
        if bad := sorted(relays & (people | {author, "user"})):
            raise ConfigError(
                f"[[adapters.hermes.human_decisions]] relays {bad}: a relay is an agent "
                "profile, not a human owner, Kelpie or Hermes' default CLI author (user)"
            )
        if twice := sorted(relays & seen):
            raise ConfigError(f"[[adapters.hermes.human_decisions]] relays {twice} twice")
        seen |= relays
        if "person" in d and d["person"].strip().casefold() not in people:
            raise ConfigError(
                f"[[adapters.hermes.human_decisions]] person {d['person'].strip()!r} must be "
                "one of [adapters.hermes] human_owners"
            )
    role_from_name = section.get("role_from_name", True)
    if not isinstance(role_from_name, bool):
        raise ConfigError("[adapters.hermes] role_from_name must be true or false")
    roles = section.get("roles", {})
    if not isinstance(roles, Mapping) or not all(
        isinstance(k, str) and k.strip() and isinstance(v, str) and v.strip()
        for k, v in roles.items()
    ):
        raise ConfigError("[adapters.hermes.roles] must map profile names to non-empty role names")
    if len({k.strip().casefold() for k in roles}) != len(roles):
        raise ConfigError("[adapters.hermes.roles] lists a profile twice (case-insensitive)")
    wait_key = section.get("wait_key")
    if wait_key is not None and (not isinstance(wait_key, str) or not wait_key.strip()):
        raise ConfigError("[adapters.hermes] wait_key must be a non-empty path")
    prework = section.get("prework_statuses", list(hermes.DEFAULT_PREWORK))
    if not isinstance(prework, list) or not all(
        isinstance(x, str) and x.strip() in hermes.PREWORK_ALLOWED for x in prework
    ):
        raise ConfigError(
            "[adapters.hermes] prework_statuses must be a list of Hermes statuses among "
            f"{sorted(hermes.PREWORK_ALLOWED)}"
        )
    return HermesSettings(
        db_path,
        paths["spool"],
        lookback,
        enabled,
        git_state,
        git_timeout,
        tuple(base_refs),
        cli,
        cli_timeout,
        frozenset(h.strip() for h in humans),
        tuple(sorted((k.strip().casefold(), v.strip().casefold()) for k, v in roles.items())),
        role_from_name,
        None if wait_key is None else Path(wait_key),
        tuple(dict.fromkeys(x.strip() for x in prework)),
        tuple(
            (
                d["marker"].strip(),
                tuple(sorted({r.strip().casefold() for r in d["relays"]})),
                d["person"].strip().casefold() if "person" in d else None,
            )
            for d in decisions
        ),
        end_worker,
        worker_grace,
        _hermes_tools(section.get("tools")),
        author,
        boards,
    )


def _hermes_boards(section: Mapping[str, Any]) -> tuple[HermesBoardSettings, ...]:
    """``[[adapters.hermes.boards]]``: names and databases distinct (paths compared after
    ``~`` expansion and resolving symlinks), none of the one-board keys (``db_path``,
    ``wait_key``) beside them. Keys are checked distinct once defaulted (_with_wait_key)."""
    if "boards" not in section:
        return ()
    where = "[[adapters.hermes.boards]]"
    raw = section["boards"]
    if not isinstance(raw, list) or not raw or not all(isinstance(b, Mapping) for b in raw):
        raise ConfigError(f"{where} must be one or more tables")
    if both := sorted({"db_path", "wait_key"} & set(section)):
        raise ConfigError(
            f"[adapters.hermes] {', '.join(both)} cannot be set beside {where}: set "
            f"{'it' if len(both) == 1 else 'them'} on each board"
        )
    boards: list[HermesBoardSettings] = []
    for entry in raw:
        if extra := set(entry) - {"name", "db_path", "wait_key"}:
            raise ConfigError(f"unknown {where} keys: {sorted(extra)}")
        name = entry.get("name")
        if not isinstance(name, str) or not _BOARD_NAME.fullmatch(name):
            raise ConfigError(
                f"{where} entries need a name of 1-32 lowercase letters, digits, '-' or '_' "
                "(it prefixes the board's task ids)"
            )
        values: dict[str, Path | None] = {}
        for key in ("db_path", "wait_key"):
            value = entry.get(key)
            if value is None and key == "wait_key":
                values[key] = None
            elif not isinstance(value, str) or not value.strip():
                raise ConfigError(f"{where} {name}: {key} must be a non-empty path")
            else:
                values[key] = Path(value)
        db_path = values["db_path"]
        assert db_path is not None
        boards.append(HermesBoardSettings(name, db_path, values["wait_key"]))
    names = [b.name for b in boards]
    if twice := sorted({n for n in names if names.count(n) > 1}):
        raise ConfigError(f"{where} names {', '.join(twice)} twice")
    dbs = [b.db_path.expanduser().resolve() for b in boards]
    if len(set(dbs)) < len(dbs):
        raise ConfigError(f"{where} lists a db_path twice: one board is one entry")
    return tuple(boards)


def _hermes_tools(raw: Any) -> ToolProcesses | None:
    """``[adapters.hermes.tools]``: tool process names, workspace patterns, slot pools."""
    if raw is None:
        return None
    where = "[adapters.hermes.tools]"
    if not isinstance(raw, Mapping):
        raise ConfigError(f"{where} must be a table")
    if extra := set(raw) - {"names", "workspace_patterns", "timeout", "slots"}:
        raise ConfigError(f"unknown {where} keys: {sorted(extra)}")

    def names(key: str, required: bool) -> tuple[str, ...]:
        value = raw.get(key)
        if value is None and not required:
            return ()
        if (
            not isinstance(value, list)
            or not value
            or not all(isinstance(x, str) and x.strip() for x in value)
        ):
            example = ' (the tools that keep a task busy, e.g. ["xcodebuild"])' if required else ""
            raise ConfigError(f"{where} {key} must be a non-empty list of strings{example}")
        return tuple(dict.fromkeys(x.strip() for x in value))

    tool_names = names("names", True)
    if bad := [n for n in tool_names if "/" in n]:
        raise ConfigError(f"{where} names are base names of processes, not paths: {bad}")
    try:
        timeout = parse_duration(raw.get("timeout", f"{DEFAULT_TOOL_TIMEOUT:g}s"))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ConfigError(f"{where} timeout: {exc}") from exc
    if not timedelta(0) < timeout <= timedelta(seconds=30):
        raise ConfigError(f"{where} timeout must be positive and at most 30s")
    pools = raw.get("slots", [])
    if not isinstance(pools, list) or not all(
        isinstance(p, Mapping)
        and {"name", "paths"} <= set(p) <= {"name", "paths", "size"}
        and (
            "size" not in p
            or (isinstance(p["size"], int) and not isinstance(p["size"], bool) and p["size"] > 0)
        )
        and isinstance(p["name"], str)
        and re.fullmatch(r"[\w.-]{1,64}", p["name"])
        and isinstance(p["paths"], list)
        and p["paths"]
        and all(isinstance(x, str) and x.strip() for x in p["paths"])
        for p in pools
    ):
        raise ConfigError(
            "[[adapters.hermes.tools.slots]] entries need a name (letters, digits, . _ -) "
            "and paths (glob patterns of the slot lock files), optionally size (a positive "
            "count), and nothing else"
        )
    if len({p["name"] for p in pools}) != len(pools):
        raise ConfigError("[[adapters.hermes.tools.slots]] names a pool twice")
    try:
        return ToolProcesses(
            names=tool_names,
            workspace_patterns=names("workspace_patterns", False),
            slots=tuple(
                SlotPool(p["name"], tuple(x.strip() for x in p["paths"]), p.get("size"))
                for p in pools
            ),
            timeout=timeout.total_seconds(),
        )
    except ValueError as exc:
        raise ConfigError(f"{where} {exc}") from exc


def _with_wait_key(hermes: HermesSettings | None, db: Path | None) -> HermesSettings | None:
    """The park-signing key defaults next to the ledger, which a deployment keeps (a
    container's data volume): losing the key leaves Kelpie's parks unrecognised. With an
    in-memory ledger it is ``~/.kelpie/hermes-wait.key``."""
    if hermes is None:
        return hermes
    where = DEFAULT_WAIT_KEY.parent if db is None else db.parent
    if hermes.boards:  # a key per board: a marker of one board never verifies on another
        boards = tuple(
            b if b.wait_key is not None else replace(b, wait_key=where / board_key(b.name))
            for b in hermes.boards
        )
        keys = [b.wait_key.expanduser().resolve() for b in boards if b.wait_key is not None]
        if len(set(keys)) < len(keys):  # compared after the defaults, ~ and symlinks
            raise ConfigError(
                "[[adapters.hermes.boards]] share a wait_key: each board signs with its own"
            )
        return replace(hermes, boards=boards)
    if hermes.wait_key is not None:
        return hermes
    return replace(hermes, wait_key=where / WAIT_KEY_NAME)


def _load_claude_code(adapters: Mapping[str, Any]) -> ClaudeCodeSettings | None:
    if "claude_code" not in adapters:
        return None
    try:
        return load_claude_code(adapters["claude_code"])
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _load_github_issues(
    adapters: Mapping[str, Any], github: GitHubSettings | None
) -> GitHubIssuesSettings | None:
    if "github_issues" not in adapters:
        return None
    try:
        return load_github_issues(adapters["github_issues"], github)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _load_linear(adapters: Mapping[str, Any]) -> LinearSettings | None:
    if "linear" not in adapters:
        return None
    try:
        return load_linear(adapters["linear"])
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _load_openclaw(adapters: Mapping[str, Any]) -> OpenClawSettings | None:
    if "openclaw" not in adapters:
        return None
    try:
        return load_openclaw(adapters["openclaw"])
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RuleStatus:
    name: str
    planned: PlannedRule
    implemented: bool
    mode: Mode
    # has a [engine.rules.<name>] section, or settings inherited into one (needs_you's
    # condition_reasons from [[block_conditions]])
    configured: bool


@dataclass(frozen=True, slots=True)
class LoadedRules:
    rules: list[Rule]
    statuses: list[RuleStatus]
    warnings: list[str]


def load_rules(
    config: EngineConfig,
    registry: Mapping[str, RuleFactory] = REGISTRY,
    planned: Sequence[PlannedRule] = PLANNED,
) -> LoadedRules:
    """Instantiate every registered rule with its config section.

    Unknown names fail; planned-but-unimplemented names in the config are skipped with a
    warning; registered rules missing from ``PLANNED`` fail (the plan owns the rule list).
    """
    by_name = {p.name: p for p in planned}
    unplanned = sorted(set(registry) - set(by_name))
    if unplanned:
        raise ConfigError(f"rules registered but not in PLANNED: {unplanned}")
    configured = set(config.rules) | set(config.modes)
    unknown = sorted(configured - set(by_name))
    if unknown:
        raise ConfigError(f"unknown rules in config: {unknown}")
    warnings = [
        f"rule {name!r} is planned but not implemented yet; its config is ignored"
        for name in sorted(configured - set(registry))
    ]
    rules: list[Rule] = []
    for name in (p.name for p in planned if p.name in registry):  # plan order
        try:
            rules.append(registry[name](_rule_config(name, config, registry)))
        except ConfigError:
            raise  # names the rule it is about (needs_you, read for stale_blocks)
        except Exception as exc:
            raise ConfigError(f"bad config for rule {name!r}: {exc}") from exc
    _warn_inbox_gaps(rules)
    statuses = [
        RuleStatus(
            name=p.name,
            planned=p,
            implemented=p.name in registry,
            mode=config.mode_for(p.name),
            configured=p.name in configured,
        )
        for p in planned
    ]
    return LoadedRules(rules, statuses, warnings)


def _with_condition_reasons(
    engine: EngineConfig, patterns: Sequence[BlockConditionPattern]
) -> EngineConfig:
    """``needs_you`` inherits the ``[[block_conditions]]`` block patterns as its
    ``condition_reasons`` unless it sets its own: a block whose reason names something a
    condition can check (a PR to merge) waits on that, not on a person's answer, whatever
    its kind (agents use ``needs_input`` to get past Hermes' block loop guard)."""
    reasons = [p.pattern.pattern for p in patterns if "block" in p.applies_to]
    section = engine.rules.get("needs_you", {})
    if not reasons or "condition_reasons" in section:
        return engine
    rules = {**engine.rules, "needs_you": {**section, "condition_reasons": reasons}}
    return replace(engine, rules=rules)


def _rule_config(
    name: str, config: EngineConfig, registry: Mapping[str, RuleFactory] | None = None
) -> dict[str, Any]:
    """A rule's config section. ``loop_breaker`` polls the same checks rule 2 coaches, so it
    inherits ``wait_condition``'s ``poll_patterns`` unless it sets its own; ``orphan_status``
    inherits ``bottlenecks``' hold labels, ``needs_you`` its planned and hold labels as
    ``planned_labels`` (one definition of a planned wait), and ``return_causes``
    ``conflict_warning``'s ``expected_shared`` the same way. ``stale_blocks`` and
    ``wait_condition`` always read ``needs_you``'s settings (see :func:`_with_inbox`);
    ``registry`` holds the rules being loaded (default: every registered one)."""
    section = dict(config.rules.get(name, {}))
    if name in _LEAVES_TO_INBOX:
        return _with_inbox(name, section, config, REGISTRY if registry is None else registry)
    if name == "needs_you" and "planned_labels" not in section:
        planned = planned_labels(config.rules.get("bottlenecks", {}))
        if planned:
            section["planned_labels"] = planned
        return section
    if name == "return_causes" and "expected_shared" not in section:
        # Files many tasks are meant to edit are not conflict hot spots either.
        shared = config.rules.get("conflict_warning", {}).get("expected_shared")
        if shared is not None:
            section["expected_shared"] = shared
        return section
    if name == "orphan_status" and "hold_labels" not in section:
        # Held tasks are parked on purpose: rule 3's stop patterns skip what rule 6 calls held.
        held = hold_labels(config.rules.get("bottlenecks", {}))
        if held:
            section["hold_labels"] = held
        return section
    if name != "loop_breaker":
        return section
    shared = config.rules.get("wait_condition", {}).get("poll_patterns")
    if shared is None:
        return section
    if "poll_patterns" not in section:
        section["poll_patterns"] = shared
    elif _normalized(section["poll_patterns"]) != _normalized(shared):
        log.warning(
            "loop_breaker.poll_patterns differs from wait_condition.poll_patterns: calls "
            "matching only one of them are coached by rule 2 or stopped by rule 10 inconsistently"
        )
    return section


# Rules that leave the tasks rule 13 lists to it, with its settings (``needs_you``): rule 4
# for blocks (#169), rule 2 for waits (#213).
_LEAVES_TO_INBOX = frozenset({"stale_blocks", "wait_condition"})


def _with_inbox(
    name: str,
    section: dict[str, Any],
    config: EngineConfig,
    registry: Mapping[str, RuleFactory],
) -> dict[str, Any]:
    """Rule ``name`` (rule 4 or 2) leaves the tasks rule 13 lists as waiting for a person to
    rule 13, while rule 13 reaches people: it is loaded, and it is not in ``observe`` while
    ``name`` is not (else ``name``'s alerts would be the only ones a person gets). It then
    gets rule 13's whole config section as ``needs_you`` (its reasons, the
    ``[[block_conditions]]`` patterns and planned labels included), so the rules read the
    farm alike; else ``needs_you = false``. Rule 13's section is checked here, so a mistake
    in it is reported as rule 13's."""
    if "needs_you" in section:
        raise ValueError(
            "needs_you is set automatically from the rules' modes (whether rule 13 reaches "
            f"people), not in [engine.rules.{name}]"
        )
    active = "needs_you" in registry and (
        config.mode_for("needs_you") is not Mode.OBSERVE or config.mode_for(name) is Mode.OBSERVE
    )
    if not active:
        section["needs_you"] = False
        return section
    inbox = _rule_config("needs_you", config, registry)
    try:
        registry["needs_you"](dict(inbox))
    except Exception as exc:
        raise ConfigError(f"bad config for rule 'needs_you': {exc}") from exc
    section["needs_you"] = inbox
    return section


# Rule 13's wait, and the wait of the rule that leaves it a task, per rule: a task listed
# there is announced by neither until rule 13's wait has passed.
_INBOX_GAPS: dict[str, tuple[tuple[str, str, str], ...]] = {
    "stale_blocks": (
        ("approval_wait_after", "unlinked_alert_after", "a block that asks a person"),
        ("human_wait_alert_after", "max_block", "a block that asks a person"),
    ),
    "wait_condition": (("human_wait_alert_after", "max_wait", "a wait on a person's task"),),
}


def _warn_inbox_gaps(rules: Sequence[Rule]) -> None:
    """A task rule 4 or rule 2 leaves to rule 13 is announced by neither until rule 13's own
    wait has passed: say so when that is longer than the rule's own (the gap is silent)."""
    for rule in rules:
        inbox = getattr(rule, "inbox", None)
        if inbox is None:
            continue
        for thirteen, own, what in _INBOX_GAPS.get(rule.name, ()):
            theirs, mine = getattr(inbox, thirteen), getattr(rule, own)
            if mine is not None and theirs > mine:
                log.warning(
                    "needs_you.%s (%s) is longer than %s.%s (%s): %s is announced by neither "
                    "rule for %s in between",
                    thirteen,
                    theirs,
                    rule.name,
                    own,
                    mine,
                    what,
                    theirs - mine,
                )


def hold_labels(bottlenecks: Mapping[str, Any]) -> list[str]:
    """``bottlenecks``' ``hold_labels`` and ``holds`` labels; malformed entries are left to
    the bottlenecks rule, which refuses them at startup."""
    labels = bottlenecks.get("hold_labels", [])
    found = [x for x in labels if isinstance(x, str)] if isinstance(labels, list) else []
    holds = bottlenecks.get("holds", [])
    for hold in holds if isinstance(holds, list) else []:
        if isinstance(hold, Mapping) and isinstance(hold.get("label"), str):
            found.append(hold["label"])
    return sorted(set(found))


def planned_labels(bottlenecks: Mapping[str, Any]) -> list[str]:
    """What rule 6 reads as planned by label: its ``planned_labels`` and hold labels
    (incident 20); rule 13 inherits the same unless it sets its own."""
    own = bottlenecks.get("planned_labels", [])
    found = [x for x in own if isinstance(x, str)] if isinstance(own, list) else []
    return sorted({*found, *hold_labels(bottlenecks)})


def _normalized(patterns: Any) -> Any:
    if isinstance(patterns, Mapping):
        return {str(k): list(v) for k, v in patterns.items() if isinstance(v, list | tuple)}
    return patterns


def utc_now() -> datetime:
    return datetime.now(UTC)


def empty_snapshot(clock: Callable[[], datetime] = utc_now) -> Callable[[], Snapshot]:
    """Snapshot source without adapters: just the current time."""
    return lambda: Snapshot(now=clock())


def merged_snapshot(
    adapters: Sequence[PlatformAdapter],
    metrics: Metrics,
    clock: Callable[[], datetime] = utc_now,
    spans: SpanWindow | None = None,
    budgets: BudgetConfig | None = None,
    remember: bool = True,
    resources: ResourceReader | None = None,
    enrich: Sequence[tuple[str, Callable[[Snapshot], Snapshot]]] = (),
    providers: ProviderReader | None = None,
    offenders: OffenderWatch | None = None,
    remote: Callable[[datetime], Snapshot] | None = None,
    snoozes: SnoozeReader | None = None,
) -> Callable[[], Snapshot]:
    """Snapshot source that merges ``snapshot(now)`` of every adapter able to give one.

    ``remote`` gives the boards of ``kelpie agent`` processes (multi-host), merged like an
    adapter's, with what it could not see in ``Snapshot.unobserved``; if it raises, the
    whole remote farm counts as unobserved for that tick.

    ``enrich`` are named connector steps run on the merged board (e.g. GitHub conditions);
    one that raises is logged, counted in ``kelpie_connector_errors_total`` and skipped.

    An adapter that raises is logged, counted in ``kelpie_adapter_errors_total`` and left
    out of that tick; the others still count. With ``spans``, stored spans of the window
    (OTLP ingest) and per-session totals are added; a span id already seen is kept once. A
    span store that fails is logged and counted, and the tick goes on without its spans.
    Tool calls reported both by hooks and over OTLP are kept once, as the OTLP span
    completed with the hook's arguments hash and command. With ``budgets`` (and ``spans``),
    token usage per budget window is priced into ``Snapshot.budgets``, and (``remember``)
    the snapshot's session owners and task labels are recorded so spend stays attributed
    after adapters drop them. A failed spend read is logged and counted, and that tick
    carries the last good budget state with ``budgets_ok`` false. With ``resources``, host
    readings (Prometheus) are added; a reader that raises is logged and counted, and the
    tick goes on without them. With ``providers``, LLM provider statuses and limit hits are
    added (before budgets, which forecast from the hits), under the same rule. With
    ``offenders``, repeated huge-output offenders (rule ``output_offenders``) are added,
    recomputed hourly from the span store; a failed read is logged and counted. With
    ``snoozes``, ``Snapshot.snoozes`` is filled on the merged board (the agents' boards
    included: an agent never sends snoozes), from the store and the block reasons' markers.
    """
    last = _LastBudgets()

    def source() -> Snapshot:
        now = clock()
        merged = Snapshot(now=now)
        complete = True  # every local adapter and connector step read fine (snooze baselines)
        for adapter in adapters:
            if not isinstance(adapter, SnapshotSource):
                continue
            try:
                part = adapter.snapshot(now)
            except Exception as exc:
                log.error("adapter %s failed to snapshot: %s", adapter.name, exc)
                metrics.adapter_failed(adapter.name)
                complete = False
                continue
            merged = Snapshot(
                now=now,
                tasks=merged.tasks + part.tasks,
                owners=_unique_owners(merged.owners + part.owners),
                dependencies=merged.dependencies + part.dependencies,
                conditions=merged.conditions + part.conditions,
                resources=merged.resources + part.resources,
                sessions=merged.sessions + part.sessions,
                spans=merged.spans + part.spans,
                limit_hits=merged.limit_hits + part.limit_hits,
                providers=merged.providers + part.providers,
                builds=merged.builds + part.builds,
            )
        if remote is not None:
            try:
                part = remote(now)
            except Exception:
                log.exception("merging the agents' boards failed")
                merged = replace(merged, unobserved=("hub",))
            else:
                merged = replace(
                    merged,
                    tasks=merged.tasks + part.tasks,
                    owners=_unique_owners(merged.owners + part.owners),
                    dependencies=merged.dependencies + part.dependencies,
                    conditions=merged.conditions + part.conditions,
                    sessions=merged.sessions + part.sessions,
                    spans=merged.spans + part.spans,
                    unobserved=part.unobserved,
                )
        for name, step in enrich:
            try:
                merged = step(merged)
            except Exception as exc:
                log.error("connector %s failed to update the snapshot: %s", name, exc)
                metrics.connector_failed(name)
                complete = False
        if snoozes is not None:
            merged = snoozes.apply(merged, complete)
        if spans is not None:
            try:
                stored, totals = spans.spans(now), spans.totals(now)
            except Exception as exc:
                log.error("span store failed to read: %s", exc)
                metrics.span_store_failed()
            else:
                merged = replace(
                    merged, spans=merge_spans(stored, merged.spans), span_totals=totals
                )
        if resources is not None:
            try:
                merged = replace(merged, resources=merged.resources + tuple(resources.read(now)))
            except Exception as exc:
                log.error("resource source failed: %s", type(exc).__name__)
                metrics.resource_failed("unexpected")
        if providers is not None:
            try:
                statuses, hits = providers.read(now)
            except Exception as exc:
                log.error("LLM provider source failed: %s", type(exc).__name__)
                metrics.provider_failed("*", "unexpected")
            else:
                merged = replace(
                    merged,
                    providers=merged.providers + tuple(statuses),
                    limit_hits=merged.limit_hits + tuple(hits),
                )
        folded = merge_hook_spans(merged.spans)
        metrics.hook_spans_merged(folded.merged)
        merged = replace(merged, spans=folded.spans)
        if offenders is not None:
            try:
                remembered = spans.attribution(merged.now) if spans is not None else None
                found = offenders.offenders(merged, remembered)
            except Exception as exc:
                log.error("span store failed to read tool outputs: %s", exc)
                metrics.span_store_failed()
            else:
                merged = replace(merged, output_offenders=found)
        if spans is not None and budgets is not None and budgets.budgets:
            merged = _with_budgets(merged, spans, budgets, metrics, last, remember)
        return merged

    return source


@dataclass(slots=True)
class SnoozeReader:
    """Fills ``Snapshot.snoozes`` on a tick: the snoozes ``stored`` gives for ``now`` (the
    database; None: markers only) and the ``until:`` markers in block reasons (with
    ``[snooze] marker``). A store that fails to read is logged once until it reads again,
    and the tick goes on with the last snoozes it read (an expired one still drops out); an
    ignored marker is logged once per task and token. ``prune``, when set, deletes
    long-expired snoozes at most every ``prune_every``; a failed prune is logged and does not
    keep the snoozes from being read. No metric counts these failures: the span store's and
    the connectors' counters are theirs, and a snooze read is one query on the ledger's own
    connection.

    **Growth** (#176). ``baseline``, when set, records a stored snooze's baseline: at the
    first complete tick after the snooze, the work behind its task on the merged snapshot
    (:func:`kelpie.rules._snoozes.behind_count`, the count the rules compare with), written
    once. ``growth``, when set, latches growth the same way: the first complete tick that
    counts more than the baseline stores when (``grown_at``) and how much more
    (``grown_by``), so a count that drops again (a dependent's condition that comes and
    goes) does not bring the snooze back. A tick is complete for a snooze when every local
    adapter and connector step read fine (``apply(..., complete=False)`` otherwise) and the
    task's own part of the farm is observed (``Snapshot.unobserved`` names parts by the
    ``<agent>:`` prefix of their task ids; ``hub``: no agent board at all); a silent agent
    elsewhere does not hold it up. A snooze still without a baseline an hour after it was set
    is logged once (``kelpie why`` shows "baseline pending"). A write that fails is logged
    once as an error, then quietly tried again every tick until one succeeds. ``kelpie
    snooze`` stores no baseline: its offline board lacks the connectors' conditions. Markers
    never get one."""

    settings: SnoozeSettings
    stored: Callable[[datetime], Mapping[str, Snooze]] | None = None
    prune: Callable[[datetime], int] | None = None
    baseline: Callable[[Snooze, int], bool] | None = None
    growth: Callable[[Snooze, datetime, int], bool] | None = None
    prune_every: timedelta = timedelta(hours=1)
    pending_after: timedelta = timedelta(hours=1)
    _pruned_at: datetime | None = field(default=None, init=False)
    _warned: set[tuple[str, str]] = field(default_factory=set, init=False)
    _last: Mapping[str, Snooze] = field(default_factory=dict, init=False)
    _failing: bool = field(default=False, init=False)
    _write_failing: bool = field(default=False, init=False)
    _pending: set[tuple[str, datetime]] = field(default_factory=set, init=False)

    def apply(self, snapshot: Snapshot, complete: bool = True) -> Snapshot:
        """``snapshot`` with its snoozes. ``complete``: every local adapter and connector
        step of the tick read fine; without it no baseline or growth is recorded."""
        now = snapshot.now
        if self.stored is not None:
            if self.prune is not None and (
                self._pruned_at is None or now - self._pruned_at >= self.prune_every
            ):
                self._pruned_at = now
                try:
                    self.prune(now)
                except Exception as exc:
                    log.error("snooze store failed to prune: %s", exc)
            try:
                self._last = self.stored(now)
            except Exception as exc:
                if not self._failing:
                    log.error("snooze store failed to read, keeping the last snoozes: %s", exc)
                self._failing = True
            else:
                self._failing = False
            if self.baseline is not None or self.growth is not None:
                self._last = self._record(snapshot, complete)
        merged, problems = with_snoozes(snapshot, self._last, self.settings)
        self._warn(problems)
        return merged

    def _record(self, snapshot: Snapshot, complete: bool) -> Mapping[str, Snooze]:
        """The stored snoozes with the baselines and growth this tick records."""
        now = snapshot.now
        on_board = {t.id for t in snapshot.tasks}
        count = behind_counter(snapshot)
        out = dict(self._last)
        for tid, s in self._last.items():
            if s.source == "marker" or not s.active(now):
                continue
            seen = complete and tid in on_board and _observed(tid, snapshot.unobserved)
            if s.behind_at_snooze is None:
                if seen and self.baseline is not None:
                    behind = count(tid)
                    if self._write(tid, "baseline", self.baseline, s, behind):
                        out[tid] = replace(s, behind_at_snooze=behind)
                        continue
                self._still_pending(s, now, tid in on_board)
            elif s.grown_at is None and seen and self.growth is not None:
                more = count(tid) - s.behind_at_snooze
                if more > 0 and self._write(tid, "growth", self.growth, s, now, more):
                    out[tid] = replace(s, grown_at=now, grown_by=more)
        return out

    def _write(self, task_id: str, what: str, write: Callable[..., bool], *args: object) -> bool:
        """Run one write; a failure is logged as an error once until a write succeeds."""
        try:
            done = write(*args)
        except Exception as exc:
            if not self._write_failing:
                log.error(
                    "snooze store failed to record the %s of %s, retrying every tick: %s",
                    what, task_id, exc,
                )  # fmt: skip
            self._write_failing = True
            return False
        if self._write_failing:
            log.info("snooze store records snooze %ss again", what)
        self._write_failing = False
        return bool(done)

    def _still_pending(self, s: Snooze, now: datetime, on_board: bool) -> None:
        key = (s.task_id, s.created_at)
        if now - s.created_at >= self.pending_after and key not in self._pending:
            self._pending.add(key)
            why = (
                "its part of the farm was not observed in full since"
                if on_board
                else "the task is not on the board"
            )
            log.warning(
                "snooze of %s has no growth baseline yet, %s after it was set: %s; growth is "
                "not checked until it is counted",
                s.task_id, now - s.created_at, why,
            )  # fmt: skip

    def _warn(self, problems: Sequence[MarkerProblem]) -> None:
        for p in problems:
            if (p.task_id, p.token) not in self._warned:
                self._warned.add((p.task_id, p.token))
                log.warning("task %s: snooze marker %r ignored: %s", p.task_id, p.token, p.why)


def _observed(task_id: str, unobserved: Sequence[str]) -> bool:
    """Whether the task's own part of the farm is in the snapshot (as the engine's
    ``_blind`` reads ``Snapshot.unobserved``: ``hub`` means no agent board at all)."""
    if "hub" in unobserved:
        return False
    return not any(task_id.startswith(f"{part.split('/')[0]}:") for part in unobserved)


def snooze_reader(settings: Settings, store: LedgerStore) -> SnoozeReader:
    """The snooze reader of ``kelpie serve``: stored snoozes when the ledger is SQLite, with
    their growth baseline and latch recorded there."""
    if isinstance(store, SqliteLedgerStore):
        return SnoozeReader(
            settings.snooze,
            store.load_snoozes,
            store.prune_snoozes,
            store.set_snooze_baseline,
            store.set_snooze_growth,
        )
    return SnoozeReader(settings.snooze)


@dataclass(slots=True)
class _LastBudgets:
    statuses: tuple[BudgetStatus, ...] = ()


def _with_budgets(
    snapshot: Snapshot,
    spans: SpanWindow,
    budgets: BudgetConfig,
    metrics: Metrics,
    last: _LastBudgets,
    remember: bool,
) -> Snapshot:
    now = snapshot.now
    if remember:
        try:
            spans.remember(snapshot.sessions, snapshot.tasks, now)
        except Exception as exc:  # the read below may still work: go on
            log.error("span store failed to record session owners and task labels: %s", exc)
            metrics.span_store_failed()
    try:
        usage = spans.usage(now, budgets.windows)
        attribution = spans.attribution(now)
    except Exception as exc:
        log.error("span store failed to read spend: %s", exc)
        metrics.span_store_failed()
        metrics.budgets_stale()
        return replace(snapshot, budgets=last.statuses, budgets_ok=False)
    result = evaluate(budgets, usage, snapshot, attribution)
    last.statuses = result.statuses
    metrics.set_budgets(result, now)
    return replace(snapshot, budgets=result.statuses)


def _unique_owners(owners: tuple[Owner, ...]) -> tuple[Owner, ...]:
    """One owner per id across adapters: the last one reported, but a person stays a
    person. An adapter that does not know who is human (Claude Code task lists name owners
    without a kind) must not turn a human assignee into an agent, or rules would count
    them as idle capacity. Like ``human_owners``, this ignores case: ``Alice`` from a task
    list is the person Hermes knows as ``alice``. A person keeps the role the adapter
    that knows them gave."""
    found: dict[str, Owner] = {}
    for o in owners:
        prev = found.get(o.id)
        if prev is not None and prev.kind is OwnerKind.HUMAN and o.kind is not OwnerKind.HUMAN:
            continue  # the copy that knows the person wins, role included
        found[o.id] = o
    people = {o.id.casefold() for o in found.values() if o.kind is OwnerKind.HUMAN}
    return tuple(
        replace(o, kind=OwnerKind.HUMAN) if o.id.casefold() in people else o for o in found.values()
    )


def build_adapters(settings: Settings) -> list[PlatformAdapter]:
    adapters: list[PlatformAdapter] = []
    if settings.hermes is not None and settings.hermes.enabled:
        if settings.hermes.boards:  # until the adapter reads several boards (#198)
            raise ConfigError("[[adapters.hermes.boards]] is not supported by this build yet")
        adapters.append(settings.hermes.build())
    if settings.claude_code is not None and settings.claude_code.enabled:
        adapters.append(settings.claude_code.build())
    if settings.openclaw is not None and settings.openclaw.enabled:
        adapters.append(settings.openclaw.build())
    for issues in (settings.github_issues, settings.linear):
        if issues is not None and issues.enabled:
            try:
                adapters.append(issues.build())
            except ValueError as exc:
                raise ConfigError(str(exc)) from exc
    return adapters


def board_only(adapter: PlatformAdapter) -> PlatformAdapter:
    """``adapter`` reading its board alone: tasks, blocks and dependencies, without the
    spool, git or process checks a tick also runs (``kelpie snooze``, ``kelpie doctor``)."""
    if isinstance(adapter, hermes.HermesAdapter):
        return replace(adapter, git_state=False, spool=None, tools=None)
    if isinstance(adapter, OpenClawAdapter):
        return replace(adapter, spool=None)
    return adapter


@dataclass(slots=True)
class ControlLoop:
    """Runs ``engine.tick`` every ``config.interval`` in a background thread."""

    engine: Engine
    snapshot_source: Callable[[], Snapshot]
    metrics: Metrics
    # Run after every tick, must return at once (e.g. start span retention in a thread).
    housekeeping: Callable[[], object] | None = None
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def tick_once(self) -> None:
        started = time.monotonic()
        taken: list[Snapshot] = []

        def source() -> Snapshot:
            taken.append(self.snapshot_source())
            return taken[-1]

        try:
            results = self.engine.tick(source)
        except Exception:
            log.exception("engine tick failed")
            self.metrics.tick_failed(self.engine)
            return
        self.metrics.observe_tick(
            time.monotonic() - started, results, self.engine, taken[-1] if taken else None
        )

    def run(self) -> None:
        interval = self.engine.config.interval.total_seconds()
        while not self._stop.is_set():
            started = time.monotonic()
            self.tick_once()
            if self.housekeeping is not None:
                try:
                    self.housekeeping()
                except Exception:
                    log.exception("housekeeping failed")
            self._stop.wait(max(0.0, interval - (time.monotonic() - started)))

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("control loop already started")
        self._thread = threading.Thread(target=self.run, name="kelpie-control-loop", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
            if self._thread.is_alive():
                log.warning("control loop still busy after %.1fs; exiting anyway", timeout)

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


class Service(Protocol):
    def start(self) -> None: ...

    def stop(self, timeout: float = 5.0) -> None: ...


@dataclass(slots=True)
class App:
    settings: Settings
    loaded: LoadedRules
    engine: Engine
    loop: ControlLoop
    metrics: Metrics
    store: LedgerStore
    span_store: SpanStore | None = None
    ingest: TraceIngest | None = None  # None: [traces] enabled = false
    retention: SpanRetention | None = None
    forwarder: Forwarder | None = None
    alerts: Delivery | None = None  # [[alerts.channels]]
    # Background services started by server.run after the loop and stopped first on close,
    # before the alert delivery they may use (the scheduled daily report).
    services: list[Service] = field(default_factory=list)

    def close(self) -> None:
        for service in self.services:
            service.stop()
        self.loop.stop()
        if self.alerts is not None:
            self.alerts.close()
        if self.forwarder is not None:
            self.forwarder.close()
        # Closing the span store first stops a running prune at its next batch.
        for store in (self.span_store, self.store):
            close = getattr(store, "close", None)
            if callable(close):
                close()
        if self.retention is not None:
            self.retention.join(5.0)


def trace_window_warnings(rules: Sequence[Rule], traces: TraceSettings) -> list[str]:
    """Rules that look further back than the spans each tick sees would undercount."""
    if not traces.enabled:
        return []
    return [
        f"rule 'no_progress' window ({rule.window}) is longer than [traces] window "
        f"({traces.window}): it sees only the newest spans and undercounts tokens"
        for rule in rules
        if isinstance(rule, NoProgress) and rule.window > traces.window
    ]


def serialize_warnings(engine: EngineConfig, rules: Sequence[Rule]) -> list[str]:
    """Rule 12 parks tasks in act mode; only rule 2 in act mode wakes them when done."""
    serializing = any(isinstance(r, ConflictWarning) and r.serialize for r in rules)
    if not serializing or engine.dry_run or engine.mode_for("conflict_warning") is not Mode.ACT:
        return []
    if engine.mode_for("wait_condition") is Mode.ACT:
        return []
    return [
        "rule 'conflict_warning' serializes tasks in act mode but 'wait_condition' is not in "
        "act mode: parked tasks are not woken when the tasks they wait for are done"
    ]


def dispatch_gate_warnings(engine: EngineConfig) -> list[str]:
    """The dispatch gate parks ready tasks in act mode; only rule 2 in act mode wakes them."""
    if engine.dry_run or engine.mode_for("dispatch_gate") is not Mode.ACT:
        return []
    if engine.mode_for("wait_condition") is Mode.ACT:
        return []
    return [
        "rule 'dispatch_gate' parks ready tasks in act mode but 'wait_condition' is not in act "
        "mode: parked tasks are not woken when their prerequisites hold"
    ]


def ci_window_errors(rules: Sequence[Rule], settings: Settings) -> list[str]:
    """Rule 9 cannot judge builds the CI connector does not read: its window must fit."""
    github = settings.github
    ci = github.ci if github is not None and github.enabled else None
    if ci is None or not ci.enabled:
        return []
    return [
        f"rule 'ci_economy' window ({rule.window}) is longer than [connectors.github.ci] "
        f"window ({ci.window}): it would undercount builds and burn"
        for rule in rules
        if isinstance(rule, CiEconomy) and rule.window > ci.window
    ]


def billing_interval_warnings(rules: Sequence[Rule], settings: Settings) -> list[str]:
    """A billing reading is kept for ``KEEP_FOR`` intervals; rule 9 skips the quota (burn
    projection included) while it holds one older than ``max_reading_age``."""
    github = settings.github
    ci = github.ci if github is not None and github.enabled else None
    if ci is None or not ci.enabled or ci.billing_account is None:
        return []
    kept = ci.billing_interval * KEEP_FOR
    return [
        f"[connectors.github.ci] billing_interval ({ci.billing_interval}) keeps a reading for "
        f"up to {kept}, longer than rule 'ci_economy' max_reading_age "
        f"({rule.max_reading_age}): when reads fail, the CI minutes quota would go unwatched "
        "until the reading expires"
        for rule in rules
        if isinstance(rule, CiEconomy) and kept > rule.max_reading_age
    ]


def resource_interval_warnings(rules: Sequence[Rule], resources: ResourceSettings) -> list[str]:
    """Readings older than the rule's ``max_reading_age`` are ignored: a slower fetch would
    leave the rule blind between fetches."""
    prometheus = resources.prometheus
    if prometheus is None:
        return []
    return [
        f"[resources.prometheus] interval ({prometheus.interval}) is longer than rule "
        f"'resources' max_reading_age ({rule.max_reading_age}): host readings would be "
        "ignored as stale between fetches"
        for rule in rules
        if isinstance(rule, Resources) and prometheus.interval > rule.max_reading_age
    ]


def budget_provider_warnings(budgets: BudgetConfig, quotas: LlmQuotaSettings | None) -> list[str]:
    """A budget's ``providers`` naming no configured LLM provider forecasts from nothing,
    unless another source (an adapter) reports limit hits under that name."""
    known = {p.name for p in quotas.providers} if quotas is not None else set()
    return [
        f"budget {b.name!r} providers names {name!r}, which is not a "
        "[connectors.llm_quotas] provider: only limit hits from another source count for it"
        for b in budgets.budgets
        for name in b.providers or ()
        if name not in known
    ]


def provider_interval_warnings(rules: Sequence[Rule], quotas: LlmQuotaSettings | None) -> list[str]:
    """Providers probed less often than rule 'resources' keeps readings would flicker."""
    if quotas is None:
        return []
    return [
        f"[connectors.llm_quotas] provider {p.name} interval ({p.interval}) is longer than "
        f"rule 'resources' max_reading_age ({rule.max_reading_age}): its readings would be "
        "ignored between probes"
        for rule in rules
        if isinstance(rule, Resources)
        for p in quotas.providers
        if p.interval > rule.max_reading_age
    ]


def build_app(settings: Settings, clock: Callable[[], datetime] = utc_now) -> App:
    loaded = load_rules(settings.engine)
    if errors := ci_window_errors(loaded.rules, settings):
        raise ConfigError("; ".join(errors))
    checkpoints = next((r for r in loaded.rules if r.name == "checkpoints"), None)
    github = settings.github
    pulls = (
        github is not None and github.enabled and github.pulls is not None and github.pulls.enabled
    )
    if (
        checkpoints is not None
        and not getattr(checkpoints, "wip_without_pr_check", True)
        and not pulls
    ):
        log.warning(
            "checkpoints makes no WIP commits: it needs [connectors.github.pulls] to see "
            "that a branch has no open PR (or set wip_without_pr_check = true where task "
            "branches never carry PRs)"
        )
    act_within = getattr(checkpoints, "act_within", None)
    if act_within is not None:
        if act_within + timedelta(hours=1) >= settings.engine.once_window:
            log.warning(
                "checkpoints act_within plus 1h should stay shorter than [engine] once_window: "
                "a WIP commit's once-key may expire before the closing alert"
            )
        lookback = github.pulls.closed_lookback if github and github.pulls else None
        checks_prs = not getattr(checkpoints, "wip_without_pr_check", False)
        if pulls and checks_prs and lookback is not None and lookback < act_within:
            log.warning(
                "[connectors.github.pulls] closed_lookback is shorter than checkpoints "
                "act_within: WIP commits for sessions older than it are refused"
            )
    for warning in [
        *loaded.warnings,
        *trace_window_warnings(loaded.rules, settings.traces),
        *serialize_warnings(settings.engine, loaded.rules),
        *dispatch_gate_warnings(settings.engine),
        *resource_interval_warnings(loaded.rules, settings.resources),
        *billing_interval_warnings(loaded.rules, settings),
        *provider_interval_warnings(loaded.rules, settings.llm_quotas),
        *budget_provider_warnings(settings.budgets, settings.llm_quotas),
        *(
            f"subscription budget {b!r} names {n!r}, which no [spend.providers.<name>] quotas"
            " lists: it sets no provider's spend mode"
            for b, n in settings.spend.unlinked()
        ),
    ]:
        log.warning(warning)
    # Everything the config can be refused for is checked before any storage is opened:
    # a kelpie serve that will not start leaves no database behind.
    traces = settings.traces
    try:
        # With [hub] this port takes no spans: its token (or token file) is not needed.
        token = traces.token() if settings.hub is None else None
        destinations = [f.destination() for f in traces.forward] if traces.enabled else []
        prometheus = settings.resources.prometheus
        reader = prometheus.build() if prometheus is not None else None
        quotas = settings.llm_quotas.build() if settings.llm_quotas is not None else None
        metrics = Metrics()
        metrics.review_roles = settings.report.review_roles
        if problems := settings.alerts.problems():  # the delivery threads start last
            raise ValueError(problems[0])
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if prometheus is not None and prometheus.sends_token_in_clear():
        log.warning(
            "[resources.prometheus] sends a bearer token over plain http to %s; use https",
            prometheus.display_url,
        )
    # Without [[alerts.channels]] there is no alerter: an effect nobody can carry out turns
    # into an alert that is only logged. The defaults (dry-run, observe) send nothing.
    adapters = build_adapters(settings)
    for adapter in adapters:  # Kelpie's park and stop markers are issued on the app's clock
        if isinstance(adapter, hermes.HermesAdapter):
            adapter.clock = clock
    _refuse_unimplemented_actions(settings, loaded, adapters)
    hub = _build_hub(settings, adapters, clock)
    enrich, connectors = connector_steps(settings, metrics, clock)
    costs_source = cost_report_source(settings)  # kelpie serve only: reads with the admin key
    tokens = None
    if traces.enabled and traces.token_file is not None and settings.hub is None:
        try:  # also refuses a file others could have written (owner, mode, folders)
            tokens = TokenFile(
                traces.token_file,
                traces.token_env,
                clock,
                on_error=lambda why: log.error("[traces] token_file %s", why),
            )
        except TokenFileError as exc:
            raise ConfigError(f"[traces] token_file {exc}") from None
        if loose_mode(traces.token_file):
            log.warning(
                "[traces] token_file %s can be read by others than its owner: chmod 600 it",
                traces.token_file,
            )
        metrics.token_file = tokens.status
    try:
        store: LedgerStore = (
            MemoryLedgerStore() if settings.db_path is None else SqliteLedgerStore(settings.db_path)
        )
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise ConfigError(f"cannot open ledger database {settings.db_path}: {exc}") from exc
    # SQLite keeps open alert states across restarts; in memory they live with the process.
    states: StateStore = store if isinstance(store, SqliteLedgerStore) else MemoryStateStore()
    metrics.set_alert_channels(c.name for c in settings.alerts.channels)
    costs = cost_report_steps(settings, metrics, costs_source)
    enrich += costs
    engine = Engine(
        loaded.rules,
        adapters=adapters,
        connectors=connectors,
        config=settings.engine,
        ledger_store=store,
        state_store=states,
        fallback=hub is None,
    )
    farm = None
    if hub is not None:
        farm = RemoteFarm(
            hub,
            engine.add_executor,
            clock,
            alerter=lambda: engine.alerter,
            observer=metrics,
            dry_run=settings.engine.dry_run,
            late_after=settings.engine.retry_after,
        )
    remote = farm.snapshot if farm is not None else None
    metrics.set_rules(loaded.statuses, dry_run=settings.engine.dry_run)
    metrics.set_adapters(a.name for a in adapters)
    metrics.set_budgets_enabled(bool(settings.budgets.budgets))
    if reader is not None:
        reader.observer = metrics
        metrics.set_resources(True)
    if quotas is not None:
        quotas.observer = metrics
        metrics.set_providers(p.name for p in quotas.providers)
    if not traces.enabled:
        source = merged_snapshot(
            adapters,
            metrics,
            clock,
            resources=reader,
            enrich=enrich,
            providers=quotas,
            remote=remote,
            snoozes=snooze_reader(settings, store),
        )
        loop = ControlLoop(engine, source, metrics)
        app = App(
            settings,
            loaded,
            engine,
            loop,
            metrics,
            store,
            alerts=_start_alerts(settings, engine, metrics, store, clock),
        )
        return _with_hub(app, hub)
    span_store = _open_span_store(settings.db_path)
    metrics.set_traces(True, [*(str(k) for k in SpanKind), IGNORED])
    metrics.set_span_rows_source(span_store.count)
    if token is None and hub is None:
        log.warning(
            "OTLP ingest on /v1/traces accepts spans without a token; set $%s",
            traces.token_env,
        )
    ingest = TraceIngest(
        store=span_store,
        clock=clock,
        observer=metrics,
        token=token,
        tokens=tokens,
        max_body_bytes=traces.max_body_bytes,
        options=traces.options,
        max_concurrent=traces.max_concurrent_requests,
        max_age=traces.retention,
    )
    forwarder = None
    for f in traces.forward:
        if f.sends_secrets_in_clear:
            log.warning(
                "[[traces.forward]] %s sends credentials over plain http to %s; use https",
                f.name,
                f.display_endpoint,
            )
    if destinations:
        forwarder = Forwarder(destinations, metrics)
        metrics.set_forward_destinations(d.name for d in destinations)
        ingest.on_accepted = forwarder.submit
        forwarder.start()
    for _, step in costs:  # reconcile the bill with the spend in this span store
        if isinstance(step, CostReportStep):
            step.spans = span_store
    retention = SpanRetention(span_store, clock, traces.retention, traces.max_rows, metrics)
    window = SpanWindow(
        span_store, traces.window, traces.totals_window, traces.max_snapshot_spans, metrics
    )
    source = merged_snapshot(
        adapters,
        metrics,
        clock,
        window,
        settings.budgets,
        resources=reader,
        enrich=enrich,
        providers=quotas,
        offenders=OffenderWatch(span_store, traces.outputs, settings.report.tz),
        remote=remote,
        snoozes=snooze_reader(settings, store),
    )
    loop = ControlLoop(engine, source, metrics, housekeeping=retention.maybe_start)
    alerts = _start_alerts(settings, engine, metrics, store, clock)
    if hub is not None:
        # A span whose ids no agent token vouches for could be forged into any host's
        # namespace: with a hub, spans come only through the agents.
        log.info(
            "[hub] is set: OTLP ingest on /v1/traces of this port is off; spans reach Kelpie "
            "through the agents' relays, on the hub listener"
        )
        relayed, ingest = ingest, None
    else:
        relayed = None
    app = App(
        settings,
        loaded,
        engine,
        loop,
        metrics,
        store,
        span_store,
        ingest,
        retention,
        forwarder,
        alerts,
    )
    return _with_hub(app, hub, relayed)


def _build_hub(
    settings: Settings, adapters: Sequence[PlatformAdapter], clock: Callable[[], datetime]
) -> Hub | None:
    """The hub for ``[hub]``, or None. With a hub every board is an agent's: in-process
    adapters are refused, so no raw platform id can pass for a namespaced one."""
    if settings.hub is None:
        return None
    if adapters:
        raise ConfigError(
            "[hub] and in-process adapters exclude each other: run `kelpie agent` with the "
            f"[adapters.*] sections on this machine too (enabled here: "
            f"{', '.join(a.name for a in adapters)})"
        )
    try:
        tokens = settings.hub.tokens()
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if settings.hub.tls:
        try:  # a cert or key that does not load is refused before any storage is opened
            server_context(settings.hub)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
    else:
        log.warning("[hub] takes agents over plain http: their tokens cross the network in clear")
    return Hub(settings.hub, tokens, clock)


def _with_hub(app: App, hub: Hub | None, traces: TraceIngest | None = None) -> App:
    """Bind the hub's listener (a startup error is a config error) and run it as a service.
    ``traces``: ingest for the OTLP exports agents relay."""
    if hub is None:
        return app
    try:
        server = HubServer(hub, traces)
    except (OSError, ValueError) as exc:
        app.close()
        raise ConfigError(
            f"[hub] cannot listen on {hub.settings.host}:{hub.settings.port}: {exc}"
        ) from exc
    app.services.append(HubService(server))
    return app


def _start_alerts(
    settings: Settings,
    engine: Engine,
    metrics: Metrics,
    store: LedgerStore,
    clock: Callable[[], datetime],
) -> Delivery | None:
    """Start the alert channels (their worker threads, and the snooze button poller of a
    Telegram channel with ``buttons = true``) and give the engine its alerter. Done last in
    ``build_app``, so a startup error before this point leaves no thread behind."""
    control = _chat_control(settings, store, metrics, clock)
    try:
        alerts = settings.alerts.build(metrics, control=control)
    except ValueError as exc:
        if control is not None:
            control.store.close()
        raise ConfigError(str(exc)) from exc
    if alerts is not None:
        engine.alerter = alerts.alerter()
    return alerts


def _chat_control(
    settings: Settings, store: LedgerStore, metrics: Metrics, clock: Callable[[], datetime]
) -> ChatControl | None:
    """What snooze buttons need (#176): their store beside the database, and the callback
    that writes a press's snooze and its ledger entry to Kelpie's store. None when no
    channel has buttons."""
    channels = settings.alerts.button_channels
    if not channels:
        return None
    if settings.db_path is None or not isinstance(store, SqliteLedgerStore):
        raise ConfigError(
            '[alerts] snooze buttons need a database: set [storage] path (not ":memory:")'
        )
    path = button_store_path(settings.db_path)
    try:
        buttons = ButtonStore(path)
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise ConfigError(f"cannot open the button store {path}: {exc}") from exc

    sqlite_store = store

    def snooze(press: Press, now: datetime) -> datetime:
        record, entry = chat_snooze(
            press.task_id,
            press.days,
            press.name,
            press.user_id,
            press.channel,
            now,
            settings.snooze,
            dry_run=settings.engine.dry_run,
        )
        # The snooze and its ledger entry in one transaction: no chat snooze without one.
        if (held := sqlite_store.put_chat_snooze(record, entry)) is not None:
            raise AlreadySnoozed(held.until)
        return record.until

    metrics.set_chat_channels(c.name for c in channels)
    return ChatControl(buttons, snooze, clock, metrics)


def connector_steps(
    settings: Settings,
    metrics: Metrics,
    clock: Callable[[], datetime],
    network: bool = True,
) -> tuple[list[tuple[str, Callable[[Snapshot], Snapshot]]], list[ActionConnector]]:
    """Connector steps (human tasks; GitHub PR/CI conditions and block conditions; with
    ``[connectors.github.ci]`` recent builds and the CI quota) and the connectors that can
    carry out actions. Without ``network`` only the steps that read nothing but the board
    (task_status, human tasks) are returned."""
    # task_status conditions need nothing but the board (dispatch gate, rule 12).
    steps: list[tuple[str, Callable[[Snapshot], Snapshot]]] = [("task_status", check_task_status)]
    if settings.human_tasks is not None and settings.human_tasks.enabled:
        steps.append(("human_tasks", HumanTaskConnector(settings.human_tasks.block_patterns)))
    if not network:
        return steps, []
    github_steps, connectors = _github_steps(settings, metrics, clock)
    return steps + github_steps, connectors


RECONCILE_EVERY = timedelta(hours=1)  # also when the reported figures change
RECONCILE_RETRY = timedelta(minutes=5)  # after a failure
# A day is reconciled once it ended this long ago: spans of calls still running at
# midnight arrive late, and the providers' reports fill in over some hours.
SETTLE = timedelta(hours=6)


class CostReportStep:
    """Takes the providers' latest cost report figures (the reader keeps the interval and
    reads in the background), stores each provider's when they change, and reconciles
    them with the spend priced from traces for the same settled days (when there is a span
    store): ``Snapshot.reconciliations``, and the ``kelpie_spend_*`` metrics. A failed
    reconciliation keeps the last result and is retried after :data:`RECONCILE_RETRY`."""

    def __init__(
        self,
        source: CostReportReader,
        store: SqliteCostReports | None,
        settings: Settings | None = None,
        spans: SpanStore | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self.source = source
        self.store = store
        self.settings = settings
        self.spans = spans
        self.metrics = metrics
        self._stored: dict[str, tuple[ReportedCost, ...]] = {}
        self._reconciled: tuple[Reconciliation, ...] = ()
        self._reconciled_from: tuple[ReportedCost, ...] | None = None
        self._next_at: datetime | None = None  # when to reconcile again, figures unchanged
        self._failing = False
        self._seed: tuple[ReportedCost, ...] | None = None  # stored figures, until read

    def __call__(self, snapshot: Snapshot) -> Snapshot:
        now = snapshot.now
        costs = tuple(self.source.read(now))
        by_provider: dict[str, list[ReportedCost]] = {}
        for c in costs:
            by_provider.setdefault(c.provider, []).append(c)
        for provider, rows in by_provider.items():
            mine = tuple(rows)
            if self.store is not None and mine != self._stored.get(provider):
                self.store.replace(mine, now)
                self._stored[provider] = mine
        if self.spans is None or self.settings is None:
            self._observe(())
            return snapshot
        statuses = getattr(self.source, "statuses", None)
        read = {c.provider for c in costs}
        read |= {s.provider for s in (statuses() if callable(statuses) else ()) if s.ok}
        costs += tuple(c for c in self._stored_figures(now) if c.provider not in read)
        due = self._next_at is None or now >= self._next_at
        if due or (costs != self._reconciled_from and not self._failing):
            try:
                fresh = reconcile_spend(self.settings, self.spans, costs, now)
            except Exception as exc:  # spans not readable: keep the last, retry later
                if not self._failing:
                    log.error("cost reports: spend could not be reconciled: %s", exc)
                self._failing = True
                self._next_at = now + RECONCILE_RETRY
            else:
                if self._failing:
                    log.info("cost reports: spend reconciled again")
                self._failing = False
                self._reconciled = self._keep_last(fresh, now)
                self._reconciled_from, self._next_at = costs, now + RECONCILE_EVERY
        self._observe(self._reconciled)
        return replace(snapshot, reconciliations=self._reconciled)

    def _keep_last(
        self, fresh: tuple[Reconciliation, ...], now: datetime
    ) -> tuple[Reconciliation, ...]:
        """``fresh``, plus the last days of providers it has none of while those days are
        still within the provider's ``days`` (no newer day settled yet, e.g. ``days = 1``
        before 06:00 UTC): a standing warning is not resolved and raised again for that.
        Older ones go (reads failing for days, an emptied span store)."""
        reports = self.settings.cost_reports if self.settings is not None else None
        days = {r.provider: r.days for r in reports.reports} if reports is not None else {}
        today = now.astimezone(UTC).date()
        have = {r.provider for r in fresh}
        kept = tuple(
            r
            for r in self._reconciled
            if r.provider not in have
            and r.provider in days
            and r.day.date() >= today - timedelta(days=days[r.provider] + 1)
        )
        return tuple(sorted(fresh + kept, key=lambda r: (-r.day.timestamp(), r.provider)))

    def _stored_figures(self, now: datetime) -> tuple[ReportedCost, ...]:
        """The figures stored by an earlier run (read once), for the providers the reader
        has not read yet: after a restart, a provider that cannot be read keeps its days."""
        if self._seed is None:
            self._seed = ()
            reports = self.settings.cost_reports if self.settings is not None else None
            if self.store is not None and reports is not None:
                days = max(r.days for r in reports.reports)
                since = now.astimezone(UTC).date() - timedelta(days=days + 1)
                try:
                    stored = self.store.read(since)
                except (OSError, sqlite3.Error) as exc:
                    log.warning("cost reports: stored figures not readable: %s", exc)
                else:
                    self._seed = tuple(c for c in stored if c.provider in reports.providers)
        return self._seed

    def _observe(self, reconciled: Sequence[Reconciliation]) -> None:
        if self.metrics is None:
            return
        statuses = getattr(self.source, "statuses", None)
        found = statuses() if callable(statuses) else ()
        up = {s.provider: bool(s.ok) for s in found if s.ok is not None}  # None: not read yet
        self.metrics.set_cost_reports(up, reconciled)


def reconcile_spend(
    settings: Settings, spans: SpanStore, costs: Sequence[ReportedCost], now: datetime
) -> tuple[Reconciliation, ...]:
    """The UTC days ``costs`` cover that ended :data:`SETTLE` ago and that the span store
    holds whole (within the retention, and not older than its oldest span), priced from
    traces per provider and compared with the bill.

    A call counts for the provider its model belongs to (:meth:`SpendConfig.provider_for`)
    unless another platform wraps the id (Bedrock, Vertex, a ``vendor/`` router): that
    platform bills it, not the provider's cost report, unless a ``[spend.providers.<name>]
    models`` pattern matches the id as written. Calls without a known provider are counted
    as ``unknown_tokens`` of every provider. Each run prices every covered day again (one
    range scan per day, up to ``days`` of the report)."""
    spend = settings.spend
    reports = settings.cost_reports
    thresholds = reports.thresholds if reports is not None else {}
    scopes = reports.scopes if reports is not None else {}
    oldest = (now - settings.traces.retention).astimezone(UTC)
    held = spans.oldest_at()
    if held is not None:
        oldest = max(oldest, held.astimezone(UTC))
    providers = {c.provider for c in costs}
    estimated: dict[tuple[str, date], Estimate] = {}
    for day in sorted({c.day for c in costs}):
        start = datetime.combine(day, datetime.min.time(), UTC)
        end = start + timedelta(days=1)
        if held is None or end + SETTLE > now or start < oldest:
            continue  # not settled, or not held whole
        usd = dict.fromkeys(providers, 0.0)
        unpriced = dict.fromkeys(providers, 0)
        unknown = 0
        for row in spans.usage_between(start, end):
            tokens = row.input_tokens + row.output_tokens
            if row.model and platform_wrapped(row.model):
                provider = spend.configured_provider(row.model, as_written=True)
                if provider is None:
                    continue  # another platform's
            elif (provider := spend.provider_for(row.model)) is None:
                unknown += tokens  # could be any provider's
                continue
            if provider not in usd:
                continue
            price = settings.budgets.prices.price_for(row.model, day)
            if price is None:
                unpriced[provider] += tokens
            else:
                usd[provider] += price.cost(row)
        for provider in providers:
            estimated[(provider, day)] = Estimate(usd[provider], unpriced[provider], unknown)
    return reconcile(costs, estimated, thresholds, scopes)


def cost_report_source(settings: Settings) -> CostReportReader | None:
    """The reader of ``[connectors.cost_reports]`` (None without it); ConfigError when an
    admin key is missing. Opens nothing."""
    reports = settings.cost_reports
    if reports is None:
        return None
    try:
        source = reports.build()
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    for r in reports.reports:  # the variable's name only, never its value
        log.info(
            "[connectors.cost_reports.%s] admin key read from $%s", r.provider, r.admin_key_env
        )
    return source


def cost_report_steps(
    settings: Settings,
    metrics: Metrics | None = None,
    source: CostReportReader | None = None,
) -> list[tuple[str, Callable[[Snapshot], Snapshot]]]:
    """The cost report step, with its store opened (``source``: from
    :func:`cost_report_source`, built here when not given)."""
    if settings.cost_reports is None:
        return []
    if source is None:
        source = cost_report_source(settings)
    assert source is not None
    try:
        store = SqliteCostReports(settings.db_path) if settings.db_path is not None else None
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise ConfigError(f"cannot open the cost report store {settings.db_path}: {exc}") from exc
    return [("cost_reports", CostReportStep(source, store, settings, metrics=metrics))]


def _github_steps(
    settings: Settings, metrics: Metrics, clock: Callable[[], datetime]
) -> tuple[list[tuple[str, Callable[[Snapshot], Snapshot]]], list[ActionConnector]]:
    github = settings.github
    if github is None or not github.enabled:
        return [], []
    try:
        token = github.token()
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    if token is None:
        log.warning(
            "[connectors.github] $%s is not set: GitHub is read anonymously (60 requests an "
            "hour, public repositories only)",
            github.token_env,
        )
    else:
        log.info("[connectors.github] token read from $%s", github.token_env)  # name only
    connector = github.build(token, clock, metrics)
    metrics.set_github(True)
    step = ConditionSync(
        connector,
        settings.block_conditions,
        metrics,
        max_requests=github.max_requests_per_tick,
        tick_budget=github.tick_budget,
    )
    try:
        ci = github.build_ci(connector, clock)
    except ValueError as exc:
        raise ConfigError(str(exc)) from None
    steps: list[tuple[str, Callable[[Snapshot], Snapshot]]] = [("github", step)]
    if github.pulls is not None and github.pulls.enabled:
        # Rule 6's hold labels: the PR labels whose time the pull request step reads.
        watch = frozenset(hold_labels(settings.engine.rules.get("bottlenecks", {})))
        steps.append(("github_pulls", PullRequestSync(connector, github.pulls, watch)))
        # A cache entry dates from its request, a few seconds into a tick: keep a margin.
        if github.cache_ttl + github.tick_budget >= github.pulls.poll_interval:
            log.warning(
                "[connectors.github] cache_ttl (%s) plus tick_budget (%s) is not below "
                "[connectors.github.pulls] poll_interval (%s): PR lists come from the cache, "
                "so they are seldom known in full and rule 3 seldom proposes closing a parent",
                _span(github.cache_ttl),
                _span(github.tick_budget),
                _span(github.pulls.poll_interval),
            )
        log.info(
            "[connectors.github.pulls] open PRs, and those closed within %s, of %s; hold "
            "labels read: %s",
            _span(github.pulls.closed_lookback),
            ", ".join(sorted(connector.repos)),
            ", ".join(sorted(watch)) or "none",
        )
    if ci is None:
        return steps, []
    log.info(
        "[connectors.github.ci] builds of %s; cancel_build %s",
        ", ".join(ci.settings.repos),
        f"allowed on {', '.join(ci.settings.cancel_repos)}"
        if ci.settings.allow_cancel
        else "off (allow_cancel = false)",
    )
    if ci.billing is not None:  # names only, never the token
        log.info(
            "[connectors.github.ci] minutes used read from the billing usage of %s every %s, "
            "token from $%s",
            ci.settings.billing_account,
            ci.settings.billing_interval,
            ci.settings.billing_token_env,
        )
    return [*steps, ("github_ci", ci)], [ci]


def check_exposure(settings: Settings, host: str) -> None:
    """Refuse to serve an unauthenticated ingest endpoint on a non-loopback address. With
    ``[hub]`` this port takes no spans (agents relay them to the hub listener)."""
    if settings.hub is not None:
        return
    try:
        settings.traces.check_exposure(host)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc


def _open_span_store(db_path: Path | None) -> SpanStore:
    if db_path is None:
        return MemorySpanStore()
    try:
        return SqliteSpanStore(db_path)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise ConfigError(f"cannot open span store {db_path}: {exc}") from exc


# Rules whose effects on a platform are actions the Hermes adapter carries out: stop, wait
# (park), wake of a parked task, comment (plus alerts). Update with the adapter.
HERMES_ACT_RULES = frozenset(
    {"loop_breaker", "wait_condition", "conflict_warning", "dispatch_gate"}
)


# What an action kind does, for "Hermes can't <verb> yet".
_VERBS: dict[ActionKind, str] = {
    ActionKind.UNBLOCK: "clear blocks",
    ActionKind.REQUEUE: "requeue tasks",
    ActionKind.WAKE: "wake tasks",
    ActionKind.WAIT: "park tasks",
    ActionKind.REASSIGN: "reassign tasks",
    ActionKind.CLOSE: "close tasks",
    ActionKind.WIP_COMMIT: "make WIP commits",
    ActionKind.COMMENT: "comment on tasks",
    ActionKind.STOP: "stop runs",
    ActionKind.CANCEL_BUILD: "cancel builds",
}
_PLATFORMS = {
    "hermes": "Hermes",
    "claude-code": "Claude Code",
    "openclaw": "OpenClaw",
    "github-issues": "GitHub Issues",
    "linear": "Linear",
}


@dataclass(frozen=True, slots=True)
class ActBlocker:
    """Why a rule cannot act on this farm."""

    reason: str  # what is missing, e.g. "Hermes can't close tasks yet"
    refused: bool  # kelpie serve refuses the rule in act mode (unless dry-run)


def act_blockers(settings: Settings, adapters: Sequence[PlatformAdapter]) -> dict[str, ActBlocker]:
    """The implemented rules that cannot act on this farm, with what is missing. A rule left
    out can act: its effects (``PlannedRule.effects``) are carried out by a configured
    adapter or connector. The startup check refuses those marked ``refused`` in act mode;
    the others would turn their actions into alerts at runtime (no adapter declares them).
    The daily report recommends act mode only for rules left out.

    On Hermes only the rules in :data:`HERMES_ACT_RULES` act (stop, park, wake of a parked
    task, comment), and ``ci_economy`` through the CI connector when ``[connectors.github.ci]
    allow_cancel`` is set. On a hub, what its agents' adapters carry out is known only once
    they report, so a platform gap is not judged there."""
    ci = settings.github.ci if settings.github is not None and settings.github.enabled else None
    cancel = ci is not None and ci.enabled and ci.allow_cancel
    has_hermes = any(isinstance(a, hermes.HermesAdapter) for a in adapters)
    found: dict[str, ActBlocker] = {}
    for planned in PLANNED:
        name, effects = planned.name, planned.effects
        if name not in REGISTRY:
            continue
        platform = effects - {ActionKind.CANCEL_BUILD}
        if ActionKind.CANCEL_BUILD in effects and not cancel:
            found[name] = ActBlocker(_no_cancel(settings), refused=has_hermes)
        elif has_hermes and name not in HERMES_ACT_RULES and not (effects and not platform):
            found[name] = ActBlocker(_hermes_gap(name, platform), refused=True)
        elif (
            platform
            and settings.hub is None
            and not platform <= frozenset().union(*(_declared(a) for a in adapters))
        ):
            found[name] = ActBlocker(_platform_gap(adapters, platform), refused=False)
    return found


def _no_cancel(settings: Settings) -> str:
    github = settings.github if settings.github is not None and settings.github.enabled else None
    if github is not None and github.ci is not None and github.ci.enabled:
        return "Kelpie cancels no builds: [connectors.github.ci] allow_cancel is off"
    if github is not None:
        return "Kelpie cancels no builds: [connectors.github.ci] is not configured"
    return "Kelpie cancels no builds: no CI connector is configured ([connectors.github.ci])"


def _hermes_gap(rule: str, platform: frozenset[ActionKind]) -> str:
    if rule == "exit_contract":  # Hermes requeues only its own parks (a wake)
        return "Hermes requeues only tasks Kelpie parked"
    if lacking := platform - hermes.HermesAdapter.actions:
        return f"Hermes can't {_verbs(lacking)} yet"
    if platform:
        return f"{rule} does not act on Hermes yet"
    return "Hermes takes act mode only for rules that act; suggest sends the same alerts"


def _platform_gap(adapters: Sequence[PlatformAdapter], platform: frozenset[ActionKind]) -> str:
    lacking = platform - frozenset().union(*(_declared(a) for a in adapters))
    names = sorted({_PLATFORMS.get(a.name, a.name) for a in adapters})
    if not names:
        return f"no platform adapter is configured to {_verbs(lacking)}"
    if lacking == {ActionKind.STOP} and (
        off := sorted(_PLATFORMS.get(a.name, a.name) for a in adapters if _stop_off(a))
    ):
        return f"stop is turned off (stop_dir = false) for {' and '.join(off)}"
    if len(names) == 1:
        return f"{names[0]} can't {_verbs(lacking)} yet"
    return f"neither {' nor '.join(names)} can {_verbs(lacking)} yet"


# A platform adapter without an ``actions`` declaration carries out what its capability
# protocols offer.
_CAPABILITY_KINDS: tuple[tuple[type, ActionKind], ...] = (
    (SupportsStop, ActionKind.STOP),
    (SupportsWait, ActionKind.WAIT),
    (SupportsReassign, ActionKind.REASSIGN),
    (SupportsClose, ActionKind.CLOSE),
)


def _declared(adapter: PlatformAdapter) -> frozenset[ActionKind]:
    actions = getattr(adapter, "actions", None)
    if isinstance(actions, frozenset | set):
        kinds = frozenset(actions)
    else:
        kinds = frozenset(k for protocol, k in _CAPABILITY_KINDS if isinstance(adapter, protocol))
    return kinds - {ActionKind.STOP} if _stop_off(adapter) else kinds


def _stop_off(adapter: PlatformAdapter) -> bool:
    """``stop_dir = false``: the adapter refuses stops (Claude Code, OpenClaw)."""
    return hasattr(adapter, "stop_dir") and adapter.stop_dir is None


def _verbs(kinds: frozenset[ActionKind]) -> str:
    words = sorted(_VERBS.get(k, str(k)) for k in kinds)
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " or " + words[-1]


def _refuse_unimplemented_actions(
    settings: Settings, loaded: LoadedRules, adapters: Sequence[PlatformAdapter]
) -> None:
    """Refuse act mode for the rules :func:`act_blockers` marks ``refused``: on Hermes their
    actions would only fail at runtime. Dry-run acts on nothing, so it refuses nothing."""
    if settings.engine.dry_run:
        return
    blockers = act_blockers(settings, adapters)
    acting = sorted(
        s.name
        for s in loaded.statuses
        if s.implemented
        and s.mode is Mode.ACT
        and (b := blockers.get(s.name)) is not None
        and b.refused
    )
    if acting:
        why = "; ".join(f"{name}: {blockers[name].reason}" for name in acting)
        raise ConfigError(
            "these rules cannot run in act mode on this farm; use observe or suggest mode "
            f"(rules in act mode: {acting}; {why})"
        )


def render_adapters(adapters: Sequence[PlatformAdapter]) -> list[str]:
    lines = ["", "adapters:"]
    for adapter in adapters:
        check = getattr(adapter, "check", None)
        problem = check() if callable(check) else None
        db = getattr(adapter, "db_path", None)
        where = f" ({db})" if db else ""
        state = "readable" if problem is None else f"NOT readable: {problem}"
        lines.append(f"  {adapter.name}{where}: {state}")
        detail = getattr(adapter, "status_detail", None)
        if callable(detail):
            lines += [f"    {line}" for line in detail(utc_now())]
    if not adapters:
        lines.append("  (none configured)")
    return lines


def _dry_run_status(settings: Settings) -> str:
    if not settings.engine.dry_run:
        return "off"
    traces = settings.traces
    if traces.enabled and traces.forward:
        return "on (no rule action leaves Kelpie; accepted traces are still forwarded)"
    return "on (nothing leaves Kelpie)"


def _traces_status(traces: TraceSettings, hub: bool = False) -> str:
    if not traces.enabled:
        return "disabled"
    if hub:
        return "POST /v1/traces off on this port: agents relay spans to the hub"
    try:
        token = traces.token()
    except ValueError as exc:
        return f"NOT ready: {exc}"
    if traces.token_file is not None:
        auth = f"bearer token from {traces.token_file} (rotatable, grace {traces.token_grace})"
    elif token:
        auth = f"bearer token from ${traces.token_env}"
    else:
        auth = f"open (${traces.token_env} unset)"
    forward = "".join(f", forward to {f.name} ({f.display_endpoint})" for f in traces.forward)
    return f"POST /v1/traces, {auth}, window {traces.window}, retention {traces.retention}{forward}"


def read_budgets(
    settings: Settings,
    adapters: Sequence[PlatformAdapter] = (),
    clock: Callable[[], datetime] = utc_now,
) -> tuple[BudgetStatus, ...] | str:
    """Current budget state for ``kelpie status``: one snapshot against the database's span
    store, opened read-only (no database yet: no spend). A string says why spend cannot be
    read."""
    if not settings.budgets.budgets:
        return ()
    db = settings.db_path
    try:
        store: SpanStore = (
            SqliteSpanStore(db, readonly=True)
            if db is not None and db.exists()
            else MemorySpanStore()
        )
    except SchemaOutdated:
        return "the database predates budgets; run kelpie serve once to upgrade it"
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return f"cannot open the span store {db}: {exc}"
    try:
        metrics = Metrics()
        traces = settings.traces
        window = SpanWindow(store, traces.window, traces.totals_window, traces.max_snapshot_spans)
        snapshot = merged_snapshot(
            adapters, metrics, clock, window, settings.budgets, remember=False
        )()
        if not snapshot.budgets_ok or metrics.span_store_errors:
            return "the span store cannot be read"
        return snapshot.budgets
    finally:
        close = getattr(store, "close", None)
        if callable(close):
            close()


UNPRICED_WINDOW = timedelta(days=7)  # how far back models without a price are looked for


def read_models(
    settings: Settings, now: datetime | None = None
) -> tuple[tuple[str, int], ...] | str:
    """The models of the counted LLM calls the span store saw in the last
    :data:`UNPRICED_WINDOW` (at most the span retention), with their tokens, most first; a
    string says why the store cannot be read. No database or traces off: none."""
    db = settings.db_path
    if not settings.traces.enabled or db is None or not db.exists():
        return ()
    at = now or utc_now()
    window = min(UNPRICED_WINDOW, settings.traces.retention)
    try:
        store = SqliteSpanStore(db, readonly=True)
    except SchemaOutdated:
        return "the database predates budgets; run kelpie serve once to upgrade it"
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return f"cannot open the span store {db}: {exc}"
    try:
        (rows,) = store.usage_since(at, [window])
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return f"cannot read the span store {db}: {exc}"
    finally:
        store.close()
    found: dict[str, int] = {}
    for row in rows:
        if row.model:
            found[row.model] = found.get(row.model, 0) + row.input_tokens + row.output_tokens
    return _most_first(found)


def _most_first(tokens: Mapping[str, int]) -> tuple[tuple[str, int], ...]:
    return tuple(sorted(tokens.items(), key=lambda kv: (-kv[1], kv[0])))


def read_unpriced(
    settings: Settings,
    now: datetime | None = None,
    models: Sequence[tuple[str, int]] | str | None = None,
) -> tuple[tuple[str, int], ...] | str:
    """Model families (:func:`kelpie.core.prices.canonical_model`) of ``models`` (read
    with :func:`read_models` when not given) that nothing in the price table prices, with
    their tokens, most first; a string says why the store cannot be read."""
    at = now or utc_now()
    seen = read_models(settings, at) if models is None else models
    if isinstance(seen, str):
        return seen
    prices = settings.budgets.prices
    found: dict[str, int] = {}
    for model, tokens in seen:
        if prices.price_for(model, at.date()) is None:
            family = canonical_model(model)
            found[family] = found.get(family, 0) + tokens
    return _most_first(found)


def spend_modes(
    settings: Settings, models: Sequence[tuple[str, int]] | str = ()
) -> tuple[tuple[ProviderMode, int], ...]:
    """Each provider's spend mode with the tokens its models used in ``models`` (configured
    providers first, then those seen); models whose provider nothing tells are one row
    with provider None."""
    spend = settings.spend
    tokens: dict[str | None, int] = {}
    for model, n in models if not isinstance(models, str) else ():
        provider = spend.provider_for(model)
        tokens[provider] = tokens.get(provider, 0) + n
    rows = [(m, tokens.get(m.provider, 0)) for m in spend.modes(tokens)]
    return tuple(sorted(rows, key=lambda r: r[0].provider is None))  # the unknown ones last


def unknown_provider_models(
    settings: Settings, models: Sequence[tuple[str, int]] | str
) -> tuple[str, ...]:
    """Model families in ``models`` whose provider nothing tells."""
    if isinstance(models, str):
        return ()
    return tuple(
        dict.fromkeys(
            canonical_model(m) for m, _ in models if settings.spend.provider_for(m) is None
        )
    )


def render_spend_modes(
    settings: Settings, models: Sequence[tuple[str, int]] | str = ()
) -> list[str]:
    """The ``spend modes:`` block of ``kelpie status``: each provider's mode and why."""
    rows = spend_modes(settings, models)
    if not rows and not settings.spend.unlinked():
        return []
    lines = ["", "spend modes:"]
    for mode, tokens in rows:
        window = _span(min(UNPRICED_WINDOW, settings.traces.retention))
        used = f", {tokens:,} tokens in the last {window}" if tokens else ""
        if mode.provider is None:
            loose = unknown_provider_models(settings, models)
            more = f" and {len(loose) - 10} more" if len(loose) > 10 else ""
            lines.append(f"  (no provider known): {', '.join(loose[:10])}{more}{used}; add them")
            lines.append("    to [spend.providers.<name>] models to give them a spend mode")
            continue
        limit = settings.spend.limit_for(mode.provider)
        share = f", limit {limit[0]:,} tokens per {_span(limit[1])}" if limit else ""
        lines.append(f"  {mode.provider}: {mode.mode}{share}{used}")
        lines.append(f"    why: {mode.reason}")
    lines += [
        f"  warning: subscription budget {b!r} names {n!r}, which no [spend.providers.<name>]"
        " quotas lists: it sets no provider's spend mode"
        for b, n in settings.spend.unlinked()
    ]
    return lines


def read_reconciliations(
    settings: Settings, since: date, until: date, now: datetime | None = None
) -> tuple[Reconciliation, ...] | str:
    """The reconciliation of the stored cost report figures of days ``since`` to
    ``until`` (inclusive) with the spend priced from the span store, both opened read-only
    (for the daily report); a string says why they cannot be read. No database: none."""
    db = settings.db_path
    if settings.cost_reports is None or db is None or not db.exists():
        return ()
    try:
        reports = SqliteCostReports(db, readonly=True)
    except SchemaOutdated:
        return "the database predates cost reports; run kelpie serve once to upgrade it"
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return f"cannot open the cost reports in {db}: {exc}"
    try:
        costs = reports.read(since, until)
    except (OSError, sqlite3.Error) as exc:
        return f"cannot read the cost reports in {db}: {exc}"
    finally:
        reports.close()
    if not costs or not settings.traces.enabled:
        return ()
    try:
        spans = SqliteSpanStore(db, readonly=True)
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return f"cannot open the span store {db}: {exc}"
    try:
        return reconcile_spend(settings, spans, costs, now or utc_now())
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        return f"cannot read the span store {db}: {exc}"
    finally:
        spans.close()


def render_prices(settings: Settings, unpriced: Sequence[tuple[str, int]] | str = ()) -> list[str]:
    """The ``prices:`` line of ``kelpie status`` and the models without a price."""
    table = settings.budgets.prices
    parts = [f"{len(table.entries)} [prices] entries"] if table.entries else []
    if table.defaults:
        models = len({r.model for r in table.defaults})
        parts.append(f"default table of {models} models, checked {CHECKED:%Y-%m-%d}")
    else:
        parts.append("default table off ([spend] default_prices = false)")
    lines = [f"prices:   {', '.join(parts)}"]
    if isinstance(unpriced, str):
        lines.append(f"warning: models without a price cannot be listed: {unpriced}")
    elif unpriced:
        named = ", ".join(f"{m} ({t:,} tokens)" for m, t in unpriced[:10])
        more = f" and {len(unpriced) - 10} more" if len(unpriced) > 10 else ""
        lines.append(
            f"warning: models without a price (their cost is unknown): {named}{more};"
            ' add [prices."<model>"] input, output'
        )
    return lines


def _amount(value: float, unit: BudgetUnit) -> str:
    return f"{value:,.2f}" if unit is BudgetUnit.USD else f"{value:,.0f}"


def render_budgets(
    settings: Settings, statuses: Sequence[BudgetStatus] | str, now: datetime
) -> list[str]:
    config = settings.budgets
    if not config.budgets:
        return []
    lines = ["", "budgets:"]
    for b in config.budgets:
        match = f" {b.match}" if b.match else ""
        limits = ", ".join(
            f"{k} {_amount(v, b.unit)}"
            for k, v in (("soft", b.soft), ("hard", b.hard))
            if v is not None
        )
        lines.append(
            f"  {b.name}: {b.scope}{match}, {b.unit}, {b.mode}, window {b.window}"
            f" (burn rate over {b.burn_window}), {limits}"
        )
        if isinstance(statuses, str):
            lines.append(f"    spend unavailable: {statuses}")
            continue
        mine = [s for s in statuses if s.budget == b.name]
        if not mine:
            lines.append("    (no spend in the window)")
        for s in mine:
            when = "never at this rate"
            if s.runs_out_at is not None:
                when = "now" if s.runs_out_at <= now else f"{s.runs_out_at:%Y-%m-%d %H:%M %Z}"
            extra = " (estimate)" if s.estimated else ""
            if s.next_limit_at is not None:
                extra += f", next provider limit {s.next_limit_at:%Y-%m-%d %H:%M %Z}"
            if s.cost_unknown_tokens:
                extra += f", {s.cost_unknown_tokens:,} tokens without a price"
            lines.append(
                f"    {s.subject}: {s.state}, spent {_amount(s.spent, s.unit)} {s.unit}{extra},"
                f" remaining {_amount(s.remaining, s.unit)},"
                f" {_amount(s.burn_rate, s.unit)}/h, runs out {when}"
            )
    return lines


def _resources_status(resources: ResourceSettings) -> str:
    p = resources.prometheus
    if p is None:
        return "disabled (no [resources.prometheus])"
    try:
        token = p.token()
    except ValueError as exc:
        return f"NOT ready: {exc}"
    auth = f"bearer token from ${p.token_env}" if token else "no token"
    return (
        f"Prometheus {p.display_url}, {len(p.hosts)} host(s) by label {p.host_label}, "
        f"every {p.interval}, {auth}"
    )


def _github_status(settings: Settings) -> list[str]:
    github = settings.github
    if github is None:
        return []
    if not github.enabled:
        return ["github:   disabled"]
    try:
        auth = f"token from ${github.token_env}" if github.token() else "anonymous"
    except ValueError as exc:
        auth = f"NOT ready: {exc}"
    patterns = len(settings.block_conditions)
    lines = [
        f"github:   {github.api_url}, {auth}, repos {', '.join(github.repos)}, "
        f"{patterns} block condition pattern(s)"
    ]
    ci = github.ci
    if ci is not None and ci.enabled:
        cancel = (
            f"cancel_build on {', '.join(ci.cancel_repos)} (needs Actions: write; "
            + (
                f"token from ${ci.cancel_token_env})"
                if ci.cancel_token_env
                else "same token as reads)"
            )
            if ci.allow_cancel
            else "read-only (allow_cancel = false)"
        )
        lines.append(f"github ci: builds of {', '.join(ci.repos)}, {cancel}")
        if ci.billing_account is not None:
            kind = "organization" if ci.billing_organization else "user"
            try:
                ci.billing_token()
                auth = f"token from ${ci.billing_token_env}"
            except ValueError as exc:  # names the variable, never the value
                auth = f"NOT ready: {exc}"
            lines.append(
                f"github ci: minutes used from the billing usage of {kind} "
                f"{ci.billing_account}, every {ci.billing_interval} ({auth})"
            )
    return lines


def _human_tasks_status(settings: Settings) -> list[str]:
    human = settings.human_tasks
    if human is None:
        return []
    if not human.enabled:
        return ["human_tasks: disabled"]
    owners = settings.hermes.human_owners if settings.hermes is not None else frozenset()
    who = (
        f"hermes human owners {', '.join(sorted(owners))}"
        if owners
        else "no human owners configured"
    )
    return [f"human_tasks: human_task conditions on the board, {who}"]


def _human_decisions_status(settings: Settings) -> list[str]:
    """Who may record a person's decisions on the Hermes board: a trust setting."""
    hermes = settings.hermes
    if hermes is None or not hermes.enabled:
        return []
    return [
        f"human decisions: {', '.join(relays)} may record {person or 'a person'}'s, "
        f"opening with {marker!r}"
        for marker, relays, person in hermes.human_decisions
    ]


def _cost_reports_status(settings: Settings) -> str:
    reports = settings.cost_reports
    if reports is None:
        return "disabled (no [connectors.cost_reports])"
    parts = []
    for r in reports.reports:
        try:
            r.admin_key()
        except ValueError as exc:
            parts.append(f"{r.provider} NOT ready: {exc}")
            continue
        parts.append(
            f"{r.provider} ({r.kind} {r.api_url}, every {_span(r.interval)}, last {r.days} days)"
        )
    return ", ".join(parts)


def _providers_status(quotas: LlmQuotaSettings | None) -> str:
    if quotas is None:
        return "disabled (no [connectors.llm_quotas])"
    parts = []
    for p in quotas.providers:
        try:
            p.token()
        except ValueError as exc:
            return f"NOT ready: {exc}"
        parts.append(
            f"{p.name} ({p.kind} {p.display_url}{p.path.split('?')[0]}, every {p.interval})"
        )
    return ", ".join(parts)


def _alerts_status(alerts: AlertSettings) -> list[str]:
    """Channels, never their secrets (only the variable names)."""
    if not alerts.channels:
        return ["alerts:   disabled (no [[alerts.channels]])"]
    problems = alerts.problems()
    lines = [f"alerts:   {c.name}: {c.describe()}" for c in alerts.channels]
    lines += [f"alerts:   NOT ready: {p}" for p in problems]
    return lines


def render_status(
    settings: Settings,
    loaded: LoadedRules,
    adapters: Sequence[PlatformAdapter] = (),
    budgets: Sequence[BudgetStatus] | str = (),
    now: datetime | None = None,
    unpriced: Sequence[tuple[str, int]] | str = (),
    models: Sequence[tuple[str, int]] | str = (),
) -> str:
    """Human-readable view of what ``kelpie serve`` would load with these settings."""
    engine = settings.engine
    lines = [
        f"config:   {settings.source or '(defaults)'}",
        f"dry-run:  {_dry_run_status(settings)}",
        f"interval: {engine.interval}",
        f"ledger:   {settings.db_path or '(in memory)'}",
        f"traces:   {_traces_status(settings.traces, settings.hub is not None)}",
        *render_prices(settings, unpriced),
        f"resources: {_resources_status(settings.resources)}",
        *_github_status(settings),
        *_human_tasks_status(settings),
        *_human_decisions_status(settings),
        f"providers: {_providers_status(settings.llm_quotas)}",
        f"cost reports: {_cost_reports_status(settings)}",
        *_alerts_status(settings.alerts),
        "",
        f"{'#':>3}  {'rule':<17} {'prio':<4}  {'status':<16} mode",
    ]
    for s in loaded.statuses:
        number = str(s.planned.number) if s.planned.number else "-"
        prio = s.planned.priority
        status = "loaded" if s.implemented else "not implemented"
        mode = str(s.mode) if s.implemented else "-"
        if s.implemented and engine.dry_run and s.mode is not Mode.OBSERVE:
            mode += " (dry-run)"
        lines.append(f"{number:>3}  {s.name:<17} {prio:<4}  {status:<16} {mode}")
    lines += [f"warning: {w}" for w in loaded.warnings]
    lines += render_budgets(settings, budgets, now or utc_now())
    lines += render_spend_modes(settings, models)
    lines += render_adapters(adapters)
    return "\n".join(lines) + "\n"


def _span(d: timedelta) -> str:
    """A config duration as it is written, e.g. ``7d``, ``36h``, ``90m``."""
    seconds = int(d.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
