"""``kelpie why <task>`` over a synthetic Hermes board, set up like the smoke test."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from kelpie.app import LoadedRules, Settings, build_app, find_config, load_rules, load_settings
from kelpie.cli import main
from kelpie.connectors.github.blocks import load_block_conditions
from kelpie.connectors.github.settings import GitHubSettings
from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Dependency,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.why import (
    Expected,
    Explanation,
    Finding,
    _age,
    _chain,
    _condition,
    _expected,
    _mentions,
    _mode,
    _offline_note,
    _text,
    as_json,
    evaluate_rules,
    expected_conditions,
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
    assert "owner    none, routed to dev (agent, role dev)" in text and "none live" in text
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
    # Rule 3 names the cause; rule 6 also sees routed review work wait while dev is idle.
    assert [s["rule"] for s in data["states"]] == ["orphan_status", "bottlenecks"]
    assert data["ledger"] and data["problems"] == []
    assert data["task"]["owner_id"] is None and data["task"]["assignee"] == "dev"


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


def test_a_card_parked_before_work_says_what_would_act() -> None:
    """Live review: on a Hermes card in scheduled, kelpie why said only "no rule acts"."""
    loaded, snapshot = fixture_rules("17-prework-not-backlog")
    lone = explain_snapshot(loaded, snapshot, "t_c210")
    assert lone is not None and not lone.findings
    text = render(lone)
    assert "status   backlog (hermes: scheduled, before work) for 1d 1h" in text
    assert "'scheduled' is before work: only a person moves the card out of it" in text
    assert "only while open tasks depend on it; none does" in text
    data = as_json(lone)["task"]
    assert data["platform_status"] == "scheduled" and data["prework"] is True
    stuck = explain_snapshot(loaded, snapshot, "t_c200")
    assert stuck is not None
    assert "stuck-before-work" in {f.action.params.get("pattern") for f in stuck.findings}
    assert "before work: only" not in render(stuck)  # a rule acts: no hint


def test_hints_for_young_cards_rules_off_and_waits() -> None:
    from kelpie.why import _hints

    card = Task("C", "c", Status.BACKLOG, NOW - timedelta(hours=1), prework=True)
    behind = Task("D", "d", Status.BACKLOG, NOW)
    snap = Snapshot(now=NOW, tasks=(card, behind), dependencies=(Dependency("D", "C"),))
    on = frozenset({"orphan_status"})

    def ex(task: Task, after: timedelta | None, rules: frozenset[str], conds=()) -> Explanation:
        return Explanation(task, snap, timedelta(minutes=30), list(conds), None, [], {}, [], [],
                           [], True, prework_after=after, loaded_rules=rules)  # fmt: skip

    hints = _hints(ex(card, timedelta(hours=4), on))
    assert hints[0].startswith("a pre-work status is before work")
    assert hints[1] == "orphan_status alerts once it has sat there 4h 00m"
    assert "skips it" in _hints(ex(replace(card, status_since=NOW - timedelta(hours=5)),
                                   timedelta(hours=4), on))[1]  # fmt: skip
    assert "is off or the rule is not loaded" in _hints(ex(card, None, on))[1]
    waiting = Task("W", "w", Status.WAITING, NOW)
    unmet = Condition("c1", ConditionKind.PR_MERGED, "W")
    [hint] = _hints(ex(waiting, None, frozenset(), [unmet]))
    assert hint.startswith("waits on 1 unmet condition(s)") and "not loaded" in hint
    assert _hints(ex(waiting, None, frozenset({"wait_condition"}), [unmet]))[0].endswith("met")
    assert _hints(ex(Task("R", "r", Status.READY, NOW), None, on)) == []


def test_a_card_parked_by_kelpie_is_not_shown_as_before_work() -> None:
    parked = Task("W", "w", Status.WAITING, NOW - timedelta(hours=2), source="hermes",
                  platform_status="scheduled")  # fmt: skip
    unmet = Condition("c1", ConditionKind.PR_MERGED, "W")
    snap = Snapshot(now=NOW, tasks=(parked,), conditions=(unmet,))
    loaded = LoadedRules([], [], [])
    ex = explain_snapshot(loaded, snap, "W")
    assert ex is not None
    text = render(ex)
    assert "status   waiting (hermes: scheduled) for 2h 00m" in text
    assert "before work" not in text and "waits on 1 unmet condition(s)" in text


def test_a_young_card_waiting_for_its_own_parent_says_so() -> None:
    from kelpie.why import _hints

    card = Task("C", "c", Status.BACKLOG, NOW - timedelta(hours=1), prework=True)
    parent = Task("P", "p", Status.IN_PROGRESS, NOW)
    behind = Task("D", "d", Status.BACKLOG, NOW)
    deps = (Dependency("D", "C"), Dependency("C", "P"))
    snap = Snapshot(now=NOW, tasks=(card, parent, behind), dependencies=deps)
    ex = Explanation(card, snap, timedelta(minutes=30), [], None, [], {}, [], [], [], True,
                     prework_after=timedelta(hours=4),
                     loaded_rules=frozenset({"orphan_status"}))  # fmt: skip
    assert _hints(ex)[1] == "orphan_status leaves it: it waits for parents of its own (P)"


# -- block conditions (#126) -------------------------------------------------------------------


def _bc_settings(*patterns: dict[str, object], enabled: bool = True) -> Settings:
    github = GitHubSettings(repos=("example/repo",), enabled=enabled)
    return SimpleNamespace(  # type: ignore[return-value]
        github=github, block_conditions=load_block_conditions(list(patterns), github.repos)
    )


MERGE = {"pattern": r"merge of (?P<pr>\S+)", "kind": "pr_merged", "repo": "example/repo"}


def _blocked(reason: str, status: Status = Status.BLOCKED, title: str = "t") -> Snapshot:
    block = Block(reason, NOW - timedelta(hours=1)) if status is Status.BLOCKED else None
    task = Task("T-1", title, status, NOW - timedelta(hours=1), block=block)
    return Snapshot(now=NOW, tasks=(task,))


def test_expected_offline_names_the_pr_without_the_network() -> None:
    snap, settings = _blocked("waiting for merge of #57"), _bc_settings(MERGE)
    [e], n = expected_conditions(settings, snap, snap.tasks[0], refreshed=False)
    assert n == 1
    assert (e.status, e.repo, e.pr, e.kind) == ("not_checked", "example/repo", 57, "pr_merged")
    assert _expected(e) == (
        "would attach pr_merged on example/repo#57 (from the block reason): "
        "not checked (offline; --refresh checks them)"
    )
    [e], _ = expected_conditions(settings, snap, snap.tasks[0], refreshed=True)
    assert e.status == "not_attached" and _expected(e).startswith("matched pr_merged on")


def test_expected_says_why_a_hit_attaches_nothing_and_escapes_agent_text() -> None:
    settings = _bc_settings(MERGE)
    snap = _blocked("merge of other/repo#5")
    [e], _ = expected_conditions(settings, snap, snap.tasks[0], False)
    assert e.status == "unresolvable" and "outside [connectors.github] repos" in _expected(e)
    snap = _blocked("merge of \x1b[31m\x07oops")
    [e], _ = expected_conditions(settings, snap, snap.tasks[0], False)
    line = _expected(e)
    assert "\x1b" not in line and "\x07" not in line and "\\x1b[31m\\x07oops" in line
    assert line.endswith("nothing is attached") and e.captured.startswith("\x1b")


def test_expected_none_when_nothing_applies() -> None:
    snap = _blocked("stuck")
    assert expected_conditions(_bc_settings(MERGE), snap, snap.tasks[0], False) == ([], 1)
    off = _bc_settings(MERGE, enabled=False)
    assert expected_conditions(off, snap, snap.tasks[0], False) == ([], 0)
    nothing = SimpleNamespace(github=None, block_conditions=())
    assert expected_conditions(nothing, snap, snap.tasks[0], False) == ([], 0)  # type: ignore[arg-type]
    # A block that already has its condition is not matched again.
    cond = Condition("block:T-1:pr_merged:example/repo#57", ConditionKind.PR_MERGED, "T-1")
    task = Task(
        "T-1", "t", Status.BLOCKED, NOW, block=Block("merge of #57", NOW, condition_id=cond.id)
    )
    held = Snapshot(now=NOW, tasks=(task,), conditions=(cond,))
    assert expected_conditions(_bc_settings(MERGE), held, task, False) == ([], 0)


def test_expected_reads_a_ready_task_title_for_the_gate() -> None:
    gate = {**MERGE, "applies_to": ["ready_task"]}
    snap = _blocked("", Status.READY, title="Redo it after merge of #7")
    [e], n = expected_conditions(_bc_settings(gate), snap, snap.tasks[0], False)
    assert n == 1 and e.source == "task_text" and e.pr == 7
    assert "ready task title" in _expected(e)


def test_render_conditions_with_target_and_offline_wording() -> None:
    task = Task("T-1", "t", Status.WAITING, NOW - timedelta(hours=1))
    unchecked = Condition("a", ConditionKind.PR_MERGED, "T-1", {"repo": "example/repo", "pr": 9})
    checked = Condition(
        "b", ConditionKind.CI_STATUS, "T-1",
        {"repo": "example/repo", "pr": 9, "sha": "0123456789abcdef0123", "ci_state": "pending"},
        satisfied=False, checked_at=NOW - timedelta(minutes=3),
    )  # fmt: skip
    snap = Snapshot(now=NOW, tasks=(task,), conditions=(unchecked, checked))
    ex = explain_snapshot(LoadedRules([], [], []), snap, "T-1")
    assert ex is not None
    offline = "a [pr_merged] example/repo#9: unknown, not checked (offline; --refresh checks it)"
    assert offline in render(ex)
    ex.refreshed = True
    text = render(ex)
    assert "a [pr_merged] example/repo#9: unknown, never checked" in text
    assert "b [ci_status] example/repo#9 @ 0123456789ab: unmet (CI pending), checked 3m ago" in text
    target = as_json(ex)["conditions"][1]["target"]
    assert target == {"repo": "example/repo", "pr": 9, "sha": "0123456789abcdef0123"}


def test_none_names_the_patterns_it_checked() -> None:
    snap = _blocked("stuck")
    ex = explain_snapshot(LoadedRules([], [], []), snap, "T-1")
    assert ex is not None
    ex.block_patterns = 2
    expected = "none (the block reason matches none of the 2 [[block_conditions]] patterns)"
    assert expected in render(ex)


# -- review follow-ups for #126 --------------------------------------------------------------


GITHUB_CONFIG = """
[connectors.github]
repos = ["example/repo"]

[[block_conditions]]
pattern = 'merge of (?P<pr>\\S+)'
kind = "pr_merged"
repo = "example/repo"
"""


@pytest.fixture
def pr_farm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.delenv("KELPIE_CONFIG", raising=False)
    monkeypatch.delenv("KELPIE_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    board = Board(tmp_path / "kanban.db")
    board.task("t_pr", "blocked", ago(days=1), assignee="dev")
    board.event("t_pr", "claimed", ago(hours=7))
    board.run("t_pr", "dev", ago(hours=7), status="blocked", ended=ago(hours=6),
              outcome="blocked")  # fmt: skip
    board.event("t_pr", "blocked", ago(hours=6), {"reason": "waiting for merge of #5"})
    board.close()
    config = CONFIG.format(db=json.dumps(str(tmp_path / "kanban.db"))) + GITHUB_CONFIG
    (tmp_path / "kelpie.toml").write_text(config)
    return tmp_path


def test_offline_rule_verdicts_on_a_block_carry_a_note(
    pr_farm: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = load_settings(find_config(None))
    ex = explain(settings, "t_pr", clock=lambda: NOW)
    assert isinstance(ex, Explanation)
    noted = [f for f in ex.findings if f.note]
    assert {f.action.rule for f in noted} >= {"stale_blocks"}
    assert {f.action.rule for f in noted} <= {"exit_contract", "stale_blocks"}
    want = (
        "offline: serve treats this block as a wait on example/repo#5 once GitHub confirms "
        "the PR; --refresh to verify"
    )
    assert all(f.note == want for f in noted)
    text = render(ex)
    assert f"    note: {want}" in text
    assert "would attach pr_merged on example/repo#5" in text
    data = as_json(ex)
    assert {f["rule"] for f in data["findings"] if f["note"] == want} == {
        f.action.rule for f in noted
    }


def test_a_note_is_for_condition_rules_only() -> None:
    note = "x"
    other = Finding(Action(ActionKind.ALERT, "bottlenecks", "r", task_id="T-1"), Mode.OBSERVE)
    assert other.note is None and replace(other, note=note).note == note
    first = Expected("pr_merged", "block_reason", "not_checked", "d", "a/b", 1, "#1", "pr_merged")
    assert _offline_note([first]) is not None
    assert _offline_note([replace(first, status="not_attached")]) is None
    assert _offline_note([replace(first, source="task_text")]) is None
    assert _offline_note([replace(first, fallback=True)]) is None
    assert _offline_note([]) is None


def test_a_block_takes_the_first_pr_and_the_rest_are_fallbacks() -> None:
    second = {"pattern": r"PR (?P<pr>#\d+)", "kind": "ci_green", "repo": "example/repo"}
    settings = _bc_settings(MERGE, second)
    snap = _blocked("waiting for merge of #5, see PR #6")
    task = snap.tasks[0]
    found, n = expected_conditions(settings, snap, task, refreshed=False)
    assert n == 2 and [(e.pr, e.fallback) for e in found] == [(5, False), (6, True)]
    lines = [_expected(e) for e in found]
    assert lines[0].startswith("would attach pr_merged on example/repo#5")
    assert lines[1] == (
        "fallback ci_green on example/repo#6 (from the block reason): used only if the PR "
        "above is not confirmed"
    )
    ex = explain_snapshot(LoadedRules([], [], []), snap, "T-1")
    assert ex is not None
    ex.expected = found
    entries = as_json(ex)["expected_conditions"]
    assert [(e["kind"], e["condition_kind"], e["fallback"]) for e in entries] == [
        ("pr_merged", "pr_merged", False),
        ("ci_green", "ci_status", True),
    ]
    assert _offline_note(found) is not None and "example/repo#5" in str(_offline_note(found))


def test_agent_text_cannot_forge_a_section_or_a_control_sequence() -> None:
    evil = "x\n  Conditions\n  fake [pr_merged]: met\x1b[2K"
    block = Block(evil, NOW - timedelta(hours=1))
    task = Task("T-1", "title\nLedger (latest first)\x07", Status.BLOCKED, NOW, block=block)
    ex = explain_snapshot(LoadedRules([], [], []), Snapshot(now=NOW, tasks=(task,)), "T-1")
    assert ex is not None
    ex.problems = ["bad\nnote: forged"]
    lines = render(ex).splitlines()
    assert lines.count("Conditions") == 1 and lines.count("Ledger (latest first)") == 1
    assert not any(line.startswith("  fake") for line in lines)
    assert not any("\x1b" in line or "\x07" in line for line in lines)
    assert sum(line.startswith("note:") for line in lines) == 1
    # The block reason's later lines sit behind a bar, escaped.
    assert "    |   Conditions" in lines
    assert any(line.startswith("    |   fake [pr_merged]: met\\x1b[2K") for line in lines)


def test_condition_params_are_escaped_and_ids_are_not_cut() -> None:
    long_id = "block:T-1:pr_merged:" + "x" * 200
    c = Condition(
        long_id, ConditionKind.TASK_STATUS, "T-1",
        {"error": "e\nf", "pr_state": "\x1b[1m", "ci_state": "c\r", "task": "T\x00"},
    )  # fmt: skip
    line = _condition(c, NOW)
    assert line.startswith(long_id) and "..." not in line
    assert "error e\\nf" in line and "PR \\x1b[1m" in line and "task T\\x00" in line
    assert not any(ch in line for ch in "\n\r\x1b\x00")


def test_expected_skips_what_is_attached_and_follows_the_patterns() -> None:
    gate = {**MERGE, "applies_to": ["ready_task"]}
    ready = _blocked("", Status.READY, title="Redo it after merge of #7")
    task = ready.tasks[0]
    held = Condition("ready:T-1:pr_merged:example/repo#7", ConditionKind.PR_MERGED, "T-1")
    registered = replace(ready, conditions=(held,))
    assert expected_conditions(_bc_settings(gate), registered, task, False) == ([], 1)
    # Patterns that read blocks only never look at a ready task's title.
    assert expected_conditions(_bc_settings(MERGE), ready, task, False) == ([], 0)
    many = _blocked("", Status.READY, title=" ".join(f"merge of #{i}" for i in range(1, 9)))
    found, _ = expected_conditions(_bc_settings(gate), many, many.tasks[0], False)
    assert [e.pr for e in found] == [1, 2, 3, 4, 5]  # MAX_READY_REFS; the rest are ignored
    assert not any(e.fallback for e in found)


# -- second review pass ------------------------------------------------------------------------


def _ci(pattern: str = r"merge of (?P<pr>\S+)") -> dict[str, object]:
    return {"pattern": pattern, "kind": "ci_green", "repo": "example/repo"}


def test_fallback_only_while_the_first_pr_is_unknown() -> None:
    other = {"pattern": r"PR (?P<pr>#\d+)", "kind": "ci_green", "repo": "example/repo"}
    third = {"pattern": r"also (?P<pr>#\d+)", "kind": "pr_merged", "repo": "example/repo"}
    snap = _blocked("merge of #5, PR #6, also #7")
    task = snap.tasks[0]
    # Offline the first PR is not confirmed: later ones are fallbacks ("the PRs above").
    found, _ = expected_conditions(_bc_settings(MERGE, other), snap, task, refreshed=False)
    assert [e.detail for e in found][1] == "used only if the PR above is not confirmed"
    found, _ = expected_conditions(_bc_settings(MERGE, other, third), snap, task, False)
    assert [e.fallback for e in found] == [False, True, True]
    assert found[2].detail == "used only if the PRs above are not confirmed"
    # After --refresh no PR was confirmed, so none is a fallback: each is simply unconfirmed.
    found, _ = expected_conditions(_bc_settings(MERGE, other), snap, task, refreshed=True)
    assert [(e.status, e.fallback) for e in found] == [("not_attached", False)] * 2
    assert all(_expected(e).startswith("matched ") for e in found)
    assert all("GitHub did not confirm the PR" in _expected(e) for e in found)
    ex = explain_snapshot(LoadedRules([], [], []), snap, "T-1")
    assert ex is not None
    ex.expected, ex.refreshed = found, True
    assert all(f["fallback"] is False for f in as_json(ex)["expected_conditions"])


def test_the_same_pr_as_another_kind_is_never_a_fallback() -> None:
    snap = _blocked("waiting for merge of #5")
    found, n = expected_conditions(_bc_settings(MERGE, _ci()), snap, snap.tasks[0], False)
    assert n == 2
    assert [(e.kind, e.fallback, e.same_pr) for e in found] == [
        ("pr_merged", False, False),
        ("ci_green", False, True),
    ]
    assert _expected(found[1]) == (
        "unused ci_green on example/repo#5 (from the block reason): not used: same PR as above"
    )
    ex = explain_snapshot(LoadedRules([], [], []), snap, "T-1")
    assert ex is not None
    ex.expected = found
    assert [e["same_pr"] for e in as_json(ex)["expected_conditions"]] == [False, True]
    assert _offline_note(found) is not None
    refreshed, _ = expected_conditions(_bc_settings(MERGE, _ci()), snap, snap.tasks[0], True)
    assert [e.same_pr for e in refreshed] == [False, True]


def test_rule_reasons_keep_their_paragraphs() -> None:
    snap = _blocked("stuck")
    ex = explain_snapshot(LoadedRules([], [], []), snap, "T-1")
    assert ex is not None
    reason = "First paragraph.\n\nSecond \x1b[2Kparagraph.\n   \nThird."
    action = Action(ActionKind.ALERT, "checkpoints", reason, task_id="T-1")
    ex.findings = [Finding(action, Mode.OBSERVE)]
    lines = render(ex).splitlines()
    i = next(i for i, line in enumerate(lines) if line.startswith("  checkpoints"))
    assert lines[i].endswith("First paragraph.")
    assert lines[i + 1 : i + 4] == [
        "    | Second \\x1b[2Kparagraph.",
        "    | Third.",
        "Open alert states",
    ]
    assert "\\n" not in "\n".join(lines)


def test_text_escapes_controls_but_keeps_emoji_joiners_and_spaces() -> None:
    zwj = "\N{ZERO WIDTH JOINER}"
    family = f"\N{MAN}{zwj}\N{WOMAN}{zwj}\N{GIRL}"
    spaces = "a\N{NO-BREAK SPACE}b\N{NARROW NO-BREAK SPACE}c\N{IDEOGRAPHIC SPACE}d"
    assert _text(family) == family and _text(spaces) == spaces
    assert _text("\N{ZERO WIDTH NON-JOINER}") == "\N{ZERO WIDTH NON-JOINER}"
    # Escaped: C0/C1 controls, bidi controls, line and paragraph separators.
    bad_chars = [
        "\x00",
        "\x1b",
        "\x7f",
        "\x85",
        "\t",
        "\N{RIGHT-TO-LEFT OVERRIDE}",
        "\N{LEFT-TO-RIGHT EMBEDDING}",
        "\N{LEFT-TO-RIGHT ISOLATE}",
        "\N{POP DIRECTIONAL ISOLATE}",
        "\N{LEFT-TO-RIGHT MARK}",
        "\N{RIGHT-TO-LEFT MARK}",
        "\N{ARABIC LETTER MARK}",
        "\N{LINE SEPARATOR}",
        "\N{PARAGRAPH SEPARATOR}",
        "\N{ZERO WIDTH NO-BREAK SPACE}",
    ]
    for bad in bad_chars:
        out = _text(f"a{bad}b")
        assert bad not in out and out.startswith("a\\") and out.endswith("b"), repr(bad)
    assert _text("x\N{RIGHT-TO-LEFT OVERRIDE}y") == "x\\u202ey"
