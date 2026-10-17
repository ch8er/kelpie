"""The engine hands alerters the fields of an alert; nothing is read back out of text."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.alerts import slack, telegram
from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import AlertEvent, Did, Transition, lead_in
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.model import Snapshot
from tests.fakes import Emit, FakeAdapter, FakeAlerter

T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
H = timedelta(hours=1)
LIVE = EngineConfig(dry_run=False, default_mode=Mode.ACT)


class Toggle:
    def __init__(self, name: str, action: Action) -> None:
        self.name, self.action, self.on = name, action, True

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        return [self.action] if self.on else []


def state_alert(rule: str, reason: str, task_id: str = "T-1", key: str = "k") -> Action:
    return Action(
        ActionKind.ALERT, rule, reason, task_id=task_id, key=key,
        severity=Severity.WARNING, state="s1", since=T0,
    )  # fmt: skip


def run_lifecycle(rule: str, reason: str, task_id: str = "T-1") -> list[AlertEvent]:
    """Open, escalate, change and resolve one state; return the events in order."""
    toggle = Toggle(rule, state_alert(rule, reason, task_id))
    alerter = FakeAlerter()
    engine = Engine([toggle], adapters=[FakeAdapter()], alerter=alerter, config=LIVE)  # type: ignore[list-item]
    engine.tick(lambda: Snapshot(now=T0))
    engine.tick(lambda: Snapshot(now=T0 + H))  # nothing new: no event
    engine.tick(lambda: Snapshot(now=T0 + 5 * H))  # first escalation threshold
    toggle.action = state_alert(rule, reason + " Now worse.", task_id, key="other-description")
    engine.tick(lambda: Snapshot(now=T0 + 6 * H))
    toggle.on = False
    engine.tick(lambda: Snapshot(now=T0 + 7 * H))  # the rule stopped reporting it: resolved
    engine.tick(lambda: Snapshot(now=T0 + 8 * H))
    return alerter.events


def test_a_state_alert_carries_each_transition_as_a_field() -> None:
    opened, escalated, changed, resolved = run_lifecycle("orphans", "T-1 has no owner.")
    assert opened == AlertEvent("orphans", "alert T-1", "T-1 has no owner.", Severity.WARNING)
    assert escalated == replace(
        opened,
        transition=Transition.ESCALATED,
        age=5 * H,  # counted from `since`
    )
    assert changed == replace(
        opened,
        reason="T-1 has no owner. Now worse.",
        transition=Transition.CHANGED,
    )
    # The rule's reason is never prefixed; the wording of a transition is the channel's.
    assert (resolved.transition, resolved.reason, resolved.age) == (
        Transition.RESOLVED,
        'task T-1: no longer reported: "T-1 has no owner".',  # quoted as the past
        7 * H,
    )
    assert resolved.severity is Severity.INFO and resolved.did is Did.NOTIFIED


def test_the_ledger_still_records_the_transition_in_front_of_the_reason() -> None:
    toggle = Toggle("orphans", state_alert("orphans", "T-1 has no owner."))
    engine = Engine([toggle], adapters=[FakeAdapter()], alerter=FakeAlerter(), config=LIVE)  # type: ignore[list-item]
    engine.tick(lambda: Snapshot(now=T0))
    (escalated,) = engine.tick(lambda: Snapshot(now=T0 + 5 * H))
    assert escalated.action.reason == "Still open after 5.0h: T-1 has no owner."


@pytest.mark.parametrize("reason", ["Changed: it was reassigned.", "Resolved after 2.0h: gone."])
def test_a_reason_that_looks_like_transition_wording_stays_a_reason(reason: str) -> None:
    opened, escalated, changed, resolved = run_lifecycle("orphans", reason)
    assert (opened.transition, opened.reason) == (Transition.NEW, reason)
    assert (escalated.transition, escalated.reason) == (Transition.ESCALATED, reason)
    assert (changed.transition, changed.reason) == (Transition.CHANGED, reason + " Now worse.")
    assert resolved.transition is Transition.RESOLVED
    for fmt in (telegram.format_message, slack.format_message):
        assert fmt(opened).split("\n")[1] == reason  # no label added for a new alert
        assert fmt(escalated).split("\n")[1] == f"Still open after 5.0h: {reason}"


def test_a_rule_or_subject_with_brackets_keeps_its_fields() -> None:
    events = run_lifecycle("odd] rule [x", "Stuck.", task_id="T-]1")
    assert {(e.rule, e.subject) for e in events} == {("odd] rule [x", "alert T-]1")}
    assert [e.transition for e in events] == [
        Transition.NEW,
        Transition.ESCALATED,
        Transition.CHANGED,
        Transition.RESOLVED,
    ]
    head = telegram.format_message(events[0]).split("\n")[0]
    assert head == "[WARNING] odd] rule [x: alert T-]1"


def test_effects_carry_what_kelpie_did_as_fields() -> None:
    requeue = Action(ActionKind.REQUEUE, "odd] rule", "Put it back.", task_id="T-]1")
    stop = Action(ActionKind.STOP, "odd] rule", "Stop it.", params={"session_id": "nope"})
    suggest = EngineConfig(dry_run=False, default_mode=Mode.SUGGEST)
    alerter = FakeAlerter()
    Engine([Emit([requeue])], adapters=[FakeAdapter()], alerter=alerter, config=suggest).tick(
        lambda: Snapshot(now=T0)
    )
    (suggested,) = alerter.events
    assert (suggested.rule, suggested.subject, suggested.did) == (
        "odd] rule",
        "requeue T-]1",
        Did.SUGGESTED,
    )
    assert suggested.did_text is not None and suggested.transition is Transition.NEW

    alerter = FakeAlerter()  # no executor can stop a session the board does not know
    Engine([Emit([stop])], adapters=[FakeAdapter()], alerter=alerter, config=LIVE).tick(
        lambda: Snapshot(now=T0)
    )
    (held,) = alerter.events
    assert (held.subject, held.did, held.severity) == ("stop", Did.NOT_EXECUTED, Severity.WARNING)

    alerter = FakeAlerter()
    adapter = FakeAdapter(fail_on="requeue")
    Engine([Emit([requeue])], adapters=[adapter], alerter=alerter, config=LIVE).tick(
        lambda: Snapshot(now=T0)
    )
    (failed,) = alerter.events
    assert (failed.subject, failed.did) == ("requeue T-]1", Did.FAILED)


def test_the_transition_is_worded_once_in_lead_in() -> None:
    assert lead_in(Transition.NEW) == ""
    assert lead_in(Transition.CHANGED) == "Changed: "
    assert lead_in(Transition.ESCALATED, 4 * H) == "Still open after 4.0h: "
    assert lead_in(Transition.RESOLVED, timedelta(minutes=5)) == "Resolved after 5m: "
    assert lead_in(Transition.ESCALATED) == "Still open. "  # no age known
    assert lead_in(Transition.RESOLVED) == "Resolved. "


class FailsOnEscalation(FakeAlerter):
    """Raises on the first escalation it is given, then records like the fake."""

    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def send(self, event: AlertEvent) -> None:
        if event.transition is Transition.ESCALATED and not self.failed:
            self.failed = True
            raise OSError("channel down")
        super().send(event)


def test_the_failure_notice_of_a_state_alert_keeps_its_transition() -> None:
    toggle = Toggle("orphans", state_alert("orphans", "T-1 has no owner."))
    alerter = FailsOnEscalation()
    engine = Engine([toggle], adapters=[FakeAdapter()], alerter=alerter, config=LIVE)  # type: ignore[list-item]
    engine.tick(lambda: Snapshot(now=T0))
    engine.tick(lambda: Snapshot(now=T0 + 5 * H))
    opened, failed = alerter.events
    assert opened.transition is Transition.NEW
    assert (failed.transition, failed.age, failed.did) == (
        Transition.ESCALATED,
        5 * H,
        Did.FAILED,
    )
    assert failed.reason == "T-1 has no owner."
