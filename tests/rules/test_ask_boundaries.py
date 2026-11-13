"""One announcer per wait (#461): asks in block reasons and agents' comment questions to
people, across rule 4 (``stale_blocks``), rule 13 (``needs_you``) and ``unanswered_asks``,
loaded as ``kelpie serve`` loads them."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from typing import Any

import pytest

from kelpie.app import ConfigError, load_rules
from kelpie.core.actions import Action, ActionKind
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.model import Snapshot
from kelpie.core.serde import snapshot_from_dict
from kelpie.rules import REGISTRY
from kelpie.rules.stale_blocks import StaleBlocks
from kelpie.rules.unanswered_asks import UnansweredAsks

NOW = "2026-03-05T15:00:00+00:00"
THREE = ("stale_blocks", "unanswered_asks", "needs_you")


def _task(tid: str, status: str, owner: str, since: str, **extra: Any) -> dict[str, Any]:
    return {
        "id": tid,
        "title": f"task {tid}",
        "status": status,
        "status_since": since,
        "role": "developer",
        "owner_id": owner,
        **extra,
    }


def _block(reason: str, since: str) -> dict[str, Any]:
    return {"reason": reason, "since": since}


def _comment(author: str, text: str, at: str, human: bool = False) -> dict[str, Any]:
    return {"author": author, "text": text, "at": at, "human": human}


SIX = "2026-03-05T09:00:00+00:00"
FARM: dict[str, Any] = {
    "now": NOW,
    "owners": [
        {"id": "agent-dev-1", "role": "developer"},
        {"id": "agent-dev-2", "role": "developer"},
        {"id": "architect-1", "role": "architect"},
        {"id": "human-owner", "role": "owner", "kind": "human"},
    ],
    "tasks": [
        # a block that asks another agent: unanswered_asks', never rule 4's
        _task(
            "T-1",
            "blocked",
            "agent-dev-1",
            SIX,
            block=_block("@architect-1 which schema version?", SIX),
        ),
        # a block that asks a person: rule 13's approval, never the others'
        _task(
            "T-2",
            "blocked",
            "agent-dev-2",
            SIX,
            block=_block("needs_input: drop v1? @architect-1 agreed?", SIX),
        ),
        # a plain block: rule 4's
        _task("T-3", "blocked", "agent-dev-2", SIX, block=_block("runner image broken", SIX)),
        # a comment question to a person: rule 13's item
        _task(
            "T-4",
            "in_progress",
            "agent-dev-1",
            SIX,
            comments=[_comment("agent-dev-1", "@human-owner can we drop v1?", SIX)],
        ),
        # a comment question to an agent: unanswered_asks'
        _task(
            "T-5",
            "review",
            "agent-dev-2",
            SIX,
            comments=[_comment("agent-dev-2", "@architect-1 is a 5m TTL fine?", SIX)],
        ),
        # one comment asking both an agent and a person: one wait each, one rule each
        _task(
            "T-6",
            "in_progress",
            "agent-dev-1",
            SIX,
            comments=[_comment("agent-dev-1", "@architect-1 @human-owner v3 or v4?", SIX)],
        ),
        # a blocked task's comment questions stay its block's (rule 4)
        _task(
            "T-7",
            "blocked",
            "agent-dev-2",
            SIX,
            block=_block("flaky suite", SIX),
            comments=[
                _comment("agent-dev-2", "@architect-1 quarantine it?", SIX),
                _comment("agent-dev-2", "@human-owner rebuild the runner?", SIX),
            ],
        ),
    ],
}


def _rules(**modes: Mode) -> dict[str, Any]:
    loaded = load_rules(EngineConfig(modes=modes))
    return {r.name: r for r in loaded.rules if r.name in THREE}


def _announcers(snapshot: Snapshot, rules: dict[str, Any]) -> dict[str, set[str]]:
    """Task id -> the rules that announce it (rule 13's digest items count per task)."""
    found: dict[str, set[str]] = defaultdict(set)
    for name, rule in rules.items():
        for action in rule.evaluate(snapshot):
            for tid in _tasks_of(action):
                found[tid].add(name)
    return found


def _tasks_of(action: Action) -> list[str]:
    if action.kind is ActionKind.ESCALATE:
        return [item["task_id"] for item in action.params.get("items", [])]
    return [action.task_id] if action.task_id else []


@pytest.mark.parametrize(
    "modes",
    [
        {},  # everything in observe, as installed
        {"stale_blocks": Mode.SUGGEST, "unanswered_asks": Mode.SUGGEST, "needs_you": Mode.SUGGEST},
    ],
)
def test_no_task_is_announced_by_two_rules(modes: dict[str, Mode]) -> None:
    announced = _announcers(snapshot_from_dict(FARM), _rules(**modes))
    assert {t: sorted(r) for t, r in announced.items()} == {
        "T-1": ["unanswered_asks"],
        "T-2": ["needs_you"],
        "T-3": ["stale_blocks"],
        "T-4": ["needs_you"],
        "T-5": ["unanswered_asks"],
        "T-6": ["needs_you", "unanswered_asks"],  # two recipients, two waits
        "T-7": ["stale_blocks"],
    }


def test_t6_is_one_wait_per_recipient() -> None:
    rules = _rules()
    snapshot = snapshot_from_dict(FARM)
    reminders = [a for a in rules["unanswered_asks"].evaluate(snapshot) if a.task_id == "T-6"]
    assert [a.params["recipient"] for a in reminders] == ["architect-1"]
    digest = rules["needs_you"].evaluate(snapshot)
    questions = {a.params["escalate_to"]: a.params["questions"] for a in digest}
    assert questions == {"human-owner": ["T-4", "T-6"], None: []}  # None: T-2, no approver


def test_rule_4_keeps_the_block_when_unanswered_asks_does_not_reach_agents() -> None:
    """unanswered_asks in observe while rule 4 suggests: rule 4 keeps its alert, so the
    block is not silent."""
    rules = _rules(stale_blocks=Mode.SUGGEST, needs_you=Mode.SUGGEST)
    assert rules["stale_blocks"].asks is None
    alerts = rules["stale_blocks"].evaluate(snapshot_from_dict(FARM))
    assert "T-1" in {a.task_id for a in alerts}


def test_rule_4_reads_the_asks_rule_with_its_settings() -> None:
    def stale(rules: dict[str, Any], **modes: Mode) -> StaleBlocks:
        loaded = load_rules(EngineConfig(rules=rules, modes=modes))
        return next(r for r in loaded.rules if r.name == "stale_blocks")

    asks = stale({"unanswered_asks": {"stop_after": "2d"}}).asks
    assert isinstance(asks, UnansweredAsks)
    assert asks.stop_after == timedelta(days=2)
    assert asks.inbox is not None  # the same rule 13 hand-off as the real rule's
    suggest, observe = Mode.SUGGEST, Mode.OBSERVE
    assert stale({}, stale_blocks=suggest, unanswered_asks=suggest).asks is not None
    assert stale({}, stale_blocks=suggest, unanswered_asks=observe).asks is None
    with pytest.raises(ConfigError, match="set automatically from the rules' modes"):
        stale({"stale_blocks": {"unanswered_asks": False}})
    with pytest.raises(ConfigError, match="bad config for rule 'unanswered_asks'"):
        stale({"unanswered_asks": {"steps": []}})
    without = {k: v for k, v in REGISTRY.items() if k != "unanswered_asks"}
    loaded = load_rules(EngineConfig(), without)
    assert next(r for r in loaded.rules if r.name == "stale_blocks").asks is None


def test_the_asks_rule_leaves_rule_13s_blocks_with_customised_reasons() -> None:
    """A customised rule 13 (``approval_reasons``) decides which blocks ask a person for
    both rule 4 and unanswered_asks, so neither double-announces."""
    farm = dict(FARM)
    farm["tasks"] = [
        _task(
            "T-8",
            "blocked",
            "agent-dev-1",
            SIX,
            block=_block("ask_human: @architect-1 said v3, ok?", SIX),
        )
    ]
    loaded = load_rules(EngineConfig(rules={"needs_you": {"approval_reasons": ["ask_human"]}}))
    rules = {r.name: r for r in loaded.rules if r.name in THREE}
    assert {t: sorted(r) for t, r in _announcers(snapshot_from_dict(farm), rules).items()} == {
        "T-8": ["needs_you"]
    }
