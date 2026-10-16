"""Rule 1: "done" needs evidence (P1, incident 2)."""

from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.model import (
    Artifact,
    Condition,
    ConditionKind,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules.exit_contract import ExitContract, build

NOW = datetime(2026, 3, 2, 16, tzinfo=UTC)
H = timedelta(hours=1)
CONFIG = {
    "done_evidence": [
        {"name": "tests", "artifacts": ["*test*", "*.junit.xml"]},
        {"name": "ci", "condition": "ci_status"},
    ]
}


def done(
    *artifacts: str, age: timedelta = H, owner: str = "dev-1", conditions: tuple = ()
) -> Snapshot:
    task = Task("T", "Ship it", Status.DONE, NOW - age, "dev", owner_id=owner)
    run = Session(
        "S",
        "T",
        "dev-1",
        NOW - 3 * H,
        ended_at=NOW - age,
        final_status=Status.DONE,
        artifacts=tuple(Artifact(a, NOW - 2 * H) for a in artifacts),
    )
    owners = (Owner("dev-1", "dev"), Owner("alice", "owner", OwnerKind.HUMAN))
    return Snapshot(now=NOW, tasks=(task,), sessions=(run,), owners=owners, conditions=conditions)


def green(satisfied: bool | None = True) -> Condition:
    return Condition("ci:T", ConditionKind.CI_STATUS, "T", satisfied=satisfied)


def test_done_without_evidence_goes_back_to_the_queue() -> None:
    [action] = build(CONFIG).evaluate(done("notes.md"))
    assert action.kind is ActionKind.REQUEUE and action.task_id == "T" and action.once
    assert action.params["missing"] == ["tests", "ci"]
    assert "moved to done without evidence" in action.reason
    assert "an artifact matching *test*, *.junit.xml" in action.reason


@pytest.mark.parametrize(
    "snapshot",
    [
        done("reports/PyTest-Output.TXT"),  # any case, on the file name
        done("build/app.junit.xml"),
        done(conditions=(green(),)),
    ],
)
def test_one_kind_of_evidence_is_enough_by_default(snapshot: Snapshot) -> None:
    assert build(CONFIG).evaluate(snapshot) == []


def test_all_kinds_when_required() -> None:
    rule = build({**CONFIG, "done_evidence_require": "all"})
    [action] = rule.evaluate(done("pytest.xml"))
    assert action.params["missing"] == ["ci"]
    assert rule.evaluate(done("pytest.xml", conditions=(green(),))) == []


def test_unmet_or_unknown_ci_is_no_evidence() -> None:
    assert build(CONFIG).evaluate(done(conditions=(green(False),)))
    assert build(CONFIG).evaluate(done(conditions=(green(None),)))


def test_alert_instead_of_requeue() -> None:
    [action] = build({**CONFIG, "done_evidence_action": "alert"}).evaluate(done())
    assert action.kind is ActionKind.ALERT and action.once and action.state is None


def test_what_is_not_judged() -> None:
    rule = build(CONFIG)
    assert ExitContract().evaluate(done()) == []  # off without done_evidence
    assert rule.evaluate(done(age=25 * H)) == []  # done long ago
    assert rule.evaluate(done(age=timedelta(minutes=5))) == []  # still settling
    assert rule.evaluate(done(owner="alice")) == []  # a person's task
    no_run = Snapshot(now=NOW, tasks=(Task("T", "T", Status.DONE, NOW - H),))
    assert rule.evaluate(no_run) == []  # no agent worked on it


def test_only_an_agents_completion_is_judged() -> None:
    from dataclasses import replace

    rule = build(CONFIG)
    snap = done()
    # A person closed it on the board an hour after the agent's last run ended unfinished.
    run = replace(snap.sessions[0], ended_at=NOW - 3 * H, final_status=Status.REVIEW)
    assert rule.evaluate(replace(snap, sessions=(run,))) == []
    # A parent: its evidence is its subtasks'.
    parent = replace(snap.tasks[0], subtasks=("A", "B"))
    assert rule.evaluate(replace(snap, tasks=(parent,))) == []


def test_evidence_must_postdate_the_last_reopen() -> None:
    from dataclasses import replace

    snap = done("pytest.xml")  # written 2 h ago
    reopened = replace(snap.tasks[0], reopened_at=(NOW - 90 * timedelta(minutes=1),))
    [action] = build(CONFIG).evaluate(replace(snap, tasks=(reopened,)))
    assert action.params["missing"] == ["tests", "ci"]


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"done_evidence": {"name": "x"}}, "list of tables"),
        ({"done_evidence": [{"artifacts": ["*"]}]}, "name"),
        ({"done_evidence": [{"name": "x"}]}, "exactly one"),
        (
            {"done_evidence": [{"name": "x", "artifacts": ["*"], "condition": "ci_status"}]},
            "exactly one",
        ),
        ({"done_evidence": [{"name": "x", "artifacts": []}]}, "globs"),
        ({"done_evidence": [{"name": "x", "condition": "green"}]}, "condition must be"),
        ({"done_evidence": [{"name": "x", "artifacts": ["*"], "extra": 1}]}, "unknown keys"),
        (
            {
                "done_evidence": [
                    {"name": "x", "artifacts": ["*"]},
                    {"name": "x", "artifacts": ["*"]},
                ]
            },
            "duplicate",
        ),
        ({"done_evidence_require": "most"}, "done_evidence_require"),
        ({"done_evidence_action": "close"}, "done_evidence_action"),
        ({"done_evidence_within": "0s"}, "done_evidence_within"),
        ({"done_evidence_within": "8d"}, "done_evidence_within"),
    ],
)
def test_build_rejects(config: dict, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build(config)
