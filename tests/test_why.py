"""``kelpie why <task>`` over a synthetic Hermes board, set up like the smoke test."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import LoadedRules, build_app, find_config, load_rules, load_settings
from kelpie.cli import main
from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.model import Block, Condition, ConditionKind, Session, Snapshot, Status, Task
from kelpie.why import (
    Explanation,
    Finding,
    _age,
    _chain,
    _mentions,
    _mode,
    as_json,
    evaluate_rules,
    explain,
    explain_snapshot,
    read_history,
    render,
)
from tests.adapters.kanban import Board
from tests.incidents import load

FIXTURES = Path(__file__).parent / "fixtures" / "incidents"

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)

CONFIG = """
[engine]
dry_run = true
default_mode = "observe"

[adapters.hermes]
db_path = {db}
spool = false
git_state = false
lookback = "30d"

[engine.rules.orphan_status]
session_stale_after = "60m"
reviewer_roles = ["reviewer"]
"""


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


@pytest.fixture
def farm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("KELPIE_CONFIG", raising=False)
    monkeypatch.delenv("KELPIE_DB", raising=False)
    monkeypatch.chdir(tmp_path)  # kelpie.toml and kelpie.db are looked up in the cwd
    board = Board(tmp_path / "kanban.db")
    # Handed to review four hours ago and left with its developer: nothing drives it.
    board.task("t_review", "review", ago(days=1), title="Add CSV export", assignee="dev")
    board.event("t_review", "claimed", ago(hours=6))
    board.run("t_review", "dev", ago(hours=6), status="done", ended=ago(hours=4),
              outcome="review_requested")  # fmt: skip
    board.event("t_review", "review_requested", ago(hours=4))
    # Waits for it.
    board.task("t_child", "todo", ago(days=1), title="Document CSV export", assignee="dev")
    board.link("t_review", "t_child")
    # Blocked for hours on a reason Kelpie cannot check.
    board.task("t_blocked", "blocked", ago(days=1), assignee="dev")
    board.event("t_blocked", "claimed", ago(hours=7))
    board.run("t_blocked", "dev", ago(hours=7), status="blocked", ended=ago(hours=6),
              outcome="blocked")  # fmt: skip
    board.event("t_blocked", "blocked", ago(hours=6), {"reason": "waiting for something"})
    board.close()
    (tmp_path / "kelpie.toml").write_text(CONFIG.format(db=json.dumps(str(tmp_path / "kanban.db"))))
    return tmp_path


def why(task: str, *extra: str) -> list[str]:
    return ["why", task, *extra]


def test_review_with_nothing_driving_it(farm: Path, capsys: pytest.CaptureFixture[str]) -> None:
    settings = load_settings(find_config(None))
    ex = explain(settings, "t_review", clock=lambda: NOW)
    assert isinstance(ex, Explanation)
    text = render(ex)
    assert "Task t_review: Add CSV export" in text
    assert "status   review for 4h 00m" in text
    assert "dev (agent, role dev)" in text and "none live" in text
    assert "final status review" in text
    assert "orphan_status [observe] alert warning" in text and "nothing is driving it" in text
    assert "behind it: 1 task(s): t_child" in text
    assert "no database at" in text  # nothing written, nothing to read
    assert not (farm / "kelpie.db").exists()


def test_chain_of_a_waiting_task(farm: Path) -> None:
    settings = load_settings(find_config(None))
    ex = explain(settings, "t_child", clock=lambda: NOW)
    assert isinstance(ex, Explanation)
    text = render(ex)
    assert "waits     t_child -> t_review: t_review is review" in text
    # The review alert counts the child as a dependent but does not name it.
    assert "no rule acts on this task" in text


def test_cli_exit_codes_and_json(farm: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(why("t_nope")) == 1
    assert "t_nope is not on the board" in capsys.readouterr().err
    assert main(why("t_nope", "--json")) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "not_found" and data["task_id"] == "t_nope"
    assert main(why("t_blocked", "--json")) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["schema_version"] == 1 and "sessions_total" in data
    assert data["task"]["status"] == "blocked" and data["task"]["block"]["reason"]
    assert data["task"]["labels"] == [] and data["task"]["held_by"] == []
    assert any(f["rule"] == "stale_blocks" for f in data["findings"])
    assert data["dry_run"] is True and data["chains"] == []
    assert not (farm / "kelpie.db").exists()


def test_reads_states_and_ledger_without_writing(
    farm: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("kelpie.why.utc_now", lambda: NOW + timedelta(minutes=1))
    settings = load_settings(find_config(None))
    load_rules(settings.engine)
    app = build_app(settings, clock=lambda: NOW)
    try:
        app.loop.tick_once()  # the control loop records proposals and opens states
    finally:
        app.close()
    db = farm / "kelpie.db"

    def rows() -> tuple[int, int]:
        conn = sqlite3.connect(db)
        try:
            actions = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
            states = conn.execute("SELECT COUNT(*) FROM states").fetchone()[0]
        finally:
            conn.close()
        return actions, states

    before = rows()
    assert main(why("t_blocked", "--entries", "3")) == 0
    out = capsys.readouterr().out
    assert rows() == before
    states = out.split("Open alert states\n")[1].split("Ledger")[0]
    assert "stale_blocks" in states
    ledger = out.split("Ledger (latest first)\n")[1]
    assert "stale_blocks alert observe" in ledger
    assert "blocked for 6h 01m" in out
    assert main(why("t_review", "--json")) == 0
    data = json.loads(capsys.readouterr().out)
    assert [s["rule"] for s in data["states"]] == ["orphan_status"]
    assert data["ledger"] and data["problems"] == []


def test_config_error_exits_2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "kelpie.toml").write_text("[engine.rules.nope]\n")
    assert main(why("t_1")) == 2


def test_failing_rule_and_mentions() -> None:
    class Broken:
        name = "orphan_status"

        def evaluate(self, snapshot: Snapshot) -> list[Action]:
            raise RuntimeError("boom")

    class Names:
        name = "stale_blocks"

        def evaluate(self, snapshot: Snapshot) -> list[Action]:
            return [
                Action(ActionKind.ALERT, "stale_blocks", "x", params={"ids": ["T-2", "T-1"]}),
                Action(ActionKind.ALERT, "stale_blocks", "y", params={"nested": {"a": "T-1"}}),
                Action(ActionKind.ALERT, "stale_blocks", "z", params={"n": 1}),
            ]

    loaded = LoadedRules([Broken(), Names()], [], [])  # type: ignore[list-item]
    findings, problems = evaluate_rules(loaded, Snapshot(now=NOW), "T-1")
    assert [f.action.reason for f in findings] == ["x", "y"]
    assert all(isinstance(f, Finding) for f in findings)
    assert problems == ["rule orphan_status failed: RuntimeError: boom"]
    assert not _mentions(3, "T-1") and _mentions({"T-1"}, "T-1")


def test_render_details() -> None:
    assert _age(timedelta(days=2, hours=3)) == "2d 3h" and _age(timedelta(minutes=5)) == "5m"
    task = Task("T-1", "t", Status.WAITING, NOW - timedelta(hours=1), owner_id="ghost")
    gone = Condition("c", ConditionKind.TASK_STATUS, "T-1", {"task": "T-9", "error": "not_found"},
                     checked_at=NOW, registered_at=NOW - timedelta(hours=1))  # fmt: skip
    snap = Snapshot(now=NOW, tasks=(task,), conditions=(gone,))
    from kelpie.rules._chains import BlockerGraph

    [chain] = BlockerGraph(snap).chains("T-1")
    assert _chain(chain, snap).startswith("DEAD END  T-1 -> T-9: is not on the board")


def fixture_rules(name: str) -> tuple[LoadedRules, Snapshot]:
    incident = load(FIXTURES / f"{name}.yaml")
    config = EngineConfig.from_mapping({"rules": {incident.rule: incident.config}})
    return load_rules(config), incident.snapshot


def test_stop_pattern_fixture_chain_to_the_dead_end() -> None:
    loaded, snapshot = fixture_rules("11-blocker-chain-dead-end")
    ex = explain_snapshot(loaded, snapshot, "T-1164")
    assert ex is not None
    text = render(ex)
    assert "DEAD END  T-1164 -> T-1163 -> T-1162 -> T-1161 -> T-1160: ended as cancelled" in text
    # Rule 4 alerts on T-1161 (the cancelled human task) and names T-1164 among the stuck.
    [finding] = [f for f in ex.findings if f.action.rule == "stale_blocks"]
    assert finding.action.task_id == "T-1161" and "T-1164" in finding.action.params["stuck_ids"]
    assert not [f for f in ex.findings if f.action.rule == "orphan_status"]
    # The chain to a task off the board is rule 3's.
    off_board = explain_snapshot(loaded, snapshot, "T-1172")
    assert off_board is not None
    [finding] = [f for f in off_board.findings if f.action.rule == "orphan_status"]
    assert finding.action.params["dead_end"] == "T-9999"
    assert explain_snapshot(loaded, snapshot, "T-0") is None


@pytest.mark.parametrize(
    ("name", "task_id", "pattern"),
    [
        ("11-review-nobody-driving", "T-1131", "review-undriven"),
        ("11-successful-run-no-next-action", "T-1144", "dependents-not-woken"),
        ("11-recovery-loop", "T-1124", "recovery-loop"),
    ],
)
def test_stop_pattern_fixtures_explain_the_task(name: str, task_id: str, pattern: str) -> None:
    loaded, snapshot = fixture_rules(name)
    ex = explain_snapshot(loaded, snapshot, task_id)
    assert ex is not None
    assert pattern in {f.action.params.get("pattern") for f in ex.findings}
    assert "orphan_status [observe] alert" in render(ex)


def test_render_sessions_conditions_and_modes(tmp_path: Path) -> None:
    hour = timedelta(hours=1)
    task = Task("T-1", "Ship it", Status.BLOCKED, NOW - 2 * hour, owner_id="ghost",
                block=Block("waiting_ci", NOW - 2 * hour, "missing"))  # fmt: skip
    waiting = Task("T-2", "Next", Status.WAITING, NOW - 2 * hour)
    sessions = (
        Session("S-1", "T-2", "dev", NOW - hour, last_heartbeat=NOW - timedelta(minutes=2)),
        Session("S-2", "T-2", "dev", NOW - 5 * hour, last_heartbeat=NOW - 4 * hour),
        Session(
            "S-3", "T-2", "dev", NOW - 6 * hour, ended_at=NOW - 5 * hour, exit_reason="crashed"
        ),
    )
    conditions = (
        Condition("a", ConditionKind.PR_MERGED, "T-2", {"terminal": True}, False, checked_at=NOW),
        Condition("b", ConditionKind.CI_STATUS, "T-2", {}, True),
        Condition("c", ConditionKind.TASK_STATUS, "T-2", {"task": "T-1", "error": "x"}),
    )
    snap = Snapshot(now=NOW, tasks=(task, waiting), sessions=sessions, conditions=conditions)
    config = EngineConfig.from_mapping({"rules": {"stale_blocks": {"mode": "act"}}})
    loaded = load_rules(config)
    first = explain_snapshot(loaded, snap, "T-1")
    assert first is not None
    text = render(first)
    assert "ghost (not in the owner directory)" in text
    assert "block    'waiting_ci' for 2h 00m, condition missing" in text
    assert "missing: named by the block, Kelpie cannot see it" in text
    second = explain_snapshot(loaded, snap, "T-2")
    assert second is not None
    text = render(second)
    assert "session  live" in text and "S-1 by dev: live" in text
    assert "S-2 by dev: silent for 4h 00m" in text
    assert "S-3 by dev: ended 5h 00m ago, no final status, crashed" in text
    assert "a [pr_merged]: unmet (can never be met), checked 0m ago" in text
    assert "b [ci_status]: met, never checked" in text
    assert "c [task_status]: unknown (error x, task T-1), never checked" in text
    data = as_json(second)
    assert sorted(s["live"] for s in data["sessions"]) == [False, False, True]
    assert _mode(Finding(Action(ActionKind.ALERT, "r", "x"), Mode.ACT), True) == "act, dry-run"
    in_memory = read_history(None, "T-1", [], 5)[2]
    assert in_memory == ["ledger in memory: no history outside kelpie serve"]
    junk = tmp_path / "junk.db"
    junk.write_text("not a database")
    assert read_history(junk, "T-1", ["s"], 5)[2][0].startswith("history left out")


def test_adapter_failure_is_not_a_missing_task(
    farm: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (farm / "kanban.db").write_bytes(b"not a database" * 100)
    assert main(why("t_review")) == 3
    err = capsys.readouterr().err
    assert "could not read its board" in err and "adapter hermes failed" in err
    assert main(why("t_review", "--json")) == 3
    data = json.loads(capsys.readouterr().out)
    assert data["error"] == "adapter_failed" and data["problems"][0].startswith("adapter hermes")


GITHUB = """
[connectors.github]
repos = ["example/repo"]
"""


def test_offline_by_default_refresh_reads_github(
    farm: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    with (farm / "kelpie.toml").open("a") as f:
        f.write(GITHUB)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    calls: list[bool] = []

    def github_steps(*args: object) -> tuple[list, list]:
        calls.append(True)
        return [], []

    monkeypatch.setattr("kelpie.app._github_steps", github_steps)
    assert main(why("t_review")) == 0
    out = capsys.readouterr().out
    assert calls == [] and "GitHub conditions were not read (offline)" in out
    assert main(why("t_review", "--refresh")) == 0
    out = capsys.readouterr().out
    assert calls == [True] and "offline" not in out


def test_labels_holds_and_session_cap() -> None:
    hour = timedelta(hours=1)
    task = Task("T-1", "Parked", Status.REVIEW, NOW - 3 * hour, owner_id="dev",
                labels=("merge-first", "ui"))  # fmt: skip
    sessions = tuple(
        Session(
            f"S-{i}",
            "T-1",
            "dev",
            NOW - (10 - i) * hour,
            ended_at=NOW - (9 - i) * hour,
            final_status=Status.REVIEW,
        )
        for i in range(5)
    )
    snap = Snapshot(now=NOW, tasks=(task,), sessions=sessions)
    rules = {"orphan_status": {"hold_labels": ["merge-first"], "reviewer_roles": ["reviewer"]}}
    loaded = load_rules(EngineConfig.from_mapping({"rules": rules}))
    ex = explain_snapshot(loaded, snap, "T-1")
    assert isinstance(ex, Explanation)
    text = render(ex)
    assert "labels   merge-first, ui (held: merge-first; stop patterns skip it)" in text
    assert "S-4 by dev" in text and "S-1 by dev" not in text and "... and 2 older" in text
    data = as_json(ex)
    assert data["task"]["held_by"] == ["merge-first"] and data["sessions_total"] == 5
    assert [s["id"] for s in data["sessions"]] == ["S-4", "S-3", "S-2", "S-1", "S-0"]
