"""The engine fills an alert's headline and items from the action and the tick's snapshot."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kelpie.alerts import render
from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import AlertEvent, AlertItem
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import BEHIND_CAP, Engine, alert_items
from kelpie.core.model import Snapshot, Status, Task
from kelpie.rules import REGISTRY, needs_you
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


def test_items_in_the_shape_rule_13_persists() -> None:
    """``task`` for ``task_id``, the entry's own ``title``, ``behind`` counted for
    ``blocking`` (an explicit ``blocking`` wins)."""
    params = {
        "items": [
            {"task": "t_2", "title": " Store the listing ", "behind": ["a", "b", "c"]},
            {"task": "t_1", "behind": ["a"], "blocking": 7},
            {"task_id": "t_3", "task": "ignored", "behind": "not a list"},
            {"task": "t_9", "title": 5},  # a malformed title: skipped
        ]
    }
    assert alert_items(alert(params=params), SNAPSHOT) == (
        AlertItem("t_2", "Store the listing", blocking=3),
        AlertItem("t_1", "Nightly backup", blocking=7),
        AlertItem("t_3"),
    )


def test_how_rule_13s_current_digest_renders() -> None:
    """Pins what the rule sends today (incident 13): its own items, the digest headline."""
    incident = load(FIXTURES / "13-human-tasks-block-agents.yaml")
    rule = REGISTRY[incident.rule](incident.config)
    (action,) = [a for a in rule.evaluate(incident.snapshot) if a.kind is ActionKind.ESCALATE]
    items = alert_items(action, incident.snapshot)
    assert [(i.task_id, i.title, i.blocking) for i in items] == [
        ("T-1302", "Decide on storage backend", 3),
        ("T-1301", "Add deploy token to repo secrets", 2),
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


def test_an_approval_in_rule_13s_digest_blocks_nothing_but_itself() -> None:
    """An approval's ``behind`` is the task itself: no "Blocking" line for it."""
    incident = load(FIXTURES / "13-approval-wait.yaml")
    rule = REGISTRY[incident.rule](incident.config)
    actions = [a for a in rule.evaluate(incident.snapshot) if a.kind is ActionKind.ESCALATE]
    assert actions
    for action in actions:
        items = alert_items(action, incident.snapshot)
        assert items and all(i.blocking is None and i.age is not None for i in items)
        event = AlertEvent(action.rule, "escalate", action.reason, action.severity, items=items)
        text = render.plain(event)
        assert "Blocking" not in text and "(oldest " in text.splitlines()[0]


def test_a_capped_behind_list_counts_as_at_least() -> None:
    capped = {"task": "t_2", "behind": [f"a{n}" for n in range(BEHIND_CAP)]}
    under = {"task": "t_1", "behind": [f"a{n}" for n in range(BEHIND_CAP - 1)]}
    exact = {"task": "t_3", "behind": [f"a{n}" for n in range(BEHIND_CAP)], "blocking": 12}
    items = alert_items(alert(params={"items": [capped, under, exact]}), SNAPSHOT)
    assert [(i.blocking, i.blocking_more) for i in items] == [
        (BEHIND_CAP, True),
        (BEHIND_CAP - 1, False),
        (12, False),
    ]
    event = AlertEvent("needs_you", "escalate", "Needs you.", Severity.WARNING, items=items)
    text = render.plain(event)
    assert "Blocking: 10+ tasks" in text and "Blocking: 9 tasks" in text
    assert "Blocking: 12 tasks" in text
    ru = render.plain(event, render.RenderSettings("ru"))
    assert "Блокирует: 10+ задач" in ru
    assert items[0].fingerprint != replace(items[0], blocking_more=False).fingerprint


def test_waiting_minutes_is_an_items_age() -> None:
    entry = {"task": "t_1", "waiting_minutes": 90}
    (one,) = alert_items(alert(params={"items": [entry]}), SNAPSHOT)
    assert (one.since, one.age) == (None, timedelta(minutes=90))
    own = {"task": "t_1", "waiting_minutes": 90, "since": NOW - H}  # an explicit since wins
    assert alert_items(alert(params={"items": [own]}), SNAPSHOT)[0].age == H
    bad = {"task": "t_1", "waiting_minutes": "soon"}
    assert alert_items(alert(params={"items": [bad]}), SNAPSHOT)[0].age is None


def test_an_item_with_only_waiting_minutes_keeps_its_fingerprint_across_ticks() -> None:
    """The rule rounds to whole minutes: tick after tick it says one minute more, at times
    that do not line up with Kelpie's clock. The worded age ("1.5h") moves only every 6 min."""

    def fingerprint(now: datetime, minutes: int) -> tuple[object, ...]:
        entry = {"task": "t_1", "waiting_minutes": minutes}
        (one,) = alert_items(alert(params={"items": [entry]}), Snapshot(now=now, tasks=BOARD))
        return one.fingerprint

    first = fingerprint(NOW, 90)
    assert fingerprint(NOW + timedelta(seconds=59), 90) == first
    assert fingerprint(NOW + timedelta(seconds=61), 91) == first
    assert fingerprint(NOW + timedelta(minutes=2), 92) == first


def test_the_shim_counts_behind_as_rule_13_caps_it() -> None:
    """``BEHIND_CAP`` is the length rule 13 cuts ``params.items[].behind`` to."""
    assert BEHIND_CAP == needs_you._SHOWN * 2
