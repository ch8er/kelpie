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
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.adapters.hermes import adapter as hermes
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import LedgerStore, MemoryLedgerStore
from kelpie.core.model import Owner, Snapshot
from kelpie.core.ports import PlatformAdapter, SnapshotSource
from kelpie.core.rule import Rule
from kelpie.core.serde import parse_duration
from kelpie.core.states import MemoryStateStore, StateStore
from kelpie.core.storage import SqliteLedgerStore
from kelpie.metrics import Metrics
from kelpie.rules import PLANNED, REGISTRY, PlannedRule, RuleFactory

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

    def build(self) -> hermes.HermesAdapter:
        return hermes.HermesAdapter(
            db_path=self.db_path,
            spool=self.spool,
            lookback=self.lookback,
            git_state=self.git_state,
            git_timeout=self.git_timeout,
        )


@dataclass(frozen=True, slots=True)
class Settings:
    engine: EngineConfig = field(default_factory=EngineConfig)
    hermes: HermesSettings | None = None  # None: no [adapters.hermes] section
    db_path: Path | None = DEFAULT_DB  # None keeps the ledger in memory
    source: Path | None = None  # the file the settings came from, if any


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
    unknown = set(data) - {"engine", "storage", "adapters"}
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
    return Settings(
        engine=engine, hermes=_load_hermes(data.get("adapters", {})), db_path=db_path, source=path
    )


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
    return HermesSettings(db_path, paths["spool"], lookback, enabled, git_state, git_timeout)


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
            rules.append(registry[name](dict(config.rules.get(name, {}))))
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


def utc_now() -> datetime:
    return datetime.now(UTC)


def empty_snapshot(clock: Callable[[], datetime] = utc_now) -> Callable[[], Snapshot]:
    """Snapshot source without adapters: just the current time."""
    return lambda: Snapshot(now=clock())


def merged_snapshot(
    adapters: Sequence[PlatformAdapter],
    metrics: Metrics,
    clock: Callable[[], datetime] = utc_now,
) -> Callable[[], Snapshot]:
    """Snapshot source that merges ``snapshot(now)`` of every adapter able to give one.

    An adapter that raises is logged, counted in ``kelpie_adapter_errors_total`` and left
    out of that tick; the others still count.
    """

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
            )
        return merged

    return source


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

    def close(self) -> None:
        self.loop.stop()
        close = getattr(self.store, "close", None)
        if callable(close):
            close()


def build_app(settings: Settings, clock: Callable[[], datetime] = utc_now) -> App:
    loaded = load_rules(settings.engine)
    for warning in loaded.warnings:
        log.warning(warning)
    try:
        store: LedgerStore = (
            MemoryLedgerStore() if settings.db_path is None else SqliteLedgerStore(settings.db_path)
        )
    except (OSError, sqlite3.Error, RuntimeError) as exc:
        raise ConfigError(f"cannot open ledger database {settings.db_path}: {exc}") from exc
    # No connectors or alerter are wired yet: an effect nobody can carry out turns into an
    # alert, and the defaults (dry-run, observe) send nothing.
    adapters = build_adapters(settings)
    _refuse_unimplemented_actions(settings, loaded, adapters)
    # SQLite keeps open alert states across restarts; in memory they live with the process.
    states: StateStore = store if isinstance(store, SqliteLedgerStore) else MemoryStateStore()
    engine = Engine(
        loaded.rules,
        adapters=adapters,
        config=settings.engine,
        ledger_store=store,
        state_store=states,
    )
    metrics = Metrics()
    metrics.set_rules(loaded.statuses, dry_run=settings.engine.dry_run)
    metrics.set_adapters(a.name for a in adapters)
    loop = ControlLoop(engine, merged_snapshot(adapters, metrics, clock), metrics)
    return App(settings, loaded, engine, loop, metrics, store)


def _refuse_unimplemented_actions(
    settings: Settings, loaded: LoadedRules, adapters: Sequence[PlatformAdapter]
) -> None:
    """Hermes cannot carry out actions yet; an act-mode rule would only fail at runtime."""
    only_hermes = bool(adapters) and all(isinstance(a, hermes.HermesAdapter) for a in adapters)
    acting = sorted(s.name for s in loaded.statuses if s.implemented and s.mode is Mode.ACT)
    if only_hermes and acting and not settings.engine.dry_run:
        raise ConfigError(
            f"Hermes actions are not implemented; use observe or suggest mode (rules in act "
            f"mode: {acting})"
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


def render_status(
    settings: Settings, loaded: LoadedRules, adapters: Sequence[PlatformAdapter] = ()
) -> str:
    """Human-readable view of what ``kelpie serve`` would load with these settings."""
    engine = settings.engine
    lines = [
        f"config:   {settings.source or '(defaults)'}",
        f"dry-run:  {'on (nothing leaves Kelpie)' if engine.dry_run else 'off'}",
        f"interval: {engine.interval}",
        f"ledger:   {settings.db_path or '(in memory)'}",
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
    lines += render_adapters(adapters)
    return "\n".join(lines) + "\n"
