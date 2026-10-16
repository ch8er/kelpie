"""Rule output_offenders: repeated huge tool outputs reach the instructions owner."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.model import OutputOffender, Snapshot
from kelpie.rules import REGISTRY
from kelpie.rules.output_offenders import OutputOffenders

NOW = datetime(2026, 3, 3, 16, tzinfo=UTC)


def offender(**kw: object) -> OutputOffender:
    base: dict[str, object] = {
        "role": "developer",
        "command": "xcodebuild",
        "filter": "xcbeautify",
        "how": "pipe it through xcbeautify",
        "days": 2,
        "calls": 14,
        "mean_tokens": 18250.0,
        "saved_tokens": 178850,
        "owner": "human-owner",
        "tasks": ("T-1", "T-2"),
        "latest_task": "T-2",
    }
    return OutputOffender(**{**base, **kw})  # type: ignore[arg-type]


def run(*found: OutputOffender) -> list:
    return OutputOffenders().evaluate(Snapshot(NOW, output_offenders=found))


def test_registered_and_takes_no_parameters() -> None:
    assert isinstance(REGISTRY["output_offenders"]({}), OutputOffenders)
    with pytest.raises(ValueError, match=r"\[traces\.outputs\]"):
        REGISTRY["output_offenders"]({"threshold_tokens": 1})


def test_nothing_without_offenders() -> None:
    assert run() == []


def test_a_comment_on_the_latest_task_addressed_to_the_owner() -> None:
    [action] = run(offender())
    assert action.kind is ActionKind.COMMENT and action.task_id == "T-2"
    assert action.severity is Severity.WARNING and action.once
    assert action.key == "output_offenders:developer:xcodebuild:xcbeautify"
    assert action.reason.startswith("human-owner: `xcodebuild` floods the developer context")
    assert "18,250 tokens per call" in action.reason and "about 178,850 tokens" in action.reason
    assert "Add to the developer instructions: pipe it through xcbeautify" in action.reason
    assert action.params["tasks"] == ["T-1", "T-2"] and action.params["owner"] == "human-owner"


def test_without_an_owner_or_a_task() -> None:
    [action] = run(offender(owner=None, latest_task=None, role=None))
    assert action.kind is ActionKind.ALERT and action.task_id is None
    assert action.reason.startswith("Whoever owns the agents instructions: ")
    assert action.key == "output_offenders:agents:xcodebuild:xcbeautify"


def test_the_key_is_stable_while_the_numbers_move() -> None:
    first, later = run(offender()), run(offender(days=3, calls=30, latest_task="T-9"))
    assert first[0].key == later[0].key  # once per role, command and filter
