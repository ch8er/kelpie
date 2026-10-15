from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome, Suppression
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


def test_tracker_transitions() -> None:
    tracker = StateTracker(MemoryStateStore())
    assert tracker.observe(alert(), T0)[0] is Transition.OPENED
    assert tracker.observe(alert(), T0 + 3 * H)[0] is Transition.ONGOING
    transition, state = tracker.observe(alert(), T0 + 4 * H)
    assert transition is Transition.ESCALATED and state.level == 1
    assert tracker.observe(alert(), T0 + 11 * H)[0] is Transition.ONGOING
    assert tracker.observe(alert(), T0 + 12 * H)[0] is Transition.ESCALATED
    assert tracker.observe(alert(), T0 + 24 * H)[0] is Transition.ESCALATED
    assert tracker.observe(alert(), T0 + 99 * H)[0] is Transition.ONGOING


def test_thresholds_already_passed_at_first_sight_are_not_news() -> None:
    tracker = StateTracker(MemoryStateStore())
    transition, state = tracker.observe(alert(since=T0 - 5 * H), T0)
    assert transition is Transition.OPENED and state.level == 1
    assert tracker.observe(alert(since=T0 - 5 * H), T0 + 6 * H)[0] is Transition.ONGOING
    assert tracker.observe(alert(since=T0 - 5 * H), T0 + 7 * H)[0] is Transition.ESCALATED


def test_severity_rise_escalates_and_since_defaults_to_now() -> None:
    tracker = StateTracker(MemoryStateStore())
    tracker.observe(alert(), T0)
    assert tracker.observe(alert(severity=Severity.CRITICAL), T0 + H)[0] is Transition.ESCALATED
    assert tracker.observe(alert(severity=Severity.WARNING), T0 + 2 * H)[0] is Transition.ONGOING
    no_since = Action(ActionKind.ALERT, "emit", "x", state="s2")
    assert tracker.observe(no_since, T0)[1].since == T0


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
    rule.on = False
    assert tick(e, T0 + 5 * H) == [("Resolved after 5.0h", Outcome.ALERTED)]
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
    assert tick(e, T0 + 2 * H) == [("Resolved after 2.0h", Outcome.ALERTED)]


def test_execute_without_evaluate_resolves_nothing() -> None:
    e = engine(Emit([]), FakeAlerter())
    e.states.store.save(OpenState("x", "emit", T0, T0, T0, 0, Severity.WARNING, "s", None))
    assert e.execute([], Snapshot(now=T0)) == []


def test_dry_run_tracks_states_but_sends_nothing() -> None:
    alerter, rule = FakeAlerter(), Toggle(alert())
    e = engine(rule, alerter, EngineConfig())  # defaults: dry-run, observe
    assert [o for _, o in tick(e, T0)] == [Outcome.DRY_RUN]
    rule.on = False
    assert [o for _, o in tick(e, T0 + H)] == [Outcome.DRY_RUN]
    assert alerter.sent == []


def test_non_alert_actions_ignore_state() -> None:
    requeue = Action(ActionKind.REQUEUE, "emit", "r", task_id="T-1", state="s", since=T0)
    e = engine(Emit([requeue]), FakeAlerter())
    tick(e, T0)
    assert e.states.store.open_states() == []


class BrokenStates(MemoryStateStore):
    def open_states(self) -> list[OpenState]:
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
    tracker.observe(alert(), T0)
    tracker.observe(alert(), T0 + 4 * H)
    store.close()

    store = SqliteLedgerStore(path)
    [state] = store.open_states()
    assert (state.id, state.level, state.task_id, state.since) == ("s1", 1, "T-1", T0)
    tracker = StateTracker(store)
    assert tracker.observe(alert(), T0 + 5 * H)[0] is Transition.ONGOING
    tracker.resolve(state, T0 + 6 * H)
    assert store.open_states() == []
    assert tracker.observe(alert(), T0 + 7 * H)[0] is Transition.OPENED  # same id, new episode
    store.close()


def test_state_escalations_config() -> None:
    config = EngineConfig.from_mapping({"state_escalations": ["1h", "6h"]})
    assert config.state_escalations == (H, 6 * H)
    for bad in (["6h", "1h"], ["1h", "1h"], ["0s"], "4h"):
        with pytest.raises(ValueError):
            EngineConfig.from_mapping({"state_escalations": bad})


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
