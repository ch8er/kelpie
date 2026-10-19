"""The GitHub Issues adapter end to end: ``kelpie serve`` reads a simulated repository's
issues as tasks (a farm of issue agents only) and acts on them through the issues API.

Rule 1 (``exit_contract``) in ``act`` mode announces a person's acceptance criterion on the
issue an agent works on: one comment, the criterion quoted inert, signed by Kelpie, never
repeated. A stranger's issue states no criteria (only owners, members and collaborators do).
"""

from __future__ import annotations

import re
from typing import Any

from kelpie.sim.github import FakeGitHub
from tests.sim.farm import GITHUB_TOKEN_ENV, REPO, Farm
from tests.sim.kelpie import wait_for

CRITERION = r"\b(\d+)\s*(?:of|/)\s*\1\b"
ACT = {"mode": "act", "acceptance_patterns": [CRITERION]}


def issues_config(farm: Farm, **extra: Any) -> dict[str, Any]:
    section = {
        "repos": [REPO],
        "api_url": farm.github.url,
        "token_env": GITHUB_TOKEN_ENV,
        "cache_ttl": "30s",
        "agents": {"copilot": {"assignee": "Copilot", "author": "copilot-swe-agent[bot]"}},
        **extra,
    }
    config = farm.config({"exit_contract": ACT}, extra={"adapters": {"github_issues": section}})
    del config["adapters"]["hermes"]  # Hermes cannot carry rule 1's actions: issues only here
    return config


def held_issue(gh: FakeGitHub, farm: Farm, number: int, body: str, **fields: Any) -> None:
    gh.repo(REPO)
    gh.issue(
        REPO,
        number,
        "Retry the flaky upload",
        farm.ago(hours=2),
        body=body,
        assignees={"Copilot": farm.ago(minutes=40)},
        **fields,
    )


def test_a_persons_criterion_is_announced_once_and_inert(farm: Farm) -> None:
    gh = farm.github
    stated = "Done when the upload test passes 20 of 20 runs. cc @claude /lgtm"
    held_issue(gh, farm, 11, stated)
    held_issue(gh, farm, 12, "Also 20 of 20, please.", author="drive-by", association="NONE")
    kelpie = farm.serve(issues_config(farm))

    issue = gh.repo(REPO).issues[11]
    wait_for("the announcement", lambda: issue.comments or None, timeout=30)
    kelpie.wait_ticks(3)  # once: never announced again
    farm.stop()

    (comment,) = issue.comments
    text = comment["body"]
    assert text.startswith("A note from Kelpie:\n\n```")
    assert re.search(r"AC-[0-9a-f]{6}", text)
    assert "20 of 20 runs" in text
    assert text.endswith("— Kelpie")
    # The person's text is quoted inert: no bot reading raw comments sees a trigger in it.
    assert re.search(r"(^|\s)@claude([\s.,!?;:]|$)", text) is None
    assert not any(line.startswith("/") for line in text.splitlines())
    assert gh.repo(REPO).issues[12].comments == []  # a stranger states no criteria
    writes = [r for r in gh.requests if r.method != "GET"]
    assert [(r.method, r.path) for r in writes] == [("POST", f"/repos/{REPO}/issues/11/comments")]
