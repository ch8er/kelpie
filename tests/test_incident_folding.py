"""The engine folds per-task wait alerts under an open CI outage (incident 20)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import AlertEvent, Transition
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.incidents import FOLDED_UNDER, CiOutage, outage_signature, outage_state_id
from kelpie.core.ledger import LedgerEntry, Outcome, Suppression
from kelpie.core.model import Snapshot
from kelpie.core.states import MemoryStateStore
from tests.fakes import FakeAdapter, FakeAlerter

T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
H = timedelta(hours=1)
M = timedelta(minutes=1)
LIVE = EngineConfig(dry_run=False, default_mode=Mode.SUGGEST)
STATE = outage_state_id("ci.yml", "build", "Select toolchain")


def outage_alert(tasks: tuple[str, ...]) -> Action:
    outage = CiOutage(
        state=STATE,
        signature=outage_signature("ci.yml", "build", "Select toolchain"),
        job="build",
        workflow="ci.yml",
        step="Select toolchain",
        repos=("example/app",),
        tasks=tasks,
    )
    return Action(
        ActionKind.ALERT, "ci_economy", "CI outage since 01:43", severity=Severity.WARNING,
        key=STATE, state=STATE, since=T0 - 8 * H, params=outage.to_params(),
    )  # fmt: skip


def wait_alert(task: str, **kw: object) -> Action:
    base: dict[str, object] = {
        "task_id": task, "key": f"overdue:{task}", "state": f"stale_blocks:{task}:b",
        "since": T0 - 6 * H, "severity": Severity.WARNING, "about_wait": True,
    }  # fmt: skip
    return Action(ActionKind.ALERT, "stale_blocks", f"{task} is blocked", **{**base, **kw})  # type: ignore[arg-type]


@dataclass
class Emit:
    name: str
    actions: list[Action] = field(default_factory=list)

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        return list(self.actions)


def farm(config: EngineConfig = LIVE) -> tuple[Engine, Emit, Emit, FakeAlerter]:
    ci, blocks, alerter = Emit("ci_economy"), Emit("stale_blocks"), FakeAlerter()
    engine = Engine([ci, blocks], adapters=[FakeAdapter()], alerter=alerter, config=config)  # type: ignore[list-item]
    return engine, ci, blocks, alerter


def tick(engine: Engine, at: datetime) -> dict[str, Outcome]:
    return {
        d.action.task_id or d.action.rule: d.outcome for d in engine.tick(lambda: Snapshot(now=at))
    }


def entries(engine: Engine, task: str) -> list[LedgerEntry]:
    return [e for e in engine.ledger.store.entries if e.task_id == task]  # type: ignore[attr-defined]


def test_wait_alerts_the_outage_explains_are_folded_under_it() -> None:
    engine, ci, blocks, alerter = farm()
    ci.actions = [outage_alert(("T-1",))]
    blocks.actions = [wait_alert("T-1"), wait_alert("T-2")]
    assert tick(engine, T0) == {
        "ci_economy": Outcome.ALERTED,
        "T-1": Outcome.SUPPRESSED,
        "T-2": Outcome.ALERTED,  # not explained by the outage
    }
    assert [e.reason for e in alerter.events] == ["CI outage since 01:43", "T-2 is blocked"]
    [folded] = entries(engine, "T-1")
    assert folded.suppression is Suppression.INCIDENT and folded.params[FOLDED_UNDER] == STATE
    # Still held back an hour later, and recorded again: the report sees it over time.
    assert tick(engine, T0 + 2 * H)["T-1"] is Outcome.SUPPRESSED
    assert [e.params[FOLDED_UNDER] for e in entries(engine, "T-1")] == [STATE, STATE]
    # The outage is over (not seen for resolve_after) and T-1 is still blocked: announced.
    ci.actions = []
    assert tick(engine, T0 + 3 * H)["T-1"] is Outcome.ALERTED


def test_folding_holds_while_the_outage_state_is_open_through_a_missed_tick() -> None:
    """Rule 9 failing once (or its detection flickering) must not let 30 alerts out."""
    engine, ci, blocks, alerter = farm()
    ci.actions = [outage_alert(("T-1", "T-2"))]
    blocks.actions = [wait_alert("T-1"), wait_alert("T-2")]
    tick(engine, T0)
    ci.actions = []  # not reported this tick; its state stays open for resolve_after
    assert tick(engine, T0 + M) == {"T-1": Outcome.SUPPRESSED, "T-2": Outcome.SUPPRESSED}
    assert [e.reason for e in alerter.events] == ["CI outage since 01:43"]


def test_a_folded_alert_stays_seen_and_its_state_is_not_reopened() -> None:
    engine, ci, blocks, alerter = farm()
    blocks.actions = [wait_alert("T-1")]
    assert tick(engine, T0)["T-1"] is Outcome.ALERTED  # before the outage was seen
    ci.actions = [outage_alert(("T-1",))]
    assert tick(engine, T0 + H)["T-1"] is Outcome.SUPPRESSED
    assert not any(e.transition is Transition.RESOLVED for e in alerter.events)
    # last_seen moves on while folded (the report counts the state's span from it).
    state = engine.states.store.get_state("stale_blocks:T-1:b")
    assert state is not None and state.last_seen == T0 + H
    # The fold has a fingerprint of its own: recorded, not counted on the delivery before.
    assert [e.suppression for e in entries(engine, "T-1")] == [None, Suppression.INCIDENT]
    ci.actions = []  # the outage is over; the block is still there
    tick(engine, T0 + 2 * H)  # the outage resolves
    assert tick(engine, T0 + 2 * H + M)["T-1"] is Outcome.SUPPRESSED  # ongoing, not new
    # An escalation that came due during the outage is sent once it is over.
    assert tick(engine, T0 + 4 * H + M)["T-1"] is Outcome.ALERTED
    assert alerter.events[-1].transition is Transition.ESCALATED


def test_a_block_clearing_with_the_outage_resolves_after_the_usual_delay() -> None:
    engine, ci, blocks, _ = farm()
    blocks.actions = [wait_alert("T-1")]
    tick(engine, T0)  # announced before the outage
    ci.actions = [outage_alert(("T-1",))]
    tick(engine, T0 + H)
    tick(engine, T0 + 8 * H)  # folded all along: last seen now, not at T0
    ci.actions, blocks.actions = [], []  # CI recovered and the block cleared
    tick(engine, T0 + 8 * H + M)
    assert engine.states.store.get_state("stale_blocks:T-1:b") is not None  # not yet
    tick(engine, T0 + 8 * H + 11 * M)
    assert engine.states.store.get_state("stale_blocks:T-1:b") is None


def test_only_wait_alerts_below_critical_are_folded() -> None:
    engine, ci, blocks, _ = farm()
    ci.actions = [outage_alert(("T-1", "T-2", "T-3"))]
    blocks.actions = [
        wait_alert("T-1", about_wait=False),  # about something else
        wait_alert("T-2", severity=Severity.CRITICAL),
        replace(wait_alert("T-3"), kind=ActionKind.ESCALATE),
    ]
    outcomes = tick(engine, T0)
    assert outcomes["T-1"] is outcomes["T-2"] is outcomes["T-3"] is Outcome.ALERTED


def test_an_outage_only_logged_never_hides_alerts_that_reach_people() -> None:
    quiet_ci = replace(LIVE, modes={"ci_economy": Mode.OBSERVE})
    engine, ci, blocks, _ = farm(quiet_ci)
    ci.actions = [outage_alert(("T-1",))]
    blocks.actions = [wait_alert("T-1")]
    assert tick(engine, T0) == {"ci_economy": Outcome.OBSERVED, "T-1": Outcome.ALERTED}
    both_quiet = replace(LIVE, modes={"ci_economy": Mode.OBSERVE, "stale_blocks": Mode.OBSERVE})
    engine, ci, blocks, _ = farm(both_quiet)
    ci.actions = [outage_alert(("T-1",))]
    blocks.actions = [wait_alert("T-1")]
    assert tick(engine, T0)["T-1"] is Outcome.SUPPRESSED  # neither reaches people anyway


def test_params_that_are_not_an_outage_fold_nothing() -> None:
    other = replace(outage_alert(("T-1",)), state="ci_economy:waste")
    mismatched = replace(outage_alert(("T-1",)), state=outage_state_id(None, "other", None))
    for incident in (other, mismatched):
        engine, ci, blocks, _ = farm()
        ci.actions, blocks.actions = [incident], [wait_alert("T-1")]
        assert tick(engine, T0)["T-1"] is Outcome.ALERTED


class FailingFor(FakeAlerter):
    """Delivers every alert except the given rule's."""

    def __init__(self, rule: str) -> None:
        super().__init__()
        self.rule = rule

    def send(self, event: AlertEvent) -> None:
        if event.rule == self.rule:
            raise RuntimeError("channel down")
        super().send(event)


def test_an_outage_alert_people_did_not_get_folds_nothing() -> None:
    ci, blocks, alerter = Emit("ci_economy"), Emit("stale_blocks"), FailingFor("ci_economy")
    engine = Engine([ci, blocks], adapters=[FakeAdapter()], alerter=alerter, config=LIVE)  # type: ignore[list-item]
    ci.actions, blocks.actions = [outage_alert(("T-1",))], [wait_alert("T-1")]
    assert tick(engine, T0)["T-1"] is Outcome.ALERTED


def test_an_outage_alert_less_severe_than_the_wait_alert_does_not_fold_it() -> None:
    engine, ci, blocks, _ = farm()
    ci.actions = [replace(outage_alert(("T-1",)), severity=Severity.INFO)]
    blocks.actions = [wait_alert("T-1")]  # warning: channels may drop the info outage
    assert tick(engine, T0)["T-1"] is Outcome.ALERTED


def test_dry_run_folds_what_live_mode_would() -> None:
    dry = replace(LIVE, dry_run=True)
    engine, ci, blocks, _ = farm(dry)
    ci.actions, blocks.actions = [outage_alert(("T-1",))], [wait_alert("T-1")]
    assert tick(engine, T0)["T-1"] is Outcome.SUPPRESSED
    quiet_ci = replace(dry, modes={"ci_economy": Mode.OBSERVE})
    engine, ci, blocks, _ = farm(quiet_ci)
    ci.actions, blocks.actions = [outage_alert(("T-1",))], [wait_alert("T-1")]
    assert tick(engine, T0)["T-1"] is Outcome.DRY_RUN  # live, people would get T-1's alert


def test_folding_through_missed_ticks_ends_after_resolve_after() -> None:
    """A rule 9 that keeps failing must not hold the per-task alerts back for ever."""
    engine, ci, blocks, _ = farm()
    ci.actions, blocks.actions = [outage_alert(("T-1",))], [wait_alert("T-1")]
    tick(engine, T0)
    ci.actions = []
    assert tick(engine, T0 + 5 * M)["T-1"] is Outcome.SUPPRESSED  # a flicker: still folded
    assert tick(engine, T0 + 10 * M)["T-1"] is Outcome.ALERTED  # resolve_after passed


def test_a_failed_escalation_of_a_known_outage_keeps_folding() -> None:
    ci, blocks = Emit("ci_economy"), Emit("stale_blocks")
    alerter = FailingFor("nobody")
    engine = Engine([ci, blocks], adapters=[FakeAdapter()], alerter=alerter, config=LIVE)  # type: ignore[list-item]
    ci.actions, blocks.actions = [outage_alert(("T-1",))], [wait_alert("T-1")]
    assert tick(engine, T0)["T-1"] is Outcome.SUPPRESSED
    alerter.rule = "ci_economy"  # the 4h escalation of the outage fails to go out
    outcomes = tick(engine, T0 + 4 * H)
    assert outcomes["ci_economy"] is not Outcome.ALERTED
    assert outcomes["T-1"] is Outcome.SUPPRESSED  # people know of the outage already


def test_a_state_store_error_stops_folding_under_an_unreported_outage() -> None:
    engine, ci, blocks, _ = farm()
    ci.actions, blocks.actions = [outage_alert(("T-1",))], [wait_alert("T-1")]
    tick(engine, T0)
    ci.actions = []

    class Broken(MemoryStateStore):
        def get_state(self, state_id: str) -> None:
            raise OSError("disk")

    engine.states.store = Broken(states=dict(engine.states.store.states))  # type: ignore[attr-defined]
    assert tick(engine, T0 + M)["T-1"] is not Outcome.SUPPRESSED


def test_an_outage_from_another_rule_folds_the_same_way() -> None:
    """A block storm (rule 4) has no CI job: it folds the alerts of the tasks it explains."""
    from kelpie.core.incidents import BLOCK_STORM, Outage, incident_state_id

    state = incident_state_id("stale_blocks", "waiting for ceo")
    storm = Outage(state=state, signature="waiting for CEO", kind=BLOCK_STORM, tasks=("T-1",))
    engine, ci, blocks, _ = farm()
    ci.actions = [
        Action(
            ActionKind.ALERT, "ci_economy", "Block storm", severity=Severity.WARNING,
            key=state, state=state, since=T0, params=storm.to_params(),
        )
    ]  # fmt: skip
    blocks.actions = [wait_alert("T-1"), wait_alert("T-2")]
    outcomes = tick(engine, T0)
    assert outcomes["T-1"] is Outcome.SUPPRESSED and outcomes["T-2"] is Outcome.ALERTED
    [folded] = [e for e in entries(engine, "T-1")]
    assert folded.params[FOLDED_UNDER] == state


def test_an_observe_only_outage_is_logged_folds_nothing_and_resolves_quietly() -> None:
    """A new check (``Action.observe_only``) stays in observe whatever its rule's mode: it
    hides no alert people get, and people never hear it ended either (#252)."""
    from kelpie.core.ledger import OBSERVE_ONLY
    from kelpie.report.verify import mode_events

    engine, ci, blocks, alerter = farm()
    quiet = replace(outage_alert(("T-1",)), observe_only=True)
    ci.actions, blocks.actions = [quiet], [wait_alert("T-1")]
    assert tick(engine, T0) == {"ci_economy": Outcome.OBSERVED, "T-1": Outcome.ALERTED}
    assert [e.reason for e in alerter.events] == ["T-1 is blocked"]
    # The rule's other alerts stay in its mode; the ledger marks which are observe-only.
    ci.actions = [quiet, replace(wait_alert("T-9"), rule="ci_economy", about_wait=False)]
    tick(engine, T0 + 2 * H)
    ci.actions = []
    done = engine.tick(lambda: Snapshot(now=T0 + 3 * H))
    [notice] = [d for d in done if d.action.params.get("state") == STATE]
    assert notice.outcome is Outcome.OBSERVED
    assert not [e for e in alerter.events if e.state == STATE]  # no "resolved" either
    log = engine.ledger.store.entries  # type: ignore[attr-defined]
    assert {e.params.get(OBSERVE_ONLY) for e in log if e.rule == "ci_economy"} == {True, None}
    assert mode_events(log) == []  # an observe-only entry is no switch of the rule's mode


def test_an_outage_switched_on_while_open_is_announced_then() -> None:
    engine, ci, blocks, alerter = farm()
    ci.actions, blocks.actions = [replace(outage_alert(("T-1",)), observe_only=True)], []
    tick(engine, T0)
    ci.actions, blocks.actions = [outage_alert(("T-1",))], [wait_alert("T-1")]
    assert tick(engine, T0 + M) == {"ci_economy": Outcome.ALERTED, "T-1": Outcome.SUPPRESSED}
    assert [e.transition for e in alerter.events] == [Transition.NEW]


def test_dry_run_folds_nothing_under_an_observe_only_outage() -> None:
    engine, ci, blocks, _ = farm(replace(LIVE, dry_run=True))
    ci.actions = [replace(outage_alert(("T-1",)), observe_only=True)]
    blocks.actions = [wait_alert("T-1")]
    assert tick(engine, T0)["T-1"] is Outcome.DRY_RUN  # live, people would get T-1's alert
