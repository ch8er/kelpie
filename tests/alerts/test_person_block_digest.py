"""A block that asks a person reaches them once, in rule 13's digest (#169), end to end: the
fixture's farm, rules 4 and 13 as Kelpie loads them, the engine, the rendered message."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kelpie.alerts import render
from kelpie.alerts.render import RenderSettings
from kelpie.app import load_rules
from kelpie.core.actions import Action, ActionKind
from kelpie.core.alerts import AlertEvent
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine, alert_items
from kelpie.core.model import Snapshot, Status
from tests.alerts.snapshot import assert_snapshot
from tests.fakes import FakeAdapter, FakeAlerter
from tests.incidents import Incident, load

FIXTURES = Path(__file__).parents[1] / "fixtures" / "incidents"
RULE_4 = load(FIXTURES / "13-person-block-left-to-inbox.yaml")
RULE_13 = load(FIXTURES / "13-person-block-in-inbox.yaml")
NOT_LISTED = load(FIXTURES / "13-person-block-not-listed.yaml")
BY_TASK = load(FIXTURES / "13-person-block-listed-by-task.yaml")
BROKEN = load(FIXTURES / "13-person-block-broken.yaml")
URL = "https://board.example.org/tasks/{id}"
NY = "needs_you"
QUESTION = "t_a1b2c3d4"  # 'waiting_human: needs_input: CEO: …', no condition
PARKED = "t_e5f6a7b9"  # parked on the CEO's task by a human_task condition


def config(stale_blocks: Mode, needs_you: Mode) -> EngineConfig:
    """The fixtures' settings as a farm writes them: rule 4 states none of what it reads
    from rule 13 (``kelpie.app`` hands it over)."""
    own = {k: v for k, v in RULE_4.config.items() if k != "needs_you"}
    return EngineConfig(
        dry_run=False,
        modes={"stale_blocks": stale_blocks, "needs_you": needs_you},
        rules={"stale_blocks": own, "needs_you": dict(RULE_13.config)},
    )


def events(engine_config: EngineConfig, snapshot: Snapshot = RULE_4.snapshot) -> list[AlertEvent]:
    loaded = load_rules(engine_config)
    rules = [r for r in loaded.rules if r.name in {"stale_blocks", "needs_you"}]
    alerter = FakeAlerter()
    engine = Engine(rules, adapters=[FakeAdapter()], alerter=alerter, config=engine_config)
    engine.tick(lambda: snapshot)
    return alerter.events


def about(event: AlertEvent) -> set[str]:
    return {i.task_id for i in event.items}


def test_the_fixtures_show_one_farm() -> None:
    assert RULE_4.snapshot == RULE_13.snapshot
    assert RULE_4.config["needs_you"] == RULE_13.config  # as kelpie.app hands it over


@pytest.mark.parametrize("farm", [RULE_4, NOT_LISTED, BY_TASK, BROKEN], ids=lambda f: f.id)
def test_every_blocked_task_reaches_a_person_through_exactly_one_rule(farm: Incident) -> None:
    """No double message, and no block both rules leave to the other (#169 review). Rule 13
    reaches a task as an item (an approval, a person's own blocked task) or among the tasks
    waiting on a listed human task. People's blocked tasks count too."""
    engine_config = config(Mode.SUGGEST, Mode.SUGGEST)
    told: dict[str, list[str]] = {}
    for rule in load_rules(engine_config).rules:
        if rule.name not in {"stale_blocks", "needs_you"}:
            continue
        for action in rule.evaluate(farm.snapshot):
            if action.kind is ActionKind.ESCALATE:
                params = action.params
                reached = {*params["waiting"], *params["approvals"], *params["human_tasks"]}
            else:
                reached = {action.task_id} if action.task_id else set()
            for task_id in reached:
                told.setdefault(task_id, []).append(rule.name)
    for task in farm.snapshot.tasks:
        if task.status is Status.BLOCKED:
            assert len(told.get(task.id, [])) == 1, (task.id, told.get(task.id))


def inbox(farm: Incident) -> list[Action]:
    rule = next(r for r in load_rules(config(Mode.SUGGEST, Mode.SUGGEST)).rules if r.name == NY)
    return list(rule.evaluate(farm.snapshot))


def test_a_persons_block_that_can_never_clear_is_said_in_the_digest() -> None:
    """S10, S11: rule 4 sends nothing on these items, so their "Needed" says what it would."""
    (digest,) = inbox(BROKEN)
    needed = {i["task_id"]: i["needed"] for i in digest.params["items"]}
    assert needed == {
        "H-10": "the PR was closed without merging (pr:x): the block will never clear on its own",
        "H-11": "the block names condition pr:gone, which Kelpie cannot see",
    }
    event = AlertEvent(
        NY, "escalate", digest.reason, digest.severity, items=alert_items(digest, BROKEN.snapshot)
    )
    text = render.plain(event, RenderSettings("en", URL))
    assert "Needed: the PR was closed without merging (pr:x)" in text
    assert "Needed: the block names condition pr:gone, which Kelpie cannot see" in text


def test_a_task_that_waits_on_a_pr_first_adds_nothing_to_the_persons_cost() -> None:
    """T-8 waits on H-7 and on PR #9: it waits on the PR first, so it is not waiting on the
    person (the report's agent-hours) and adds nothing to their cost: 40, not 60."""
    (digest,) = inbox(BY_TASK)
    assert digest.params["waiting"] == ["T-7", "T-9"]
    assert digest.params["cost_hours"] == 40.0


def test_a_question_for_a_person_reaches_them_once_in_the_digest() -> None:
    sent = events(config(Mode.SUGGEST, Mode.SUGGEST))
    four = [e for e in sent if e.rule == "stale_blocks"]
    assert sorted(i for e in four for i in about(e)) == ["t_c0de0012", "t_c0de0013"]
    inbox = [e for e in sent if e.rule == "needs_you" and QUESTION in about(e)]
    assert len(inbox) == 1
    assert [i.task_id for i in inbox[0].items] == ["t_e5f6a7b8", QUESTION]  # by cost
    assert not any(e.rule == "needs_you" and "t_c0de0012" in about(e) for e in sent)


def test_without_rule_13_reaching_people_rule_4_alerts_as_before() -> None:
    sent = events(config(Mode.SUGGEST, Mode.OBSERVE))
    assert not [e for e in sent if e.rule == "needs_you"]
    four = {i for e in sent if e.rule == "stale_blocks" for i in about(e)}
    assert {QUESTION, PARKED} <= four


@pytest.mark.parametrize("language", ["en", "ru"])
def test_the_digest_as_people_read_it(language: str) -> None:
    (digest,) = [
        e
        for e in events(config(Mode.SUGGEST, Mode.SUGGEST))
        if e.rule == "needs_you" and QUESTION in about(e)
    ]
    settings = RenderSettings(language, URL)
    name = "person_block_digest" + ("" if language == "en" else f"_{language}")
    text = render.plain(digest, settings)
    first = "Waiting for you: 2 tasks (oldest 46 h)" if language == "en" else "Ждут вас: 2 задачи"
    assert text.splitlines()[0].startswith(f"\U0001f7e1 {first}")
    assert_snapshot(f"{name}.txt", text)
    assert_snapshot(f"{name}.html", render.telegram_html(digest, settings))
    payload = render.slack_payload(digest, settings)
    assert_snapshot(f"{name}.json", json.dumps(payload, indent=2, ensure_ascii=False))
