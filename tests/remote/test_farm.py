import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome as LedgerOutcome
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Dependency,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)
from kelpie.remote.config import HubAgent, HubSettings
from kelpie.remote.farm import (
    MAX_ACTIONS_PER_TICK,
    Busy,
    RemoteActionFailed,
    RemoteExecutor,
    RemoteFarm,
    _Budget,
)
from kelpie.remote.hub import AgentUnavailable, Hub
from kelpie.remote.protocol import (
    AdapterReport,
    Outcome,
    Report,
    Result,
    local,
    local_condition_id,
    namespace_snapshot,
)

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
KINDS = frozenset(
    {ActionKind.STOP, ActionKind.WAIT, ActionKind.WAKE, ActionKind.COMMENT, ActionKind.REASSIGN}
)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class Alerter:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, title: str, body: str, severity: str, did: str | None = None) -> None:
        self.sent.append((title, body, severity))


class Observer:
    def __init__(self) -> None:
        self.states: dict[str, tuple[bool, datetime | None]] = {}
        self.failed: list[str] = []

    def agent_state(self, agent: str, up: bool, last_report: datetime | None) -> None:
        self.states[agent] = (up, last_report)

    def adapter_failed(self, name: str) -> None:
        self.failed.append(name)


def board(now: datetime = NOW) -> Snapshot:
    return Snapshot(
        now=now,
        tasks=(
            Task(
                "t_1a",
                "Fix login",
                Status.BLOCKED,
                now,
                owner_id="dev-1",
                assignee="dev-1",
                subtasks=("t_2b",),
                repo="/srv/repo",
                block=Block("waiting", now, "c1"),
            ),
            Task("t_2b", "Child", Status.READY, now),
        ),
        owners=(Owner("dev-1", "dev"), Owner("alice", "pm", OwnerKind.HUMAN)),
        dependencies=(Dependency("t_2b", "t_1a"),),
        conditions=(Condition("c1", ConditionKind.TASK_STATUS, "t_1a", {"task": "t_2b"}),),
        sessions=(Session("s1", "t_1a", "dev-1", now),),
        spans=(
            Span(
                "tr",
                "sp",
                "bash",
                SpanKind.TOOL,
                now,
                parent_id="root",
                task_id="t_1a",
                session_id="s1",
                attributes={"gen_ai.tool.call.id": "call-1", "x": "y"},
            ),
        ),
    )


def test_every_id_is_namespaced() -> None:
    part = namespace_snapshot("laptop", "hermes", board())
    task = part.tasks[0]
    assert task.id == "laptop:t_1a"
    assert (task.owner_id, task.assignee) == ("laptop:dev-1", "laptop:dev-1")
    assert task.subtasks == ("laptop:t_2b",)
    assert task.repo == "laptop:/srv/repo"
    assert (task.source, task.host) == ("laptop/hermes", "laptop")
    assert task.block is not None and task.block.condition_id == "c1@laptop"
    assert [o.id for o in part.owners] == ["laptop:dev-1", "laptop:alice"]
    assert part.dependencies == (Dependency("laptop:t_2b", "laptop:t_1a"),)
    cond = part.conditions[0]
    assert (cond.id, cond.task_id, cond.params) == (
        "c1@laptop",
        "laptop:t_1a",
        {"task": "laptop:t_2b"},
    )
    session = part.sessions[0]
    assert (session.id, session.task_id, session.owner_id, session.source) == (
        "laptop:s1",
        "laptop:t_1a",
        "laptop:dev-1",
        "laptop/hermes",
    )
    span = part.spans[0]
    assert (span.trace_id, span.span_id, span.parent_id) == (
        "laptop:tr",
        "laptop:sp",
        "laptop:root",
    )
    assert (span.task_id, span.session_id) == ("laptop:t_1a", "laptop:s1")
    assert span.attributes == {"gen_ai.tool.call.id": "laptop:call-1", "x": "y"}


def test_local_ids() -> None:
    assert local("laptop", "laptop:t_1a") == "t_1a"
    assert local("laptop", "vm-1:t_1a") is None
    assert local("laptop", "laptop:") is None
    assert local("lap", "laptop:t_1a") is None
    assert local_condition_id("laptop", "c1@laptop") == "c1"
    assert local_condition_id("laptop", "dispatch_gate:laptop:t_1a:p") == (
        "dispatch_gate:laptop:t_1a:p"
    )


def test_rule_condition_prefixes_survive_the_round_trip() -> None:
    made = "dispatch_gate:laptop:t_1a:laptop:t_2b"
    back = namespace_snapshot(
        "laptop",
        "hermes",
        Snapshot(now=NOW, conditions=(Condition(made, ConditionKind.TASK_STATUS, "t_1a"),)),
    ).conditions[0]
    assert back.id.startswith("dispatch_gate:laptop:t_1a:")
    assert local_condition_id("laptop", back.id) == made  # registered again under its id


# --- the farm ---------------------------------------------------------------------------


def settings(**kw: Any) -> HubSettings:
    base = HubSettings(
        agents=(HubAgent("laptop", "T1"), HubAgent("vm-1", "T2")),
        host="127.0.0.1",
        port=0,
        allow_plain_http=True,
        silent_after=timedelta(seconds=90),
        action_timeout=timedelta(seconds=2),
    )
    return replace(base, **kw)


class Rig:
    def __init__(self, **kw: Any) -> None:
        self.clock = Clock()
        self.hub = Hub(settings(**kw), {"laptop": "a" * 40, "vm-1": "b" * 40}, self.clock)
        self.alerter = Alerter()
        self.observer = Observer()
        self.executors: dict[str, Any] = {}
        self.seq = 0
        self.farm = RemoteFarm(
            self.hub,
            lambda e: self.executors.__setitem__(e.name, e),
            self.clock,
            alerter=lambda: self.alerter,
            observer=self.observer,
        )

    def report(
        self,
        agent: str = "laptop",
        *parts: AdapterReport,
        sent_at: datetime | None = None,
    ) -> None:
        parts = parts or (AdapterReport("hermes", KINDS, board(self.clock.now)),)
        self.seq += 1
        report = Report(
            "0.0.1", sent_at or self.clock.now, timedelta(seconds=15), parts, "c0ffee00", self.seq
        )
        self.hub.receive_report(agent, report)

    def tick(self) -> Snapshot:
        return self.farm.snapshot(self.clock.now)

    def titles(self) -> list[str]:
        return [title for title, *_ in self.alerter.sent]


def test_fresh_boards_are_merged_and_executors_registered() -> None:
    rig = Rig()
    rig.report("laptop")
    rig.report("vm-1")
    snap = rig.tick()
    assert {t.id for t in snap.tasks} == {"laptop:t_1a", "laptop:t_2b", "vm-1:t_1a", "vm-1:t_2b"}
    assert snap.unobserved == ()
    assert set(rig.executors) == {"laptop/hermes", "vm-1/hermes"}
    assert rig.observer.states["laptop"] == (True, NOW)


def test_a_stale_board_is_left_out_and_the_snapshot_says_so() -> None:
    rig = Rig()
    rig.report("laptop")
    rig.report("vm-1")
    rig.clock.now += timedelta(seconds=41)  # over 2 x 15s + 10s
    rig.report("vm-1")
    snap = rig.tick()
    assert {t.id for t in snap.tasks} == {"vm-1:t_1a", "vm-1:t_2b"}
    assert snap.unobserved == ("laptop",)
    assert rig.observer.states["laptop"] == (False, NOW)
    assert rig.alerter.sent == []  # not yet silent


def test_silence_is_told_once_and_resolved() -> None:
    rig = Rig()
    rig.report("laptop")
    rig.report("vm-1")
    rig.tick()
    for _ in range(3):
        rig.clock.now += timedelta(seconds=60)
        rig.report("vm-1")
        rig.tick()
    assert rig.titles() == ["[kelpie] agent laptop silent"]
    _, body, severity = rig.alerter.sent[0]
    assert severity == Severity.WARNING
    assert "not evaluated" in body and NOW.isoformat(timespec="seconds") in body
    rig.report("laptop")
    rig.tick()
    assert rig.titles()[-1] == "[kelpie] agent laptop silent"
    assert "Resolved" in rig.alerter.sent[-1][1]
    assert len(rig.alerter.sent) == 2


def test_an_agent_that_never_reported_is_silent_after_the_grace() -> None:
    rig = Rig()
    rig.report("vm-1")
    assert rig.tick().unobserved == ("laptop",)
    assert rig.alerter.sent == []
    rig.clock.now += timedelta(seconds=91)
    rig.report("vm-1")
    rig.tick()
    assert "since Kelpie started" in rig.alerter.sent[0][1]


def test_a_skewed_clock_keeps_the_board_out() -> None:
    rig = Rig()
    rig.report("laptop", sent_at=NOW - timedelta(minutes=5))
    rig.report("vm-1")
    snap = rig.tick()
    assert "laptop" in snap.unobserved
    assert rig.titles() == ["[kelpie] agent laptop clock off"]
    assert "5 min behind" in rig.alerter.sent[0][1]
    rig.clock.now += timedelta(seconds=1)
    rig.report("laptop")
    assert rig.tick().unobserved == ()
    assert "Resolved" in rig.alerter.sent[-1][1]


def test_a_failed_adapter_is_unobserved() -> None:
    rig = Rig()
    rig.report(
        "laptop",
        AdapterReport("hermes", KINDS, board()),
        AdapterReport("claude-code", frozenset({ActionKind.STOP}), error="OSError"),
    )
    rig.report("vm-1")
    snap = rig.tick()
    assert snap.unobserved == ("laptop/claude-code",)
    assert rig.observer.failed == ["laptop/claude-code"]
    assert "laptop/claude-code" in rig.executors  # its stops still have somewhere to go


def test_dry_run_only_logs_alerts() -> None:
    rig = Rig()
    rig.farm.dry_run = True
    rig.clock.now += timedelta(seconds=91)
    rig.tick()
    assert rig.alerter.sent == []


# --- actions ------------------------------------------------------------------------------


def answer(hub: Hub, agent: str, outcome: Outcome, detail: str | None = None) -> threading.Thread:
    """A fake agent: takes one action and answers it."""

    def run() -> None:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            actions = hub.poll(agent, [], 1)
            if actions:
                hub.poll(agent, [Result(actions[0].id, outcome, detail)], 0)
                seen.append(actions[0])
                return

    seen: list[Any] = []
    thread = threading.Thread(target=run, daemon=True)
    thread.seen = seen  # type: ignore[attr-defined]
    thread.start()
    return thread


def ready() -> Rig:
    rig = Rig()
    rig.report("laptop")
    rig.report("vm-1")
    rig.tick()
    return rig


def stop(session: str = "laptop:s1", **kw: Any) -> Action:
    return Action(ActionKind.STOP, "loop_breaker", "looping", params={"session_id": session}, **kw)


@pytest.mark.parametrize(
    ("outcome", "detail", "expected"),
    [
        (Outcome.DONE, "exits within 2 minutes", "exits within 2 minutes"),
        (Outcome.DONE, None, True),
        (Outcome.NOTHING_TO_DO, None, LedgerOutcome.NOTHING_TO_DO),
        (Outcome.NOT_EXECUTED, "refused", False),
    ],
)
def test_results_map_onto_the_engine(outcome: Outcome, detail: str | None, expected: Any) -> None:
    rig = ready()
    agent = answer(rig.hub, "laptop", outcome, detail)
    assert rig.executors["laptop/hermes"].execute(stop()) == expected
    agent.join(5)
    (sent,) = agent.seen  # type: ignore[attr-defined]
    assert sent.params == {"session_id": "s1"} and sent.task_id is None


def test_a_failure_carries_only_the_public_reason() -> None:
    rig = ready()
    agent = answer(rig.hub, "laptop", Outcome.FAILED, "CalledProcessError (details in the log)")
    with pytest.raises(RemoteActionFailed) as exc:
        rig.executors["laptop/hermes"].execute(stop())
    assert exc.value.public_reason == "CalledProcessError (details in the log)"
    agent.join(5)


def test_ids_are_translated_back() -> None:
    rig = ready()
    agent = answer(rig.hub, "laptop", Outcome.DONE)
    wait = Action(
        ActionKind.WAIT,
        "dispatch_gate",
        "parent not done",
        task_id="laptop:t_1a",
        params={
            "conditions": [
                {"id": "c1@laptop", "kind": "task_status", "params": {"task": "laptop:t_2b"}},
                {"id": "dispatch_gate:laptop:t_1a:x", "kind": "task_status", "params": {}},
            ],
            "extra": "dropped",
        },
    )
    assert rig.executors["laptop/hermes"].execute(wait) is True
    agent.join(5)
    (sent,) = agent.seen  # type: ignore[attr-defined]
    assert sent.task_id == "t_1a"
    assert sent.params == {
        "conditions": [
            {"id": "c1", "kind": "task_status", "params": {"task": "t_2b"}},
            {"id": "dispatch_gate:laptop:t_1a:x", "kind": "task_status", "params": {}},
        ]
    }


@pytest.mark.parametrize(
    "action",
    [
        Action(ActionKind.COMMENT, "r", "x", task_id="vm-1:t_1a"),  # another host's task
        Action(ActionKind.COMMENT, "r", "x", task_id="t_1a"),  # not namespaced at all
        stop("vm-1:s1"),
        Action(
            ActionKind.REASSIGN, "r", "x", task_id="laptop:t_1a", params={"owner_id": "vm-1:dev-2"}
        ),
        Action(
            ActionKind.WAIT,
            "r",
            "x",
            task_id="laptop:t_1a",
            params={
                "conditions": [{"id": "c", "kind": "task_status", "params": {"task": "vm-1:t_9"}}]
            },
        ),
        Action(ActionKind.REQUEUE, "r", "x", task_id="laptop:t_1a"),  # not offered
    ],
)
def test_actions_that_cross_hosts_or_kinds_are_not_executed(action: Action) -> None:
    rig = ready()
    assert rig.executors["laptop/hermes"].execute(action) is False
    assert not rig.hub._agents["laptop"].queue


def test_an_agent_that_is_not_reporting_fails_fast() -> None:
    rig = ready()
    rig.clock.now += timedelta(seconds=60)
    rig.report("vm-1")
    rig.tick()
    started = time.monotonic()
    with pytest.raises(Busy, match="not reporting"):
        rig.executors["laptop/hermes"].execute(stop())
    assert time.monotonic() - started < 0.5


def test_a_slow_agent_costs_one_budget_per_tick() -> None:
    rig = ready()  # action_timeout 2s, nobody answers
    executor: RemoteExecutor = rig.executors["laptop/hermes"]
    started = time.monotonic()
    with pytest.raises(AgentUnavailable, match="not carried out"):
        executor.execute(stop("laptop:s1"))
    for session in ("laptop:s2", "laptop:s3"):
        with pytest.raises(Busy):  # deferred, not failed: no attempt is counted
            executor.execute(stop(session))
    assert time.monotonic() - started < 3  # not 3 x 2s
    rig.tick()  # a new tick: a new budget
    with pytest.raises(AgentUnavailable, match="not carried out"):
        executor.execute(stop("laptop:s4"))


def test_at_most_16_actions_per_agent_per_tick() -> None:
    rig = ready()
    rig.farm._budgets["laptop"] = _Budget(actions=MAX_ACTIONS_PER_TICK)
    with pytest.raises(Busy, match="busy"):
        rig.executors["laptop/hermes"].execute(stop())


def test_a_late_done_is_told() -> None:
    rig = ready()
    taker = threading.Thread(target=rig.hub.poll, args=("laptop", [], 3), daemon=True)
    taker.start()  # the agent takes the action but never answers in time
    with pytest.raises(AgentUnavailable, match="may still"):
        rig.executors["laptop/hermes"].execute(stop())
    taker.join(5)
    (pending,) = rig.hub._agents["laptop"].pending.values()
    rig.hub.poll("laptop", [Result(pending.action.id, Outcome.DONE, "exits soon")], 0)
    rig.clock.now += timedelta(minutes=6)
    rig.report("laptop")
    rig.report("vm-1")
    rig.tick()
    assert rig.titles() == ["[loop_breaker] stop laptop:s1 done after all"]
    assert "Carried out after all" in rig.alerter.sent[0][1]


# --- the engine ---------------------------------------------------------------------------


def test_the_engine_routes_to_remote_boards_by_source() -> None:
    rig = ready()
    engine = Engine([], fallback=False)
    for executor in rig.executors.values():
        engine.add_executor(executor)
    snap = rig.tick()
    comment = Action(ActionKind.COMMENT, "r", "x", task_id="vm-1:t_1a")
    assert engine.route(comment, snap) is rig.executors["vm-1/hermes"]
    assert engine.route(stop(), snap) is rig.executors["laptop/hermes"]
    assert engine.route(Action(ActionKind.COMMENT, "r", "x", task_id="nope"), snap) is None
    with pytest.raises(ValueError, match="duplicate"):
        engine.add_executor(rig.executors["vm-1/hermes"])


# --- review follow-ups --------------------------------------------------------------------


def test_human_task_refs_round_trip() -> None:
    part = Snapshot(
        now=NOW,
        conditions=(
            Condition("h1", ConditionKind.HUMAN_TASK, "t_1a", {"task": "T-42"}),
            Condition("x1", ConditionKind.CUSTOM, "t_1a", {"task": "free text"}),
        ),
    )
    human, custom = namespace_snapshot("laptop", "hermes", part).conditions
    assert human.params == {"task": "laptop:T-42"}
    assert custom.params == {"task": "free text"}  # not a task reference
    rig = ready()
    agent = answer(rig.hub, "laptop", Outcome.DONE)
    wait = Action(
        ActionKind.WAIT,
        "wait_condition",
        "waiting for a person",
        task_id="laptop:t_1a",
        params={
            "conditions": [
                {"id": "h1@laptop", "kind": "human_task", "params": {"task": "laptop:T-42"}},
                {"id": "x1@laptop", "kind": "custom", "params": {"task": "free text"}},
            ]
        },
    )
    assert rig.executors["laptop/hermes"].execute(wait) is True
    agent.join(5)
    (sent,) = agent.seen  # type: ignore[attr-defined]
    assert sent.params["conditions"] == [
        {"id": "h1", "kind": "human_task", "params": {"task": "T-42"}},
        {"id": "x1", "kind": "custom", "params": {"task": "free text"}},
    ]


def test_a_block_reason_names_a_human_task_of_the_same_board() -> None:
    import re

    from kelpie.connectors.human_tasks import HumanTaskConnector

    board_part = Snapshot(
        now=NOW,
        tasks=(
            Task("t_1a", "Ship", Status.BLOCKED, NOW, block=Block("approval in T-42", NOW)),
            Task("T-42", "Approve", Status.IN_PROGRESS, NOW, owner_id="alice"),
        ),
        owners=(Owner("alice", "pm", OwnerKind.HUMAN),),
    )
    merged = namespace_snapshot("laptop", "hermes", board_part)
    connector = HumanTaskConnector([re.compile(r"approval in (?P<task>\S+)")])
    out = connector(merged)
    (cond,) = out.conditions
    assert cond.params["task"] == "laptop:T-42"
    assert cond.satisfied is False and cond.params["owner"] == "laptop:alice"


def test_an_adapter_that_keeps_failing_is_told_once() -> None:
    rig = Rig()
    failing = AdapterReport("claude-code", frozenset({ActionKind.STOP}), error="OSError")
    for _ in range(3):
        rig.report("laptop", AdapterReport("hermes", KINDS, board()), failing)
        rig.report("vm-1")
        rig.tick()
    assert rig.titles() == ["[kelpie] board laptop/claude-code unreadable"]
    rig.report("laptop", AdapterReport("hermes", KINDS, board()))
    rig.report("vm-1")
    rig.tick()
    assert "Resolved" in rig.alerter.sent[-1][1]


def test_deferred_actions_are_not_attempts() -> None:
    from kelpie.core.config import EngineConfig, Mode
    from kelpie.core.ledger import Outcome as Done

    rig = ready()
    rig.farm._budgets["laptop"] = _Budget(actions=MAX_ACTIONS_PER_TICK)  # busy this tick
    action = Action(ActionKind.COMMENT, "rule", "note", task_id="laptop:t_1a", once=True)

    class Rule:
        name = "rule"

        def evaluate(self, snapshot: Snapshot) -> list[Action]:
            return [action]

    engine = Engine(
        [Rule()],  # type: ignore[list-item]
        config=EngineConfig(dry_run=False, default_mode=Mode.ACT),
        fallback=False,
    )
    engine.add_executor(rig.executors["laptop/hermes"])
    snap = rig.tick()
    rig.farm._budgets["laptop"] = _Budget(actions=MAX_ACTIONS_PER_TICK)
    (d,) = engine.execute(engine.evaluate(snap), snap)
    assert d.outcome is Done.DEFERRED
    assert engine.ledger.failed_attempts(action, Mode.ACT, snap.now) == 0
    assert engine.ledger.check(action, Mode.ACT, snap.now) is None  # free to try next tick


def test_owners_serve_tasks_of_their_own_machine() -> None:
    from kelpie.rules.ping_pong import PingPong

    task = replace(namespace_snapshot("laptop", "hermes", board()).tasks[0])
    mine = Owner("laptop:alice", "pm", OwnerKind.HUMAN, host="laptop")
    theirs = Owner("a:aaa", "pm", OwnerKind.HUMAN, host="vm-1")
    assert mine.serves(task) and not theirs.serves(task)
    assert Owner("alice", "pm").serves(task)  # single host: no machine to compare
    snap = Snapshot(now=NOW, tasks=(task,), owners=(theirs, mine))
    assert PingPong()._target(snap, task) == "laptop:alice"


def test_a_wake_reaches_a_remote_board() -> None:
    rig = ready()
    agent = answer(rig.hub, "laptop", Outcome.DONE)
    wake = Action(
        ActionKind.WAKE,
        "wait_condition",
        "conditions met",
        task_id="laptop:t_1a",
        params={"conditions": ["c1@laptop"]},  # condition ids, for the record
    )
    assert rig.executors["laptop/hermes"].execute(wake) is True
    agent.join(5)
    (sent,) = agent.seen  # type: ignore[attr-defined]
    assert (sent.kind, sent.task_id, sent.params) == (ActionKind.WAKE, "t_1a", {})


def test_a_wait_cannot_carry_another_machines_condition() -> None:
    rig = ready()
    wait = Action(
        ActionKind.WAIT,
        "r",
        "x",
        task_id="laptop:t_1a",
        params={"conditions": [{"id": "c1@vm-1", "kind": "custom", "params": {}}]},
    )
    assert rig.executors["laptop/hermes"].execute(wait) is False


def test_a_block_reason_never_binds_another_machines_task() -> None:
    import re

    from kelpie.connectors.human_tasks import HumanTaskConnector

    laptop = namespace_snapshot(
        "laptop",
        "hermes",
        Snapshot(
            now=NOW,
            tasks=(
                Task(
                    "t_1a", "Ship", Status.BLOCKED, NOW, block=Block("approval in vm-1:T-42", NOW)
                ),
            ),
        ),
    )
    vm = namespace_snapshot(
        "vm-1",
        "hermes",
        Snapshot(
            now=NOW,
            tasks=(Task("T-42", "Approve", Status.DONE, NOW, owner_id="bob"),),
            owners=(Owner("bob", "pm", OwnerKind.HUMAN),),
        ),
    )
    merged = replace(laptop, tasks=laptop.tasks + vm.tasks, owners=vm.owners)
    out = HumanTaskConnector([re.compile(r"approval in (?P<task>\S+)")])(merged)
    assert out.conditions == ()


def test_deferred_effects_are_not_announced_as_done() -> None:
    from kelpie.core.alerts import ANNOUNCES
    from kelpie.core.config import EngineConfig, Mode

    rig = ready()
    stop_it = stop(key="runaway:laptop:s1")
    alert = Action(
        ActionKind.ALERT,
        "loop_breaker",
        "Runaway run",
        params={ANNOUNCES: "runaway:laptop:s1"},
    )
    sent: list[str | None] = []

    class Alerts:
        def send(self, title: str, body: str, severity: str, did: str | None = None) -> None:
            sent.append(did)

    engine = Engine(
        [],
        alerter=Alerts(),
        config=EngineConfig(dry_run=False, default_mode=Mode.ACT),
        fallback=False,
    )
    engine.add_executor(rig.executors["laptop/hermes"])
    snap = rig.tick()
    rig.farm._budgets["laptop"] = _Budget(actions=MAX_ACTIONS_PER_TICK)
    engine.execute([stop_it, alert], snap)
    assert sent and sent[0] is not None and "Not done yet" in sent[0]


def test_an_unblock_reaches_a_remote_board() -> None:
    rig = ready()
    rig.farm._kinds["laptop/hermes"] = KINDS | {ActionKind.UNBLOCK}
    agent = answer(rig.hub, "laptop", Outcome.DONE)
    unblock = Action(
        ActionKind.UNBLOCK,
        "stale_blocks",
        "cause fixed",
        task_id="laptop:t_1a",
        params={"conditions": ["c1@laptop"]},
    )
    assert rig.executors["laptop/hermes"].execute(unblock) is True
    agent.join(5)
    (sent,) = agent.seen  # type: ignore[attr-defined]
    assert (sent.kind, sent.task_id, sent.params) == (ActionKind.UNBLOCK, "t_1a", {})


def test_a_reported_condition_named_like_another_machine_is_fine() -> None:
    from kelpie.remote.farm import _condition

    raw = {"id": "review@vm-1@laptop", "kind": "custom", "params": {}}
    assert _condition("laptop", raw, ["vm-1"]) == {
        "id": "review@vm-1",
        "kind": "custom",
        "params": {},
    }
    assert _condition("laptop", {"id": "c1@vm-1", "kind": "custom"}, ["vm-1"]) is None
