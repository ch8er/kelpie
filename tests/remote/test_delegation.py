"""Delegation under ``[hub]`` (#347): a ``delegate`` action crosses the wire to the agent
whose board holds the task, the card speaks the operator's own ids, the card's id comes
back in the farm's namespace, and nothing lets one host's card land on another's board."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import pytest

from kelpie.core.actions import Action, ActionKind
from kelpie.core.delegation import CARD_DETAIL, card_for
from kelpie.core.model import Problem, Status, Task
from kelpie.core.ports import UnsupportedAction
from kelpie.remote.farm import _farm_card, _local_card
from kelpie.remote.hub import HubServer
from kelpie.remote.protocol import (
    MAX_CARD_BODY,
    MAX_CARD_KEY,
    MAX_CARD_TITLE,
    Outcome,
    ProtocolError,
    WireAction,
    adapter_actions,
    decode_action,
    encode_action,
    wire_params,
)
from tests.remote.test_farm import KINDS, answer, ready
from tests.remote.test_hub import BoardAdapter, Clock, agent_for, clock, hub, server  # noqa: F401

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
KEY = "kelpie-orphan_status:laptop:t_1a:1760616000:1"


def card(key: str = KEY, task: str = "laptop:t_1a") -> dict[str, str]:
    problem = Problem(
        id=f"orphan_status:{task}",
        rule="orphan_status",
        severity="warning",
        summary=f"Task {task} has no owner for 2h.",
        since=NOW,
        opened_at=NOW,
        last_seen=NOW,
        task_id=task,
    )
    t = Task(task, "Fix login", Status.IN_PROGRESS, NOW)
    return card_for(problem, t, "ops-1", 1, key).params()


def delegate(task: str = "laptop:t_1a", **params: Any) -> Action:
    return Action(
        ActionKind.DELEGATE,
        "delegation",
        "For ops-1 (attempt 1): no owner",
        task_id=task,
        params={
            "operator": "ops-1",
            "card": card(task=task),
            "problem": "orphan_status:" + task,
            "attempt": 1,
            **params,
        },
    )


# --- the wire ---------------------------------------------------------------------------


def test_a_card_crosses_the_wire_with_only_its_own_params() -> None:
    out = wire_params(ActionKind.DELEGATE, delegate().params)
    assert set(out) == {"operator", "card"}  # problem, attempt: the hub's ledger only
    assert out["card"] == card()


def bad(**change: Any) -> dict[str, Any]:
    return {**card(), **change}


@pytest.mark.parametrize(
    ("params", "why"),
    [
        ({"card": card()}, "operator and card are required"),
        ({"operator": "ops-1"}, "operator and card are required"),
        ({"operator": "ops 1", "card": card()}, "profile name"),
        ({"operator": "-rf", "card": card()}, "profile name"),
        ({"operator": "ops-1", "card": "text"}, "must be a dict"),
        ({"operator": "ops-1", "card": {**card(), "assignee": "root"}}, "exactly title"),
        ({"operator": "ops-1", "card": {"title": "t", "body": "b"}}, "exactly title"),
        ({"operator": "ops-1", "card": bad(title="")}, "card title"),
        ({"operator": "ops-1", "card": bad(title="x" * (MAX_CARD_TITLE + 1))}, "card title"),
        ({"operator": "ops-1", "card": bad(title="a\nb")}, "printable"),
        ({"operator": "ops-1", "card": bad(key="")}, "card key"),
        ({"operator": "ops-1", "card": bad(key="k" * (MAX_CARD_KEY + 1))}, "card key"),
        ({"operator": "ops-1", "card": bad(body="x" * (MAX_CARD_BODY + 1))}, "card body"),
        ({"operator": "ops-1", "card": bad(body=card()["body"] + "\x00")}, "card body"),
        # A body whose key line names another card would make the agent find the wrong one.
        ({"operator": "ops-1", "card": bad(body=card()["body"] + "\nmore")}, "key line"),
        ({"operator": "ops-1", "card": bad(key="kelpie-other")}, "key line"),
    ],
)
def test_malformed_cards_are_refused(params: dict[str, Any], why: str) -> None:
    with pytest.raises(ProtocolError, match=why):
        wire_params(ActionKind.DELEGATE, params)


def test_the_agent_refuses_a_card_for_a_board_that_does_not_offer_delegate() -> None:
    raw = encode_action(
        WireAction(
            "a1",
            "hermes",
            ActionKind.DELEGATE,
            "delegation",
            "r",
            "t_1a",
            wire_params(ActionKind.DELEGATE, delegate().params),
        )
    )
    with pytest.raises(ProtocolError, match="does not carry out 'delegate'"):
        decode_action(raw, {"hermes": KINDS})
    decoded = decode_action(raw, {"hermes": KINDS | {ActionKind.DELEGATE}})
    assert decoded.params["operator"] == "ops-1"


def test_a_delegate_capable_adapter_offers_delegate() -> None:
    assert ActionKind.DELEGATE in adapter_actions(DelegatingAdapter())
    assert ActionKind.DELEGATE not in adapter_actions(BoardAdapter())
    assert ActionKind.DELEGATE not in adapter_actions(
        DelegatingAdapter(), allowed={ActionKind.STOP}
    )  # [agent] actions narrows it away


# --- the hub ----------------------------------------------------------------------------


def test_the_card_speaks_the_agents_own_ids_but_keeps_its_key_line() -> None:
    local = _local_card(card(), "laptop:t_1a", "t_1a")
    assert "laptop:t_1a" not in local["title"] and "t_1a" in local["title"]
    *head, key_line = local["body"].split("\n")
    assert all("laptop:t_1a" not in line for line in head)
    assert key_line == f"kelpie-delegation: {KEY}"  # unchanged: the agent finds it by it
    assert local["key"] == KEY
    wire_params(ActionKind.DELEGATE, {"operator": "ops-1", "card": local})  # still valid


def test_a_delegate_reaches_the_agent_and_its_card_id_comes_back_namespaced() -> None:
    rig = ready()
    rig.farm._kinds["laptop/hermes"] = KINDS | {ActionKind.DELEGATE}
    agent = answer(rig.hub, "laptop", Outcome.DONE, "card t_9 for ops-1")
    detail = rig.executors["laptop/hermes"].execute(delegate())
    agent.join(5)
    assert detail == "card laptop:t_9 for ops-1"
    found = CARD_DETAIL.match(detail)
    assert found is not None and found[1] == "laptop:t_9"  # delegation finds the card task
    (sent,) = agent.seen  # type: ignore[attr-defined]
    assert sent.kind is ActionKind.DELEGATE and sent.task_id == "t_1a"
    assert set(sent.params) == {"operator", "card"}
    assert "laptop:" not in sent.params["card"]["title"]


@pytest.mark.parametrize(
    "action",
    [
        delegate("vm-1:t_1a"),  # another host's task, through laptop's executor
        delegate("t_1a"),  # not namespaced at all
        Action(ActionKind.DELEGATE, "delegation", "r", task_id="laptop:t_1a"),  # no card
        delegate(operator="ops 1"),  # not a profile name
    ],
)
def test_cards_that_cross_hosts_or_are_malformed_are_not_sent(action: Action) -> None:
    rig = ready()
    rig.farm._kinds["laptop/hermes"] = KINDS | {ActionKind.DELEGATE}
    assert rig.executors["laptop/hermes"].execute(action) is False
    assert not rig.hub._agents["laptop"].queue


def test_a_board_that_does_not_offer_delegate_gets_no_card() -> None:
    rig = ready()
    assert rig.executors["laptop/hermes"].execute(delegate()) is False
    assert not rig.hub._agents["laptop"].queue


@pytest.mark.parametrize(
    ("detail", "expected"),
    [
        ("card t_9 for ops-1", "card laptop:t_9 for ops-1"),
        ("card ops:t_9 for ops-1", "card laptop:ops:t_9 for ops-1"),  # a named Hermes board
        ("made something", "made something"),  # no card id: delegation reads none
        (None, True),
    ],
)
def test_card_ids_in_results_are_namespaced(detail: str | None, expected: Any) -> None:
    assert _farm_card("laptop", detail) == expected


# --- the agent --------------------------------------------------------------------------


@dataclass
class DelegatingAdapter(BoardAdapter):
    cards: list[tuple[str, str, str, str, str]] = field(default_factory=list)

    def delegate(self, task_id: str, operator: str, title: str, body: str, key: str) -> str:
        if operator == "alice":
            raise UnsupportedAction("'alice' is not an agent; no card made")
        self.cards.append((task_id, operator, title, body, key))
        return "t_9"


def wire_delegate(operator: str = "ops-1") -> dict[str, Any]:
    local = _local_card(card(), "laptop:t_1a", "t_1a")
    return encode_action(
        WireAction(
            "d1",
            "hermes",
            ActionKind.DELEGATE,
            "delegation",
            "For ops-1",
            "t_1a",
            wire_params(ActionKind.DELEGATE, {"operator": operator, "card": local}),
        )
    )


def test_the_agent_makes_the_card_on_its_own_board(server: HubServer, clock: Clock) -> None:  # noqa: F811
    adapter = DelegatingAdapter()
    agent = agent_for(server, adapter, clock)
    result = agent.execute(wire_delegate())
    assert result is not None and result.outcome is Outcome.DONE
    assert result.detail == "card t_9 for ops-1"
    ((task, operator, title, body, key),) = adapter.cards
    assert (task, operator, key) == ("t_1a", "ops-1", KEY)
    assert body.endswith(f"kelpie-delegation: {KEY}") and "laptop:" not in title


def test_the_agents_adapter_still_refuses_a_person(server: HubServer, clock: Clock) -> None:  # noqa: F811
    adapter = DelegatingAdapter()
    agent = agent_for(server, adapter, clock)
    result = agent.execute(wire_delegate("alice"))
    assert result is not None and result.outcome is Outcome.NOT_EXECUTED
    assert adapter.cards == []


def test_an_agent_can_turn_delegate_off(server: HubServer, clock: Clock) -> None:  # noqa: F811
    adapter = DelegatingAdapter()
    agent = agent_for(server, adapter, clock, actions=frozenset({ActionKind.STOP}))
    result = agent.execute(wire_delegate())
    assert result is not None and result.outcome is Outcome.NOT_EXECUTED
    assert adapter.cards == []


def test_a_flood_of_cards_costs_one_budget_per_tick() -> None:
    """The hub's per-agent budget caps delegate actions like any other: a rule run wild
    (or a flood of problems) queues at most 16 per agent per tick, the rest fail fast for
    the ledger to retry; the rule's own hourly and daily limits come first."""
    from kelpie.remote.farm import MAX_ACTIONS_PER_TICK, Busy, _Budget

    rig = ready()
    rig.farm._kinds["laptop/hermes"] = KINDS | {ActionKind.DELEGATE}
    rig.farm._budgets["laptop"] = _Budget(actions=MAX_ACTIONS_PER_TICK)
    with pytest.raises(Busy, match="busy this tick"):
        rig.executors["laptop/hermes"].execute(delegate())
    assert not rig.hub._agents["laptop"].queue
