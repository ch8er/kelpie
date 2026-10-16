"""Wires the engine for ``kelpie serve`` and ``kelpie status``.

This module sits outside ``kelpie.core`` on purpose: it may know about rules (and, later,
adapters and connectors) and it may read the clock. Rules never do.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from kelpie.adapters.claude_code.config import ClaudeCodeSettings, load_claude_code
from kelpie.adapters.hermes import adapter as hermes
from kelpie.adapters.hermes.git_state import valid_base_ref
from kelpie.adapters.hermes.park import DEFAULT_WAIT_KEY, WAIT_KEY_NAME
from kelpie.alerts.config import AlertSettings, load_alerts
from kelpie.alerts.delivery import Delivery
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
from kelpie.core.budgets import BudgetConfig, BudgetStatus, BudgetUnit, evaluate, load_budgets
from kelpie.core.conditions import check_task_status
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import LedgerStore, MemoryLedgerStore
from kelpie.core.model import Owner, OwnerKind, Snapshot, SpanKind
from kelpie.core.ports import (
    ActionConnector,
    PlatformAdapter,
    ProviderReader,
    ResourceReader,
    SnapshotSource,
)
from kelpie.core.rule import Rule
from kelpie.core.serde import parse_duration
from kelpie.core.spans import MemorySpanStore, SpanStore
from kelpie.core.states import MemoryStateStore, StateStore
from kelpie.core.storage import SchemaOutdated, SqliteLedgerStore, SqliteSpanStore
from kelpie.metrics import Metrics
from kelpie.remote.config import AgentSettings, HubSettings, load_agent, load_hub
from kelpie.remote.farm import RemoteFarm
from kelpie.remote.hub import Hub, HubServer, HubService
from kelpie.report.config import ReportSettings, load_report
from kelpie.resources.config import ResourceSettings, load_resources
from kelpie.rules import PLANNED, REGISTRY, PlannedRule, RuleFactory
from kelpie.rules.ci_economy import CiEconomy
from kelpie.rules.conflict_warning import ConflictWarning
from kelpie.rules.no_progress import NoProgress
from kelpie.rules.resources import Resources
from kelpie.traces.config import TraceSettings, load_traces
from kelpie.traces.forward import Forwarder
from kelpie.traces.hooks import merge_hook_spans
from kelpie.traces.ingest import IGNORED, TraceIngest
from kelpie.traces.store import OffenderWatch, SpanRetention, SpanWindow, merge_spans

log = logging.getLogger(__name__)

DEFAULT_CONFIG = Path("kelpie.toml")
DEFAULT_DB = Path("kelpie.db")


class ConfigError(ValueError):
    """The configuration cannot be used; Kelpie refuses to start."""


@dataclass(frozen=True, slots=True)
class HermesSettings:
    """``[adapters.hermes]``: where the Kanban board and the hooks spool live."""

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
            roles=dict(self.roles),
            role_from_name=self.role_from_name,
            wait_key=self.wait_key or DEFAULT_WAIT_KEY,
            prework_statuses=self.prework_statuses,
        )


@dataclass(frozen=True, slots=True)
class Settings:
    engine: EngineConfig = field(default_factory=EngineConfig)
    hermes: HermesSettings | None = None  # None: no [adapters.hermes] section
    claude_code: ClaudeCodeSettings | None = None  # None: no [adapters.claude_code] section
    db_path: Path | None = DEFAULT_DB  # None keeps the ledger in memory
    source: Path | None = None  # the file the settings came from, if any
    traces: TraceSettings = field(default_factory=TraceSettings)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)  # [[budgets]], [prices]
    resources: ResourceSettings = field(default_factory=ResourceSettings)
    github: GitHubSettings | None = None  # None: no [connectors.github] section
    block_conditions: tuple[BlockConditionPattern, ...] = ()
    human_tasks: HumanTaskSettings | None = None  # None: no [connectors.human_tasks]
    llm_quotas: LlmQuotaSettings | None = None  # [connectors.llm_quotas]
    alerts: AlertSettings = field(default_factory=AlertSettings)  # [alerts]
    report: ReportSettings = field(default_factory=ReportSettings)  # [report]
    agent: AgentSettings | None = None  # [agent]: this host runs ``kelpie agent``
    hub: HubSettings | None = None  # [hub]: ``kelpie serve`` takes reports from agents


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
        "resources",
        "connectors",
        "block_conditions",
        "alerts",
        "report",
        "agent",
        "hub",
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
        budgets = load_budgets(data.get("budgets"), data.get("prices"), traces.retention)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if budgets.budgets and not traces.enabled:
        raise ConfigError("[[budgets]] read spend from traces: they need [traces] enabled")
    github, block_conditions, human_tasks, llm_quotas = _load_connectors(data)
    try:
        alerts = load_alerts(data.get("alerts"))
        report = load_report(data.get("report"))
        agent = load_agent(data.get("agent"))
        hub = load_hub(data.get("hub"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return Settings(
        github=github,
        human_tasks=human_tasks,
        block_conditions=block_conditions,
        engine=_with_condition_reasons(engine, block_conditions),
        hermes=_with_wait_key(_load_hermes(data.get("adapters", {})), db_path),
        claude_code=_load_claude_code(data.get("adapters", {})),
        db_path=db_path,
        source=path,
        traces=traces,
        budgets=budgets,
        resources=resources,
        llm_quotas=llm_quotas,
        alerts=alerts,
        report=report,
        agent=agent,
        hub=hub,
    )


def _load_connectors(
    data: Mapping[str, Any],
) -> tuple[
    GitHubSettings | None,
    tuple[BlockConditionPattern, ...],
    HumanTaskSettings | None,
    LlmQuotaSettings | None,
]:
    """``[connectors.github]`` (with ``[[block_conditions]]``), ``[connectors.human_tasks]``
    and ``[connectors.llm_quotas]``."""
    connectors = data.get("connectors", {})
    if not isinstance(connectors, Mapping):
        raise ConfigError("[connectors] must be a table")
    if extra := set(connectors) - {"github", "human_tasks", "llm_quotas"}:
        raise ConfigError(f"unknown [connectors] entries: {sorted(extra)}")
    try:
        github = load_github(connectors["github"]) if "github" in connectors else None
        repos = github.repos if github is not None else None
        patterns = load_block_conditions(data.get("block_conditions"), repos)
        human_tasks = (
            load_human_tasks(connectors["human_tasks"]) if "human_tasks" in connectors else None
        )
        llm_quotas = load_llm_quotas(connectors.get("llm_quotas"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    return github, patterns, human_tasks, llm_quotas


def _load_hermes(adapters: Any) -> HermesSettings | None:
    if not isinstance(adapters, Mapping):
        raise ConfigError("[adapters] must be a table")
    if extra := set(adapters) - {"hermes", "claude_code"}:
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
        "roles",
        "role_from_name",
        "prework_statuses",
        "wait_key",
    }:
        raise ConfigError(f"unknown [adapters.hermes] keys: {sorted(extra)}")
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
    humans = section.get("human_owners", [])
    if (
        not isinstance(humans, list)
        or not all(isinstance(h, str) and h.strip() for h in humans)
        or len({h.strip().casefold() for h in humans}) != len(humans)
    ):
        raise ConfigError(
            "[adapters.hermes] human_owners must be a list of distinct non-empty strings"
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
    )


def _with_wait_key(hermes: HermesSettings | None, db: Path | None) -> HermesSettings | None:
    """The park-signing key defaults next to the ledger, which a deployment keeps (a
    container's data volume): losing the key leaves Kelpie's parks unrecognised. With an
    in-memory ledger it is ``~/.kelpie/hermes-wait.key``."""
    if hermes is None or hermes.wait_key is not None:
        return hermes
    return replace(hermes, wait_key=DEFAULT_WAIT_KEY if db is None else db.parent / WAIT_KEY_NAME)


def _load_claude_code(adapters: Mapping[str, Any]) -> ClaudeCodeSettings | None:
    if "claude_code" not in adapters:
        return None
    try:
        return load_claude_code(adapters["claude_code"])
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
            rules.append(registry[name](_rule_config(name, config)))
        except Exception as exc:
            raise ConfigError(f"bad config for rule {name!r}: {exc}") from exc
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


def _rule_config(name: str, config: EngineConfig) -> dict[str, Any]:
    """A rule's config section. ``loop_breaker`` polls the same checks rule 2 coaches, so it
    inherits ``wait_condition``'s ``poll_patterns`` unless it sets its own; ``orphan_status``
    inherits ``bottlenecks``' hold labels and ``return_causes`` ``conflict_warning``'s
    ``expected_shared`` the same way."""
    section = dict(config.rules.get(name, {}))
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
    recomputed hourly from the span store; a failed read is logged and counted.
    """
    last = _LastBudgets()

    def source() -> Snapshot:
        now = clock()
        merged = Snapshot(now=now)
        for adapter in adapters:
            if not isinstance(adapter, SnapshotSource):
                continue
            try:
                part = adapter.snapshot(now)
            except Exception as exc:
                log.error("adapter %s failed to snapshot: %s", adapter.name, exc)
                metrics.adapter_failed(adapter.name)
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
        adapters.append(settings.hermes.build())
    if settings.claude_code is not None and settings.claude_code.enabled:
        adapters.append(settings.claude_code.build())
    return adapters


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
    for warning in [
        *loaded.warnings,
        *trace_window_warnings(loaded.rules, settings.traces),
        *serialize_warnings(settings.engine, loaded.rules),
        *dispatch_gate_warnings(settings.engine),
        *resource_interval_warnings(loaded.rules, settings.resources),
        *billing_interval_warnings(loaded.rules, settings),
        *provider_interval_warnings(loaded.rules, settings.llm_quotas),
        *budget_provider_warnings(settings.budgets, settings.llm_quotas),
    ]:
        log.warning(warning)
    try:
        store: LedgerStore = (
            MemoryLedgerStore() if settings.db_path is None else SqliteLedgerStore(settings.db_path)
        )
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise ConfigError(f"cannot open ledger database {settings.db_path}: {exc}") from exc
    traces = settings.traces
    try:
        token = traces.token()
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
    _refuse_unimplemented_actions(settings, loaded, adapters)
    hub = _build_hub(settings, adapters, clock)
    # SQLite keeps open alert states across restarts; in memory they live with the process.
    states: StateStore = store if isinstance(store, SqliteLedgerStore) else MemoryStateStore()
    metrics.set_alert_channels(c.name for c in settings.alerts.channels)
    enrich, connectors = connector_steps(settings, metrics, clock)
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
        )
        loop = ControlLoop(engine, source, metrics)
        app = App(
            settings,
            loaded,
            engine,
            loop,
            metrics,
            store,
            alerts=_start_alerts(settings, engine, metrics),
        )
        return _with_hub(app, hub)
    span_store = _open_span_store(settings.db_path)
    metrics.set_traces(True, [*(str(k) for k in SpanKind), IGNORED])
    metrics.set_span_rows_source(span_store.count)
    if token is None:
        log.warning(
            "OTLP ingest on /v1/traces accepts spans without a token; set $%s",
            traces.token_env,
        )
    ingest = TraceIngest(
        store=span_store,
        clock=clock,
        observer=metrics,
        token=token,
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
    )
    loop = ControlLoop(engine, source, metrics, housekeeping=retention.maybe_start)
    alerts = _start_alerts(settings, engine, metrics)
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
    if not settings.hub.tls:
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


def _start_alerts(settings: Settings, engine: Engine, metrics: Metrics) -> Delivery | None:
    """Start the alert channels (their worker threads) and give the engine its alerter. Done
    last in ``build_app``, so a startup error before this point leaves no thread behind."""
    try:
        alerts = settings.alerts.build(metrics)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if alerts is not None:
        engine.alerter = alerts.alerter()
    return alerts


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


def _refuse_unimplemented_actions(
    settings: Settings, loaded: LoadedRules, adapters: Sequence[PlatformAdapter]
) -> None:
    """Hermes carries out stop, wait, wake of a parked task and comment; any other act-mode
    rule would only fail at runtime on Hermes tasks, also next to adapters that refuse
    actions with ``UnsupportedAction``.

    ``ci_economy`` acts through the CI connector (``cancel_build``), not the platform, so
    it may act when ``[connectors.github.ci] allow_cancel`` is set."""
    has_hermes = any(isinstance(a, hermes.HermesAdapter) for a in adapters)
    ci = settings.github.ci if settings.github is not None and settings.github.enabled else None
    by_connector = {"ci_economy"} if ci is not None and ci.enabled and ci.allow_cancel else set()
    acting = sorted(
        s.name
        for s in loaded.statuses
        if s.implemented
        and s.mode is Mode.ACT
        and s.name not in HERMES_ACT_RULES
        and s.name not in by_connector
    )
    if has_hermes and acting and not settings.engine.dry_run:
        raise ConfigError(
            "these rules emit actions the Hermes adapter does not carry out (it does stop, "
            "wait, wake of a parked task and comment); use observe or suggest mode "
            f"(rules in act mode: {acting})"
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


def _traces_status(traces: TraceSettings) -> str:
    if not traces.enabled:
        return "disabled"
    try:
        token = traces.token()
    except ValueError as exc:
        return f"NOT ready: {exc}"
    auth = (
        f"bearer token from ${traces.token_env}" if token else f"open (${traces.token_env} unset)"
    )
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
) -> str:
    """Human-readable view of what ``kelpie serve`` would load with these settings."""
    engine = settings.engine
    lines = [
        f"config:   {settings.source or '(defaults)'}",
        f"dry-run:  {_dry_run_status(settings)}",
        f"interval: {engine.interval}",
        f"ledger:   {settings.db_path or '(in memory)'}",
        f"traces:   {_traces_status(settings.traces)}",
        f"resources: {_resources_status(settings.resources)}",
        *_github_status(settings),
        *_human_tasks_status(settings),
        f"providers: {_providers_status(settings.llm_quotas)}",
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
    lines += render_adapters(adapters)
    return "\n".join(lines) + "\n"


def _span(d: timedelta) -> str:
    """A config duration as it is written, e.g. ``7d``, ``36h``, ``90m``."""
    seconds = int(d.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds % size == 0:
            return f"{seconds // size}{unit}"
    return f"{seconds}s"
