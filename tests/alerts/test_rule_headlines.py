"""Each rule's alert as people read it (#169, part 3), end to end: an incident fixture, the
rule as Kelpie builds it, the engine, the rendered message. The rule writes the headline
(and, for an alert about one task, what is needed and how many tasks it blocks); the reason
stands under it as "Why", whole."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kelpie.alerts import render
from kelpie.alerts.render import RenderSettings
from kelpie.core.actions import Action, ActionKind, ActionResult, Severity
from kelpie.core.alerts import ANNOUNCES, AlertEvent
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.model import Build, BuildStatus, Comment, Snapshot, Task
from kelpie.core.ports import Deferred
from kelpie.rules import REGISTRY
from kelpie.rules._alerts import MAX_HEADLINE, alert_params
from tests.alerts.snapshot import assert_snapshot
from tests.fakes import FakeAdapter, FakeAlerter, ReassigningAdapter, StoppingAdapter
from tests.incidents import Incident, load

FIXTURES = Path(__file__).parents[1] / "fixtures" / "incidents"
URL = "https://board.example.org/tasks/{id}"

# golden name -> (fixture, a word of the alert's headline that picks it out of the tick)
CASES = {
    "rule_wait_condition": ("10-condition-satisfied-wake", "no condition registered"),
    "rule_loop_breaker": ("field-01-heartbeat-runaway", "T-F101"),
    "rule_exit_contract": ("18-acceptance-criterion-dropped", "Acceptance criterion"),
    "rule_checkpoints": ("20-checkpoint-live-review", "Uncommitted work"),
    "rule_ci_economy": ("20-ci-outage", "CI outage"),
    "rule_no_progress": ("field-02-idle-context-resend", "No progress"),
    "rule_output_offenders": ("field-05-repeated-huge-outputs", "floods"),
    "rule_ping_pong": ("12-review-ping-pong", "ping-pong"),
    "rule_orphan_status": ("11-blocker-chain-dead-end", "Dead end"),
    "rule_resources": ("06-provider-quota-exhausted", "no quota left"),
    "rule_return_causes": ("18-flaky-test-returns", "Flaky test"),
    "rule_bottlenecks": ("16-human-task-gates-chain", "Human tasks gate"),
    # rule 4, one golden per alert kind
    "rule_stale_blocks_unlinked": ("10-block-waits-on-pr-merge", "nothing Kelpie can check"),
    "rule_stale_blocks_unseen": ("01-block-names-unseen-condition", "cannot see"),
    "rule_stale_blocks_overdue": ("10-block-waits-on-pr-merge", "past the 8h limit"),
    "rule_stale_blocks_dead_end": ("10-block-pr-closed-unmerged", "can never clear"),
    "rule_stale_blocks_stale_evidence": ("01-guard-block-after-new-push", "is stale"),
}
RUSSIAN = ("rule_wait_condition", "rule_orphan_status", "rule_stale_blocks_unseen")

# A review return as agents write it on a mixed farm: long, half Russian, with a quote.
MIXED = (
    "Ревьюер вернул задачу: «тест test_checkout_total падает на CI после rebase» — the fix "
    "in cart/total.py rounds before the discount, so 19.99 * 3 comes out as 59.96 instead "
    "of 59.97; нужно пересчитать скидку до округления и добавить кейс в tests/test_cart.py, "
    "затем прогнать весь набор ещё раз. "
) * 3


def alerts(incident: Incident, snapshot: Snapshot | None = None) -> list[AlertEvent]:
    """What the incident's rule sends on one tick, in suggest mode."""
    rule = REGISTRY[incident.rule](dict(incident.config))
    config = EngineConfig(dry_run=False, modes={incident.rule: Mode.SUGGEST})
    alerter = FakeAlerter()
    engine = Engine([rule], adapters=[FakeAdapter()], alerter=alerter, config=config)
    engine.tick(lambda: snapshot or incident.snapshot)
    return [e for e in alerter.events if e.rule == incident.rule]


def the_alert(name: str) -> AlertEvent:
    """The case's alert: the one whose headline has the case's word."""
    fixture, word = CASES[name]
    return next(e for e in alerts(load(FIXTURES / f"{fixture}.yaml")) if word in (e.headline or ""))


def check(name: str, event: AlertEvent, language: str = "en") -> str:
    settings = RenderSettings(language, URL)
    suffix = "" if language == "en" else f"_{language}"
    text = render.plain(event, settings)
    assert_snapshot(f"{name}{suffix}.txt", text)
    assert_snapshot(f"{name}{suffix}.html", render.telegram_html(event, settings))
    payload = render.slack_payload(event, settings)
    assert_snapshot(f"{name}{suffix}.json", json.dumps(payload, indent=2, ensure_ascii=False))
    return text


@pytest.mark.parametrize("name", sorted(CASES))
def test_every_rule_heads_its_alert_with_its_own_line(name: str) -> None:
    event = the_alert(name)
    assert event.headline and len(event.headline) <= MAX_HEADLINE
    text = check(name, event)
    head = text.splitlines()[0]
    assert head.endswith(event.headline)  # the rule's line, not a cut of the reason
    assert not head.endswith("…")
    # The reason is never dropped: it stands under the headline, whole up to its limit.
    assert render.one_line(event.reason, render.MAX_REASON) in text
    for item in event.items:
        if item.needed is not None:
            assert f"Needed: {item.needed}" in text


@pytest.mark.parametrize("name", RUSSIAN)
def test_the_rules_lines_stay_english_in_a_russian_message(name: str) -> None:
    """The language setting translates Kelpie's own words; the rule's text passes as is."""
    event = the_alert(name)
    text = check(name, event, "ru")
    assert text.splitlines()[0].endswith(str(event.headline))
    assert "Нужно: " in text and "Почему: " in text
    assert "Needed:" not in text


def test_a_long_mixed_language_reason_stays_under_a_short_headline() -> None:
    """Rule 8's escalation quotes the agents' returns: the headline is the rule's, the long
    half-Russian reason follows as "Why", cut at its limit and never split mid-entity."""
    incident = load(FIXTURES / "12-review-ping-pong.yaml")
    snapshot = incident.snapshot
    tasks = tuple(
        replace(t, returns=tuple(replace(r, reason=MIXED) for r in t.returns))
        if t.id == "T-1201"
        else t
        for t in snapshot.tasks
    )
    (event,) = [e for e in alerts(incident, replace(snapshot, tasks=tasks)) if e.headline]
    assert len(event.reason) > render.MAX_REASON
    text = check("rule_ping_pong_mixed", event)
    lines = text.splitlines()
    assert lines[0] == f"\U0001f7e1 {event.headline}"
    why = next(line for line in lines if line.startswith("Why: "))
    assert why.endswith("…") and len(why) <= len("Why: ") + render.MAX_REASON
    assert "«тест test_checkout_total падает на CI после rebase»" in why


def test_rule_4_keeps_a_long_mixed_language_block_reason_out_of_its_lines() -> None:
    """A block that stays rule 4's (an agent's, with nothing to check) quotes the agent's long
    half-Russian words in "Why", cut at its limit; the headline and "Needed" are the rule's."""
    incident = load(FIXTURES / "10-block-waits-on-pr-merge.yaml")
    snapshot = incident.snapshot
    tasks = tuple(
        replace(t, block=replace(t.block, reason=MIXED * 2)) if t.id == "T-1023" and t.block else t
        for t in snapshot.tasks
    )
    (event,) = [
        e
        for e in alerts(incident, replace(snapshot, tasks=tasks))
        if "nothing Kelpie can check" in (e.headline or "")
    ]
    assert len(event.reason) > render.MAX_REASON
    text = check("rule_stale_blocks_mixed", event)
    lines = text.splitlines()
    assert lines[0] == "\U0001f7e1 T-1023 blocked 13h with nothing Kelpie can check"
    assert "Needed: Clear the block, or register a condition Kelpie can check" in text
    why = next(line for line in lines if line.startswith("Why: "))
    assert why.endswith("…") and len(why) <= len("Why: ") + render.MAX_REASON
    assert "«тест test_checkout_total падает на CI после rebase»" in why
    assert "тест" not in lines[0]


def test_a_rules_headline_keeps_the_reason_under_a_task_that_says_what_it_needs() -> None:
    """With a task link and a whole "Needed" line, an agent's ask stands in for the reason
    (the rest is on the task); a rule's own reason is not on the task, so it stays."""
    event = the_alert("rule_no_progress")
    (item,) = event.items
    assert item.needed and "Why: " in render.plain(event, RenderSettings(task_url=URL))
    bare = replace(event, headline=None)
    assert "Why: " not in render.plain(bare, RenderSettings(task_url=URL))


def test_a_rule_says_how_many_tasks_wait_behind_the_task_when_it_knows() -> None:
    (behind,) = the_alert("rule_orphan_status").items
    assert behind.blocking == 1
    (alone,) = the_alert("rule_wait_condition").items
    assert alone.blocking == 0  # known: nothing waits on it ("Blocking: none")
    (loop,) = the_alert("rule_loop_breaker").items
    assert loop.blocking is None  # not known: no "Blocking" line
    assert "Blocking" not in render.plain(the_alert("rule_loop_breaker"))


def test_alerts_without_a_task_carry_only_a_headline() -> None:
    """``needed`` and ``blocking`` belong to a task's line: a farm-wide alert has none."""
    for name in ("rule_ci_economy", "rule_resources", "rule_return_causes", "rule_bottlenecks"):
        event = the_alert(name)
        assert not event.items and event.headline


def engine_alerts(
    incident: Incident, mode: Mode, adapter: object, snapshot: Snapshot | None = None
) -> list[AlertEvent]:
    rule = REGISTRY[incident.rule](dict(incident.config))
    config = EngineConfig(dry_run=False, modes={incident.rule: mode})
    alerter = FakeAlerter()
    engine = Engine([rule], adapters=[adapter], alerter=alerter, config=config)  # type: ignore[list-item]
    engine.tick(lambda: snapshot or incident.snapshot)
    return [e for e in alerter.events if e.rule == incident.rule and e.headline]


@dataclass
class Hands(StoppingAdapter, ReassigningAdapter):
    """An adapter that carries out every effect these alerts announce."""

    def close(self, task_id: str, reason: str) -> None:
        self._record("close", task_id)


@dataclass
class Refusing(FakeAdapter):
    def stop(self, session_id: str, reason: str) -> ActionResult:
        return ActionResult.REFUSED


# fixture, the task, the effect Kelpie carries out in act mode
ANNOUNCED = {
    "runaway": ("field-01-heartbeat-runaway", "T-F101", "Stop the run if it is still going"),
    "close": ("17-parent-left-open", "T-1700", "Close the parent task"),
    "reassign": ("11-orphan-accepted-task", "T-1101", "Let agent-merge-1 take it"),
}


@dataclass(frozen=True)
class TwoStops:
    """A runaway alert announcing the stops of two sessions, and the stops."""

    once: bool = False
    name: str = "loop_breaker"

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        stops = [
            Action(
                ActionKind.STOP,
                self.name,
                f"Stop {s}.",
                task_id=f"T-{s}",
                key=f"stop:{s}",
                once=self.once,
                params={"session_id": s},
            )
            for s in ("S-1", "S-2")
        ]
        alert = Action(
            ActionKind.ALERT,
            self.name,
            "Runaway runs S-1 and S-2.",
            task_id="T-S-1",
            severity=Severity.CRITICAL,
            key="runaway",
            params={
                ANNOUNCES: ["stop:S-1", "stop:S-2"],
                **alert_params("Runaway runs", "Stop the runs if they are still going"),
            },
        )
        return [alert, *stops]


@dataclass
class StopsOne(StoppingAdapter):
    """Stops S-1; S-2 cannot be stopped this tick (``Deferred``) or fails (``RuntimeError``)."""

    error: Exception = field(default_factory=lambda: Deferred("the run is starting"))

    def stop(self, session_id: str, reason: str) -> None:
        if session_id == "S-2":
            raise self.error
        super().stop(session_id, reason)


@pytest.mark.parametrize(
    ("error", "once"),
    [(Deferred("the run is starting"), False), (RuntimeError("timeout"), True)],
    ids=["deferred", "failing-with-retries-left"],
)
def test_an_ask_stands_while_one_announced_effect_is_still_to_do(
    error: Exception, once: bool
) -> None:
    """S-1 is stopped, S-2 is deferred or fails with retries left (nothing to tell yet):
    S-2 keeps running, so the alert still asks for it."""
    adapter = StopsOne(error=error)
    alerter = FakeAlerter()
    engine = Engine(
        [TwoStops(once=once)],
        adapters=[adapter],
        alerter=alerter,
        config=EngineConfig(dry_run=False, modes={"loop_breaker": Mode.ACT}),
    )
    farm = Snapshot(now=datetime(2026, 3, 2, 12, tzinfo=UTC))
    engine.tick(lambda: farm)
    assert ("stop", "S-1") in adapter.calls and ("stop", "S-2") not in adapter.calls
    (event,) = [e for e in alerter.events if e.headline == "Runaway runs"]
    (item,) = event.items
    assert item.needed == "Stop the runs if they are still going"
    assert "Needed: Stop the runs" in render.plain(event)


@pytest.mark.parametrize("case", sorted(ANNOUNCED))
def test_an_ask_kelpie_carried_out_is_not_asked_again(case: str) -> None:
    """In act mode the alert says what Kelpie did and asks nobody to do it again; in suggest
    mode (and when the effect failed or was refused) the ask stands."""
    name, task_id, ask = ANNOUNCED[case]
    incident = load(FIXTURES / f"{name}.yaml")
    settings = RenderSettings(task_url=URL)

    def about(mode: Mode, adapter: object) -> str:
        (event,) = [
            e
            for e in engine_alerts(incident, mode, adapter)
            if task_id in {i.task_id for i in e.items}
        ]
        return render.plain(event, settings)

    done = about(Mode.ACT, Hands())
    assert "Kelpie did: " in done and "Needed:" not in done
    suggested = about(Mode.SUGGEST, FakeAdapter())
    assert f"Needed: {ask}" in suggested and "Kelpie did: Suggested: " in suggested
    if case == "runaway":
        refused = about(Mode.ACT, Refusing())
        assert f"Needed: {ask}" in refused and "Kelpie did: " in refused


def test_the_acceptance_ask_is_what_rule_1_accepts() -> None:
    """The "Needed" line's comment, with its example result, answers the criterion; so does
    a passing CI workflow of the task's branch named after it."""
    incident = load(FIXTURES / "18-acceptance-criterion-dropped.yaml")
    event = the_alert("rule_exit_contract")
    (item,) = event.items
    assert item.needed is not None
    match = re.fullmatch(
        r'Comment "(AC-\w+): met, <result>" \(for example: (.+)\), or run a CI workflow named '
        r"\1 that passes on the task's branch",
        item.needed,
    )
    assert match, item.needed
    ref, example = match.groups()
    rule = REGISTRY[incident.rule](dict(incident.config))
    task = next(t for t in incident.snapshot.tasks if t.id == item.task_id)
    comment = Comment("agent-dev-1", f"{ref}: met, {example}", incident.snapshot.now)
    told = replace(task, comments=(*task.comments, comment))
    branch = replace(task, branch="app/APP-101")
    job = Build(
        "B-1",
        source="ci",
        repo="example/app",
        workflow=ref,
        branch="app/APP-101",
        status=BuildStatus.SUCCESS,
        finished_at=incident.snapshot.now,
    )

    def alerted(changed: Task, **more: object) -> bool:
        tasks = tuple(changed if t.id == task.id else t for t in incident.snapshot.tasks)
        snapshot = replace(incident.snapshot, tasks=tasks, **more)  # type: ignore[arg-type]
        return any(
            a.kind is ActionKind.ALERT and a.task_id == task.id for a in rule.evaluate(snapshot)
        )

    assert alerted(task)
    assert not alerted(told)  # the comment the line describes answers it
    assert not alerted(branch, builds=(job,))  # and so does the CI workflow it names
    assert alerted(replace(task, comments=(*task.comments, replace(comment, text=f"{ref}: met"))))


def test_a_long_headline_is_cut_by_the_renderer_never_inside_a_quote() -> None:
    """Rules pass the whole line: the layout cuts it at a word, before a quote, or (when
    little stands before the quote) inside it, closed."""
    words = "Hold 'merge-hold' looks forgotten: " + "nothing moved " * 10
    early = "Test 'LoginTests.testSessionTimeout " + "flaky again " * 8 + "' sent 2 tasks back"
    late = "Dead end in front of T-1171: chain " + "goes on " * 5 + "«a quoted reason " * 4 + "»"
    for headline in (words, early, late):
        shown = render.layout(replace(the_alert("rule_no_progress"), headline=headline)).headline
        assert len(shown) <= render.MAX_HEADLINE and shown.endswith(("…", "…'", "…»")), shown
        assert not render._open_quotes(shown), shown  # every quote it opens, it closes
    assert render.layout(replace(the_alert("rule_no_progress"), headline=early)).headline.endswith(
        "…'"
    )


def test_only_alerts_and_escalations_carry_a_headline() -> None:
    """Effects (a stop, a comment) are announced in their own words (``Did``): no headline."""
    incident = load(FIXTURES / "field-05-repeated-huge-outputs.yaml")
    actions = REGISTRY[incident.rule](dict(incident.config)).evaluate(incident.snapshot)
    for action in actions:
        alerting = action.kind in (ActionKind.ALERT, ActionKind.ESCALATE)
        assert ("headline" in action.params) is alerting


def test_the_headline_is_no_part_of_the_reason() -> None:
    """The reason is unchanged (the ledger and the report read it): the headline is apart."""
    incident = load(FIXTURES / "12-review-ping-pong.yaml")
    (action,) = REGISTRY[incident.rule](dict(incident.config)).evaluate(incident.snapshot)
    assert action.reason.startswith("Task T-1201 has been through 9 review rounds (limit 3)")
    assert action.params["headline"] not in action.reason
