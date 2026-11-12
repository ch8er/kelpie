"""lift_hold on PR labels (kelpie.connectors.github.holds): opt-in, allow-listed, its own
token; what it removes, what it refuses and what it leaves to retry."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pytest

from kelpie.connectors.github.client import GitHubError
from kelpie.connectors.github.holds import HoldLifter
from kelpie.connectors.github.pulls import load_pulls
from kelpie.core.actions import Action, ActionKind, ActionResult


class Client:
    def __init__(self, errors: dict[str, GitHubError] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.errors = errors or {}

    def send(self, method: str, path: str, body: Any = None) -> Any:
        self.calls.append((method, path))
        if path in self.errors:
            raise self.errors[path]
        return None


def lift(refs: list[str], hold: str = "merge first", **params: Any) -> Action:
    return Action(
        ActionKind.LIFT_HOLD,
        "bottlenecks",
        "hold past its expiry",
        executor="github_pulls",
        params={"hold": hold, "lift_prs": refs, **params},
    )


def lifter(client: Client) -> HoldLifter:
    return HoldLifter(client, ("example/repo",))  # type: ignore[arg-type]


def test_the_label_is_taken_off_each_pr() -> None:
    client = Client()
    result = lifter(client).execute(lift(["example/repo#51", "Example/Repo#52"]))
    assert result is ActionResult.DONE
    assert client.calls == [
        ("DELETE", "/repos/example/repo/issues/51/labels/merge%20first"),
        ("DELETE", "/repos/Example/Repo/issues/52/labels/merge%20first"),
    ]


def test_a_label_already_gone_is_nothing_to_do() -> None:
    path = "/repos/example/repo/issues/51/labels/merge%20first"
    client = Client({path: GitHubError("not_found", "gone", 404)})
    # Removed nothing: gone already, or a repository the token cannot see: a person is told.
    assert lifter(client).execute(lift(["example/repo#51"])) is ActionResult.REFUSED
    other = "/repos/example/repo/issues/52/labels/merge%20first"
    partly = Client({path: GitHubError("not_found", "gone", 404)})
    assert lifter(partly).execute(lift(["example/repo#51", "example/repo#52"])) is (
        ActionResult.DONE
    )
    assert partly.calls[-1] == ("DELETE", other)
    # On no PR (held board tasks only): their board owns the label, a person is told.
    assert lifter(Client()).execute(lift([])) is ActionResult.REFUSED


@pytest.mark.parametrize(
    "action",
    [
        lift(["other/repo#1"]),  # not in lift_repos
        lift(["example/repo#1/../2"]),
        lift(["example/repo"]),
        lift(["example/repo#1"], hold=""),
        lift(["example/repo#1"], hold="x" * 101),
        lift(["example/repo#1"], hold_kind="flag"),  # a flag lives on the board
        lift("example/repo#1"),  # type: ignore[arg-type]
        Action(ActionKind.ALERT, "bottlenecks", "r", params={"hold": "h", "lift_prs": []}),
    ],
)
def test_anything_but_a_label_on_an_allowed_pr_is_refused(action: Action) -> None:
    client = Client()
    assert lifter(client).execute(action) is ActionResult.REFUSED
    assert client.calls == []


def test_github_refusals_refuse_and_transient_trouble_raises() -> None:
    path = "/repos/example/repo/issues/51/labels/merge%20first"
    denied = Client({path: GitHubError("forbidden", "no", 403)})
    assert lifter(denied).execute(lift(["example/repo#51"])) is ActionResult.REFUSED
    flaky = Client({path: GitHubError("server", "502", 502)})
    with pytest.raises(GitHubError):
        lifter(flaky).execute(lift(["example/repo#51"]))
    limited = Client({path: GitHubError("rate_limited", "secondary limit", 403)})
    with pytest.raises(GitHubError):  # GitHub's secondary rate limit answers 403: retried
        lifter(limited).execute(lift(["example/repo#51"]))


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ({"allow_lift_holds": "yes"}, "allow_lift_holds must be"),
        ({"allow_lift_holds": True, "lift_token_env": "T"}, "needs lift_repos"),
        ({"allow_lift_holds": True, "lift_repos": ["a/b"]}, "needs lift_token_env"),
        ({"lift_repos": ["not a repo"]}, "lift_repos must be"),
        ({"lift_token_env": "1BAD"}, "lift_token_env must be"),
    ],
)
def test_lift_settings_are_checked(raw: dict[str, Any], error: str) -> None:
    with pytest.raises(ValueError, match=error):
        load_pulls(raw)


def test_lifting_is_off_by_default() -> None:
    assert load_pulls({}).allow_lift_holds is False


def _settings(tmp_path: Any, extra: str = "") -> Any:
    from kelpie.app import load_settings

    path = tmp_path / "kelpie.toml"
    path.write_text(
        '[connectors.github]\nrepos = ["example/repo"]\n'
        "[connectors.github.pulls]\nallow_lift_holds = true\n"
        'lift_repos = ["example/repo"]\nlift_token_env = "KELPIE_TEST_LIFT"\n' + extra
    )
    return load_settings(path)


def test_the_lift_token_must_be_set_and_its_own(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kelpie.app import ConfigError, _hold_lifter

    settings = _settings(tmp_path)
    clock = lambda: datetime(2026, 3, 2, tzinfo=UTC)  # noqa: E731
    monkeypatch.delenv("KELPIE_TEST_LIFT", raising=False)
    with pytest.raises(ConfigError, match="is not set"):
        _hold_lifter(settings.github, clock, ())
    monkeypatch.setenv("KELPIE_TEST_LIFT", "synthetic-lift-token")
    monkeypatch.setenv(settings.github.token_env, "synthetic-lift-token")
    with pytest.raises(ConfigError, match="needs a token of its own") as exc:
        _hold_lifter(settings.github, clock, ())
    assert "synthetic-lift-token" not in str(exc.value)
    monkeypatch.setenv(settings.github.token_env, "synthetic-read-token")
    found = _hold_lifter(settings.github, clock, ())
    assert found.name == "github_pulls" and found.actions == frozenset({ActionKind.LIFT_HOLD})
    assert "KELPIE_TEST_LIFT" in os.environ


def test_lift_repos_must_be_read_repos(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from kelpie.app import ConfigError, _hold_lifter

    settings = _settings(tmp_path)
    pulls = settings.github.pulls
    from dataclasses import replace

    stray = replace(settings.github, pulls=replace(pulls, lift_repos=("other/repo",)))
    monkeypatch.setenv("KELPIE_TEST_LIFT", "synthetic-lift-token")
    with pytest.raises(ConfigError, match="lift_repos must be in"):
        _hold_lifter(stray, lambda: datetime(2026, 3, 2, tzinfo=UTC), ())


def test_bottlenecks_can_act_only_with_lifting_allowed(tmp_path: Any) -> None:
    from kelpie.app import act_blockers

    off = _settings(tmp_path).github
    from dataclasses import replace

    settings = _settings(tmp_path)
    assert "bottlenecks" not in act_blockers(settings, [])
    closed = replace(
        settings, github=replace(off, pulls=replace(off.pulls, allow_lift_holds=False))
    )
    blocker = act_blockers(closed, [])["bottlenecks"]
    assert "allow_lift_holds is off" in blocker.reason


def test_a_lift_is_no_platform_effect(tmp_path: Any) -> None:
    from kelpie.app import act_gaps
    from kelpie.rules import PLANNED

    class Board:
        name = "claude-code"
        actions = frozenset({ActionKind.COMMENT})

    gaps = act_gaps(_settings(tmp_path), [Board()])  # type: ignore[list-item]
    assert "bottlenecks" not in gaps
    assert ActionKind.LIFT_HOLD in next(p for p in PLANNED if p.name == "bottlenecks").effects
