"""Rule 9's CI configuration changes rendered into GitHub Actions workflow files: each edit
keeps the file's comments and layout, is checked by parsing the result, and is left out
(with why) when it does not fit."""

from __future__ import annotations

import pytest
import yaml

from kelpie.connectors.github.workflow_config import (
    MAX_WORKFLOW,
    NOT_DRAFT,
    WorkflowError,
    render,
)

TYPICAL = """\
name: CI
# Runs the tests on every change.
on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: make test  # the full matrix
  lint:
    if: github.actor != 'dependabot[bot]'
    runs-on: ubuntu-latest
    steps:
      - run: make lint
"""
DOCS = {"change": "ignore_docs_paths", "minutes": 9.0, "builds": 1, "paths": ["docs/*", "*.md"]}
DRAFTS = {"change": "skip_drafts", "minutes": 25.0, "builds": 2}
CANCEL = {"change": "cancel_in_progress", "minutes": 30.0, "builds": 3}


def on(text: str) -> dict:  # type: ignore[type-arg]
    data = yaml.safe_load(text)
    return data.get("on", data.get(True))


def test_all_three_changes_on_a_typical_workflow() -> None:
    out = render(TYPICAL, [CANCEL, DOCS, DRAFTS], "main")
    assert out.applied == ("cancel_in_progress", "ignore_docs_paths", "skip_drafts")
    assert out.left_out == ()
    data = yaml.safe_load(out.text)
    assert data["concurrency"]["cancel-in-progress"] is True
    group = data["concurrency"]["group"]
    assert "github.ref != 'refs/heads/main'" in group and group.endswith("|| github.run_id }}")
    assert on(out.text)["push"] == {"branches": ["main"], "paths-ignore": ["docs/**", "**.md"]}
    assert on(out.text)["pull_request"] == {
        "paths-ignore": ["docs/**", "**.md"],
        "types": ["opened", "synchronize", "reopened", "ready_for_review"],
    }
    assert data["jobs"]["test"]["if"] == NOT_DRAFT
    assert data["jobs"]["lint"]["if"] == "github.actor != 'dependabot[bot]'"  # its own
    # Comments and layout stay: every original line is still there, in order.
    kept = iter(out.text.splitlines())
    assert all(any(line == k for k in kept) for line in TYPICAL.splitlines())
    assert "# Runs the tests on every change." in out.text
    assert "make test  # the full matrix" in out.text


def test_the_diff_names_the_file() -> None:
    out = render(TYPICAL, [CANCEL], "main")
    diff = out.diff(TYPICAL, ".github/workflows/ci.yml")
    assert diff.startswith("--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n")
    assert "+concurrency:" in diff


@pytest.mark.parametrize(
    ("form", "events"),
    [("on: push\n", ["push"]), ("on: [push, pull_request]\n", ["push", "pull_request"])],
)
def test_short_on_forms_become_a_block(form: str, events: list[str]) -> None:
    text = f"name: CI\n{form}jobs:\n  test:\n    runs-on: ubuntu-latest\n    steps: []\n"
    out = render(text, [DOCS], "main")
    assert out.applied == ("ignore_docs_paths",)
    assert {e: on(out.text)[e]["paths-ignore"] for e in events} == {
        e: ["docs/**", "**.md"] for e in events
    }


def test_what_does_not_fit_is_left_out_with_why() -> None:
    text = """\
on:
  push:
    paths: [src/**]
  pull_request:
    types: [opened, synchronize]
concurrency: ci
jobs:
  test:
    runs-on: ubuntu-latest
    steps: []
"""
    wip = {"change": "skip_wip", "minutes": 3.0, "builds": 1, "patterns": ["^wip"]}
    verified = {"change": "skip_verified", "minutes": 6.0, "builds": 1}
    out = render(text, [CANCEL, DOCS, DRAFTS, wip, verified], "main")
    assert out.applied == ()
    assert out.text == text
    assert dict(out.left_out) == {
        "cancel_in_progress": "the workflow sets concurrency already",
        "ignore_docs_paths": "its push trigger filters paths already",
        "skip_drafts": "its pull_request types leave out ready_for_review",
        "skip_wip": "GitHub expressions cannot match the WIP patterns (no regular expressions)",
        "skip_verified": "GitHub Actions has no setting that skips an already verified commit",
    }


def test_flow_style_triggers_are_left_alone() -> None:
    text = "on:\n  pull_request: {branches: [main]}\njobs:\n  t:\n    runs-on: x\n    steps: []\n"
    out = render(text, [DOCS, DRAFTS], "main")
    assert out.applied == ()
    assert dict(out.left_out)["ignore_docs_paths"] == (
        "its pull_request trigger is not a block Kelpie edits"
    )


def test_drafts_need_a_pull_request_trigger_and_a_free_job() -> None:
    push_only = "on:\n  push:\njobs:\n  t:\n    runs-on: x\n    steps: []\n"
    assert dict(render(push_only, [DRAFTS], "main").left_out) == {
        "skip_drafts": "the workflow does not run on pull_request"
    }
    guarded = "on:\n  pull_request:\njobs:\n  t:\n    if: always()\n    runs-on: x\n"
    assert dict(render(guarded, [DRAFTS], "main").left_out) == {
        "skip_drafts": "every job has a condition of its own"
    }


def test_a_key_named_like_a_job_inside_another_job_is_not_edited() -> None:
    text = """\
on:
  pull_request:
jobs:
  build:
    runs-on: x
    services:
      test:
        image: postgres
  test:
    runs-on: x
"""
    out = render(text, [DRAFTS], "main")
    data = yaml.safe_load(out.text)
    assert data["jobs"]["test"]["if"] == NOT_DRAFT
    assert "if" not in data["jobs"]["build"]["services"]["test"]


def test_files_kelpie_does_not_edit() -> None:
    for text, why in (
        ("x" * (MAX_WORKFLOW + 1), "too large"),
        ("on: [push\n", "not valid YAML"),
        ("- a list\n", "not a mapping"),
    ):
        with pytest.raises(WorkflowError, match=why):
            render(text, [CANCEL], "main")
    no_jobs = "on: push\n"
    assert dict(render(no_jobs, [CANCEL], "main").left_out) == {
        "cancel_in_progress": "no top-level jobs key to place it before"
    }


def test_crlf_files_keep_crlf() -> None:
    out = render(TYPICAL.replace("\n", "\r\n"), [CANCEL, DOCS, DRAFTS], "main")
    assert len(out.applied) == 3
    assert "\n" not in out.text.replace("\r\n", "")


def test_a_block_list_of_events_becomes_a_mapping() -> None:
    text = "on:\n  - push\n  - pull_request\njobs:\n  t:\n    runs-on: x\n    steps: []\n"
    out = render(text, [DOCS], "main")
    assert out.applied == ("ignore_docs_paths",)
    assert set(on(out.text)) == {"push", "pull_request"}


def test_quoted_and_dotted_job_names_and_a_four_space_file() -> None:
    text = (
        'on:\n    pull_request:\njobs:\n    "my job":\n        runs-on: x\n'
        "    build.linux:\n        runs-on: x\n"
    )
    out = render(text, [DRAFTS], "main")
    data = yaml.safe_load(out.text)
    assert (
        data["jobs"]["my job"]["if"] == NOT_DRAFT and data["jobs"]["build.linux"]["if"] == NOT_DRAFT
    )
    assert "        types: [opened" in out.text  # the file's own step of 4


def test_concurrency_goes_above_the_comment_that_belongs_to_jobs() -> None:
    text = "on: push\n# The jobs, in order.\njobs:\n  t:\n    runs-on: x\n"
    out = render(text, [CANCEL], "main")
    assert out.text.index("concurrency:") < out.text.index("# The jobs, in order.\njobs:")


def test_nothing_from_outside_is_written_unchecked() -> None:
    why = "the default branch's name is not one Kelpie writes into an expression"
    assert dict(render(TYPICAL, [CANCEL], "ma'in").left_out) == {"cancel_in_progress": why}
    bad = {**DOCS, "paths": ["docs/*", "x'\n  - ${{ github.head_ref }}"]}
    assert dict(render(TYPICAL, [bad], "main").left_out) == {
        "ignore_docs_paths": "a docs path has characters Kelpie does not write into a workflow"
    }
