"""What an alert says Kelpie did comes from the outcome and the rule's mode, not from the
rule's own text (issues #69, #70)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from kelpie.alerts import slack, telegram
from kelpie.core.actions import Action, ActionKind, ActionResult, Severity
from kelpie.core.alerts import (
    ANNOUNCES,
    AlertEvent,
    Did,
    done_line,
    failure_reason,
    intent,
)
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.model import Session, Snapshot
from tests.fakes import Emit, FakeAdapter, StoppingAdapter

NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
ACT = EngineConfig(dry_run=False, default_mode=Mode.ACT)
SUGGEST = EngineConfig(dry_run=False, default_mode=Mode.SUGGEST)
SESSION = Session("S-1", "T-1", "agent-1", NOW)


class Recorder:
    def __init__(self) -> None:
        self.messages: list[AlertEvent] = []

    def send(self, event: AlertEvent) -> None:
        self.messages.append(event)


class Detailed(StoppingAdapter):
    """A platform that says what a stop means on it."""

    def stop(self, session_id: str, reason: str) -> str:  # type: ignore[override]
        super().stop(session_id, reason)
        return "the worker exits within ~2 min"


class Refusing(StoppingAdapter):
    def stop(self, session_id: str, reason: str) -> None:
        raise RuntimeError("cli said: token sk-secret-value")


class PublicError(RuntimeError):
    public_reason = "Hermes refused: the run is no longer current"


class Refusing2(StoppingAdapter):
    def stop(self, session_id: str, reason: str) -> None:
        raise PublicError("raw stderr with sk-secret-value")


def stop(key: str = "stop:S-1", session: str = "S-1") -> Action:
    return Action(
        ActionKind.STOP,
        "loop",
        f"Runaway run: session {session} made 20 identical calls.",
        task_id="T-1",
        key=key,
        params={"session_id": session},
    )


def runaway(announces: object = "stop:S-1") -> Action:
    return Action(
        ActionKind.ALERT,
        "loop",
        "Runaway run: session S-1 made 20 identical calls. Check the task before resuming it.",
        task_id="T-1",
        key="loop:S-1",
        severity=Severity.CRITICAL,
        state="loop:S-1",
        since=NOW,
        params={ANNOUNCES: announces},
    )


def tick(actions: list[Action], adapter: FakeAdapter, config: EngineConfig) -> Recorder:
    recorder = Recorder()
    engine = Engine([Emit(actions)], adapters=[adapter], alerter=recorder, config=config)
    engine.tick(lambda: Snapshot(now=NOW, sessions=(SESSION,)))
    return recorder


# --- lines ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "params", "wanted", "past"),
    [
        (ActionKind.UNBLOCK, {}, "clear the block on T-1", "Cleared the block on T-1."),
        (ActionKind.REQUEUE, {}, "return T-1 to the queue", "Returned T-1 to the queue."),
        (ActionKind.WAKE, {}, "wake T-1", "Woke T-1."),
        (ActionKind.WAIT, {}, "park T-1 until its conditions hold", "Parked T-1 until its "
         "conditions hold."),
        (ActionKind.REASSIGN, {"owner_id": "dev-2"}, "assign T-1 to dev-2", "Assigned T-1 to "
         "dev-2."),
        (ActionKind.STOP, {"session_id": "S-1"}, "stop run S-1", "Stopped run S-1."),
        (ActionKind.CANCEL_BUILD, {"build_id": "r:1", "superseded_by": "r:2"},
         "cancel build r:1", "Cancelled build r:1 (superseded by r:2)."),
        (ActionKind.COMMENT, {}, "comment on T-1", "Commented on T-1."),
        (ActionKind.WIP_COMMIT, {}, "make a WIP commit on T-1", "Made a WIP commit on T-1."),
    ],
)  # fmt: skip
def test_intent_and_past_tense_come_from_kind_and_params(
    kind: ActionKind, params: dict[str, str], wanted: str, past: str
) -> None:
    action = Action(kind, "r", "why", task_id="T-1", params=params)
    assert intent(action) == wanted
    assert done_line(action) == past


def test_done_line_adds_the_executors_specifics_without_control_characters() -> None:
    line = done_line(stop(), "the worker exits\nwithin ~2 min")
    assert line == "Stopped run S-1; the worker exits within ~2 min."


def test_failure_reason_never_shows_raw_exception_text() -> None:
    assert failure_reason(RuntimeError("token sk-secret-value")) == (
        "RuntimeError (details in the Kelpie log)"
    )
    assert failure_reason(PublicError("x")) == "Hermes refused: the run is no longer current"
    long = PublicError()
    long.public_reason = "y" * 500  # type: ignore[misc]
    assert len(failure_reason(long)) == 200


# --- engine: the line follows the outcome --------------------------------------------------


def test_suggest_says_suggested_and_not_done() -> None:
    unblock = Action(ActionKind.UNBLOCK, "stale", "The block is stale.", task_id="T-1")
    (m,) = tick([unblock], FakeAdapter(), SUGGEST).messages
    assert (m.did, m.reason) == (Did.SUGGESTED, "The block is stale.")
    assert m.did_text == "Suggested: clear the block on T-1 (not done, rule in suggest mode)."


def test_act_success_is_announced_in_the_past_tense_with_specifics() -> None:
    adapter = Detailed()
    (m,) = tick([stop()], adapter, ACT).messages
    assert adapter.calls == [("stop", "S-1")]
    assert (m.did, m.subject) == (Did.DONE, "stop T-1")
    assert m.did_text == "Stopped run S-1; the worker exits within ~2 min."


def test_act_failure_says_failed_and_hides_the_raw_error() -> None:
    (m,) = tick([stop()], Refusing(), ACT).messages
    assert m.did is Did.FAILED and m.severity is Severity.WARNING
    assert m.did_text == (
        "Failed: could not stop run S-1: RuntimeError (details in the Kelpie log), needs a human."
    )
    assert "sk-secret" not in m.did_text
    (m,) = tick([stop()], Refusing2(), ACT).messages
    assert m.did_text == (
        "Failed: could not stop run S-1: Hermes refused: the run is no longer current, "
        "needs a human."
    )


def test_a_comment_carried_out_is_not_announced_again() -> None:
    comment = Action(ActionKind.COMMENT, "coach", "Please commit.", task_id="T-1")
    adapter = FakeAdapter()
    assert tick([comment], adapter, ACT).messages == []
    assert adapter.calls == [("comment", "T-1")]


# --- companion alerts carry their effect's line --------------------------------------------


@pytest.mark.parametrize(
    ("config", "adapter", "line"),
    [
        (ACT, Detailed(), "Stopped run S-1; the worker exits within ~2 min."),
        (SUGGEST, StoppingAdapter(), "Suggested: stop run S-1 (not done, rule in suggest mode)."),
        (ACT, Refusing(), "Failed: could not stop run S-1: RuntimeError (details in the Kelpie "
         "log), needs a human."),
    ],
)  # fmt: skip
def test_the_alert_announcing_an_effect_says_what_happened_to_it(
    config: EngineConfig, adapter: FakeAdapter, line: str
) -> None:
    # The rule yields the alert first; the engine still settles the effect first.
    (m,) = tick([runaway(), stop()], adapter, config).messages
    assert (m.severity, m.subject) == (Severity.CRITICAL, "alert T-1")
    assert m.did_text == line
    assert "Stop proposed" not in m.reason


def test_an_effect_whose_alert_stays_quiet_is_announced_on_its_own() -> None:
    adapter, recorder = Detailed(), Recorder()
    first = Emit([runaway(), stop()])
    engine = Engine([first], adapters=[adapter], alerter=recorder, config=ACT)
    engine.tick(lambda: Snapshot(now=NOW, sessions=(SESSION,)))
    # A new run of the same loop: the state alert is ongoing (no message), the stop is new
    # and goes out on its own, as urgent as the alert that would have carried it.
    first.actions = [runaway("stop:S-2"), stop("stop:S-2", "S-2")]
    engine.tick(lambda: Snapshot(now=NOW, sessions=(SESSION, Session("S-2", "T-1", "a", NOW))))
    assert [(m.subject, m.did_text, m.severity) for m in recorder.messages] == [
        ("alert T-1", "Stopped run S-1; the worker exits within ~2 min.", Severity.CRITICAL),
        ("stop T-1", "Stopped run S-2; the worker exits within ~2 min.", Severity.CRITICAL),
    ]


def test_an_alert_announcing_several_effects_lists_them_all() -> None:
    other = Action(
        ActionKind.STOP,
        "loop",
        "Session S-2 is over budget.",
        task_id="T-1",
        key="stop:S-2",
        params={"session_id": "S-2"},
    )
    messages = tick([runaway(["stop:S-1", "stop:S-2"]), stop(), other], Detailed(), ACT)
    (m,) = messages.messages
    assert m.did_text == (
        "Stopped run S-1; the worker exits within ~2 min. "
        "Stopped run S-2; the worker exits within ~2 min."
    )


def test_observe_and_dry_run_send_nothing() -> None:
    for config in (EngineConfig(dry_run=False), EngineConfig(default_mode=Mode.ACT)):
        assert tick([runaway(), stop()], Detailed(), config).messages == []


# --- channels print the engine's line ------------------------------------------------------


@pytest.mark.parametrize("fmt", [telegram.format_message, slack.format_message])
def test_channels_print_the_engines_line_last(fmt: object) -> None:
    message = AlertEvent(
        "loop",
        "alert T-1",
        "Runaway run.",
        Severity.CRITICAL,
        did_text="Stopped run S-1; exits soon.",
    )
    text = fmt(message)  # type: ignore[operator]
    assert text.splitlines()[-1] == "Stopped run S-1; exits soon."
    assert "Kelpie did:" not in text
    plain = fmt(AlertEvent("loop", "alert T-1", "Runaway run.", Severity.CRITICAL))  # type: ignore[operator]
    assert plain.splitlines()[-1] == telegram.DID[Did.NOTIFIED]


def test_the_did_line_is_part_of_what_makes_two_alerts_the_same() -> None:
    a = AlertEvent("r", "alert T-1", "b", Severity.INFO, did_text="Stopped run S-1.")
    b = AlertEvent("r", "alert T-1", "b", Severity.INFO, did_text="Suggested: stop run S-1.")
    assert a.fingerprint != b.fingerprint


def test_an_announcing_alert_held_back_by_the_ledger_releases_the_effects_line() -> None:
    """Not a lasting state: the ledger's cooldown silences the alert on the second tick."""
    plain = replace(runaway(), state=None, since=None)
    adapter, recorder = Detailed(), Recorder()
    rule = Emit([plain, stop()])
    engine = Engine([rule], adapters=[adapter], alerter=recorder, config=ACT)
    engine.tick(lambda: Snapshot(now=NOW, sessions=(SESSION,)))
    rule.actions = [replace(plain, params={ANNOUNCES: "stop:S-1:again"}), stop("stop:S-1:again")]
    engine.tick(lambda: Snapshot(now=NOW, sessions=(SESSION,)))
    assert [(m.subject, m.severity) for m in recorder.messages] == [
        ("alert T-1", Severity.CRITICAL),
        ("stop T-1", Severity.CRITICAL),  # released, at the alert's severity
    ]


class FailingAlerts(Recorder):
    """Delivers everything but plain alerts."""

    def send(self, event: AlertEvent) -> None:
        if event.did is Did.NOTIFIED and event.subject.startswith("alert"):
            raise ConnectionError("chat down")
        super().send(event)


def test_an_announcing_alert_that_cannot_be_sent_still_lets_the_effect_be_told() -> None:
    recorder = FailingAlerts()
    engine = Engine(
        [Emit([runaway(), stop()])], adapters=[Detailed()], alerter=recorder, config=ACT
    )
    engine.tick(lambda: Snapshot(now=NOW, sessions=(SESSION,)))
    failed, done = recorder.messages
    assert (failed.subject, failed.did) == ("alert T-1", Did.FAILED)  # once, needs a human
    assert failed.did_text == (
        "Failed: could not send this alert: ConnectionError (details in the Kelpie log), "
        "needs a human."
    )
    assert (done.subject, done.did) == ("stop T-1", Did.DONE)  # the stop is still told
    assert done.did_text == "Stopped run S-1; the worker exits within ~2 min."


class Ended(StoppingAdapter):
    def stop(self, session_id: str, reason: str) -> ActionResult:  # type: ignore[override]
        super().stop(session_id, reason)
        return ActionResult.NOTHING_TO_DO


def test_a_session_that_was_already_over_is_not_announced_as_stopped() -> None:
    recorder = Recorder()
    engine = Engine([Emit([runaway(), stop()])], adapters=[Ended()], alerter=recorder, config=ACT)
    (result, _) = engine.tick(lambda: Snapshot(now=NOW, sessions=(SESSION,)))
    assert result.outcome is Outcome.NOTHING_TO_DO
    (m,) = recorder.messages
    assert m.did_text is None  # the alert only: nothing was stopped
    assert "Stopped" not in telegram.format_message(m)


def test_a_carried_failure_raises_an_info_alert_to_warning() -> None:
    quiet = replace(runaway(), severity=Severity.INFO)
    (m,) = tick([quiet, stop()], Refusing(), ACT).messages
    assert m.severity is Severity.WARNING and m.did_text.startswith("Failed:")


def test_a_public_reason_ending_in_a_period_reads_cleanly() -> None:
    class Dotted(StoppingAdapter):
        def stop(self, session_id: str, reason: str) -> None:
            exc = RuntimeError("raw")
            exc.public_reason = "the run is no longer current."  # type: ignore[attr-defined]
            raise exc

    (m,) = tick([stop()], Dotted(), ACT).messages
    assert m.did_text == (
        "Failed: could not stop run S-1: the run is no longer current, needs a human."
    )


def test_the_did_line_survives_a_long_body_whole() -> None:
    body = "x" * 5000
    did = "Stopped run S-1; " + "y" * 1200 + "."
    for fmt in (telegram.format_message, slack.format_message):
        text = fmt(AlertEvent("loop", "alert T-1", body, Severity.CRITICAL, did_text=did))
        assert text.splitlines()[-1] == did and len(text) <= telegram.MAX_TEXT


@pytest.mark.parametrize(
    ("result", "outcome", "line"),
    [
        (ActionResult.DONE, Outcome.EXECUTED, "Stopped run S-1."),
        (None, Outcome.EXECUTED, "Stopped run S-1."),
        (ActionResult.REFUSED, Outcome.NOT_EXECUTED, "Failed: nothing here can stop run S-1, "
         "needs a human."),
    ],
)  # fmt: skip
def test_a_stop_result_is_never_read_as_its_own_detail(
    result: ActionResult | None, outcome: Outcome, line: str
) -> None:
    class Answering(StoppingAdapter):
        def stop(self, session_id: str, reason: str) -> ActionResult | None:  # type: ignore[override]
            return result

    recorder = Recorder()
    engine = Engine([Emit([stop()])], adapters=[Answering()], alerter=recorder, config=ACT)
    (dispatched,) = engine.tick(lambda: Snapshot(now=NOW, sessions=(SESSION,)))
    assert dispatched.outcome is outcome
    (m,) = recorder.messages
    assert m.did_text == line
