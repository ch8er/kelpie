"""The channels read the engine's real alert output (not a copy of its wording)."""

from datetime import UTC, datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import AlertMessage, Did, Transition, parse_alert
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.model import Snapshot
from tests.fakes import Emit, FakeAdapter

T0 = datetime(2026, 3, 2, 10, tzinfo=UTC)
LIVE = EngineConfig(dry_run=False, default_mode=Mode.ACT)


class Recorder:
    def __init__(self) -> None:
        self.messages: list[AlertMessage] = []

    def send(self, title: str, body: str, severity: str, did: str | None = None) -> None:
        self.messages.append(parse_alert(title, body, severity, did))


class Toggle:
    name = "emit"

    def __init__(self, action: Action) -> None:
        self.action, self.on = action, True

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        return [self.action] if self.on else []


def state_alert(key: str = "k") -> Action:
    return Action(
        ActionKind.ALERT, "emit", "T-1 is stuck.", task_id="T-1", key=key,
        severity=Severity.WARNING, state="s1", since=T0,
    )  # fmt: skip


def test_state_transitions_are_read_back_from_the_engines_wording() -> None:
    rule, recorder = Toggle(state_alert()), Recorder()
    engine = Engine([rule], adapters=[FakeAdapter()], alerter=recorder, config=LIVE)  # type: ignore[list-item]
    engine.tick(lambda: Snapshot(now=T0))
    engine.tick(lambda: Snapshot(now=T0 + timedelta(hours=5)))  # first escalation threshold
    rule.action = state_alert(key="other-description")
    engine.tick(lambda: Snapshot(now=T0 + timedelta(hours=6)))
    rule.on = False
    engine.tick(lambda: Snapshot(now=T0 + timedelta(hours=7)))
    assert [m.transition for m in recorder.messages] == [
        Transition.NEW,
        Transition.ESCALATED,
        Transition.CHANGED,
        Transition.RESOLVED,
    ]
    assert {(m.rule, m.subject, m.did) for m in recorder.messages} == {
        ("emit", "alert T-1", Did.NOTIFIED)
    }


def test_suggested_and_unexecutable_actions_are_read_back() -> None:
    requeue = Action(ActionKind.REQUEUE, "emit", "Put it back.", task_id="T-1")
    recorder = Recorder()
    suggest = EngineConfig(dry_run=False, default_mode=Mode.SUGGEST)
    Engine([Emit([requeue])], adapters=[FakeAdapter()], alerter=recorder, config=suggest).tick(
        lambda: Snapshot(now=T0)
    )
    (message,) = recorder.messages
    assert (message.did, message.subject) == (Did.SUGGESTED, "requeue T-1")

    stop = Action(ActionKind.STOP, "emit", "Stop it.", params={"session_id": "nope"})
    recorder = Recorder()  # no executor can stop a session the board does not know
    Engine([Emit([stop])], adapters=[FakeAdapter()], alerter=recorder, config=LIVE).tick(
        lambda: Snapshot(now=T0)
    )
    assert [m.did for m in recorder.messages] == [Did.NOT_EXECUTED]
