"""ci-ok, the only required check: it never passes for a head that was not built (#344)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("ci_ok", ROOT / "scripts/ci_ok.py")
assert _spec is not None and _spec.loader is not None
ci_ok = importlib.util.module_from_spec(_spec)
sys.modules["ci_ok"] = ci_ok
_spec.loader.exec_module(ci_ok)

HEAD = "a" * 40
OTHER = "b" * 40


def needs(code: str | None = "true", **results: str) -> dict[str, Any]:
    """The `needs` context of ci-ok; every job passed unless `results` says otherwise."""
    out: dict[str, Any] = {
        "changes": {
            "result": results.pop("changes", "success"),
            "outputs": {} if code is None else {"code": code},
        }
    }
    default = "success" if code == "true" else "skipped"
    for job in ci_ok.BUILD_JOBS:
        out[job] = {"result": results.pop(job, default), "outputs": {}}
    assert not results
    return out


def pr(n: dict[str, Any], *, draft: bool = False, head: str = HEAD, now: str = HEAD) -> str | None:
    return ci_ok.decide("pull_request", draft, n, head, now)


def test_a_built_code_change_passes() -> None:
    assert pr(needs()) is None
    assert ci_ok.decide("push", False, needs()) is None


def test_a_docs_only_change_passes_without_lint_and_test() -> None:
    assert pr(needs(code="false")) is None
    assert ci_ok.decide("push", False, needs(code="false")) is None


def test_a_draft_fails_even_when_nothing_failed() -> None:
    skipped = needs(code=None, changes="skipped", lint="skipped", test="skipped")
    reason = pr(skipped, draft=True)
    assert reason is not None and "draft" in reason
    assert pr(needs(code="false"), draft=True) is not None  # docs-only drafts too


def test_a_run_for_a_head_the_pr_no_longer_has_fails() -> None:
    """#344: the ready event built the previous head; the new head must not pass on it."""
    reason = pr(needs(), head=HEAD, now=OTHER)
    assert reason is not None and HEAD[:12] in reason and OTHER[:12] in reason


@pytest.mark.parametrize(("head", "now"), [("", HEAD), (HEAD, "")])
def test_an_unknown_head_fails(head: str, now: str) -> None:
    assert pr(needs(), head=head, now=now) is not None


@pytest.mark.parametrize("job", ci_ok.BUILD_JOBS)
@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled", "missing"])
def test_a_code_change_needs_every_build_job_to_pass(job: str, result: str) -> None:
    n = needs()
    if result == "missing":
        del n[job]
    else:
        n[job]["result"] = result
    reason = pr(n)
    assert reason is not None
    assert ci_ok.decide("push", False, n) is not None


@pytest.mark.parametrize("result", ["skipped", "failure", "cancelled"])
def test_the_changes_job_must_have_run(result: str) -> None:
    assert pr(needs(code=None, changes=result)) is not None


def test_a_failed_job_fails_a_docs_only_change_too() -> None:
    assert pr(needs(code="false", lint="failure")) is not None


def test_main_reads_the_environment(capsys: pytest.CaptureFixture[str]) -> None:
    env = {
        "CI_EVENT": "pull_request",
        "CI_DRAFT": "false",
        "CI_NEEDS": json.dumps(needs()),
        "CI_HEAD_SHA": HEAD,
        "CI_PR_HEAD_SHA": HEAD,
    }
    assert ci_ok.main(env) == 0
    assert capsys.readouterr().out.strip() == "CI ok"
    assert ci_ok.main({**env, "CI_DRAFT": "true"}) == 1
    assert ci_ok.main({**env, "CI_NEEDS": "not json"}) == 1
    assert ci_ok.main({**env, "CI_NEEDS": "[]"}) == 1
    assert ci_ok.main({**env, "CI_PR_HEAD_SHA": OTHER}) == 1


def test_the_workflow_runs_the_decision_for_every_event() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["ci-ok"]
    assert job["if"] == "always()"  # a skipped required check would count as passed
    assert set(ci_ok.BUILD_JOBS) | {"changes"} == set(job["needs"])
    assert set(ci_ok.BUILD_JOBS) <= set(workflow["jobs"])
    run = [step for step in job["steps"] if step.get("run") == "python3 scripts/ci_ok.py"]
    assert len(run) == 1 and "if" not in run[0]
    env = run[0]["env"]
    assert env["CI_NEEDS"] == "${{ toJSON(needs) }}"
    assert env["CI_HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert env["CI_PR_HEAD_SHA"] == "${{ steps.pr.outputs.sha }}"
    # ready_for_review must start a run, so a PR marked ready gets a ci-ok for its head
    assert "ready_for_review" in workflow[True]["pull_request"]["types"]
