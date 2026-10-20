"""``kelpie ci-config``: rule 9's CI configuration proposals as diffs, without writing.

Read-only, like ``kelpie why``: one snapshot is built as a tick would (the GitHub CI
connector reads the recent builds), rule 9 (``ci_economy``, with its config) is evaluated
on it, and for each ``propose_ci_config`` it emits the workflow file is read from the
default branch with the read token (``Contents: read``) and the changes are rendered as
:mod:`kelpie.connectors.github.proposals` would commit them. The diff, the changes left
out and why are printed; nothing is written, whatever the rule's mode or the dry-run.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime

from kelpie.app import Settings, load_rules, utc_now
from kelpie.connectors.github.proposals import preview
from kelpie.core.actions import ActionKind
from kelpie.why import read_snapshot

# Terminal control characters: the workflow files are the repository's, not Kelpie's text.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029\u202a-\u202e\u2066-\u2069]")


def run(settings: Settings, clock: Callable[[], datetime] | None = None) -> tuple[str, str, int]:
    """(stdout, stderr, exit code): 0 with the proposals (or none), 2 when it cannot run."""
    clock = clock or utc_now
    github = settings.github
    if github is None or not github.enabled or github.ci is None or not github.ci.enabled:
        return "", "kelpie ci-config needs [connectors.github.ci]\n", 2
    rule = next((r for r in load_rules(settings.engine).rules if r.name == "ci_economy"), None)
    if rule is None:
        return "", "kelpie ci-config needs rule ci_economy\n", 2
    try:
        client = github.build(github.token(), clock).client
    except ValueError as exc:
        return "", f"{exc}\n", 2
    # Reading needs no write token: the snapshot is built with proposing and cancelling off.
    reading = replace(
        settings,
        github=replace(github, ci=replace(github.ci, allow_propose=False, allow_cancel=False)),
    )
    read = read_snapshot(reading, clock, refresh=True)
    problems = [*read.adapter_failures, *read.problems]
    if not getattr(rule, "propose_config", False):
        problems.append(
            "[engine.rules.ci_economy] propose_config is off: the proposals below are what it "
            "would make"
        )
        rule = replace(rule, propose_config=True)  # type: ignore[type-var]
    proposals = [a for a in rule.evaluate(read.snapshot) if a.kind is ActionKind.PROPOSE_CI_CONFIG]
    out: list[str] = []
    for action in proposals:
        repo, path = action.params.get("repo"), action.params.get("workflow")
        out += [f"# {path} in {repo}: {action.reason}", preview(client, action), ""]
    if not proposals:
        out.append(
            "No pipeline wasted enough minutes for a proposal "
            "(ci_economy propose_min_minutes and waste_min_minutes, within its window)."
        )
    err = "".join(f"warning: {p}\n" for p in problems)
    return _printable("\n".join(out).rstrip("\n") + "\n"), _printable(err), 0


def _printable(text: str) -> str:
    return _CONTROL.sub("\N{REPLACEMENT CHARACTER}", text)
