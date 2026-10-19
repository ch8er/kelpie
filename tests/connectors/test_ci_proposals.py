"""``propose_ci_config`` on GitHub: a draft pull request with rule 9's changes written into the
workflow file, behind an allow-list and a token of its own; nothing twice."""

from __future__ import annotations

import base64
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlencode

import pytest
import yaml

from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.connectors.github.proposals import Proposer
from kelpie.core.actions import Action, ActionKind, ActionResult
from tests.connectors.fake_github import TOKEN, FakeGitHub, serve
from tests.connectors.test_workflow_config import TYPICAL

REPO = "example/app"
PATH = ".github/workflows/test.yml"
BRANCH = "kelpie/ci-config/test"
CHANGES = [
    {"change": "skip_drafts", "minutes": 25.0, "builds": 2},
    {"change": "ignore_docs_paths", "minutes": 9.0, "builds": 1, "paths": ["docs/*", "*.md"]},
    {"change": "skip_verified", "minutes": 6.0, "builds": 1},
]


@pytest.fixture
def gh() -> Iterator[FakeGitHub]:
    with serve() as fake:
        fake.reply(f"/repos/{REPO}", {"default_branch": "main"})
        fake.reply(
            f"/repos/{REPO}/pulls?" + urlencode({"head": f"example:{BRANCH}", "state": "open"}), []
        )
        content = base64.b64encode(TYPICAL.encode()).decode()
        fake.reply(
            f"/repos/{REPO}/contents/{PATH}?ref=main",
            {"encoding": "base64", "content": content, "sha": "blob-1"},
        )
        fake.reply(f"/repos/{REPO}/git/ref/heads/main", {"object": {"sha": "c" * 40}})
        fake.reply(f"POST /repos/{REPO}/git/refs", {"ref": f"refs/heads/{BRANCH}"}, 201)
        fake.reply(f"PUT /repos/{REPO}/contents/{PATH}", {"content": {}}, 200)
        fake.reply(f"POST /repos/{REPO}/pulls", {"number": 9}, 201)
        yield fake


def proposer(gh: FakeGitHub, repos: tuple[str, ...] = (REPO,)) -> Proposer:
    return Proposer(GitHubClient(gh.url, TOKEN), repos)


def action(**params: Any) -> Action:
    base: dict[str, Any] = {"repo": REPO, "workflow": PATH, "minutes": 40.0, "changes": CHANGES}
    base.update(params)
    return Action(
        ActionKind.PROPOSE_CI_CONFIG, "ci_economy", "wasted", executor="github_ci", params=base
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
    assert "- **Skip changes that touch only docs**: would have saved 9 min (1 builds)." in text
    assert "Skip builds of a commit that already passed: GitHub Actions has no setting" in text
    assert "required status check" in text  # the paths-ignore warning
    assert "Kelpie never merges it" in text


def test_an_open_proposal_or_a_branch_left_from_one_is_not_proposed_again(gh: FakeGitHub) -> None:
    gh.reply(
        f"/repos/{REPO}/pulls?" + urlencode({"head": f"example:{BRANCH}", "state": "open"}),
        [{"number": 9}],
    )
    assert proposer(gh).execute(action()) is ActionResult.NOTHING_TO_DO
    assert writes(gh) == []
    gh.reply(
        f"/repos/{REPO}/pulls?" + urlencode({"head": f"example:{BRANCH}", "state": "open"}), []
    )
    gh.reply(f"POST /repos/{REPO}/git/refs", {"message": "Reference already exists"}, 422)
    assert proposer(gh).execute(action()) is ActionResult.NOTHING_TO_DO  # a human closed it
    assert [w[0] for w in writes(gh)] == ["POST"]  # the branch, refused; nothing more


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
        {"changes": "skip_drafts"},
    ],
)
def test_refused_before_anything_is_asked(gh: FakeGitHub, params: dict[str, Any]) -> None:
    assert proposer(gh).execute(action(**params)) is ActionResult.REFUSED
    assert gh.requests == []


def test_a_token_without_the_permission_is_refused_and_transient_failures_raise(
    gh: FakeGitHub,
) -> None:
    gh.reply(f"PUT /repos/{REPO}/contents/{PATH}", {"message": "needs workflow scope"}, 403)
    gh.reply(f"DELETE /repos/{REPO}/git/refs/heads/{BRANCH}", None, 204)
    assert proposer(gh).execute(action()) is ActionResult.REFUSED  # a human is told
    assert writes(gh)[-1][:2] == ("DELETE", f"/repos/{REPO}/git/refs/heads/{BRANCH}")
    gh.reply(f"/repos/{REPO}", {"message": "boom"}, 502)
    with pytest.raises(GitHubError):
        proposer(gh).execute(action())


def test_a_file_that_is_no_workflow_is_refused(gh: FakeGitHub) -> None:
    bad = base64.b64encode(b"- not\n- a mapping\n").decode()
    gh.reply(
        f"/repos/{REPO}/contents/{PATH}?ref=main",
        {"encoding": "base64", "content": bad, "sha": "b"},
    )
    assert proposer(gh).execute(action()) is ActionResult.REFUSED
    assert writes(gh) == []
