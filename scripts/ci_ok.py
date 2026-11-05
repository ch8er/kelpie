"""The `ci-ok` decision for .github/workflows/ci.yml (stdlib only).

``ci-ok`` is the only required check, and GitHub counts a *skipped* required check as
passed. So it must never pass, or be skipped, for a head whose lint, test, e2e and macOS
jobs did not run on that exact commit (issue 344): a draft run, or a run for a head the
PR no longer has, fails instead, and a later run for the same commit replaces it. The
PR's current head comes from the REST API: if GitHub cannot answer, ci-ok fails closed.
Docs-only changes still pass without those jobs.

    CI_EVENT=pull_request CI_DRAFT=false CI_NEEDS='<toJSON(needs)>' \\
    CI_HEAD_SHA=<the head this run built> CI_PR_HEAD_SHA=<the PR's head now> \\
        python3 scripts/ci_ok.py

Exit 0 prints "CI ok"; exit 1 prints why not.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from typing import Any

# The jobs a non-docs change must have run, and passed, on the commit ci-ok reports for.
BUILD_JOBS = ("lint", "test", "e2e", "macos")


def decide(
    event: str,
    draft: bool,
    needs: Mapping[str, Any],
    head_sha: str = "",
    pr_head_sha: str = "",
) -> str | None:
    """None when ci-ok may pass, else the reason it fails."""
    if event == "pull_request":
        if draft:
            return "draft PRs are not built: mark the PR ready for review to run CI on its head"
        if not head_sha or not pr_head_sha:
            return "could not tell which head this run built (no head SHA)"
        if head_sha != pr_head_sha:
            return (
                f"this run built {head_sha[:12]}, but the PR's head is now "
                f"{pr_head_sha[:12]}: this run is for an old head; the current head's own run "
                "decides (mark the PR ready if it is a draft)"
            )
    results = {name: str((needs.get(name) or {}).get("result", "missing")) for name in needs}
    bad = {n: r for n, r in results.items() if r in ("failure", "cancelled")}
    if bad:
        return "a needed job failed or was cancelled: " + _listing(results)
    changes = needs.get("changes") or {}
    if changes.get("result") != "success":
        return "the changes job did not run: " + _listing(results)
    if (changes.get("outputs") or {}).get("code") != "true":
        return None  # docs-only: lint and test are skipped on purpose
    not_run = [job for job in BUILD_JOBS if results.get(job, "missing") != "success"]
    if not_run:
        return "a code change needs " + ", ".join(not_run) + " to pass: " + _listing(results)
    return None


def _listing(results: Mapping[str, str]) -> str:
    return " ".join(f"{name}={result}" for name, result in sorted(results.items()))


def main(env: Mapping[str, str] = os.environ) -> int:
    try:
        needs = json.loads(env.get("CI_NEEDS") or "{}")
    except json.JSONDecodeError as exc:
        print(f"ci-ok: CI_NEEDS is not JSON: {exc}")
        return 1
    if not isinstance(needs, dict):
        print("ci-ok: CI_NEEDS is not a JSON object")
        return 1
    reason = decide(
        env.get("CI_EVENT", ""),
        env.get("CI_DRAFT", "false") == "true",
        needs,
        env.get("CI_HEAD_SHA", ""),
        env.get("CI_PR_HEAD_SHA", ""),
    )
    if reason is None:
        print("CI ok")
        return 0
    print(f"ci-ok: {reason}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
