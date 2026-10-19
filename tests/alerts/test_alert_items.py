"""The engine fills an alert's headline and items from the action and the tick's snapshot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kelpie.alerts import render
from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import AlertEvent, AlertItem
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine, alert_items
from kelpie.core.model import Snapshot, Status, Task
from kelpie.rules import REGISTRY
from tests.fakes import Emit, FakeAdapter, FakeAlerter
from tests.incidents import load

FIXTURES = Path(__file__).parents[1] / "fixtures" / "incidents"
NOW = datetime(2026, 3, 2, 10, tzinfo=UTC)
H = timedelta(hours=1)
LIVE = EngineConfig(dry_run=False, default_mode=Mode.ACT)
BOARD = (
    Task("t_1", "  Nightly backup  ", Status.BLOCKED, NOW - 44 * H),
    Task("t_2", "Store listing", Status.BLOCKED, NOW - 2 * H),
    Task("t_3", "", Status.BLOCKED, NOW - H),
)
SNAPSHOT = Snapshot(now=NOW, tasks=BOARD)


def alert(**kw: object) -> Action:
    base: dict[str, object] = {"kind": ActionKind.ALERT, "rule": "r", "reason": "Why."}
    return Action(**{**base, **kw})  # type: ignore[arg-type]


def item(task_id: str, title: str | None, hours: float, **kw: object) -> AlertItem:
    """An item whose situation began ``hours`` before NOW."""
    return AlertItem(task_id, title, hours * H, since=NOW - hours * H, **kw)  # type: ignore[arg-type]


def test_one_item_from_the_task_and_the_snapshot() -> None:
    (one,) = alert_items(alert(task_id="t_1"), SNAPSHOT)
    assert one == item("t_1", "Nightly backup", 44)  # since the task's status_since
    since = alert(task_id="t_1", since=NOW - 3 * H, params={"needed": " Approve it. "})
    assert alert_items(since, SNAPSHOT) == (item("t_1", "Nightly backup", 3, needed="Approve it."),)
    # not on the board: no title and no age; an empty title is no title
    assert alert_items(alert(task_id="t_9"), SNAPSHOT) == (AlertItem("t_9"),)
    assert alert_items(alert(task_id="t_3"), SNAPSHOT)[0].title is None
    assert alert_items(alert(), SNAPSHOT) == ()


def test_blocking_comes_only_from_params_blocking() -> None:
    def blocking(params: dict[str, object]) -> int | None:
        return alert_items(alert(task_id="t_1", params=params), SNAPSHOT)[0].blocking

    assert blocking({"blocking": 4}) == 4
    assert blocking({"blocking": 0}) == 0
    assert blocking({}) is None
    # a rule's own params (stuck_ids) are not core's to read
    assert blocking({"stuck_ids": ["a", "b"]}) is None
    assert blocking({"blocking": True}) is None and blocking({"blocking": -1}) is None


def test_items_from_params_in_the_rules_order() -> None:
    params = {
        "items": [
            {"task_id": "t_2", "needed": "Approve the listing.", "since": NOW - 46 * H},
            {"task_id": "t_1", "since": (NOW - 44 * H).isoformat(), "blocking": 2},
            {"task_id": "t_9"},
        ]
    }
    assert alert_items(alert(task_id="t_1", params=params), SNAPSHOT) == (
        item("t_2", "Store listing", 46, needed="Approve the listing."),
        item("t_1", "Nightly backup", 44, blocking=2),
        AlertItem("t_9"),
    )


def test_malformed_items_are_skipped_and_never_raise() -> None:
    bad: list[object] = [
        "t_1",
        None,
        {},
        {"task_id": ""},
        {"task_id": 5},
        {"task_id": "t_1", "needed": 3},
        {"task_id": "t_1", "since": "yesterday"},
        {"task_id": "t_1", "since": "2026-03-01T10:00:00"},  # no time zone
        {"task_id": "t_1", "since": 12},
        {"task_id": "t_1", "blocking": "two"},
        {"task_id": "t_1", "blocking": False},
        {"task_id": "t_2", "since": NOW + H},  # in the future: age 0
    ]
    assert alert_items(alert(params={"items": bad}), SNAPSHOT) == (
        AlertItem("t_2", "Store listing", timedelta(0), since=NOW + H),
    )
    # nothing usable: the action's own task stands in
    fallback = alert(task_id="t_1", params={"items": bad[:3]})
    assert alert_items(fallback, SNAPSHOT) == (item("t_1", "Nightly backup", 44),)
    assert alert_items(alert(params={"items": "t_1"}), SNAPSHOT) == ()


def test_the_engine_sends_the_headline_and_items() -> None:
    alerter = FakeAlerter()
    action = alert(
        task_id="t_1",
        severity=Severity.WARNING,
        params={"headline": " Backups stopped ", "blocking": 1, "needed": "Enable R2."},
    )
    Engine([Emit([action])], adapters=[FakeAdapter()], alerter=alerter, config=LIVE).tick(
        lambda: SNAPSHOT
    )
    (event,) = alerter.events
    assert event.headline == "Backups stopped"
    assert event.items == (item("t_1", "Nightly backup", 44, needed="Enable R2.", blocking=1),)
    assert event.reason == "Why."  # the reason is untouched


def test_an_effect_message_carries_its_task_too() -> None:
    alerter = FakeAlerter()
    requeue = Action(ActionKind.REQUEUE, "r", "Put it back.", task_id="t_2")
    suggest = EngineConfig(dry_run=False, default_mode=Mode.SUGGEST)
    Engine([Emit([requeue])], adapters=[FakeAdapter()], alerter=alerter, config=suggest).tick(
        lambda: SNAPSHOT
    )
    (event,) = alerter.events
    assert event.items == (item("t_2", "Store listing", 2),)
    assert event.headline is None


def test_the_fingerprint_counts_an_items_start_to_the_minute() -> None:
    def at(now: datetime, **kw: object) -> AlertEvent:
        action = alert(task_id="t_1", **kw)  # type: ignore[arg-type]
        items = alert_items(action, Snapshot(now=now, tasks=BOARD))
        return AlertEvent("r", "alert t_1", "Why.", Severity.WARNING, items=items)

    first = at(NOW)
    # the same alert sent again later is the same one, whatever its age reads by then
    assert at(NOW + timedelta(minutes=1)).fingerprint == first.fingerprint
    assert at(NOW + timedelta(hours=2)).fingerprint == first.fingerprint
    # a start in the same minute is the same start; another minute is another one
    seconds = NOW - 44 * H + timedelta(seconds=30)
    assert at(NOW, since=seconds).fingerprint == first.fingerprint
    assert at(NOW, since=NOW - 43 * H).fingerprint != first.fingerprint
    assert at(NOW, params={"needed": "x"}).fingerprint != first.fingerprint
    assert at(NOW, params={"blocking": 1}).fingerprint != first.fingerprint
    headed = AlertEvent("r", "alert t_1", "Why.", Severity.WARNING, headline="H", items=first.items)
    assert headed.fingerprint != first.fingerprint
    assert all(not isinstance(part, timedelta) for part in first.fingerprint[-1][0])
    # without a start, the age counts as it is worded
    aged = AlertItem("t_9", age=4 * H)
    assert aged.fingerprint == AlertItem("t_9", age=4 * H + timedelta(minutes=2)).fingerprint


def test_rule_params_beyond_the_item_fields_are_not_read() -> None:
    """Core reads only ``task_id``, ``title``, ``needed``, ``since`` and ``blocking``: a
    rule's own keys (rule 13's ``task``, ``behind``, ``waiting_minutes``) mean nothing."""
    params = {
        "items": [
            {"task": "t_2", "behind": ["a", "b", "c"], "waiting_minutes": 90},
            {"task_id": "t_1", "kind": "human_task", "behind": ["a", "b"], "cost_hours": 2.0},
        ]
    }
    assert alert_items(alert(params=params), SNAPSHOT) == (AlertItem("t_1", "Nightly backup"),)


def test_how_rule_13s_digest_renders() -> None:
    """What the rule sends (incident 13): exact ids, ages and counts; a human task that is
    just to do needs nothing beyond its title."""
    incident = load(FIXTURES / "13-human-tasks-block-agents.yaml")
    rule = REGISTRY[incident.rule](incident.config)
    (action,) = [a for a in rule.evaluate(incident.snapshot) if a.kind is ActionKind.ESCALATE]
    items = alert_items(action, incident.snapshot)
    assert [(i.task_id, i.title, i.blocking, i.needed) for i in items] == [
        ("T-1302", "Decide on storage backend", 3, None),
        ("T-1301", "Add deploy token to repo secrets", 2, None),
    ]
    event = AlertEvent(action.rule, "escalate", action.reason, action.severity, items=items)
    lines = render.plain(event).splitlines()
    assert lines[:6] == [
        "\U0001f7e1 Waiting for you: 2 tasks (oldest 4.5 h)",
        "",
        "1. Decide on storage backend — 3.7 h · T-1302",
        "   Blocking: 3 tasks",
        "2. Add deploy token to repo secrets — 4.5 h · T-1301",
        "   Blocking: 2 tasks",
    ]
    assert lines[6].startswith("Why: Needs you (human-owner): 2 item(s) hold 5 agent task(s)")
    assert lines[-1] == "Rule: needs_you"


def test_needed_that_only_repeats_the_title_is_left_out() -> None:
    same = AlertItem("t_1", "Approve the  Store listing", needed="approve the store listing")
    other = AlertItem("t_2", "Approve the store listing", needed="Which price tier?")
    event = AlertEvent("r", "alert", "Why.", Severity.WARNING, items=(same, other))
    text = render.plain(event)
    assert "Needed: approve" not in text and "Needed: Which price tier?" in text


def test_an_approval_in_rule_13s_digest_says_what_it_asks() -> None:
    """An approval's question is what is needed; nothing waits behind these ones."""
    incident = load(FIXTURES / "13-approval-wait.yaml")
    rule = REGISTRY[incident.rule](incident.config)
    actions = [a for a in rule.evaluate(incident.snapshot) if a.kind is ActionKind.ESCALATE]
    assert actions
    for action in actions:
        items = alert_items(action, incident.snapshot)
        assert items and all(i.blocking == 0 and i.since is not None for i in items)
        assert all(i.needed for i in items)
        event = AlertEvent(action.rule, "escalate", action.reason, action.severity, items=items)
        text = render.plain(event)
        assert "Blocking: none" in text and "(oldest " in text.splitlines()[0]
