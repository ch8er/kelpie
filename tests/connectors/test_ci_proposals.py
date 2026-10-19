"""``propose_ci_config`` on GitHub: a draft pull request with rule 9's changes written into the
workflow file, behind an allow-list and a token of its own; nothing twice, nothing left
half-made, and only answers that retrying will not change refuse."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import pytest
import yaml

from kelpie.connectors.github.builds import SOURCE
from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.connectors.github.proposals import Proposer, preview
from kelpie.connectors.github.workflow_config import render
from kelpie.core.actions import Action, ActionKind, ActionResult
from tests.connectors.fake_github import TOKEN, FakeGitHub, serve
from tests.connectors.test_workflow_config import TYPICAL

REPO = "example/app"
PATH = ".github/workflows/test.yml"
BRANCH = "kelpie/ci-config/test.yml"
OPEN = f"/repos/{REPO}/pulls?" + urlencode({"head": f"example:{BRANCH}", "state": "open"})
CLOSED = f"/repos/{REPO}/pulls?" + urlencode({"head": f"example:{BRANCH}", "state": "closed"})
REF = f"/repos/{REPO}/git/ref/heads/{BRANCH}"
COMMIT = "e" * 40  # the commit Kelpie makes on its branch
CHANGES = [
    {"change": "skip_drafts", "minutes": 25.0, "builds": 2},
    {"change": "ignore_docs_paths", "minutes": 9.0, "builds": 1, "paths": ["docs/*", "*.md"]},
    {"change": "skip_verified", "minutes": 6.0, "builds": 1},
]


@pytest.fixture
def gh() -> Iterator[FakeGitHub]:
    with serve() as fake:
        fake.reply(f"/repos/{REPO}", {"default_branch": "main"})
        fake.reply(OPEN, [])
        content = base64.b64encode(TYPICAL.encode()).decode()
        fake.reply(
            f"/repos/{REPO}/contents/{PATH}?ref=main",
            {"encoding": "base64", "content": content, "sha": "blob-1"},
        )
        fake.reply(f"/repos/{REPO}/git/ref/heads/main", {"object": {"sha": "c" * 40}})
        fake.reply(f"POST /repos/{REPO}/git/refs", {"ref": f"refs/heads/{BRANCH}"}, 201)
        fake.reply(f"PUT /repos/{REPO}/contents/{PATH}", {"commit": {"sha": COMMIT}}, 200)
        fake.reply(f"POST /repos/{REPO}/pulls", {"number": 9}, 201)
        fake.reply(f"DELETE /repos/{REPO}/git/refs/heads/{BRANCH}", None, 204)
        yield fake


def proposer(gh: FakeGitHub, repos: tuple[str, ...] = (REPO,)) -> Proposer:
    return Proposer(GitHubClient(gh.url, TOKEN), repos)


def action(**params: Any) -> Action:
    base: dict[str, Any] = {"repo": REPO, "workflow": PATH, "minutes": 40.0, "changes": CHANGES}
    base.update(params)
    return Action(
        ActionKind.PROPOSE_CI_CONFIG, "ci_economy", "wasted", executor=SOURCE, params=base
    )


def writes(gh: FakeGitHub) -> list[tuple[str, str, Any]]:
    return [(s.method, s.path, s.body) for s in gh.requests if s.method != "GET"]


def test_a_draft_pull_request_with_the_changes_written_in(gh: FakeGitHub) -> None:
    assert proposer(gh).execute(action()) is ActionResult.DONE
    (ref, put, pr) = writes(gh)
    assert ref == (
        "POST",
        f"/repos/{REPO}/git/refs",
        {"ref": f"refs/heads/{BRANCH}", "sha": "c" * 40},
    )
    assert put[1] == f"/repos/{REPO}/contents/{PATH}"
    assert (put[2]["branch"], put[2]["sha"]) == (BRANCH, "blob-1")  # the file read
    workflow = yaml.safe_load(base64.b64decode(put[2]["content"]).decode())
    assert workflow[True]["pull_request"]["paths-ignore"] == ["docs/**", "**.md"]
    assert pr[2]["draft"] is True and (pr[2]["head"], pr[2]["base"]) == (BRANCH, "main")
    text = pr[2]["body"]
    assert "- **Skip draft pull requests**: would have saved 25 min (2 builds)." in text
    assert "- **Skip changes that touch only docs**: would have saved 9 min (1 build)." in text
    assert "Skip builds of a commit that already passed: GitHub Actions has no setting" in text
    assert "required status check" in text  # the paths-ignore warning
    assert "marked ready (auto-merge, an agent)" in text  # the drafts warning
    assert "Kelpie never merges it" in text


def test_an_open_proposal_or_a_branch_left_from_one_is_not_proposed_again(
    gh: FakeGitHub,
) -> None:
    gh.reply(OPEN, [{"number": 9}])
    assert proposer(gh).execute(action()) is ActionResult.NOTHING_TO_DO
    assert writes(gh) == []
    gh.reply(OPEN, [])
    gh.reply(f"POST /repos/{REPO}/git/refs", {"message": "Reference already exists"}, 422)
    gh.reply(REF, {"object": {"sha": "d" * 40}})
    gh.reply(CLOSED, [{"number": 3}])
    assert proposer(gh).execute(action()) is ActionResult.NOTHING_TO_DO  # a human closed it
    assert [w[0] for w in writes(gh)] == ["POST"]  # the branch, refused; nothing more


def test_a_branch_github_will_not_create_is_refused_not_taken_for_a_proposal(
    gh: FakeGitHub,
) -> None:
    gh.reply(f"POST /repos/{REPO}/git/refs", {"message": "Reference name invalid"}, 422)
    assert proposer(gh).execute(action()) is ActionResult.REFUSED  # no such branch exists


def left_behind(gh: FakeGitHub, ahead: int, files: list[str], text: str) -> None:
    """The branch an attempt could not take back (e.g. while rate limited)."""
    gh.reply(f"POST /repos/{REPO}/git/refs", {"message": "Reference already exists"}, 422)
    gh.reply(REF, {"object": {"sha": COMMIT}})
    gh.reply(CLOSED, [])
    gh.reply(
        f"/repos/{REPO}/compare/main...{BRANCH}",
        {"ahead_by": ahead, "files": [{"filename": f} for f in files]},
    )
    gh.reply(
        f"/repos/{REPO}/contents/{PATH}?" + urlencode({"ref": BRANCH}),
        {"encoding": "base64", "content": base64.b64encode(text.encode()).decode(), "sha": "x"},
    )


def test_a_proposal_left_half_made_is_carried_on(gh: FakeGitHub) -> None:
    left_behind(gh, 1, [PATH], render(TYPICAL, CHANGES, "main").text)
    assert proposer(gh).execute(action()) is ActionResult.DONE
    assert [w[:2] for w in writes(gh)] == [
        ("POST", f"/repos/{REPO}/git/refs"),
        ("POST", f"/repos/{REPO}/pulls"),  # the commit is there already
    ]
    gh.requests.clear()
    left_behind(gh, 0, [], TYPICAL)  # made, but nothing committed on it yet
    assert proposer(gh).execute(action()) is ActionResult.DONE
    assert [w[0] for w in writes(gh)] == ["POST", "PUT", "POST"]


@pytest.mark.parametrize(
    ("ahead", "files", "text"),
    [
        (1, [PATH], TYPICAL),  # another change to the file
        (1, [PATH, "src/app.py"], None),  # more than the workflow
        (2, [PATH], None),  # more than one commit
    ],
)
def test_someone_elses_branch_of_that_name_is_left_alone(
    gh: FakeGitHub, ahead: int, files: list[str], text: str | None
) -> None:
    left_behind(gh, ahead, files, text or render(TYPICAL, CHANGES, "main").text)
    assert proposer(gh).execute(action()) is ActionResult.REFUSED
    assert [w[0] for w in writes(gh)] == ["POST"]


def test_nothing_to_render_is_nothing_to_do(gh: FakeGitHub) -> None:
    only = [{"change": "skip_verified", "minutes": 6.0, "builds": 1}]
    assert proposer(gh).execute(action(changes=only)) is ActionResult.NOTHING_TO_DO
    assert writes(gh) == []


@pytest.mark.parametrize(
    "params",
    [
        {"repo": "example/other"},  # not in propose_repos
        {"workflow": "scripts/deploy.sh"},  # not a workflow file
        {"workflow": ".github/workflows/../../evil.yml"},
        {"workflow": ".github/workflows/a..b.yml"},  # not a safe branch name
        {"workflow": ".github/workflows/x.lock.yml"},
        {"workflow": "dynamic/github-code-scanning/codeql"},
        {"changes": "skip_drafts"},
    ],
)
def test_refused_before_anything_is_asked(gh: FakeGitHub, params: dict[str, Any]) -> None:
    assert proposer(gh).execute(action(**params)) is ActionResult.REFUSED
    assert gh.requests == []


@pytest.mark.parametrize(
    ("route", "status", "body"),
    [
        ("PUT", 403, {"message": "Resource not accessible: needs the workflow scope"}),
        ("POST", 422, {"message": "Draft pull requests are not supported in this repository."}),
    ],
)
def test_what_retrying_will_not_change_refuses_and_takes_the_branch_back(
    gh: FakeGitHub, route: str, status: int, body: dict[str, Any]
) -> None:
    where = f"PUT /repos/{REPO}/contents/{PATH}" if route == "PUT" else f"POST /repos/{REPO}/pulls"
    gh.reply(where, body, status)
    gh.reply(REF, {"object": {"sha": COMMIT if route == "POST" else "c" * 40}})
    assert proposer(gh).execute(action()) is ActionResult.REFUSED  # a human is told
    assert writes(gh)[-1][:2] == ("DELETE", f"/repos/{REPO}/git/refs/heads/{BRANCH}")


def test_a_branch_that_moved_on_is_not_deleted(gh: FakeGitHub) -> None:
    gh.reply(f"POST /repos/{REPO}/pulls", {"message": "Validation Failed"}, 422)
    gh.reply(REF, {"object": {"sha": "f" * 40}})  # someone pushed to it meanwhile
    assert proposer(gh).execute(action()) is ActionResult.REFUSED
    assert "DELETE" not in [w[0] for w in writes(gh)]


def test_nothing_from_the_workflow_file_reaches_the_description(gh: FakeGitHub) -> None:
    evil = "Click https://evil.example/now"
    text = TYPICAL.replace("  lint:\n", f"  lint:\n    name: {evil}\n").replace(
        "  test:\n", f'  "{evil}":\n    if: x\n    runs-on: x\n  test:\n', 1
    )
    gh.reply(
        f"/repos/{REPO}/contents/{PATH}?ref=main",
        {"encoding": "base64", "content": base64.b64encode(text.encode()).decode(), "sha": "b"},
    )
    proposer(gh).execute(action())
    (pr,) = [w for w in writes(gh) if w[1] == f"/repos/{REPO}/pulls"]
    assert "evil" not in pr[2]["body"] and "evil" not in pr[2]["title"]


def test_a_file_gone_since_the_runs_refuses(gh: FakeGitHub) -> None:
    gh.reply(f"/repos/{REPO}/contents/{PATH}?ref=main", {"message": "Not Found"}, 404)
    assert proposer(gh).execute(action()) is ActionResult.REFUSED
    assert writes(gh) == []


def test_rate_limits_and_server_errors_raise_for_a_retry(gh: FakeGitHub) -> None:
    gh.reply(
        f"POST /repos/{REPO}/pulls",
        {"message": "You have exceeded a secondary rate limit."},
        403,
    )
    with pytest.raises(GitHubError) as err:
        proposer(gh).execute(action())
    assert err.value.kind == "rate_limited"
    assert "DELETE" not in [w[0] for w in writes(gh)]  # backing off: the branch stays
    gh.requests.clear()
    gh.reply(f"/repos/{REPO}", {"message": "boom"}, 502)
    with pytest.raises(GitHubError):
        proposer(gh).execute(action())


def test_odd_numbers_in_the_params_cannot_leave_a_branch_behind(gh: FakeGitHub) -> None:
    odd = [{"change": "skip_drafts", "minutes": "lots", "builds": None}]
    assert proposer(gh).execute(action(changes=odd)) is ActionResult.DONE
    (_, _, pr) = writes(gh)
    assert "would have saved 0 min (0 builds)" in pr[2]["body"]


def test_a_file_that_is_no_workflow_is_refused(gh: FakeGitHub) -> None:
    bad = base64.b64encode(b"- not\n- a mapping\n").decode()
    gh.reply(
        f"/repos/{REPO}/contents/{PATH}?ref=main",
        {"encoding": "base64", "content": bad, "sha": "b"},
    )
    assert proposer(gh).execute(action()) is ActionResult.REFUSED
    assert writes(gh) == []


def test_the_preview_reads_and_writes_nothing(gh: FakeGitHub) -> None:
    text = preview(GitHubClient(gh.url, TOKEN), action())
    assert text.startswith(f"--- a/{PATH}\n+++ b/{PATH}\n")
    assert "left out: skip_verified:" in text and "note: GitHub reports a job skipped" in text
    assert writes(gh) == []
