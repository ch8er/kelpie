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
from typing import Any

from kelpie.adapters.hermes import adapter as hermes
from kelpie.connectors.github import (
    BlockConditionPattern,
    ConditionSync,
    GitHubSettings,
    load_block_conditions,
    load_github,
)
from kelpie.connectors.human_tasks import (
    HumanTaskConnector,
    HumanTaskSettings,
    load_human_tasks,
)
from kelpie.connectors.llm_quotas import LlmQuotaSettings, load_llm_quotas
from kelpie.core.budgets import BudgetConfig, BudgetStatus, BudgetUnit, evaluate, load_budgets
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import LedgerStore, MemoryLedgerStore
from kelpie.core.model import Owner, Snapshot, SpanKind
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
from kelpie.resources.config import ResourceSettings, load_resources
from kelpie.rules import PLANNED, REGISTRY, PlannedRule, RuleFactory
from kelpie.rules.no_progress import NoProgress
from kelpie.rules.resources import Resources
from kelpie.traces.config import TraceSettings, load_traces
from kelpie.traces.forward import Forwarder
from kelpie.traces.hooks import merge_hook_spans
from kelpie.traces.ingest import IGNORED, TraceIngest
from kelpie.traces.store import SpanRetention, SpanWindow, merge_spans

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
    cli: str = hermes.DEFAULT_CLI  # Hermes executable that carries out ``stop``
    cli_timeout: timedelta = timedelta(seconds=hermes.CLI_TIMEOUT)  # one stop command
    human_owners: frozenset[str] = frozenset()  # assignees that are people, not agents

    def build(self) -> hermes.HermesAdapter:
        return hermes.HermesAdapter(
            db_path=self.db_path,
            spool=self.spool,
            lookback=self.lookback,
            git_state=self.git_state,
            git_timeout=self.git_timeout,
            cli=self.cli,
            cli_timeout=self.cli_timeout.total_seconds(),
            human_owners=self.human_owners,
        )


@dataclass(frozen=True, slots=True)
class Settings:
    engine: EngineConfig = field(default_factory=EngineConfig)
    hermes: HermesSettings | None = None  # None: no [adapters.hermes] section
    db_path: Path | None = DEFAULT_DB  # None keeps the ledger in memory
    source: Path | None = None  # the file the settings came from, if any
    traces: TraceSettings = field(default_factory=TraceSettings)
    budgets: BudgetConfig = field(default_factory=BudgetConfig)  # [[budgets]], [prices]
    resources: ResourceSettings = field(default_factory=ResourceSettings)
    github: GitHubSettings | None = None  # None: no [connectors.github] section
    block_conditions: tuple[BlockConditionPattern, ...] = ()
    human_tasks: HumanTaskSettings | None = None  # None: no [connectors.human_tasks]
    llm_quotas: LlmQuotaSettings | None = None  # [connectors.llm_quotas]


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
    return Settings(
        github=github,
        human_tasks=human_tasks,
        block_conditions=block_conditions,
        engine=engine,
        hermes=_load_hermes(data.get("adapters", {})),
        db_path=db_path,
        source=path,
        traces=traces,
        budgets=budgets,
        resources=resources,
        llm_quotas=llm_quotas,
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
    if extra := set(adapters) - {"hermes"}:
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
        "cli",
        "cli_timeout",
        "human_owners",
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
    return HermesSettings(
        db_path,
        paths["spool"],
        lookback,
        enabled,
        git_state,
        git_timeout,
        cli,
        cli_timeout,
        frozenset(h.strip() for h in humans),
    )


@dataclass(frozen=True, slots=True)
class RuleStatus:
    name: str
    planned: PlannedRule
    implemented: bool
    mode: Mode
    configured: bool  # has a [engine.rules.<name>] section


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


def _rule_config(name: str, config: EngineConfig) -> dict[str, Any]:
    """A rule's config section. ``loop_breaker`` polls the same checks rule 2 coaches, so it
    inherits ``wait_condition``'s ``poll_patterns`` unless it sets its own."""
    section = dict(config.rules.get(name, {}))
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
) -> Callable[[], Snapshot]:
    """Snapshot source that merges ``snapshot(now)`` of every adapter able to give one.

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
    added (before budgets, which forecast from the hits), under the same rule.
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
    return tuple({o.id: o for o in owners}.values())


def build_adapters(settings: Settings) -> list[PlatformAdapter]:
    adapters: list[PlatformAdapter] = []
    if settings.hermes is not None and settings.hermes.enabled:
        adapters.append(settings.hermes.build())
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
        try:
            results = self.engine.tick(self.snapshot_source)
        except Exception:
            log.exception("engine tick failed")
            self.metrics.tick_failed(self.engine)
            return
        self.metrics.observe_tick(time.monotonic() - started, results, self.engine)

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

    def close(self) -> None:
        self.loop.stop()
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
    for warning in [
        *loaded.warnings,
        *trace_window_warnings(loaded.rules, settings.traces),
        *resource_interval_warnings(loaded.rules, settings.resources),
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
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if prometheus is not None and prometheus.sends_token_in_clear():
        log.warning(
            "[resources.prometheus] sends a bearer token over plain http to %s; use https",
            prometheus.display_url,
        )
    # No alerter is wired yet: an effect nobody can carry out turns into an alert, and the
    # defaults (dry-run, observe) send nothing. The only connector action is cancel_build.
    adapters = build_adapters(settings)
    _refuse_unimplemented_actions(settings, loaded, adapters)
    # SQLite keeps open alert states across restarts; in memory they live with the process.
    states: StateStore = store if isinstance(store, SqliteLedgerStore) else MemoryStateStore()
    metrics = Metrics()
    enrich, connectors = _connector_steps(settings, metrics, clock)
    engine = Engine(
        loaded.rules,
        adapters=adapters,
        connectors=connectors,
        config=settings.engine,
        ledger_store=store,
        state_store=states,
    )
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
            adapters, metrics, clock, resources=reader, enrich=enrich, providers=quotas
        )
        loop = ControlLoop(engine, source, metrics)
        return App(settings, loaded, engine, loop, metrics, store)
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
    )
    loop = ControlLoop(engine, source, metrics, housekeeping=retention.maybe_start)
    return App(
        settings, loaded, engine, loop, metrics, store, span_store, ingest, retention, forwarder
    )


def _connector_steps(
    settings: Settings, metrics: Metrics, clock: Callable[[], datetime]
) -> tuple[list[tuple[str, Callable[[Snapshot], Snapshot]]], list[ActionConnector]]:
    """Connector steps (human tasks; GitHub PR/CI conditions and block conditions; with
    ``[connectors.github.ci]`` recent builds and the CI quota) and the connectors that can
    carry out actions."""
    steps: list[tuple[str, Callable[[Snapshot], Snapshot]]] = []
    if settings.human_tasks is not None and settings.human_tasks.enabled:
        steps.append(("human_tasks", HumanTaskConnector(settings.human_tasks.block_patterns)))
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
    ci = github.build_ci(connector)
    if ci is None:
        return [("github", step)], []
    log.info(
        "[connectors.github.ci] builds of %s; cancel_build %s",
        ", ".join(ci.settings.repos),
        f"allowed on {', '.join(ci.settings.cancel_repos)}"
        if ci.settings.allow_cancel
        else "off (allow_cancel = false)",
    )
    return [("github", step), ("github_ci", ci)], [ci]


def check_exposure(settings: Settings, host: str) -> None:
    """Refuse to serve an unauthenticated ingest endpoint on a non-loopback address."""
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


# Rules whose only effect on a platform is ``stop`` (plus alerts), the one action the Hermes
# adapter carries out. Update with the adapter when it gains more actions.
HERMES_ACT_RULES = frozenset({"loop_breaker"})


def _refuse_unimplemented_actions(
    settings: Settings, loaded: LoadedRules, adapters: Sequence[PlatformAdapter]
) -> None:
    """Hermes carries out ``stop`` only; any other act-mode rule would only fail at runtime.

    ``ci_economy`` acts through the CI connector (``cancel_build``), not the platform, so
    it may act when ``[connectors.github.ci] allow_cancel`` is set."""
    only_hermes = bool(adapters) and all(isinstance(a, hermes.HermesAdapter) for a in adapters)
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
    if only_hermes and acting and not settings.engine.dry_run:
        raise ConfigError(
            f"Hermes actions other than stop are not implemented; use observe or suggest mode "
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
            f"cancel_build on {', '.join(ci.cancel_repos)} (needs Actions: write)"
            if ci.allow_cancel
            else "read-only (allow_cancel = false)"
        )
        lines.append(f"github ci: builds of {', '.join(ci.repos)}, {cancel}")
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
