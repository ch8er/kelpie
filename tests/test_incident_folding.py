"""The engine folds per-task wait alerts under an open CI outage (incident 20)."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import Transition
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.incidents import FOLDED_UNDER, CiOutage, outage_signature, outage_state_id
from kelpie.core.ledger import Outcome, Suppression
from kelpie.core.model import Snapshot
from tests.fakes import FakeAdapter, FakeAlerter

T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
H = timedelta(hours=1)
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
    [folded] = [e for e in engine.ledger.store.entries if e.task_id == "T-1"]  # type: ignore[attr-defined]
    assert folded.suppression is Suppression.INCIDENT and folded.params[FOLDED_UNDER] == STATE
    # Still held back an hour later, and recorded again: the report sees it over time.
    assert tick(engine, T0 + 2 * H)["T-1"] is Outcome.SUPPRESSED
    entries = [e for e in engine.ledger.store.entries if e.task_id == "T-1"]  # type: ignore[attr-defined]
    assert len(entries) == 2 and all(e.params[FOLDED_UNDER] == STATE for e in entries)
    ci.actions = []  # the outage is over and T-1 is still blocked: now it is announced
    assert tick(engine, T0 + 3 * H)["T-1"] is Outcome.ALERTED


def test_a_folded_alert_does_not_resolve_and_comes_back_after_the_outage() -> None:
    engine, ci, blocks, alerter = farm()
    blocks.actions = [wait_alert("T-1")]
    assert tick(engine, T0)["T-1"] is Outcome.ALERTED  # before the outage was seen
    ci.actions = [outage_alert(("T-1",))]
    assert tick(engine, T0 + H)["T-1"] is Outcome.SUPPRESSED
    assert not any(e.transition is Transition.RESOLVED for e in alerter.events)
    # The folded alert has a fingerprint of its own: its fold is recorded, not counted on
    # the delivery an hour ago.
    folded = [e for e in engine.ledger.store.entries if e.suppression is Suppression.INCIDENT]  # type: ignore[attr-defined]
    assert len(folded) == 1
    ci.actions = []  # the outage is over; the block is still there
    after = tick(engine, T0 + 2 * H)
    assert after["T-1"] is Outcome.SUPPRESSED  # its state stayed open: ongoing, not new


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
