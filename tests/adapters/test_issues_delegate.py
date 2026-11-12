"""Delegation cards on GitHub Issues (#347): an issue in the task's repository that starts
the operator agent, found again by its key line, never made twice."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.issues.adapter import JOINER, card_line, card_text
from kelpie.adapters.issues.github import CARD_LOOKBACK
from kelpie.adapters.issues.mapping import Agent, Board, IssuesError
from kelpie.app import ConfigError, _check_operators, load_settings
from kelpie.core.actions import Action, ActionKind
from kelpie.core.delegation import Policy
from kelpie.core.executors import PlatformExecutor
from kelpie.core.ports import Deferred, SupportsDelegate, UnsupportedAction
from tests.adapters.test_issues_actions import (  # noqa: F401
    ISSUE,
    TASK,
    Pacing,
    gh,
    pacing,
    writes,
)
from tests.adapters.test_issues_actions import adapter as http_adapter
from tests.adapters.test_issues_github import REPO, issue, route
from tests.connectors.fake_github import FakeGitHub

KEY = "kelpie-stale_blocks:abc"
LINE = card_line(KEY)
CARDS = f"/repos/{REPO}/issues"
CARD = f"{REPO}#7"
SEARCH = route(
    CARDS,
    creator="kelpie-bot",
    state="all",
    sort="created",
    direction="desc",
    per_page=str(CARD_LOOKBACK),
)


def kelpie_account(gh: FakeGitHub, opened: list[dict[str, Any]] | None = None) -> None:  # noqa: F811
    gh.reply("/user", {"login": "kelpie-bot"})
    gh.reply(SEARCH, opened or [])
    gh.reply(f"POST {CARDS}", {"number": 7}, 201)
    gh.reply(f"{CARDS}/7", issue(7))
    gh.reply(f"POST {CARDS}/7/assignees", issue(7, assignees=[{"login": "Copilot"}]), 201)
    gh.reply(f"POST {CARDS}/7/labels", {}, 200)
    gh.reply(f"POST {CARDS}/7/comments", {}, 201)


def delegate(**params: Any) -> Action:
    card = {"title": "Kelpie: stale_blocks on x", "body": f"Facts\n\n{LINE}", "key": KEY}
    return Action(
        ActionKind.DELEGATE,
        "delegation",
        "hand over",
        task_id=TASK,
        params={"card": card, "operator": "copilot", **params},
    )


def test_a_card_is_an_issue_that_starts_the_operator(gh: FakeGitHub, pacing: Pacing) -> None:  # noqa: F811
    kelpie_account(gh)
    a = http_adapter(gh, pacing)
    assert isinstance(a, SupportsDelegate) and ActionKind.DELEGATE in a.actions
    assert PlatformExecutor(a).execute(delegate()) == f"card {CARD} for copilot"
    made = writes(gh)
    assert made[0][:2] == ("POST", CARDS)
    body = made[0][2]
    assert body["title"] == "Kelpie: stale_blocks on x" and "labels" not in body
    assert body["body"].splitlines()[-1] == LINE  # the key, on its own line, last
    assert body["body"] == card_text("Kelpie hands a problem to copilot.", f"Facts\n\n{LINE}", LINE)
    assert made[1] == ("POST", f"{CARDS}/7/assignees", {"assignees": ["Copilot"]})


def test_the_card_carries_the_boards_label(gh: FakeGitHub, pacing: Pacing) -> None:  # noqa: F811
    kelpie_account(gh)
    board = (Board(REPO, label="agents"),)
    a = http_adapter(gh, pacing, board, labels=[{"name": "agents"}])
    a.delegate(TASK, "copilot", "t", "b", KEY)
    assert writes(gh)[0][2]["labels"] == ["agents"]


def test_a_card_made_before_is_returned_not_made_again(gh: FakeGitHub, pacing: Pacing) -> None:  # noqa: F811
    held = issue(7, body=f"x\n{LINE}", assignees=[{"login": "Copilot"}])
    kelpie_account(gh, [issue(9, body=f"  {LINE}"), held])  # a quoted key does not count
    gh.reply(f"{CARDS}/7", held)
    a = http_adapter(gh, pacing)
    assert a.delegate(TASK, "copilot", "t", "b", KEY) == CARD
    assert writes(gh) == []


def test_a_card_whose_agent_was_not_started_is_started_on_the_retry(
    gh: FakeGitHub,  # noqa: F811
    pacing: Pacing,  # noqa: F811
) -> None:
    kelpie_account(gh, [issue(7, body=f"x\n{LINE}")])
    a = http_adapter(gh, pacing)
    assert a.delegate(TASK, "copilot", "t", "b", KEY) == CARD
    assert writes(gh) == [("POST", f"{CARDS}/7/assignees", {"assignees": ["Copilot"]})]


def test_a_failed_start_says_the_card_exists(gh: FakeGitHub, pacing: Pacing) -> None:  # noqa: F811
    kelpie_account(gh)
    gh.reply(f"POST {CARDS}/7/assignees", {"message": "no"}, 422)
    a = http_adapter(gh, pacing)
    with pytest.raises(IssuesError) as err:
        a.delegate(TASK, "copilot", "t", "b", KEY)
    assert err.value.public_reason.startswith(f"card {CARD} was opened but copilot was not started")


def test_without_the_tokens_login_no_card_is_made(gh: FakeGitHub, pacing: Pacing) -> None:  # noqa: F811
    kelpie_account(gh)
    gh.reply("/user", {"message": "Resource not accessible by integration"}, 403)
    a = http_adapter(gh, pacing)
    with pytest.raises(IssuesError):
        a.delegate(TASK, "copilot", "t", "b", KEY)
    assert writes(gh) == []


@pytest.mark.parametrize(
    ("operator", "task", "match"),
    [
        ("ghost", TASK, "not an agent"),
        ("watcher", TASK, "no assignee, label or mention"),
        ("copilot", f"{REPO}#99", "not a task"),
    ],
)
def test_refused_before_any_write(
    gh: FakeGitHub,  # noqa: F811
    pacing: Pacing,  # noqa: F811
    operator: str,
    task: str,
    match: str,
) -> None:
    kelpie_account(gh)
    a = http_adapter(gh, pacing)
    with pytest.raises(UnsupportedAction, match=match):
        a.delegate(task, operator, "t", "b", KEY)
    assert writes(gh) == []


def test_a_board_that_does_not_take_the_operator_refuses(gh: FakeGitHub, pacing: Pacing) -> None:  # noqa: F811
    kelpie_account(gh)
    a = http_adapter(gh, pacing, (Board(REPO, agents=("openhands",)),))
    with pytest.raises(UnsupportedAction, match="does not take tasks for copilot"):
        a.delegate(TASK, "copilot", "t", "b", KEY)


def test_titles_ping_nobody() -> None:
    from kelpie.adapters.issues.adapter import inert_title

    assert inert_title("Kelpie:  fix @claude\nnow") == f"Kelpie: fix @{JOINER}claude now"


def test_operators_are_checked_at_startup(tmp_path: Path) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text(
        '[connectors.github]\nrepos = ["example/repo"]\n[adapters.github_issues]\n'
        '[adapters.github_issues.agents.copilot]\nassignee = "Copilot"\n'
    )
    settings = load_settings(config)
    _check_operators(settings, Policy(delegate_to={"github-issues": "copilot"}))
    with pytest.raises(ConfigError, match="not one of"):
        _check_operators(settings, Policy(delegate_to={"github-issues": "ghost"}))
    config.write_text(
        '[connectors.github]\nrepos = ["example/repo"]\n[adapters.github_issues]\n'
        '[adapters.github_issues.agents.watcher]\nauthor = "watcher[bot]"\n'
        '[adapters.github_issues.agents.copilot]\nassignee = "Copilot"\n'
        '[adapters.github_issues.boards."example/repo"]\nagents = ["watcher"]\n'
    )
    other = load_settings(config)
    with pytest.raises(ConfigError, match="cannot start it"):
        _check_operators(other, Policy(delegate_to={"github-issues": "watcher"}))
    with pytest.raises(ConfigError, match="no board takes it"):
        _check_operators(other, Policy(delegate_to={"github-issues": "copilot"}))
    with pytest.raises(ConfigError, match="Linear cannot take delegation cards"):
        _check_operators(settings, Policy(delegate_to={"linear": "codex"}))


def test_the_key_line_is_a_short_digest() -> None:
    line = card_line("kelpie-stale_blocks:acme/app#12 @someone\nx")
    assert line.startswith("kelpie-delegation: ") and len(line) == 19 + 32
    assert "#" not in line and "@" not in line


def test_no_card_about_a_card(gh: FakeGitHub, pacing: Pacing) -> None:  # noqa: F811
    kelpie_account(gh)
    a = http_adapter(gh, pacing, body=f"Kelpie hands a problem to copilot.\n\n{LINE}")
    with pytest.raises(UnsupportedAction, match="is a delegation card"):
        a.delegate(TASK, "copilot", "t", "b", KEY)
    assert writes(gh) == []


def test_github_asking_to_wait_defers_the_card(gh: FakeGitHub, pacing: Pacing) -> None:  # noqa: F811
    kelpie_account(gh)
    a = http_adapter(gh, pacing)
    gh.reply(SEARCH, {"message": "slow down"}, 429, retry_after="60")
    with pytest.raises(Deferred):
        a.delegate(TASK, "copilot", "t", "b", KEY)
    assert writes(gh) == []


def test_a_started_agent_missing_its_label_gets_it_on_the_retry(
    gh: FakeGitHub,  # noqa: F811
    pacing: Pacing,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.adapters import test_issues_actions as actions

    both = {**actions.AGENTS, "copilot": Agent("copilot", assignee="Copilot", label="ai")}
    monkeypatch.setattr(actions, "AGENTS", both)
    card = issue(7, body=f"x\n{LINE}", assignees=[{"login": "Copilot"}])
    kelpie_account(gh, [card])
    gh.reply(f"{CARDS}/7", card)
    a = http_adapter(gh, pacing)
    assert a.delegate(TASK, "copilot", "t", "b", KEY) == CARD
    assert ("POST", f"{CARDS}/7/labels", {"labels": ["ai"]}) in writes(gh)
