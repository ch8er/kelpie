"""``kelpie doctor``: which source GitHub CI is read from, per repository, and the exact
permission a 403 means."""

from __future__ import annotations

from pathlib import Path

from kelpie.doctor import Check, run_checks
from tests.connectors.fake_github import TOKEN, FakeGitHub, serve

REPO = "example/repo"
SHA = "a" * 40
ENV = {"GH_DOCTOR_TOKEN": TOKEN}
RUN = {
    "name": "CI",
    "workflow_id": 1,
    "event": "push",
    "status": "completed",
    "conclusion": "success",
}


def github_checks(
    tmp_path: Path, gh: FakeGitHub, extra: str = "", repos: tuple[str, ...] = (REPO,)
) -> dict[str, Check]:
    path = tmp_path / "kelpie.toml"
    names = ", ".join(f'"{r}"' for r in repos)
    path.write_text(
        f'[connectors.github]\nrepos = [{names}]\napi_url = "{gh.url}"\n'
        f'token_env = "GH_DOCTOR_TOKEN"\n{extra}'
    )
    found = run_checks(path, environ=ENV)
    for check in found:
        assert TOKEN not in check.render()
    github = [c for c in found if c.name.startswith("github")]
    names = [c.name for c in github]
    assert len(names) == len(set(names)), f"two checks with one name: {names}"
    return {c.name: c for c in github}


GATE = {"name": "CI Gate", "path": ".github/workflows/gate.yml", "state": "active"}


def repo_with_commit(gh: FakeGitHub, repo: str = REPO) -> None:
    gh.reply(f"/repos/{repo}/pulls?per_page=1&state=all", [{"head": {"sha": SHA}}])


def test_check_runs_and_statuses_are_the_source_when_readable(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        checks = github_checks(tmp_path, gh)
    check = checks[f"github ci {REPO}"]
    assert check.status == "PASS"
    assert (
        check.detail
        == "statuses and check-runs; probe commit (newest pull request or run): success"
    )
    assert not any("actions/runs" in p for p in gh.paths())


def test_actions_runs_are_the_source_for_a_fine_grained_token(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, [RUN])
        checks = github_checks(tmp_path, gh)
    check = checks[f"github ci {REPO}"]
    assert check.status == "WARN"  # external CI statuses would be missed: say so
    assert "Commit statuses: read" in check.fix and "Jenkins" in check.fix
    assert check.render().splitlines()[1].startswith("  fix: ")
    assert "Actions runs (statuses refused (403), needs Commit statuses: read" in check.detail
    assert "check-runs refused (403), needs Checks: read" in check.detail
    assert "all workflows count" in check.detail and check.detail.endswith(": success")


def test_the_workflow_allowlist_is_shown_and_a_miss_is_explained(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, [RUN])
        gh.workflows(REPO, GATE)
        checks = github_checks(tmp_path, gh, 'ci_workflows = ["CI Gate"]\n')
    check = checks[f"github ci {REPO}"]
    assert "workflows required: CI Gate" in check.detail
    assert check.detail.endswith("pending (no result yet from 'CI Gate' in ci_workflows)")


def test_nothing_readable_names_actions_read(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.forbid_ci(REPO, SHA)
        gh.reply(f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {}, 403)
        check = github_checks(tmp_path, gh)[f"github ci {REPO}"]
    assert check.status == "FAIL"
    assert check.detail == "HTTP 403, the token lacks Actions: read"
    assert "give the token Actions: read on example/repo" in check.fix


def test_without_pull_request_access_it_fails_but_ci_is_still_reported(tmp_path: Path) -> None:
    with serve() as gh:
        gh.reply(f"/repos/{REPO}/pulls?per_page=1&state=all", {"message": "no"}, 403)
        gh.reply(f"/repos/{REPO}/actions/runs?per_page=1", {"workflow_runs": [{"head_sha": SHA}]})
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        checks = github_checks(tmp_path, gh)
    pulls = checks[f"github pull requests {REPO}"]
    assert (
        pulls.status == "FAIL" and pulls.detail == "HTTP 403, the token lacks Pull requests: read"
    )
    assert "give the token Pull requests: read" in pulls.fix
    assert checks[f"github ci {REPO}"].status == "PASS"  # the CI source is shown anyway


def test_no_access_to_pull_requests_and_runs_names_actions_read(tmp_path: Path) -> None:
    with serve() as gh:
        gh.reply(f"/repos/{REPO}/pulls?per_page=1&state=all", {"message": "no"}, 403)
        gh.reply(f"/repos/{REPO}/actions/runs?per_page=1", {"message": "no"}, 403)
        check = github_checks(tmp_path, gh)[f"github ci {REPO}"]
    assert check.status == "FAIL" and "Actions: read" in check.detail


def test_a_workflow_run_is_the_sample_when_there_is_no_pull_request(tmp_path: Path) -> None:
    with serve() as gh:
        gh.reply(f"/repos/{REPO}/pulls?per_page=1&state=all", [])
        gh.reply(f"/repos/{REPO}/actions/runs?per_page=1", {"workflow_runs": [{"head_sha": SHA}]})
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        check = github_checks(tmp_path, gh)[f"github ci {REPO}"]
    assert check.status == "PASS"


def test_nothing_to_probe_with_is_a_skip(tmp_path: Path) -> None:
    with serve() as gh:
        gh.reply(f"/repos/{REPO}/pulls?per_page=1&state=all", [])
        gh.reply(f"/repos/{REPO}/actions/runs?per_page=1", {"workflow_runs": []})
        check = github_checks(tmp_path, gh)[f"github ci {REPO}"]
    assert check.status == "SKIP" and "no pull request" in check.detail


def test_a_rejected_token_fails_once_and_skips_the_rest(tmp_path: Path) -> None:
    with serve() as gh:
        gh.reply("/repos/example/a/pulls?per_page=1&state=all", {"message": "Bad credentials"}, 401)
        checks = github_checks(tmp_path, gh, repos=("example/a", "example/b"))
    assert checks["github ci example/a"].status == "FAIL"
    assert "GH_DOCTOR_TOKEN" in checks["github ci example/a"].fix
    assert checks["github ci example/b"].status == "SKIP"
    assert [p for p in gh.paths() if "example/b" in p] == []


def test_an_unknown_repository_fails(tmp_path: Path) -> None:
    with serve() as gh:
        check = github_checks(tmp_path, gh)[f"github ci {REPO}"]  # the fake answers 404
    assert check.status == "FAIL" and "404" in check.detail


def test_a_rate_limit_is_a_skip_not_a_missing_permission(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.reply(
            f"/repos/{REPO}/commits/{SHA}/status?per_page=100",
            {"message": "API rate limit exceeded"},
            403,
            retry_after="60",
        )
        check = github_checks(tmp_path, gh)[f"github ci {REPO}"]
    assert check.status == "SKIP" and "rate_limited" in check.detail


def test_unreachable_github_fails(tmp_path: Path) -> None:
    with serve() as gh:
        pass
    check = github_checks(tmp_path, gh)[f"github ci {REPO}"]
    assert check.status == "FAIL" and "network" in check.detail


def test_without_a_github_section_there_is_no_github_check() -> None:
    assert [c for c in run_checks(None, environ={}) if c.name.startswith("github")] == []


def test_a_disabled_section_is_skipped(tmp_path: Path) -> None:
    with serve() as gh:
        checks = github_checks(tmp_path, gh, "enabled = false\n")
    assert checks["github ci"].status == "SKIP" and gh.requests == []


def test_a_named_token_variable_that_is_unset_fails(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text('[connectors.github]\nrepos = ["example/repo"]\ntoken_env = "GH_UNSET"\n')
    (check,) = [c for c in run_checks(path, environ={}) if c.name.startswith("github")]
    assert (check.status, check.name) == ("FAIL", "github token")
    assert "GH_UNSET" in check.fix


def test_only_a_bounded_number_of_repositories_is_asked(tmp_path: Path) -> None:
    repos = tuple(f"example/r{i}" for i in range(21))
    with serve() as gh:
        checks = github_checks(tmp_path, gh, repos=repos)
    assert checks["github ci"].detail == "1 more repos not asked"
    assert len([c for c in checks if c != "github ci"]) == 20


def test_only_check_runs_refused_is_a_pass(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        gh.forbid_ci(REPO, SHA, statuses=False)
        gh.actions(REPO, SHA, [RUN])
        check = github_checks(tmp_path, gh)[f"github ci {REPO}"]
    assert check.status == "PASS" and check.detail.startswith("statuses and Actions runs")
    assert "check-runs refused (403), needs Checks: read" in check.detail


def test_a_refused_actions_call_fails_even_when_a_red_status_decides(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "failure"}], status_state="failure")
        gh.forbid_ci(REPO, SHA, statuses=False)
        gh.reply(f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {}, 403)
        check = github_checks(tmp_path, gh)[f"github ci {REPO}"]
    assert check.status == "FAIL"
    assert "Actions runs are refused (403), needs Actions: read" in check.detail
    assert "never green" in check.fix


def test_an_organization_policy_is_not_reported_as_a_permission(tmp_path: Path) -> None:
    with serve() as gh:
        gh.reply(
            f"/repos/{REPO}/pulls?per_page=1&state=all",
            {"message": "Resource protected by organization SAML enforcement."},
            403,
            x_github_sso="required; url=https://example.invalid/sso?token=SECRET",
        )
        checks = github_checks(tmp_path, gh)
    check = checks[f"github ci {REPO}"]
    assert check.status == "FAIL" and "SAML SSO or an IP allow list" in check.detail
    assert "SECRET" not in check.render()


def test_doctor_judges_the_probe_commit_for_its_own_pull_request(tmp_path: Path) -> None:
    other = {**RUN, "event": "pull_request"}
    other["pull_requests"] = [{"number": 9}]
    with serve() as gh:
        gh.reply(f"/repos/{REPO}/pulls?per_page=1&state=all", [{"number": 7, "head": {"sha": SHA}}])
        gh.forbid_ci(REPO, SHA)
        gh.actions(REPO, SHA, [other])
        check = github_checks(tmp_path, gh)[f"github ci {REPO}"]
    assert check.detail.endswith(": none")  # PR 9's green run is not PR 7's


def test_check_runs_refused_says_what_is_not_seen(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        gh.forbid_ci(REPO, SHA, statuses=False)
        gh.actions(REPO, SHA, [RUN])
        check = github_checks(tmp_path, gh)[f"github ci {REPO}"]
    assert "check runs of GitHub Apps other than Actions are not seen" in check.detail


# --- ci_workflows entries ----------------------------------------------------------------

CI = {"name": "CI", "path": ".github/workflows/ci.yml", "state": "active"}
CI_COPY = {"name": "CI", "path": ".github/workflows/ci-old.yml", "state": "disabled_manually"}


def entries(tmp_path: Path, workflows: list[str], *listed: dict[str, str]) -> Check:
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        gh.actions(REPO, SHA, [RUN])
        gh.workflows(REPO, *listed)
        names = ", ".join(f'"{w}"' for w in workflows)
        checks = github_checks(tmp_path, gh, f"ci_workflows = [{names}]\n")
    return checks[f"github ci_workflows {REPO}"]


def test_entries_that_match_a_workflow_by_name_or_path_pass(tmp_path: Path) -> None:
    check = entries(tmp_path, ["CI", ".github/workflows/gate.yml"], CI, GATE)
    assert check.status == "PASS" and "2 entries" in check.detail


def test_a_typo_in_ci_workflows_fails(tmp_path: Path) -> None:
    check = entries(tmp_path, ["CI gate", "CI"], CI, GATE)
    assert check.status == "FAIL" and check.detail == "matches no workflow: 'CI gate'"
    assert "keeps every condition pending" in check.fix


def test_a_name_shared_by_two_workflow_files_warns_and_suggests_the_path(tmp_path: Path) -> None:
    check = entries(tmp_path, ["CI"], CI, CI_COPY)
    assert check.status == "WARN" and ".github/workflows/ci-old.yml" in check.detail
    assert check.fix == "list the path instead"
    assert entries(tmp_path, [".github/workflows/ci.yml"], CI, CI_COPY).status == "PASS"


def test_more_than_a_page_of_workflows_downgrades_an_unmatched_entry_to_a_warning(
    tmp_path: Path,
) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        gh.actions(REPO, SHA, [RUN])
        gh.reply(
            f"/repos/{REPO}/actions/workflows?per_page=100",
            {"total_count": 130, "workflows": [CI]},
        )
        check = github_checks(tmp_path, gh, 'ci_workflows = ["Elsewhere"]\n')[
            f"github ci_workflows {REPO}"
        ]
    assert check.status == "WARN" and "first 100" in check.detail


def test_no_entries_means_no_entries_check(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        checks = github_checks(tmp_path, gh)
    assert f"github ci_workflows {REPO}" not in checks


def test_required_workflows_with_unreadable_runs_fail_naming_the_permission(
    tmp_path: Path,
) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        gh.reply(f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {}, 403)
        check = github_checks(tmp_path, gh, 'ci_workflows = ["CI"]\n')[f"github ci {REPO}"]
    assert check.status == "FAIL"
    assert check.detail == "HTTP 403, the token lacks Actions: read (ci_workflows needs it)"


def test_an_entry_that_names_only_a_disabled_workflow_fails(tmp_path: Path) -> None:
    off = {**GATE, "state": "disabled_manually"}
    check = entries(tmp_path, ["CI Gate"], CI, off)
    assert check.status == "FAIL" and check.detail == "only disabled workflows match: 'CI Gate'"
    assert check.fix.startswith("enable the workflow")


def test_a_refused_runs_call_is_reported_once_with_ci_workflows(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        gh.reply(f"/repos/{REPO}/actions/runs?head_sha={SHA}&per_page=100", {}, 403)
        checks = github_checks(tmp_path, gh, 'ci_workflows = ["CI"]\n')
    assert [c for c in checks if c.startswith("github ci")] == [f"github ci {REPO}"]


def test_a_failing_workflow_list_is_reported_under_its_own_name(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        gh.actions(REPO, SHA, [RUN])
        gh.reply(f"/repos/{REPO}/actions/workflows?per_page=100", {}, 403)
        checks = github_checks(tmp_path, gh, 'ci_workflows = ["CI"]\n')
    assert checks[f"github ci {REPO}"].status != "FAIL"  # the CI probe itself worked
    listing = checks[f"github ci_workflows {REPO}"]
    assert listing.status == "FAIL" and "Actions: read" in listing.detail


def test_with_more_than_a_page_of_workflows_a_disabled_entry_only_warns(tmp_path: Path) -> None:
    off = {**GATE, "state": "disabled_manually"}
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        gh.actions(REPO, SHA, [RUN])
        gh.reply(
            f"/repos/{REPO}/actions/workflows?per_page=100",
            {"total_count": 130, "workflows": [CI, off]},
        )
        check = github_checks(tmp_path, gh, 'ci_workflows = ["CI Gate"]\n')[
            f"github ci_workflows {REPO}"
        ]
    assert check.status == "WARN" and "first 100" in check.detail


def test_with_more_than_a_page_of_workflows_a_shared_name_warning_says_so(tmp_path: Path) -> None:
    with serve() as gh:
        repo_with_commit(gh)
        gh.ci(REPO, SHA, statuses=[{"state": "success"}])
        gh.actions(REPO, SHA, [RUN])
        gh.reply(
            f"/repos/{REPO}/actions/workflows?per_page=100",
            {"total_count": 130, "workflows": [CI, CI_COPY]},
        )
        check = github_checks(tmp_path, gh, 'ci_workflows = ["CI"]\n')[
            f"github ci_workflows {REPO}"
        ]
    assert check.status == "WARN" and "there may be more" in check.detail
