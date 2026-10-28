"""Edge cases of rule ``instruction_bloat`` (incident 21); the scenarios are fixtures."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Severity
from kelpie.core.model import InstructionDuplicate, InstructionFile, Snapshot
from kelpie.rules.instruction_bloat import MAX_PLACES, InstructionBloat, build

NOW = datetime(2026, 3, 9, 12, tzinfo=UTC)
WEEK_AGO = NOW - timedelta(days=7)


def file(**kw: Any) -> InstructionFile:
    base: dict[str, Any] = {
        "path": "~/farm/dev/skills/a/SKILL.md",
        "role": "developer",
        "chars": 10_000,
        "tokens": 2_500,
        "sections": 5,
        "since": WEEK_AGO,
        "chars_since": 10_000,
    }
    return InstructionFile(**(base | kw))


def run(*files: InstructionFile, dupes: tuple[InstructionDuplicate, ...] = (), **kw: Any):
    snap = Snapshot(now=NOW, instruction_files=files, instruction_duplicates=dupes)
    return InstructionBloat(**kw).evaluate(snap)


def test_small_stable_file_is_silent() -> None:
    assert run(file()) == []


def test_crossing_exactly_at_the_threshold_alerts_below_it_does_not() -> None:
    (a,) = run(file(chars=40_000, chars_since=39_999, appends=1), max_growth_per_week=10**6)
    assert "crossed 40,000" in a.reason
    assert a.state == a.key == "instruction_bloat:~/farm/dev/skills/a/SKILL.md"
    assert run(file(chars=39_999, chars_since=30_000), max_growth_per_week=10**6) == []


def test_growth_needs_min_span_and_reaches_the_threshold() -> None:
    young = file(since=NOW - timedelta(hours=12), chars=20_000, chars_since=10_000)
    assert run(young) == []  # +10,000 in 12 hours: too short to call a weekly rate
    (a,) = run(file(chars=20_000, chars_since=10_000, appends=3))
    assert a.params["growth_per_week"] == 10_000
    assert a.params["only_grows"] is True
    assert "+10,000 a week" in a.params["headline"]
    assert run(file(chars=19_999, chars_since=10_000)) == []


def test_a_file_that_was_edited_is_not_called_append_only() -> None:
    (a,) = run(file(chars=30_000, chars_since=10_000, appends=4, edits=1))
    assert a.params["only_grows"] is False
    assert "only grows" not in a.reason


def test_shrinking_file_is_silent() -> None:
    assert run(file(chars=50_000, chars_since=90_000, edits=2)) == []


def test_cost_counts_only_for_always_loaded_files_or_counted_loads() -> None:
    big = {"chars": 60_000, "chars_since": 60_000, "tokens_per_day": 900_000}
    assert run(file(**big, loaded="on_demand", token_basis="sessions")) == []
    (a,) = run(file(**big, loaded="on_demand", token_basis="loads"))
    assert "from counted loads" in a.reason
    (b,) = run(file(**big, loaded="always", token_basis="sessions"))
    assert "upper bound" not in b.reason and "size times" in b.reason
    assert run(file(**big | {"tokens_per_day": 499_999}, token_basis="sessions")) == []
    assert run(file(**big | {"tokens_per_day": None})) == []


def test_unmeasured_history_does_not_alert() -> None:
    assert run(file(chars=90_000, since=None, chars_since=None)) == []


def test_owner_and_every_role_are_named() -> None:
    (a,) = run(file(role=None, owner="human-owner", chars=50_000, chars_since=1_000))
    assert a.reason.startswith("human-owner: `~/farm/dev/skills/a/SKILL.md` (every role)")
    assert a.severity is Severity.WARNING


def test_copies_go_with_the_alerted_file_or_to_their_own_role_alert() -> None:
    path = "~/farm/dev/skills/a/SKILL.md"
    many = tuple(f"~/farm/dev/f{i}.md:{i}" for i in range(MAX_PLACES + 2))
    dupes = (
        InstructionDuplicate("developer", "section", (f"{path}:3", f"{path}:90"), 0.9),
        InstructionDuplicate("ops", "value", many),
    )
    actions = run(file(chars=50_000, chars_since=1_000), dupes=dupes)
    by_role = {a.params["role"]: a for a in actions}
    assert by_role["developer"].params["duplicate_sections"] == 1
    ops = by_role["ops"]
    assert ops.severity is Severity.INFO and ops.state == "instruction_bloat:copies:ops"
    assert f"one value in {MAX_PLACES + 2} places" in ops.reason
    assert "and 2 more" in ops.reason
    assert ops.params["headline"] == "ops instructions: 0 duplicated sections, 1 repeated value"
    assert len(actions) == 2


def test_build_validates_its_parameters() -> None:
    rule = build({"max_chars": 5, "max_growth_per_week": 6, "max_tokens_per_day": 7,
                  "min_span": "2d"})  # fmt: skip
    assert (rule.max_chars, rule.max_growth_per_week, rule.max_tokens_per_day) == (5, 6, 7)
    assert rule.min_span == timedelta(days=2)
    with pytest.raises(ValueError, match="max_chars must be a positive integer"):
        build({"max_chars": 0})
    with pytest.raises(ValueError, match="max_chars must be a positive integer"):
        build({"max_chars": True})
    with pytest.raises(ValueError, match="min_span must be positive"):
        build({"min_span": "0s"})
    with pytest.raises(ValueError, match="min_span:"):
        build({"min_span": "soon"})
    with pytest.raises(ValueError, match=r"unknown parameters: \['paths'\]"):
        build({"paths": []})
