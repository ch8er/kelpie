"""The farm view model (kelpie.core.farmview): pure, from a snapshot and stored history."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.config import Mode
from kelpie.core.farmview import (
    MAX_ITEMS,
    SCHEMA_VERSION,
    build_view,
    from_dict,
    latest_per_rule,
    to_dict,
)
from kelpie.core.ledger import LedgerEntry, Outcome
from kelpie.core.model import Condition, ConditionKind, Snapshot, Status, Task
from kelpie.core.states import OpenState
from kelpie.core.transitions import StatusChange

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)


def task(tid: str, status: Status, board: str | None = None) -> Task:
    return Task(tid, f"title {tid}", status, NOW - timedelta(hours=1), board=board)


def entry(rule: str, minutes: int, task_id: str | None = "T-101") -> LedgerEntry:
    return LedgerEntry(
        at=NOW - timedelta(minutes=minutes),
        fingerprint=f"{rule}-{minutes}",
        subject=task_id or "farm",
        rule=rule,
        kind=ActionKind.ALERT,
        mode=Mode.OBSERVE,
        outcome=Outcome.OBSERVED,
        reason=f"{rule} {minutes}",
        task_id=task_id,
    )


def state(sid: str, opened_minutes: int, task_id: str | None = None) -> OpenState:
    at = NOW - timedelta(minutes=opened_minutes)
    return OpenState(sid, "stale_blocks", at, at, NOW, 0, Severity.WARNING, "blocked", task_id)


def test_counts_waiting_alerts_and_actions_per_rule() -> None:
    snapshot = Snapshot(
        now=NOW,
        tasks=(
            task("a:T-1", Status.IN_PROGRESS, "a"),
            task("a:T-2", Status.WAITING, "a"),
            task("b:T-3", Status.IN_PROGRESS, "b"),
            task("b:T-4", Status.DONE, "b"),
        ),
        conditions=(
            Condition("c1", ConditionKind.PR_MERGED, "a:T-2", {"repo": "example/repo", "pr": 7}),
            Condition("c2", ConditionKind.CI_STATUS, "a:T-2", satisfied=True),  # met: left out
            Condition("c3", ConditionKind.TIME),  # no task: left out
        ),
    )
    entries = [entry("rule_a", m) for m in (1, 2, 3, 4, 5)] + [entry("rule_b", 9, None)]
    view = build_view(NOW, snapshot, [state("s1", 30, "b:T-3"), state("s2", 5)], entries)

    assert view.source == "board" and view.snapshot_at is None
    assert view.boards == ("a", "b") and view.tasks == 4
    assert [(c.board, c.status, c.count) for c in view.statuses] == [
        ("a", "in_progress", 1),
        ("a", "waiting", 1),
        ("b", "in_progress", 1),
        ("b", "done", 1),
    ]
    assert [(w.task_id, w.kind, w.target, w.task_status, w.board) for w in view.waiting] == [
        ("a:T-2", "pr_merged", "example/repo#7", "waiting", "a")
    ]
    assert [a.id for a in view.alerts] == ["s2", "s1"]  # newest first
    assert view.alerts[1].board == "b"
    by_rule = {r.rule: [e.reason for e in r.actions] for r in view.rules}
    assert by_rule == {"rule_a": ["rule_a 1", "rule_a 2", "rule_a 3"], "rule_b": ["rule_b 9"]}


def test_an_empty_farm_is_an_empty_view_not_an_error() -> None:
    view = build_view(NOW, Snapshot(now=NOW))
    assert view.tasks == 0 and not view.statuses and not view.waiting and not view.rules


def test_stored_statuses_stand_in_without_a_board() -> None:
    stored = {
        "T-1": StatusChange(NOW, "T-1", Status.BLOCKED, NOW),
        "T-2": StatusChange(NOW, "T-2", Status.BLOCKED, NOW),
    }
    view = build_view(NOW, None, stored=stored)
    assert view.source == "stored"
    assert [(c.status, c.count) for c in view.statuses] == [("blocked", 2)]
    assert build_view(NOW, None).source == "none"


def test_a_snapshot_wins_over_stored_statuses() -> None:
    stored = {"T-9": StatusChange(NOW, "T-9", Status.BLOCKED, NOW)}
    snapshot = Snapshot(now=NOW - timedelta(minutes=1), tasks=(task("T-1", Status.READY),))
    view = build_view(NOW, snapshot, stored=stored, source="server")
    assert view.source == "server" and view.snapshot_at == snapshot.now
    assert [c.status for c in view.statuses] == ["ready"]


def test_lists_are_capped_and_counted() -> None:
    states = [state(f"s{i}", i) for i in range(MAX_ITEMS + 5)]
    view = build_view(NOW, None, states)
    assert len(view.alerts) == MAX_ITEMS and view.alerts_total == MAX_ITEMS + 5


def test_long_text_is_cut() -> None:
    long = OpenState("s", "r", NOW, NOW, NOW, 0, Severity.INFO, "x" * 2000)
    assert len(build_view(NOW, None, [long]).alerts[0].summary) == 500


def test_naive_now_is_refused() -> None:
    with pytest.raises(ValueError):
        build_view(datetime(2026, 3, 2), None)


def test_latest_per_rule_keeps_the_newest() -> None:
    kept = latest_per_rule([entry("r", 5), entry("r", 1), entry("r", 3)], per_rule=2)
    assert [e.reason for e in kept["r"]] == ["r 1", "r 3"]


def test_wire_form_round_trips() -> None:
    snapshot = Snapshot(
        now=NOW,
        tasks=(task("T-2", Status.WAITING),),
        conditions=(Condition("c1", ConditionKind.HUMAN_TASK, "T-2", {"task_id": "T-9"}),),
    )
    view = build_view(NOW, snapshot, [state("s1", 3, "T-2")], [entry("r", 1)], problems=["p"])
    data = to_dict(view)
    assert data["schema_version"] == SCHEMA_VERSION
    assert from_dict(data) == view
    assert data["waiting"][0]["target"] == "T-9"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"schema_version": 99, "now": NOW.isoformat(), "source": "server"},
        {"schema_version": SCHEMA_VERSION, "source": "server"},
        {"schema_version": SCHEMA_VERSION, "now": "2026-03-02T15:00:00", "source": "s"},
        {"schema_version": SCHEMA_VERSION, "now": NOW.isoformat(), "source": "s", "alerts": [{}]},
        [],
    ],
)
def test_malformed_wire_form_is_refused(data: object) -> None:
    with pytest.raises(ValueError):
        from_dict(data)  # type: ignore[arg-type]


def test_a_shared_condition_is_listed_under_each_waiter() -> None:
    snapshot = Snapshot(
        now=NOW,
        tasks=(task("a:T-1", Status.WAITING, "a"), task("b:T-2", Status.WAITING, "b")),
        conditions=(
            Condition(
                "c1",
                ConditionKind.PR_MERGED,
                "a:T-1",
                {"repo": "example/repo", "pr": 7},
                task_ids=("b:T-2",),
            ),
            Condition("c2", ConditionKind.TIME, task_ids=("b:T-2",), satisfied=True),  # met
        ),
    )
    view = build_view(NOW, snapshot, [], [])
    assert [(w.task_id, w.condition_id, w.board) for w in view.waiting] == [
        ("a:T-1", "c1", "a"),
        ("b:T-2", "c1", "b"),
    ]
