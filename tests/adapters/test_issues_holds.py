"""Holds on issue trackers (rule 6): pause labels and states read as pauses, GitHub's lock
as a flag, and ``lift_hold`` taking pause labels off in act mode (opt-in)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.issues.adapter import MAX_LIFTS, IssuesAdapter, note
from kelpie.adapters.issues.config import load_github_issues, load_linear
from kelpie.adapters.issues.mapping import PAUSE, Agent, Board, IssuesError, Item, map_items
from kelpie.app import act_blockers, build_adapters, load_settings
from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.executors import PlatformExecutor
from kelpie.core.model import Snapshot, Status, Task
from kelpie.core.ports import (
    Deferred,
    NothingToDo,
    SupportsLiftHold,
    UnsupportedAction,
    lifts_holds,
)
from kelpie.rules.bottlenecks import Hold, is_paused, with_hold_marks
from tests.adapters.test_issues_actions import ISSUE, TASK, Pacing, gh, pacing, writes  # noqa: F401
from tests.adapters.test_issues_actions import adapter as http_adapter
from tests.adapters.test_issues_mapping import NOW, REPO, ago, item
from tests.connectors.fake_github import FakeGitHub

PAUSED = Board(REPO, pause_labels=("on-hold",))
TEAM = Board("ENG", pause_labels=("on-hold",), pause_states=("Paused",))


def task(it: Item, board: Board = PAUSED) -> Task:
    (t,) = map_items([it], {board.key: board}, {}, "github-issues").tasks
    return t


# --- reading ------------------------------------------------------------------------------


def test_a_pause_label_is_a_pause_block_since_it_was_put_on() -> None:
    put_on = ago(hours=6)
    labelled = item(labels=("bug", "On-Hold"), events=(_labeled("on-hold", put_on),))
    t = task(labelled)
    assert t.status is Status.BLOCKED and t.status_since == put_on
    assert t.block is not None and t.block.category == PAUSE
    assert t.block.reason == "paused: label 'on-hold'" and t.block.since == put_on
    assert t.platform_status == "open, on-hold"
    assert is_paused(t)  # rule 6's pause holds match it, with their default pattern too


def test_a_blocked_label_wins_over_a_pause_label() -> None:
    t = task(item(labels=("on-hold", "blocked")))
    assert t.block is not None and t.block.category is None
    assert t.block.reason == "label 'blocked'" and not is_paused(t)


def test_without_pause_labels_nothing_is_a_pause() -> None:
    t = task(item(labels=("on-hold",)), Board(REPO))
    assert t.status is Status.BACKLOG and t.block is None


def test_a_closed_issue_with_a_pause_label_is_done() -> None:
    closed = item(open=False, closed_reason="completed", closed_at=ago(hours=1))
    assert task(replace(closed, labels=("on-hold",))).status is Status.DONE


def test_a_pause_state_is_a_pause_block() -> None:
    it = replace(item(state="paused", board="ENG"), id="ENG-1", seen_since=ago(hours=3))
    t = task(it, TEAM)
    assert t.status is Status.BLOCKED and t.status_since == ago(hours=3)  # first seen so
    assert t.block is not None and t.block.category == PAUSE
    assert t.block.reason == "paused: state 'paused'"
    other = task(replace(it, state="In Progress", started=True), TEAM)
    assert other.status is Status.IN_PROGRESS and other.block is None


def test_github_lock_is_a_flag(gh: FakeGitHub, pacing: Pacing) -> None:  # noqa: F811
    a = http_adapter(gh, pacing, locked=True)
    (t,) = a.snapshot(NOW).tasks
    assert t.flags == ("locked",) and t.labels == ()
    assert task(item()).flags == ()


def test_rule_6_holds_issue_tasks_by_pause_and_flag() -> None:
    paused, locked = task(item(1, labels=("on-hold",))), task(item(2, flags=("locked",)))
    snap = Snapshot(now=NOW, tasks=(paused, locked))
    marked = with_hold_marks(snap, (Hold("pause", kind="pause"), Hold("locked", kind="flag")))
    assert [t.labels for t in marked.tasks] == [("on-hold", "pause"), ("locked",)]


# --- lifting ------------------------------------------------------------------------------


@dataclass
class Tracker:
    """Issues in memory, and the writes made to them."""

    items: dict[str, Item]
    writes: list[tuple[str, str, Any]] = field(default_factory=list)
    reserved: list[int] = field(default_factory=list)

    def read(self, now: Any) -> list[Item]:
        return list(self.items.values())

    def fetch(self, item_id: str) -> Item:
        return self.items[item_id]

    def remove_labels(self, item_id: str, labels: Sequence[str]) -> None:
        self.writes.append(("unlabel", item_id, list(labels)))
        drop = {label.casefold() for label in labels}
        it = self.items[item_id]
        self.items[item_id] = replace(
            it, labels=tuple(x for x in it.labels if x.casefold() not in drop)
        )

    def comment(self, item_id: str, body: str) -> None:
        self.writes.append(("comment", item_id, body))

    def reserve(self, writes: int, agents: Sequence[Agent] = ()) -> None:
        self.reserved.append(writes)

    def add_labels(self, item_id: str, labels: Sequence[str]) -> None:  # pragma: no cover
        raise AssertionError

    assign = unassign = add_labels  # type: ignore[assignment]

    def reopen(self, item_id: str) -> None:  # pragma: no cover
        raise AssertionError


def board_of(
    *items: Item, lift: bool = True, board: Board = PAUSED
) -> tuple[IssuesAdapter, Tracker]:
    tracker = Tracker({i.id: i for i in items})
    a = IssuesAdapter(tracker, (board,), {}, lift_holds=lift, clock=lambda: NOW)
    a.snapshot(NOW)
    return a, tracker


def lift(hold: str = "on-hold", kind: str = "pause", held: list[str] | None = None) -> Action:
    return Action(
        ActionKind.LIFT_HOLD,
        "bottlenecks",
        f"Hold '{hold}' is past its expected 4h: lift it.",
        executor="github-issues",
        params={"hold": hold, "hold_kind": kind, "lift_prs": [], "held_tasks": held or []},
    )


def said(task: str, reason: str, label: str = "on-hold") -> tuple[str, str, str]:
    quoted = f"Removed: {label}\n{reason}"
    return ("comment", task, note("Hold 'on-hold' lifted: its label came off.", quoted))


def test_lifting_takes_the_holds_own_label_off_each_held_task() -> None:
    a, tracker = board_of(
        item(1, labels=("bug", "on-hold", "freeze")), item(2, labels=("On-Hold",))
    )
    assert isinstance(a, SupportsLiftHold) and lifts_holds(a)
    action = lift(held=[f"{REPO}#1", f"{REPO}#2", "t_other"])
    done = PlatformExecutor(a).execute(action)
    assert done == f"label 'on-hold' off {REPO}#1, {REPO}#2"
    assert tracker.writes == [
        ("unlabel", f"{REPO}#1", ["on-hold"]),  # never another pause ("freeze")
        said(f"{REPO}#1", action.reason),
        ("unlabel", f"{REPO}#2", ["On-Hold"]),
        said(f"{REPO}#2", action.reason, "On-Hold"),
    ]
    assert tracker.reserved == [2, 2]  # each task's writes held before its first


def test_a_second_pause_label_is_left_for_its_own_hold() -> None:
    board = Board(REPO, pause_labels=("on-hold", "freeze"))
    a, tracker = board_of(
        item(1, labels=("freeze",)), item(2, labels=("freeze", "on-hold")), board=board
    )
    with pytest.raises(NothingToDo, match="carries its label"):
        a.lift_hold("on-hold", "pause", [f"{REPO}#1"], "r")
    assert a.lift_hold("on-hold", "pause", [f"{REPO}#1", f"{REPO}#2"], "r") == (
        f"label 'on-hold' off {REPO}#2"
    )
    assert tracker.items[f"{REPO}#2"].labels == ("freeze",)


def test_a_hold_named_after_no_pause_label_is_left_to_a_person() -> None:
    a, tracker = board_of(item(1, labels=("on-hold",)))
    with pytest.raises(UnsupportedAction, match="no pause label"):
        a.lift_hold("pause", "pause", [f"{REPO}#1"], "r")
    assert tracker.writes == []


def test_lifting_is_off_by_default() -> None:
    a, tracker = board_of(item(1, labels=("on-hold",)), lift=False)
    assert not lifts_holds(a)
    with pytest.raises(UnsupportedAction, match="allow_lift_holds is off"):
        a.lift_hold("on-hold", "pause", [f"{REPO}#1"], "r")
    assert PlatformExecutor(a).execute(lift(held=[f"{REPO}#1"])) is False  # a person is told
    assert tracker.writes == []


@pytest.mark.parametrize("kind", ["flag", "label"])
def test_only_pauses_are_lifted(kind: str) -> None:
    a, tracker = board_of(item(1, labels=("on-hold",), flags=("locked",)))
    with pytest.raises(UnsupportedAction, match="pause holds only"):
        a.lift_hold("locked", kind, [f"{REPO}#1"], "r")
    assert tracker.writes == []


def test_a_hold_on_no_task_of_this_board_is_refused() -> None:
    a, tracker = board_of(item(1, labels=("on-hold",)))
    with pytest.raises(UnsupportedAction, match="no task of github-issues"):
        a.lift_hold("on-hold", "pause", ["t_1a", f"{REPO}#9"], "r")
    assert tracker.writes == []


def test_a_pause_lifted_meanwhile_is_nothing_to_do() -> None:
    a, tracker = board_of(item(1, labels=("on-hold",)))
    tracker.items[f"{REPO}#1"] = item(1, labels=("bug",))  # a person took it off
    with pytest.raises(NothingToDo, match="carries its label"):
        a.lift_hold("on-hold", "pause", [f"{REPO}#1"], "r")
    assert tracker.writes == []


def test_closed_blocked_or_unreadable_tasks_are_left_alone() -> None:
    a, tracker = board_of(*(item(n, labels=("on-hold",)) for n in (1, 2, 3, 4)))
    tracker.items[f"{REPO}#1"] = item(1, labels=("on-hold", "blocked"))
    tracker.items[f"{REPO}#2"] = item(2, labels=("on-hold",), open=False, closed_reason="completed")
    tracker.items[f"{REPO}#3"] = item(3, labels=("on-hold",), board="other/repo")
    held = [f"{REPO}#{n}" for n in (1, 2, 3, 4)]
    assert a.lift_hold("on-hold", "pause", held, "r") == (
        f"label 'on-hold' off {REPO}#4; 1 left to a person"
    )
    assert [w[:2] for w in tracker.writes] == [("unlabel", f"{REPO}#4"), ("comment", f"{REPO}#4")]


def test_a_pause_state_is_left_to_a_person() -> None:
    paused = replace(item(1, board="ENG", state="Paused"), id="ENG-1")
    labelled = replace(item(2, board="ENG", labels=("on-hold",)), id="ENG-2")
    a, tracker = board_of(paused, board=TEAM)
    with pytest.raises(NothingToDo):  # the state is no label to take off
        a.lift_hold("on-hold", "pause", ["ENG-1"], "r")
    with pytest.raises(UnsupportedAction, match="no pause label"):
        a.lift_hold("Paused", "pause", ["ENG-1"], "r")
    assert tracker.writes == []
    a, tracker = board_of(paused, labelled, board=TEAM)
    assert a.lift_hold("on-hold", "pause", ["ENG-1", "ENG-2"], "r") == "label 'on-hold' off ENG-2"


def test_at_most_a_few_tasks_are_lifted_and_the_rest_are_said() -> None:
    items = [item(n, labels=("on-hold",)) for n in range(1, 8)]
    a, tracker = board_of(*items)
    done = a.lift_hold("on-hold", "pause", [i.id for i in items], "r")
    assert (
        done
        == f"label 'on-hold' off {', '.join(i.id for i in items[:MAX_LIFTS])}; 3 left to a person"
    )
    assert len([w for w in tracker.writes if w[0] == "unlabel"]) == MAX_LIFTS


def test_a_cap_reached_midway_keeps_what_was_lifted() -> None:
    a, tracker = board_of(item(1, labels=("on-hold",)), item(2, labels=("on-hold",)))
    calls = iter([None])

    def reserve(writes: int, agents: Sequence[Agent] = ()) -> None:
        if next(calls, "full") == "full":
            raise Deferred("cap")

    tracker.reserve = reserve  # type: ignore[method-assign]
    assert a.lift_hold("on-hold", "pause", [f"{REPO}#1", f"{REPO}#2"], "r") == (
        f"label 'on-hold' off {REPO}#1; 1 left to a person"
    )
    calls = iter([])
    with pytest.raises(Deferred):  # nothing lifted yet: deferred whole
        a.lift_hold("on-hold", "pause", [f"{REPO}#2"], "r")


def test_a_failure_after_a_lift_says_what_was_lifted() -> None:
    a, tracker = board_of(item(1, labels=("on-hold",)), item(2, labels=("on-hold",)))
    real = tracker.remove_labels

    def remove(item_id: str, labels: Sequence[str]) -> None:
        if item_id.endswith("#2"):
            raise IssuesError("boom", "GitHub refused the change (http_error)")
        real(item_id, labels)

    tracker.remove_labels = remove  # type: ignore[method-assign]
    with pytest.raises(IssuesError) as err:
        a.lift_hold("on-hold", "pause", [f"{REPO}#1", f"{REPO}#2"], "r")
    assert (
        err.value.public_reason
        == "hold 'on-hold' lifted on acme/app#1 only; the rest is tried again"
    )


def test_lift_over_http_deletes_the_label(gh: FakeGitHub, pacing: Pacing) -> None:  # noqa: F811
    gh.reply(f"DELETE {ISSUE}/labels/on-hold", {}, 200)
    a = http_adapter(gh, pacing, (PAUSED,), labels=[{"name": "on-hold"}])
    a.lift_holds = True
    a.lift_hold("on-hold", "pause", [TASK], "r")
    assert [(m, p) for m, p, _ in writes(gh)] == [
        ("DELETE", f"{ISSUE}/labels/on-hold"),
        ("POST", f"{ISSUE}/comments"),
    ]


def test_the_executor_refuses_a_malformed_lift() -> None:
    a, tracker = board_of(item(1, labels=("on-hold",)))
    bad = lift(held=[f"{REPO}#1"])
    for params in ({"hold": ""}, {"held_tasks": "x"}, {"held_tasks": [1, ""]}, {"hold_kind": 3}):
        assert PlatformExecutor(a).execute(replace(bad, params={**bad.params, **params})) is False
    assert tracker.writes == []


class LiftRule:
    """Rule 6's lift, as it asks for one (the rule itself is tested in test_bottlenecks)."""

    name = "bottlenecks"

    def evaluate(self, snapshot: Snapshot) -> list[Action]:
        return [lift(held=[t.id for t in snapshot.tasks])]


@pytest.mark.parametrize(
    ("dry_run", "mode", "lifted"),
    [
        (False, Mode.ACT, True),
        (True, Mode.ACT, False),
        (False, Mode.OBSERVE, False),
        (False, Mode.SUGGEST, False),
    ],
)
def test_the_engine_lifts_only_in_act_mode_without_dry_run(
    dry_run: bool, mode: Mode, lifted: bool
) -> None:
    a, tracker = board_of(item(1, labels=("on-hold",)))
    config = EngineConfig(dry_run=dry_run, modes={"bottlenecks": mode})
    engine = Engine(rules=[LiftRule()], adapters=[a], config=config)
    engine.tick(lambda: a.snapshot(NOW))
    assert bool(tracker.writes) is lifted


# --- config and wiring --------------------------------------------------------------------


def test_config() -> None:
    cfg = load_github_issues(
        {
            "repos": ["acme/app", "acme/web"],
            "pause_labels": ["on-hold"],
            "allow_lift_holds": True,
            "boards": {"acme/web": {"pause_labels": []}},
        },
        None,
    )
    assert [b.pause_labels for b in cfg.boards] == [("on-hold",), ()]
    assert cfg.allow_lift_holds and cfg.build({}).lift_holds is True
    assert load_github_issues({"repos": ["acme/app"]}, None).build({}).lift_holds is False
    linear = load_linear(
        {"teams": ["ENG"], "pause_states": ["Paused"], "boards": {"ENG": {"pause_labels": ["x"]}}}
    )
    assert linear.boards[0].pause_states == ("Paused",) and linear.boards[0].pause_labels == ("x",)
    assert linear.allow_lift_holds is False


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({"allow_lift_holds": "yes"}, "allow_lift_holds must be true or false"),
        ({"pause_labels": "on-hold"}, "pause_labels"),
        ({"pause_labels": ["blocked"]}, "also its label, blocked_labels or ready_labels"),
        ({"ready_labels": ["go"], "pause_labels": ["Go"]}, "also its label"),
        ({"pause_labels": [f"p{n}" for n in range(9)]}, "at most 8"),
        ({"pause_labels": ["x"], "agents": {"a": {"label": "x"}}}, "also a board label"),
        ({"pause_states": ["Paused"]}, "unknown"),  # GitHub has no workflow states
    ],
)
def test_bad_github_config(section: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        load_github_issues({"repos": ["acme/app"], **section}, None)


def test_bad_linear_config() -> None:
    with pytest.raises(ValueError, match="also ready_states"):
        load_linear({"teams": ["ENG"], "ready_states": ["Todo"], "pause_states": ["todo"]})
    with pytest.raises(ValueError, match="pause_states"):
        load_linear({"teams": ["ENG"], "pause_states": "Paused"})


def test_rule_6_can_act_when_an_issue_board_lifts_holds(tmp_path: Path) -> None:
    base = '[connectors.github]\nrepos = ["example/repo"]\n[adapters.github_issues]\n'
    config = tmp_path / "kelpie.toml"
    config.write_text(base)
    settings = load_settings(config)
    found = act_blockers(settings, build_adapters(settings))
    assert found["bottlenecks"].reason == (
        "Kelpie lifts no holds: no connector owns them ([connectors.github.pulls] "
        "allow_lift_holds); allow_lift_holds is off for GitHub Issues"
    )
    config.write_text(base + "allow_lift_holds = true\n")
    settings = load_settings(config)
    assert "bottlenecks" not in act_blockers(settings, build_adapters(settings))


def _labeled(name: str, at: Any) -> Any:
    from kelpie.adapters.issues.mapping import Event

    return Event("labeled", at, name)


def test_a_new_workflow_state_restarts_when_it_was_first_seen() -> None:
    """The fallback time of a task whose events say nothing (a pause state here) restarts
    when its workflow state changes, as when its labels or assignees do."""
    todo = replace(item(1, board="ENG", state="Todo", updated_at=ago(hours=3)), id="ENG-1")
    a, tracker = board_of(todo, board=TEAM)
    tracker.items["ENG-1"] = replace(todo, state="Paused", updated_at=ago(minutes=10))
    (t,) = a.snapshot(NOW).tasks
    assert t.status is Status.BLOCKED and t.status_since == ago(minutes=10)


def test_a_tracker_asking_to_wait_after_a_lift_stops_the_lift() -> None:
    items = [item(n, labels=("on-hold",)) for n in (1, 2, 3)]
    a, tracker = board_of(*items)
    real = tracker.fetch

    def fetch(item_id: str) -> Item:
        if item_id.endswith("#2"):
            raise IssuesError("wait", kind="backoff")
        return real(item_id)

    tracker.fetch = fetch  # type: ignore[method-assign]
    done = a.lift_hold("on-hold", "pause", [i.id for i in items], "r")
    assert done == f"label 'on-hold' off {REPO}#1; 2 left to a person"
    assert {w[1] for w in tracker.writes} == {f"{REPO}#1"}  # nothing after the wait
