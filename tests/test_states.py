from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Dispatched, Outcome, Suppression
from kelpie.core.model import Snapshot
from kelpie.core.states import MemoryStateStore, OpenState, StateTracker, Transition
from kelpie.core.storage import SqliteLedgerStore
from tests.fakes import Emit, FakeAdapter, FakeAlerter

T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
H = timedelta(hours=1)
LIVE = EngineConfig(dry_run=False, default_mode=Mode.ACT)


def alert(since: datetime = T0, severity: Severity = Severity.WARNING, state: str = "s1") -> Action:
    return Action(
        ActionKind.ALERT, "emit", f"T-1 is stuck. Details {severity}.", task_id="T-1",
        key="k", severity=severity, state=state, since=since,
    )  # fmt: skip


class Toggle:
    """A rule that reports its state while ``on`` and raises while ``broken``."""

    name = "emit"

    def __init__(self, action: Action) -> None:
        self.action, self.on, self.broken = action, True, False

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        if self.broken:
            raise RuntimeError("bug")
        return [self.action] if self.on else []


def engine(rule: object, alerter: FakeAlerter, config: EngineConfig = LIVE, **kw: object) -> Engine:
    return Engine([rule], adapters=[FakeAdapter()], alerter=alerter, config=config, **kw)  # type: ignore[list-item, arg-type]


def tick(e: Engine, at: datetime) -> list[tuple[str, Outcome]]:
    return [(d.action.reason.split(":")[0], d.outcome) for d in e.tick(lambda: Snapshot(now=at))]


# --- tracker ----------------------------------------------------------------------------


def observe(
    tracker: StateTracker, action: Action, at: datetime, mode: Mode = Mode.ACT
) -> Transition:
    transition, new, _ = tracker.peek(action, at, mode)
    tracker.commit(new)
    return transition


def test_tracker_transitions_count_from_first_report() -> None:
    tracker = StateTracker(MemoryStateStore())
    old = alert(since=T0 - 30 * H)  # began long ago: escalations still count from now
    assert observe(tracker, old, T0) is Transition.OPENED
    assert observe(tracker, old, T0 + 3 * H) is Transition.ONGOING
    assert observe(tracker, old, T0 + 4 * H) is Transition.ESCALATED
    assert observe(tracker, old, T0 + 11 * H) is Transition.ONGOING
    assert observe(tracker, old, T0 + 12 * H) is Transition.ESCALATED
    assert observe(tracker, old, T0 + 24 * H) is Transition.ESCALATED
    assert observe(tracker, old, T0 + 99 * H) is Transition.ONGOING


def test_severity_rise_and_description_change() -> None:
    tracker = StateTracker(MemoryStateStore())
    observe(tracker, alert(), T0)
    assert observe(tracker, alert(severity=Severity.CRITICAL), T0 + H) is Transition.ESCALATED
    assert observe(tracker, alert(severity=Severity.WARNING), T0 + 2 * H) is Transition.ONGOING
    changed = replace(alert(), key="other-description")
    assert observe(tracker, changed, T0 + 3 * H) is Transition.CHANGED


def test_since_is_kept_across_sightings() -> None:
    tracker = StateTracker(MemoryStateStore())
    observe(tracker, alert(since=T0 - 5 * H), T0)
    no_since = replace(alert(), since=None)
    _, state, _ = tracker.peek(no_since, T0 + H, Mode.ACT)
    assert state.since == T0 - 5 * H
    _, fresh, _ = tracker.peek(Action(ActionKind.ALERT, "emit", "x", state="s2"), T0, Mode.ACT)
    assert fresh.since == T0


def test_state_seen_only_while_observing_is_announced_when_live() -> None:
    tracker = StateTracker(MemoryStateStore())
    assert observe(tracker, alert(), T0, Mode.OBSERVE) is Transition.OPENED
    assert observe(tracker, alert(), T0 + H, Mode.OBSERVE) is Transition.ONGOING
    assert observe(tracker, alert(), T0 + 2 * H, Mode.ACT) is Transition.OPENED
    assert observe(tracker, alert(), T0 + 3 * H, Mode.ACT) is Transition.ONGOING


# --- engine -----------------------------------------------------------------------------


def test_engine_alerts_on_transitions_only() -> None:
    alerter, rule = FakeAlerter(), Toggle(alert())
    e = engine(rule, alerter)
    assert tick(e, T0) == [("T-1 is stuck. Details warning.", Outcome.ALERTED)]
    # Hours of ongoing state: suppressed every tick, well past the 30m ledger cooldown.
    for minutes in range(30, 240, 30):
        [(_, outcome)] = tick(e, T0 + timedelta(minutes=minutes))
        assert outcome is Outcome.SUPPRESSED
    assert tick(e, T0 + 4 * H) == [("Still open after 4.0h", Outcome.ALERTED)]
    [(_, outcome)] = tick(e, T0 + 5 * H)
    assert outcome is Outcome.SUPPRESSED
    rule.on = False
    assert tick(e, T0 + 5 * H + timedelta(minutes=5)) == []  # a short gap is not a resolution
    assert tick(e, T0 + 5 * H + timedelta(minutes=10)) == [("Resolved after 5.2h", Outcome.ALERTED)]
    assert len(alerter.sent) == 3
    assert e.states.store.open_states() == []
    assert tick(e, T0 + 6 * H) == []


def test_ongoing_is_recorded_as_suppressed_state_open() -> None:
    e = engine(Toggle(alert()), FakeAlerter())
    e.tick(lambda: Snapshot(now=T0))
    [d] = e.tick(lambda: Snapshot(now=T0 + H))
    assert d.suppression is Suppression.STATE_OPEN


def test_a_failing_rule_does_not_resolve_its_states() -> None:
    alerter, rule = FakeAlerter(), Toggle(alert())
    e = engine(rule, alerter)
    tick(e, T0)
    rule.broken = True
    assert tick(e, T0 + H) == []
    assert len(e.states.store.open_states()) == 1
    rule.broken, rule.on = False, False
    assert tick(e, T0 + 2 * H) == [("Resolved after 2.0h", Outcome.ALERTED)]  # last seen T0


def test_execute_without_evaluate_resolves_nothing() -> None:
    e = engine(Emit([]), FakeAlerter())
    e.states.store.save(OpenState("x", "emit", T0, T0, T0, 0, Severity.WARNING, "s", None))
    assert e.execute([], Snapshot(now=T0)) == []


def test_dry_run_tracks_states_but_sends_nothing() -> None:
    alerter, rule = FakeAlerter(), Toggle(alert())
    e = engine(rule, alerter, EngineConfig())  # defaults: dry-run, observe
    assert [o for _, o in tick(e, T0)] == [Outcome.DRY_RUN]
    rule.on = False
    assert [o for _, o in tick(e, T0 + H)] == [Outcome.DRY_RUN]  # the resolved notice
    assert alerter.sent == []


def test_non_alert_actions_ignore_state() -> None:
    requeue = Action(ActionKind.REQUEUE, "emit", "r", task_id="T-1", state="s", since=T0)
    e = engine(Emit([requeue]), FakeAlerter())
    tick(e, T0)
    assert e.states.store.open_states() == []


class BrokenStates(MemoryStateStore):
    def get_state(self, state_id: str) -> OpenState | None:
        raise OSError("disk")


def test_state_store_failure_falls_back_to_plain_alerts() -> None:
    alerter = FakeAlerter()
    e = engine(Toggle(alert()), alerter, state_store=BrokenStates())
    assert [o for _, o in tick(e, T0)] == [Outcome.ALERTED]
    assert [o for _, o in tick(e, T0 + H)] == [Outcome.ALERTED]  # ledger cooldown 30m
    assert len(alerter.sent) == 2


# --- persistence and config -------------------------------------------------------------


def test_sqlite_state_store_survives_restart_and_reopens(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.db"
    store = SqliteLedgerStore(path)
    tracker = StateTracker(store)
    observe(tracker, alert(), T0)
    observe(tracker, alert(), T0 + 4 * H)
    store.close()

    store = SqliteLedgerStore(path)
    [state] = store.open_states()
    assert (state.id, state.level, state.task_id, state.since, state.mode) == (
        "s1",
        1,
        "T-1",
        T0,
        Mode.ACT,
    )
    tracker = StateTracker(store)
    assert observe(tracker, alert(), T0 + 5 * H) is Transition.ONGOING
    tracker.resolve(state, T0 + 6 * H)
    assert store.open_states() == [] and store.get_state("s1") is None
    assert observe(tracker, alert(), T0 + 7 * H) is Transition.OPENED  # same id, new episode
    store.close()


def test_v1_database_upgrades_and_keeps_its_ledger(tmp_path: Path) -> None:
    import sqlite3

    from kelpie.core import storage

    path = tmp_path / "kelpie.db"
    db = sqlite3.connect(path)
    db.executescript(storage.MIGRATIONS[1])
    db.execute(
        "INSERT INTO actions (at, fingerprint, subject, rule, kind, mode, outcome, reason,"
        " params) VALUES ('2026-03-02T09:00:00.000000+00:00', 'f', 'task:T-1', 'r', 'alert',"
        " 'observe', 'observed', 'x', '{}')"
    )
    db.execute("PRAGMA user_version = 1")
    db.commit()
    db.close()
    store = SqliteLedgerStore(path)
    assert len(store.since(T0 - 2 * H)) == 1
    assert store.open_states() == []
    store.close()


# --- delivery failures and flapping -----------------------------------------------------


class FlakyAlerter(FakeAlerter):
    def __init__(self) -> None:
        super().__init__()
        self.down = True

    def send(self, title: str, body: str, severity: str) -> None:
        if self.down:
            raise ConnectionError("slack down")
        super().send(title, body, severity)


def test_failed_delivery_is_retried_next_tick() -> None:
    alerter = FlakyAlerter()
    e = engine(Toggle(alert()), alerter)
    assert [o for _, o in tick(e, T0)] == [Outcome.FAILED]
    alerter.down = False
    assert [o for _, o in tick(e, T0 + timedelta(minutes=6))] == [Outcome.ALERTED]  # retry_after
    assert len(alerter.sent) == 1


def test_cooldown_never_swallows_a_new_transition() -> None:
    alerter, rule = FakeAlerter(), Toggle(alert())
    e = engine(rule, alerter, replace(LIVE, state_resolve_after=timedelta(0)))
    tick(e, T0)
    rule.on = False
    assert [o for _, o in tick(e, T0 + timedelta(minutes=5))] == [Outcome.ALERTED]  # resolved
    rule.on = True
    assert [o for _, o in tick(e, T0 + timedelta(minutes=10))] == [Outcome.ALERTED]  # re-opened
    assert len(alerter.sent) == 3


def test_suppressed_transition_is_retried() -> None:
    alerter = FakeAlerter()
    e = engine(Toggle(alert()), alerter, replace(LIVE, max_actions_per_task_per_hour=1))
    e.ledger.record(  # the task already had its one action this hour
        Dispatched(
            Action(ActionKind.REQUEUE, "other", "x", task_id="T-1"), Mode.ACT, Outcome.EXECUTED
        ),
        T0,
    )
    assert [o for _, o in tick(e, T0 + timedelta(minutes=1))] == [Outcome.SUPPRESSED]
    assert e.states.store.open_states() == []  # not announced, so not opened
    assert [o for _, o in tick(e, T0 + 2 * H)] == [Outcome.ALERTED]


def test_going_live_announces_states_seen_in_dry_run() -> None:
    alerter, rule = FakeAlerter(), Toggle(alert())
    store = MemoryStateStore()
    tick(engine(rule, alerter, EngineConfig(), state_store=store), T0)
    live = engine(rule, alerter, LIVE, state_store=store)
    assert [o for _, o in tick(live, T0 + 10 * H)] == [Outcome.ALERTED]
    assert len(alerter.sent) == 1


def test_a_changed_description_does_not_flap() -> None:
    alerter = FakeAlerter()
    rule = Toggle(alert())
    e = engine(rule, alerter)
    tick(e, T0)
    rule.action = replace(alert(), key="stale-evidence", reason="Connector stopped. More.")
    assert tick(e, T0 + H) == [("Changed", Outcome.ALERTED)]
    assert [t for t, _ in alerter.sent] == ["[emit] alert T-1"] * 2  # no resolved in between
    assert len(e.states.store.open_states()) == 1


def test_execute_after_one_evaluate_resolves_only_once() -> None:
    e = engine(Toggle(alert()), FakeAlerter())
    tick(e, T0)
    e.evaluate(Snapshot(now=T0 + H))
    e.execute([], Snapshot(now=T0 + H))
    assert e.execute([], Snapshot(now=T0 + 2 * H)) == []


def test_state_escalations_config() -> None:
    config = EngineConfig.from_mapping({"state_escalations": ["1h", "6h"]})
    assert config.state_escalations == (H, 6 * H)
    for bad in (["6h", "1h"], ["1h", "1h"], ["0s"], "4h"):
        with pytest.raises(ValueError):
            EngineConfig.from_mapping({"state_escalations": bad})
    assert EngineConfig.from_mapping(
        {"state_resolve_after": "1m"}
    ).state_resolve_after == timedelta(minutes=1)
    with pytest.raises(ValueError):
        EngineConfig.from_mapping({"state_resolve_after": -1})


@pytest.mark.parametrize(
    "fixture",
    ["10-condition-satisfied-wake", "11-orphan-accepted-task", "01-guard-block-after-new-push"],
)
def test_rules_mark_long_lived_alerts_as_states(fixture: str) -> None:
    from kelpie.rules import REGISTRY
    from tests.incidents import FIXTURES, load

    incident = load(FIXTURES / f"{fixture}.yaml")
    actions = REGISTRY[incident.rule](incident.config).evaluate(incident.snapshot)
    alerts = [a for a in actions if a.kind is ActionKind.ALERT]
    assert alerts and all(a.state and a.since for a in alerts)
