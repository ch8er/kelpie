"""Linear issues as tasks, over HTTP to a local fake of Linear's GraphQL API: reading and the
mapping of workflow states, delegates and relations; the refresh; actions; limits."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.issues.adapter import IssuesAdapter, note
from kelpie.adapters.issues.config import load_linear
from kelpie.adapters.issues.linear import UNKNOWN, LinearClient, LinearError, LinearIssues
from kelpie.adapters.issues.mapping import Agent, Board, IssuesError
from kelpie.adapters.issues.writes import WriteGate
from kelpie.app import ConfigError, build_adapters, load_settings
from kelpie.core.model import Dependency, Status
from kelpie.core.ports import Deferred, UnsupportedAction
from tests.adapters.fake_linear import KEY, FakeLinear, iso, serve

NOW = datetime.now(UTC).replace(microsecond=0)
AGENTS = {
    "codex": Agent("codex", assignee="Codex"),  # a Linear agent: the issue is delegated to it
    "fixer": Agent("fixer", label="fix-me"),
}


def ago(**kw: float) -> datetime:
    return NOW - timedelta(**kw)


class Pacing:
    def __init__(self) -> None:
        self.now = 100.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def lin() -> Iterator[FakeLinear]:
    with serve() as fake:
        fake.team("ENG")
        fake.user("Codex", app=True)
        fake.user("dana")
        fake.label("blocked")
        fake.label("fix-me", "ENG")
        fake.label("fix-me")  # a workspace label of the same name: the team's wins
        yield fake


def adapter(
    lin: FakeLinear, boards: tuple[Board, ...] = (Board("ENG"),), **kw: Any
) -> IssuesAdapter:
    client = LinearClient(lin.url, KEY)
    pacing = Pacing()
    gate = WriteGate(sleep=pacing.sleep, monotonic=pacing.monotonic)
    agents = kw.setdefault("agents", AGENTS)
    reader = LinearIssues(client, boards, timedelta(days=7), gate=gate, **kw)
    return IssuesAdapter(reader, boards, agents, name="linear", clock=lambda: NOW)


def tasks(a: IssuesAdapter) -> dict[str, Any]:
    return {t.id: t for t in a.snapshot(NOW).tasks}


def test_workflow_states_delegates_and_relations(lin: FakeLinear) -> None:
    lin.issue("ENG-1", "Todo, nobody", ago(days=2))
    lin.issue(
        "ENG-2",
        "Delegated to Codex",
        ago(days=2),
        state="In Progress",
        state_type="started",
        delegate="Codex",
        updated=ago(minutes=5),
        history=[
            {
                "createdAt": iso(ago(hours=1)),
                "toDelegate": {"id": "user-codex", "displayName": "Codex", "app": True},
            },
            {
                "createdAt": iso(ago(minutes=30)),  # someone else under the agent's name
                "toDelegate": {"id": "user-impostor", "displayName": "Codex", "app": True},
            },
        ],
        attachments=[
            {
                "url": "https://github.com/acme/app/pull/7",
                "sourceType": "github",
                "metadata": {"status": "draft", "branch": "codex/eng-2"},
                "createdAt": iso(ago(minutes=50)),
            }
        ],
    )
    lin.issue(
        "ENG-3",
        "Started, nobody",
        ago(days=1),
        state="Doing",
        state_type="started",
        started=ago(hours=3),
    )
    lin.issue(
        "ENG-4",
        "Backlog",
        ago(days=1),
        state="Backlog",
        state_type="backlog",
        blockers=["ENG-1"],
        children=["ENG-1", "OPS-9"],
    )
    lin.issue(
        "ENG-5", "Done", ago(days=3), state="Done", state_type="completed", updated=ago(hours=2)
    )
    lin.issue(
        "ENG-6",
        "Blocked",
        ago(days=1),
        labels=["blocked"],
        history=[{"createdAt": iso(ago(hours=4)), "addedLabels": [{"name": "blocked"}]}],
    )
    assert tasks(adapter(lin))["ENG-1"].status is Status.BACKLOG  # Todo: nobody said ready
    a = adapter(lin, (Board("ENG", ready_states=("todo",)),))
    snap = a.snapshot(NOW)
    t = {task.id: task for task in snap.tasks}
    assert (t["ENG-1"].status, t["ENG-1"].owner_id) == (Status.READY, None)  # a ready state
    assert t["ENG-1"].platform_status == "open, Todo"
    assert (t["ENG-2"].status, t["ENG-2"].owner_id) == (Status.IN_PROGRESS, "codex")
    assert t["ENG-2"].status_since == ago(hours=1)  # when it was delegated
    assert t["ENG-2"].platform_status.endswith("draft pull request #7")
    assert t["ENG-2"].branch is None  # not taken from metadata that apps can write
    assert (t["ENG-3"].status, t["ENG-3"].owner_id) == (Status.IN_PROGRESS, None)
    assert t["ENG-3"].status_since == ago(hours=3)
    assert t["ENG-4"].status is Status.BACKLOG
    assert t["ENG-4"].subtasks == ()  # an open sub-issue in another team: not a task here
    assert t["ENG-5"].status is Status.DONE
    assert (t["ENG-6"].status, t["ENG-6"].status_since) == (Status.BLOCKED, ago(hours=4))
    assert Dependency("ENG-4", "ENG-1") in snap.dependencies
    assert t["ENG-1"].body_author == "dana"  # a member, not a guest or an app
    assert all(r["auth"] == KEY for r in lin.requests)


def test_reads_are_refreshed_at_most_every_refresh(lin: FakeLinear) -> None:
    lin.issue("ENG-1", "one", ago(days=1))
    a = adapter(lin, refresh=timedelta(minutes=2))
    a.snapshot(NOW)
    count = len(lin.requests)
    a.snapshot(NOW + timedelta(minutes=1))
    assert len(lin.requests) == count  # served from the last read
    a.snapshot(NOW + timedelta(minutes=3))
    assert len(lin.requests) > count


def test_a_write_makes_the_next_snapshot_read_again(lin: FakeLinear) -> None:
    lin.issue("ENG-1", "one", ago(days=1))
    a = adapter(lin)
    a.snapshot(NOW)
    a.comment("ENG-1", "hello")
    count = len(lin.requests)
    a.snapshot(NOW)
    assert "KelpieIssues" in lin.ops()[count:]


def test_requeue_restarts_a_delegated_agent(lin: FakeLinear) -> None:
    lin.issue("ENG-2", "x", ago(days=1), state_type="started", delegate="Codex")
    a = adapter(lin)
    a.snapshot(NOW)
    a.requeue("ENG-2", "it went quiet")
    updates = [
        r["variables"]["input"] for r in lin.requests if r["operationName"] == "KelpieUpdate"
    ]
    assert updates == [{"delegateId": None}, {"delegateId": "user-codex"}]
    assert lin.issues["ENG-2"].delegate == "Codex"
    assert lin.issues["ENG-2"].comments == [note("Restarted codex.", "it went quiet")]


def test_unblock_and_reassign_change_only_their_labels(lin: FakeLinear) -> None:
    lin.issue("ENG-6", "x", ago(days=1), labels=["blocked", "keep"])
    lin.issue("ENG-7", "y", ago(days=1), delegate="Codex")
    a = adapter(lin)
    a.snapshot(NOW)
    a.unblock("ENG-6", "its condition is met")
    assert lin.issues["ENG-6"].labels == ["keep"]
    a.reassign("ENG-7", "fixer", "codex had it for 3h")
    assert lin.issues["ENG-7"].labels == ["fix-me"] and lin.issues["ENG-7"].delegate is None
    added = [
        r["variables"]["input"]["addedLabelIds"]
        for r in lin.requests
        if r["operationName"] == "KelpieUpdate" and "addedLabelIds" in r["variables"]["input"]
    ]
    assert added == [["label-ENG-fix-me"]]  # the team's label, not the workspace's


def test_reopen_moves_to_the_first_unstarted_state(lin: FakeLinear) -> None:
    lin.issue("ENG-5", "x", ago(days=1), state="Done", state_type="completed", delegate="Codex")
    a = adapter(lin)
    a.snapshot(NOW)
    a.requeue("ENG-5", "done without evidence")
    assert (lin.issues["ENG-5"].state_type, lin.issues["ENG-5"].delegate) == ("unstarted", "Codex")


def test_failures_are_said_plainly(lin: FakeLinear) -> None:
    lin.issue("ENG-2", "x", ago(days=1), delegate="Codex")
    a = adapter(lin)
    a.snapshot(NOW)
    with pytest.raises(UnsupportedAction, match="was not a task"):
        a.comment("OPS-1", "x")
    with pytest.raises(IssuesError, match="not an issue of a configured team"):
        a.tracker.comment("OPS-1", "x")  # type: ignore[attr-defined]
    agents = {**AGENTS, "ghost": Agent("ghost", assignee="Ghost")}  # no such user in Linear
    a = adapter(lin, agents=agents)
    a.snapshot(NOW)
    with pytest.raises(IssuesError) as err:
        a.reassign("ENG-2", "ghost", "r")
    assert "Linear has no single user 'Ghost'" in str(err.value.public_reason)
    assert "KelpieUpdate" not in lin.ops()  # refused before any write


def test_a_rate_limit_defers_and_backs_off(lin: FakeLinear) -> None:
    lin.issue("ENG-1", "one", ago(days=1))
    a = adapter(lin)
    a.snapshot(NOW)
    lin.rate_limited = True
    with pytest.raises((Deferred, IssuesError)):
        a.comment("ENG-1", "x")
    count = len(lin.requests)
    with pytest.raises(Deferred):
        a.comment("ENG-1", "x")  # backing off: nothing is sent
    assert len(lin.requests) == count


def test_the_client_refuses_a_wrong_key_and_redirects(lin: FakeLinear) -> None:
    client = LinearClient(lin.url, "lin_api_wrong")
    with pytest.raises(LinearError) as err:
        client.query("KelpieUsers", "query", {"filter": {"or": []}})
    assert err.value.kind == "unauthorized" and "lin_api_wrong" not in str(err.value)
    assert "lin_api" not in repr(client)


def test_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lin: FakeLinear) -> None:
    settings = load_linear(
        {
            "teams": ["eng", "OPS"],
            "api_url": lin.url,
            "token_env": "LIN_KEY",
            "ready_labels": ["agent ready"],
            "agents": {"codex": {"assignee": "Codex"}, "fixer": {"label": "fix-me"}},
            "boards": {"OPS": {"label": "agent"}},
        }
    )
    assert [b.key for b in settings.boards] == ["ENG", "OPS"]
    assert settings.boards[1].label == "agent"
    assert settings.pull_repos == frozenset()
    states = load_linear(
        {"teams": ["ENG", "OPS"], "ready_states": ["Todo"], "boards": {"OPS": {"ready_states": []}}}
    )
    assert [b.ready_states for b in states.boards] == [("Todo",), ()]
    repos = load_linear({"teams": ["ENG"], "pull_repos": ["Acme/App", "ghe.example/acme/x"]})
    assert repos.pull_repos == {"github.com/acme/app", "ghe.example/acme/x"}
    for bad, message in (
        ({}, "teams"),
        ({"teams": ["eng-1"]}, "team key"),
        ({"teams": ["ENG"], "refresh": "5s"}, "refresh"),
        ({"teams": ["ENG"], "api_url": "http://127.0.0.1:9/graphql"}, "token_env is required"),
        ({"teams": ["ENG"], "agents": {"a": {"assignee": "x@y"}}}, "Linear display name"),
        ({"teams": ["ENG"], "boards": {"OPS": {}}}, "not in teams"),
        ({"teams": ["ENG"], "pull_repos": ["acme"]}, "not owner/repo"),
        ({"teams": ["ENG"], "ready_states": "Todo"}, "ready_states must be a list"),
        ({"teams": ["ENG"], "pull_repos": "acme/app"}, "pull_repos must be a list"),
    ):
        with pytest.raises(ValueError, match=message):
            load_linear(bad)
    with pytest.raises(ValueError, match=r"\$LIN_KEY is not set"):
        settings.build({})
    config = tmp_path / "kelpie.toml"
    config.write_text(
        f'[adapters.linear]\nteams = ["ENG"]\napi_url = "{lin.url}"\ntoken_env = "LIN_KEY"\n'
    )
    monkeypatch.setenv("LIN_KEY", KEY)
    (built,) = build_adapters(load_settings(config))
    assert built.name == "linear"
    monkeypatch.delenv("LIN_KEY")
    with pytest.raises(ConfigError, match="LIN_KEY"):
        build_adapters(load_settings(config))


def test_doctor(lin: FakeLinear) -> None:
    from kelpie.adapters.issues import doctor

    lin.issue("ENG-1", "one", ago(days=1))
    settings = load_linear({"teams": ["ENG"], "api_url": lin.url, "token_env": "LIN_KEY"})

    def run(settings: Any, environ: dict[str, str]) -> dict[str, str]:
        return {name: status for status, name, _, _ in doctor.run_linear(settings, NOW, environ)}

    assert run(settings, {"LIN_KEY": KEY}) == {
        doctor.LINEAR_TOKEN: "PASS",
        doctor.LINEAR_TEAMS: "PASS",
        doctor.LINEAR_NAMES: "PASS",
        doctor.LINEAR_READ: "WARN",  # no agents
    }
    assert set(run(settings, {}).values()) == {"FAIL", "SKIP"}
    wrong = load_linear(
        {
            "teams": ["ENG", "ENH"],  # a typo: Linear reads no issues, and says nothing
            "api_url": lin.url,
            "token_env": "LIN_KEY",
            "ready_labels": ["agent ready"],
            "agents": {"codex": {"assignee": "Codex"}, "ghost": {"assignee": "Nobody"}},
        }
    )
    found = {
        name: (status, detail)
        for status, name, detail, _ in doctor.run_linear(wrong, NOW, {"LIN_KEY": KEY})
    }
    assert found[doctor.LINEAR_TEAMS] == (
        "FAIL",
        "Linear has no team ENH, or the key cannot see it",
    )
    status, detail = found[doctor.LINEAR_NAMES]
    assert status == "WARN" and "ENG/agent ready" in detail and "Nobody" in detail
    assert "Codex" not in detail


def _pull(url: str, source: str | None = "github", **meta: Any) -> dict[str, Any]:
    return {"url": url, "sourceType": source, "metadata": meta, "createdAt": iso(ago(hours=1))}


def test_only_pull_requests_linked_by_the_github_integration_count(lin: FakeLinear) -> None:
    def started(identifier: str, *attachments: dict[str, Any]) -> None:
        lin.issue(
            identifier,
            "x",
            ago(days=2),
            state_type="started",
            delegate="Codex",
            attachments=list(attachments),
        )

    started("ENG-1", _pull("https://github.com/acme/app/pull/7", branch="main"))
    started("ENG-2", _pull("https://github.com/acme/app/pull/8", source="unknown"))  # pasted
    started("ENG-3", _pull("https://attacker.example/acme/app/pull/9"))
    started("ENG-4", _pull("https://ghe.example/acme/app/pull/10"))
    t = tasks(adapter(lin))
    assert (t["ENG-1"].status, t["ENG-1"].branch) == (Status.REVIEW, None)
    assert [t[k].status for k in ("ENG-2", "ENG-3", "ENG-4")] == [Status.IN_PROGRESS] * 3
    t = tasks(adapter(lin, pull_repos=frozenset({"ghe.example/acme/app"})))
    assert t["ENG-4"].status is Status.REVIEW
    assert t["ENG-1"].status is Status.IN_PROGRESS  # not a listed repository


def test_a_description_edited_by_a_guest_or_an_app_is_nobodys_word(lin: FakeLinear) -> None:
    member = {"app": False, "guest": False}
    for identifier, actor in (
        ("ENG-1", member),
        ("ENG-2", {"app": True, "guest": False}),  # the delegated agent
        ("ENG-3", {"app": False, "guest": True}),
        ("ENG-4", None),  # an integration or automation
    ):
        lin.issue(
            identifier,
            "x",
            ago(days=1),
            description="Done when 20/20 runs pass.",
            updated=ago(hours=1),
            history=[
                {"createdAt": iso(ago(hours=2)), "actor": member, "updatedDescription": True},
                {"createdAt": iso(ago(hours=1)), "actor": actor, "updatedDescription": True},
            ],
        )
    lin.issue("ENG-5", "never edited", ago(days=1), description="Done when it builds.")
    lin.issue(
        "ENG-6",
        "too much history to tell",
        ago(days=1),
        description="Done when it builds.",
        updated=ago(hours=1),
        history=[{"createdAt": iso(ago(hours=2)), "actor": member}] * 101,
    )
    t = tasks(adapter(lin))
    authors = {k: t[k].body_author for k in sorted(t)}
    assert authors == {
        "ENG-1": "dana",
        "ENG-2": None,
        "ENG-3": None,
        "ENG-4": None,
        "ENG-5": "dana",
        "ENG-6": None,
    }


def test_a_person_named_like_an_agent_is_not_the_agent(lin: FakeLinear) -> None:
    lin.user("codex[bot]")
    lin.issue("ENG-1", "x", ago(days=1), state_type="started", assignee="codex[bot]")
    lin.issue("ENG-2", "y", ago(days=1), state_type="started", assignee="dana", delegate="Codex")
    a = adapter(lin)
    t = tasks(a)
    assert t["ENG-1"].owner_id == UNKNOWN  # held by someone, not by codex
    a.reassign("ENG-2", "fixer", "codex had it for 3h")
    assert (lin.issues["ENG-2"].delegate, lin.issues["ENG-2"].assignee) == (None, "dana")


def test_writes_go_to_the_issue_that_was_checked(lin: FakeLinear) -> None:
    lin.issue("ENG-2", "x", ago(days=1), state_type="started", delegate="Codex")
    a = adapter(lin)
    a.snapshot(NOW)
    a.requeue("ENG-2", "quiet")
    writes = [r["variables"] for r in lin.requests if r["operationName"] == "KelpieUpdate"]
    assert {w["id"] for w in writes} == {"uuid-ENG-2"}
    (comment,) = [r for r in lin.requests if r["operationName"] == "KelpieComment"]
    assert comment["variables"]["input"]["issueId"] == "uuid-ENG-2"


def test_an_action_after_a_spent_read_budget_is_not_cut_short(lin: FakeLinear) -> None:
    lin.issue("ENG-2", "x", ago(days=1), state_type="started", delegate="Codex", labels=["a"])
    lin.issue("ENG-7", "y", ago(days=1), delegate="Codex")
    a = adapter(lin, max_requests=0)  # no history reads left
    a.snapshot(NOW)
    assert "KelpieHistory" not in lin.ops()
    a.reassign("ENG-7", "fixer", "r")
    a.requeue("ENG-2", "r")
    assert lin.issues["ENG-7"].labels == ["fix-me"] and lin.issues["ENG-2"].delegate == "Codex"
    (labels,) = [r for r in lin.requests if r["operationName"] == "KelpieLabels"]
    scope = labels["variables"]["filter"]["and"][1]["or"]
    assert scope == [{"team": {"key": {"eq": "ENG"}}}, {"team": {"null": True}}]


def test_state_and_label_names_are_printable(lin: FakeLinear) -> None:
    lin.issue("ENG-1", "x", ago(days=1), state="Todo\n@everyone", labels=["ok", "b‮idi"])
    t = tasks(adapter(lin))
    assert "\n" not in t["ENG-1"].platform_status and "everyone" not in t["ENG-1"].platform_status


def test_odd_rate_limit_headers_are_ignored(lin: FakeLinear) -> None:
    client = LinearClient(lin.url, KEY, clock=lambda: NOW)
    for value in ("²", "9" * 5000, "-", ""):
        client._note_limits({"X-RateLimit-Requests-Remaining": value}, NOW)
        assert client._reset({"X-RateLimit-Requests-Reset": value}, NOW) is None
    assert client.backoff_until is None
    client._note_limits(
        {
            "X-RateLimit-Requests-Remaining": "0",
            "X-RateLimit-Requests-Reset": str(int((NOW.timestamp() + 60) * 1000)),
        },
        NOW,
    )
    assert client.backoff_until == NOW + timedelta(seconds=60)


def test_plain_http_never_goes_through_a_proxy(
    lin: FakeLinear, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # nothing listens there
    lin.user("dana")
    client = LinearClient(lin.url, KEY)
    filter_ = {"or": [{"displayName": {"eqIgnoreCase": "dana"}}]}
    assert client.query("KelpieUsers", "query", {"filter": filter_})["users"]["nodes"]


# --- the review's findings ---------------------------------------------------------------


# Fields that hold one object; any other field with a selection and no ``first`` is a list,
# counted as Linear's default page of 50 objects.
_SINGLE = {
    *("state", "team", "creator", "botActor", "assignee", "delegate", "issue", "actor"),
    *("fromAssignee", "toAssignee", "fromDelegate", "toDelegate", "fromState", "toState"),
    *("pageInfo", "nodes", "issueUpdate", "commentCreate"),
}


def _estimate(document: str) -> float:
    """Linear's query complexity, as its docs describe it: 0.1 a scalar field, 1 an object,
    and a list's contents times its ``first`` (50 when it has none). Fragments are spread in
    place."""
    import re

    tokens = re.findall(r"\.\.\.\w+|[{}()]|\w+", re.sub(r"\$\w+", "", document))
    fragments: dict[str, list[str]] = {}
    i = 0
    operations: list[list[str]] = []
    while i < len(tokens):
        if tokens[i] == "fragment":
            name, start = tokens[i + 1], tokens.index("{", i)
            body, depth, j = [], 0, start
            while True:
                depth += {"{": 1, "}": -1}.get(tokens[j], 0)
                body.append(tokens[j])
                if depth == 0:
                    break
                j += 1
            fragments[name], i = body, j + 1
        elif tokens[i] in ("query", "mutation"):
            start = tokens.index("{", tokens.index(")", i) if "(" in tokens[i : i + 3] else i)
            body, depth, j = [], 0, start
            while True:
                depth += {"{": 1, "}": -1}.get(tokens[j], 0)
                body.append(tokens[j])
                if depth == 0:
                    break
                j += 1
            operations.append(body)
            i = j + 1
        else:
            i += 1

    def cost(body: list[str]) -> float:  # body: "{" ... "}"
        total, i = 0.0, 1
        while i < len(body) - 1:
            token = body[i]
            if token.startswith("..."):
                total += cost(fragments[token[3:]])
                i += 1
                continue
            first, paged, i = (1 if token in _SINGLE else 50), False, i + 1
            if i < len(body) and body[i] == "(":
                close = body.index(")", i)
                args = body[i + 1 : close]
                if "first" in args:
                    first, paged = int(args[args.index("first") + 1]), True
                i = close + 1
            if i < len(body) and body[i] == "{":
                depth, j = 0, i
                while True:
                    depth += {"{": 1, "}": -1}.get(body[j], 0)
                    if depth == 0:
                        break
                    j += 1
                if paged:  # a connection: its nodes are the objects
                    total += 1 + first * cost(body[i : j + 1])
                elif token in _SINGLE:
                    total += 1 + cost(body[i : j + 1])
                else:  # a plain list: each element an object
                    total += 1 + first * (1 + cost(body[i : j + 1]))
                i = j + 1
            else:
                total += 0.1
        return total

    return max(cost(op) for op in operations)


def test_every_query_stays_under_linears_complexity_limit() -> None:
    from kelpie.adapters.issues import linear

    documents = [linear.ISSUE, linear.HISTORY, linear.LABELS, linear.USERS, linear.STATES]
    documents += [linear.UPDATE, linear.COMMENT, linear.TEAMS]
    # The page of issues is sized by a variable: the estimate takes PAGE.
    page = linear.ISSUES.replace("first: $first", f"first: {linear.PAGE}")
    for document in [page, *documents]:
        assert _estimate(document) < linear.MAX_COMPLEXITY * 0.75, document[:80]
    assert _estimate(page) < linear.MAX_COMPLEXITY / 2  # the one read most often
    assert _estimate("query Q { a(first: 10) { nodes { b c } } }") == pytest.approx(
        1 + 10 * (1 + 0.2)
    )
    assert _estimate("query Q { issue { labels { name } } }") == pytest.approx(1 + 1 + 50 * 1.1)


def test_lists_of_every_team_come_before_any_history(lin: FakeLinear) -> None:
    lin.team("OPS")
    for team in ("ENG", "OPS"):
        lin.issue(f"{team}-1", "x", ago(days=1), labels=["a"])
    adapter(lin, (Board("ENG"), Board("OPS"))).snapshot(NOW)
    ops = [op for op in lin.ops() if op != "KelpieUsers"]  # the agents' users, first
    assert ops[:4] == ["KelpieIssues"] * 4 and ops[4:] == ["KelpieHistory"] * 2


def test_history_is_read_within_the_budget(lin: FakeLinear) -> None:
    for n in range(1, 6):
        lin.issue(f"ENG-{n}", "x", ago(days=1), labels=["a"])
    a = adapter(lin, max_requests=3)
    a.snapshot(NOW)
    assert lin.ops().count("KelpieHistory") == 3
    a.snapshot(NOW + timedelta(minutes=3))  # the rest next time; the first three are kept
    assert lin.ops().count("KelpieHistory") == 5


def test_a_team_that_cannot_be_read_keeps_its_last_read_for_a_while(lin: FakeLinear) -> None:
    lin.team("OPS")
    lin.issue("ENG-1", "x", ago(days=1))
    lin.issue("OPS-1", "y", ago(days=1))
    a = adapter(lin, (Board("ENG"), Board("OPS")))
    assert set(tasks(a)) == {"ENG-1", "OPS-1"}
    lin.broken_teams.add("OPS")
    later = NOW + timedelta(minutes=3)
    assert {t.id for t in a.snapshot(later).tasks} == {"ENG-1", "OPS-1"}
    assert "OPS: last read" in (a.tracker.problem or "")  # type: ignore[attr-defined]
    much_later = NOW + timedelta(minutes=15)
    assert {t.id for t in a.snapshot(much_later).tasks} == {"ENG-1"}
    assert "OPS: not read" in (a.tracker.problem or "")  # type: ignore[attr-defined]


def test_no_team_read_raises_and_is_not_asked_again_before_the_refresh(lin: FakeLinear) -> None:
    lin.broken_teams.add("ENG")
    reader = adapter(lin).tracker
    with pytest.raises(IssuesError):
        reader.read(NOW)
    count = len(lin.requests)
    with pytest.raises(IssuesError):
        reader.read(NOW + timedelta(minutes=1))
    assert len(lin.requests) == count
    lin.broken_teams.clear()
    assert reader.read(NOW + timedelta(minutes=3)) == []


def test_reads_leave_the_end_of_the_hours_limits_to_writes(lin: FakeLinear) -> None:
    lin.issue("ENG-2", "x", ago(days=1), state_type="started", delegate="Codex")
    a = adapter(lin)
    a.snapshot(NOW)
    lin.headers = {"X-RateLimit-Complexity-Remaining": "20000"}
    a.comment("ENG-2", "first")  # Linear now says little is left
    count = len(lin.requests)
    a.snapshot(NOW + timedelta(minutes=3))
    assert len(lin.requests) == count  # no read: the rest is for writes
    assert "kept for writes" in (a.tracker.problem or "")  # type: ignore[attr-defined]
    a.requeue("ENG-2", "quiet")  # writes and their lookups still go
    assert lin.issues["ENG-2"].comments[-1].startswith("Restarted codex.")


def test_the_refresh_stretches_to_keep_reads_within_a_share_of_the_hour(
    lin: FakeLinear,
) -> None:
    lin.issue("ENG-1", "x", ago(days=1))
    lin.headers = {"X-Complexity": "5000", "X-RateLimit-Complexity-Limit": "300000"}
    a = adapter(lin)
    a.snapshot(NOW)  # users and two pages: 15,000 points; half of 300,000 an hour: one in 6m
    count = len(lin.requests)
    a.snapshot(NOW + timedelta(minutes=5))
    assert len(lin.requests) == count
    a.snapshot(NOW + timedelta(minutes=7))
    assert len(lin.requests) > count


def test_a_used_up_limit_backs_off_until_its_own_reset(lin: FakeLinear) -> None:
    client = LinearClient(lin.url, KEY, clock=lambda: NOW)
    soon, late = NOW + timedelta(minutes=5), NOW + timedelta(minutes=50)
    client._note_limits(
        {
            "X-RateLimit-Requests-Remaining": "900",
            "X-RateLimit-Requests-Reset": str(int(late.timestamp() * 1000)),
            "X-RateLimit-Complexity-Remaining": "0",
            "X-RateLimit-Complexity-Reset": str(int(soon.timestamp() * 1000)),
        },
        NOW,
    )
    assert client.backoff_until == soon


def test_lookups_are_made_before_the_first_write(lin: FakeLinear) -> None:
    lin.issue("ENG-5", "x", ago(days=1), state="Done", state_type="completed", delegate="Codex")
    a = adapter(lin)
    a.snapshot(NOW)
    start = len(lin.requests)
    a.requeue("ENG-5", "done without evidence")
    ops = lin.ops()[start:]
    first_write = ops.index("KelpieUpdate")
    assert {"KelpieLabels", "KelpieStates"} <= set(ops[:first_write])  # users: at the read
    assert not {"KelpieLabels", "KelpieUsers", "KelpieStates"} & set(ops[first_write:])


def test_a_lookup_linear_cannot_answer_now_defers_the_whole_action(lin: FakeLinear) -> None:
    lin.issue("ENG-2", "x", ago(days=1), state_type="started", delegate="Codex")
    a = adapter(lin)
    a.snapshot(NOW)
    lin.limited_ops = {"KelpieLabels"}
    with pytest.raises(Deferred):
        a.requeue("ENG-2", "quiet")
    assert "KelpieUpdate" not in lin.ops() and lin.issues["ENG-2"].delegate == "Codex"


def test_a_rate_limit_on_the_first_write_defers_and_changes_nothing(lin: FakeLinear) -> None:
    lin.issue("ENG-1", "one", ago(days=1))
    a = adapter(lin)
    a.snapshot(NOW)
    lin.limited_ops = {"KelpieComment"}
    with pytest.raises(Deferred):
        a.comment("ENG-1", "x")
    assert lin.issues["ENG-1"].comments == []


def test_a_person_assigned_is_not_replaced(lin: FakeLinear) -> None:
    lin.user("rob")
    lin.issue("ENG-1", "x", ago(days=1), assignee="dana")
    reader = adapter(lin).tracker
    reader.fetch("ENG-1")  # type: ignore[attr-defined]
    with pytest.raises(IssuesError) as err:
        reader.assign("ENG-1", ["rob"])  # type: ignore[attr-defined]
    assert "assigned to someone else" in str(err.value.public_reason)
    assert lin.issues["ENG-1"].assignee == "dana"


def test_an_assignment_linear_did_not_make_is_an_error(lin: FakeLinear) -> None:
    lin.issue("ENG-1", "x", ago(days=1))
    reader = adapter(lin).tracker
    reader.fetch("ENG-1")  # type: ignore[attr-defined]
    lin.ignore_slots = True
    with pytest.raises(IssuesError) as err:
        reader.assign("ENG-1", ["Codex"])  # type: ignore[attr-defined]
    assert err.value.public_reason == "Linear did not delegate to Codex"


def test_reopen_falls_back_to_a_backlog_state(lin: FakeLinear) -> None:
    lin.states["ENG"] = [s for s in lin.states["ENG"] if s["type"] != "unstarted"]
    lin.issue("ENG-5", "x", ago(days=1), state="Done", state_type="completed")
    reader = adapter(lin).tracker
    reader.reopen("ENG-5")  # type: ignore[attr-defined]
    assert lin.issues["ENG-5"].state_type == "backlog"


def test_history_gives_closes_reopens_and_merged_pull_requests_are_not_open(
    lin: FakeLinear,
) -> None:
    lin.issue(
        "ENG-1",
        "x",
        ago(days=2),
        delegate="Codex",
        history=[
            {"createdAt": iso(ago(hours=5)), "toState": {"type": "completed"}},
            {
                "createdAt": iso(ago(hours=2)),
                "fromState": {"type": "completed"},
                "toState": {"type": "unstarted"},
            },
        ],
        attachments=[_pull("https://github.com/acme/app/pull/3", status="merged")],
    )
    reader = adapter(lin).tracker
    (item,) = reader.read(NOW)  # type: ignore[attr-defined]
    assert [(e.kind, e.at) for e in item.events] == [
        ("closed", ago(hours=5)),
        ("reopened", ago(hours=2)),
    ]
    assert item.pulls == ()


def test_closed_issues_are_the_most_recently_updated(lin: FakeLinear) -> None:
    from kelpie.adapters.issues.linear import PAGE

    for n in range(1, PAGE + 2):  # more than a page, ENG-1 the oldest but closed last
        lin.issue(
            f"ENG-{n}",
            "x",
            ago(days=6, hours=n),
            state="Done",
            state_type="completed",
            updated=ago(minutes=1) if n == 1 else ago(days=2),
        )
    assert "ENG-1" in tasks(adapter(lin))


def test_the_client_says_what_went_wrong(lin: FakeLinear) -> None:
    for status, kind in ((429, "rate_limited"), (502, "server"), (302, "redirect")):
        lin.status = status
        lin.headers = {"Location": "http://127.0.0.1:9/elsewhere"} if status == 302 else {}
        client = LinearClient(lin.url, KEY, clock=lambda: NOW)
        with pytest.raises(LinearError) as err:
            client.query("KelpieTeams", "query", {"keys": []})
        assert err.value.kind == kind
        assert (client.backoff_until is not None) == (kind != "redirect")


# --- the security re-check's findings ----------------------------------------------------


@pytest.mark.parametrize(
    "name", ["Zoe \U0001f469‍\U0001f4bb", "x" * 101, "rob[bot]", "codex[bot] ", " Codex"]
)
def test_a_person_under_a_name_kelpie_does_not_take_still_holds_the_issue(
    lin: FakeLinear, name: str
) -> None:
    lin.user(name)
    lin.user("worker")
    lin.issue("ENG-1", "x", ago(days=1), assignee=name)
    a = adapter(lin, (Board("ENG", ready_states=("Todo",)),))
    t = tasks(a)["ENG-1"]
    assert (t.status, t.owner_id) == (Status.IN_PROGRESS, UNKNOWN)  # not free, not codex
    reader = a.tracker
    reader.fetch("ENG-1")  # type: ignore[attr-defined]
    with pytest.raises(IssuesError, match="assigned elsewhere"):
        reader.assign("ENG-1", ["worker"])  # type: ignore[attr-defined]
    assert lin.issues["ENG-1"].assignee == name


def test_an_agents_name_on_another_user_is_not_the_agent(lin: FakeLinear) -> None:
    lin.issue("ENG-1", "x", ago(days=1), state_type="started", delegate="Codex")
    a = adapter(lin)
    reader = a.tracker
    reader._users["codex"] = ("user-the-real-codex", True)  # type: ignore[attr-defined]
    reader._looked_up = datetime.now(UTC)  # type: ignore[attr-defined]
    assert tasks(a)["ENG-1"].owner_id == UNKNOWN


def test_another_apps_delegation_is_not_replaced(lin: FakeLinear) -> None:
    lin.user("Helper", app=True)  # an app, but none of the agents
    lin.issue("ENG-1", "x", ago(days=1), delegate="Helper")
    reader = adapter(lin).tracker
    reader.fetch("ENG-1")  # type: ignore[attr-defined]
    with pytest.raises(IssuesError, match="delegated elsewhere"):
        reader.assign("ENG-1", ["Codex"])  # type: ignore[attr-defined]
    assert lin.issues["ENG-1"].delegate == "Helper"


def test_reads_resume_once_the_hours_figures_are_past(lin: FakeLinear) -> None:
    now = [NOW]
    client = LinearClient(lin.url, KEY, clock=lambda: now[0])
    client._note_limits({"X-RateLimit-Complexity-Remaining": "1000"}, NOW)
    assert not client.may_read()
    now[0] = NOW + timedelta(minutes=6)  # nothing said since: ask Linear again
    assert client.may_read()
    reset = NOW + timedelta(minutes=2)
    client._note_limits(
        {
            "X-RateLimit-Requests-Remaining": "10",
            "X-RateLimit-Requests-Reset": str(int(reset.timestamp() * 1000)),
        },
        NOW,
    )
    now[0] = NOW + timedelta(minutes=1)
    assert not client.may_read()
    now[0] = NOW + timedelta(minutes=2)
    assert client.may_read()


@pytest.mark.parametrize(
    ("cost", "limit"), [("1000000000", "3000000"), ("999999999999999", "1"), ("5000", "0")]
)
def test_odd_complexity_headers_stretch_the_refresh_an_hour_at_most(
    lin: FakeLinear, cost: str, limit: str
) -> None:
    lin.issue("ENG-1", "x", ago(days=1))
    lin.headers = {"X-Complexity": cost, "X-RateLimit-Complexity-Limit": limit}
    a = adapter(lin)
    a.snapshot(NOW)
    count = len(lin.requests)
    a.snapshot(NOW + timedelta(minutes=61))
    assert len(lin.requests) > count


def test_history_of_an_odd_shape_is_read_as_far_as_it_makes_sense() -> None:
    from kelpie.adapters.issues.linear import _events

    at = iso(ago(hours=1))
    assert (
        _events(
            [{"createdAt": at, "addedLabels": 5, "removedLabels": [None, {"name": 3}]}],
            lambda user, delegate: None,
        )
        == ()
    )


def test_issues_and_edits_through_an_integration_are_nobodys_word(lin: FakeLinear) -> None:
    lin.issue(
        "ENG-1", "synced", ago(days=1), description="Done when ok.", bot_actor={"id": "slack"}
    )
    lin.issue(
        "ENG-2",
        "edited through a sync",
        ago(days=1),
        description="Done when ok.",
        updated=ago(hours=1),
        history=[
            {
                "createdAt": iso(ago(hours=1)),
                "actor": {"app": False, "guest": False},
                "botActor": {"id": "github"},
                "updatedDescription": True,
            }
        ],
    )
    t = tasks(adapter(lin))
    assert (t["ENG-1"].body_author, t["ENG-2"].body_author) == (None, None)


def test_only_a_closed_issue_needs_the_state_to_reopen_to(lin: FakeLinear) -> None:
    lin.issue("ENG-1", "x", ago(days=1))
    lin.states.pop("ENG")  # a team without states would refuse a reopen, not a comment
    a = adapter(lin)
    a.snapshot(NOW)
    a.comment("ENG-1", "hello")
    assert "KelpieStates" not in lin.ops() and len(lin.issues["ENG-1"].comments) == 1


def test_config_refuses_agent_names_that_never_match() -> None:
    for name in ("codex[bot]", "Zoe​"):
        with pytest.raises(ValueError, match="not ending in"):
            load_linear({"teams": ["ENG"], "agents": {"a": {"assignee": name}}})


# --- the code re-check's findings ---------------------------------------------------------


def test_an_agent_whose_label_linear_lacks_is_refused_before_any_write(lin: FakeLinear) -> None:
    lin.user("Rob")
    agents = {"both": Agent("both", assignee="Rob", label="gone"), **AGENTS}
    lin.issue("ENG-1", "x", ago(days=1), state_type="started", assignee="Rob")
    a = adapter(lin, agents=agents)
    a.snapshot(NOW)
    with pytest.raises(IssuesError) as err:
        a.requeue("ENG-1", "quiet")
    assert "has no label 'gone'" in str(err.value.public_reason)
    assert "KelpieUpdate" not in lin.ops() and lin.issues["ENG-1"].assignee == "Rob"


def test_unblocking_a_label_found_for_no_team_of_ours_says_nothing_came_off(
    lin: FakeLinear,
) -> None:
    lin.label("held", "PARENT")  # a parent team's label on the issue: not found for ENG
    lin.issue("ENG-1", "x", ago(days=1), labels=["held"])
    a = adapter(lin, (Board("ENG", blocked_labels=("held",)),))
    a.snapshot(NOW)
    with pytest.raises(IssuesError) as err:
        a.unblock("ENG-1", "its condition is met")
    assert "has no label 'held'" in str(err.value.public_reason)
    assert lin.issues["ENG-1"].labels == ["held"] and lin.issues["ENG-1"].comments == []


def test_a_person_whose_name_folds_to_an_agents_is_not_replaced(lin: FakeLinear) -> None:
    lin.user("Strasse")
    lin.user("Straße")
    agents = {"st": Agent("st", assignee="Strasse"), **AGENTS}
    lin.issue("ENG-1", "x", ago(days=1), state_type="started", assignee="Straße")
    a = adapter(lin, agents=agents)
    assert tasks(a)["ENG-1"].owner_id == "Straße"  # a person
    with pytest.raises(IssuesError, match="assigned elsewhere"):
        a.reassign("ENG-1", "st", "r")
    assert lin.issues["ENG-1"].assignee == "Straße"


def test_one_person_agent_is_handed_over_to_another(lin: FakeLinear) -> None:
    lin.user("Rob")
    lin.user("Sam")
    agents = {"rob": Agent("rob", assignee="Rob"), "sam": Agent("sam", assignee="Sam")}
    lin.issue("ENG-1", "x", ago(days=1), state_type="started", assignee="Rob")
    a = adapter(lin, agents=agents)
    a.snapshot(NOW)
    a.reassign("ENG-1", "sam", "rob had it for 3h")
    assert lin.issues["ENG-1"].assignee == "Sam"


def test_a_rate_limit_backs_off_until_the_reset_of_the_limit_that_ran_out(
    lin: FakeLinear,
) -> None:
    soon, late = NOW + timedelta(minutes=3), NOW + timedelta(minutes=40)
    lin.rate_limited = True
    lin.headers = {
        "X-RateLimit-Requests-Remaining": "500",
        "X-RateLimit-Requests-Reset": str(int(late.timestamp() * 1000)),
        "X-RateLimit-Complexity-Remaining": "0",
        "X-RateLimit-Complexity-Reset": str(int(soon.timestamp() * 1000)),
    }
    client = LinearClient(lin.url, KEY, clock=lambda: NOW)
    with pytest.raises(LinearError):
        client.query("KelpieTeams", "query", {"keys": []})
    assert client.backoff_until == soon


def test_writes_do_not_bring_reads_forward_once_reads_are_stretched(lin: FakeLinear) -> None:
    lin.issue("ENG-1", "x", ago(days=1))
    lin.headers = {"X-Complexity": "9000", "X-RateLimit-Complexity-Limit": "300000"}
    a = adapter(lin)
    a.snapshot(NOW)  # 27,000 points; half of 300,000 an hour: one read in about 11m
    a.comment("ENG-1", "hello")
    count = len(lin.requests)
    a.snapshot(NOW + timedelta(minutes=1))
    assert "KelpieIssues" not in lin.ops()[count:]


def test_doctor_names_ready_states_a_team_lacks(lin: FakeLinear) -> None:
    from kelpie.adapters.issues import doctor

    settings = load_linear(
        {"teams": ["ENG"], "api_url": lin.url, "token_env": "K", "ready_states": ["To Do"]}
    )
    found = {name: (s, d) for s, name, d, _ in doctor.run_linear(settings, NOW, {"K": KEY})}
    status, detail = found[doctor.LINEAR_NAMES]
    assert status == "WARN" and "ready_states not defined: ENG/To Do" in detail


def test_config_refuses_an_author_for_a_linear_agent() -> None:
    with pytest.raises(ValueError, match="no pull request author"):
        load_linear({"teams": ["ENG"], "agents": {"a": {"author": "someone"}}})


def test_the_agents_stay_known_when_the_hourly_lookup_fails(lin: FakeLinear) -> None:
    lin.issue("ENG-2", "x", ago(days=1), state_type="started", delegate="Codex")
    a = adapter(lin)
    assert tasks(a)["ENG-2"].owner_id == "codex"
    a.tracker._looked_up = NOW - timedelta(hours=2)  # type: ignore[attr-defined]
    lin.limited_ops = {"KelpieUsers"}
    lin.rate_limited = False
    snap = a.snapshot(NOW + timedelta(minutes=3))
    assert {t.id: t.owner_id for t in snap.tasks} == {"ENG-2": "codex"}


def test_a_teams_read_waits_until_the_hour_has_room_for_it_and_the_reserve(
    lin: FakeLinear,
) -> None:
    lin.issue("ENG-1", "x", ago(days=1))
    lin.headers = {"X-Complexity": "5000"}  # each request, users and two pages: 15,000
    a = adapter(lin)
    a.snapshot(NOW)
    lin.headers = {"X-RateLimit-Complexity-Remaining": "59000"}  # room for the reserve only
    a.comment("ENG-1", "x")
    count = len(lin.requests)
    a.snapshot(NOW + timedelta(minutes=3))
    assert "KelpieIssues" not in lin.ops()[count:]  # 50,000 kept + 10,000 last read > 59,000
    assert "kept for writes" in (a.tracker.problem or "")  # type: ignore[attr-defined]
