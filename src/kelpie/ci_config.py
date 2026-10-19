"""``kelpie ci-config``: rule 9's CI configuration proposals as diffs, without writing.

Read-only, like ``kelpie why``: one snapshot is built as a tick would (the GitHub CI
connector reads the recent builds), rule 9 (``ci_economy``, with its config) is evaluated
on it, and for each ``propose_ci_config`` it emits the workflow file is read from the
default branch with the read token (``Contents: read``) and the changes are rendered as
:mod:`kelpie.connectors.github.proposals` would commit them. The diff, the changes left
out and why are printed; nothing is written, whatever the rule's mode or the dry-run.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime

from kelpie.app import Settings, load_rules, utc_now
from kelpie.connectors.github.client import GitHubError
from kelpie.connectors.github.proposals import WORKFLOW_PATH, default_branch, read_workflow
from kelpie.connectors.github.workflow_config import DOCS_WARNING, WorkflowError, render
from kelpie.core.actions import ActionKind
from kelpie.why import read_snapshot


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
    read = read_snapshot(settings, clock, refresh=True)
    problems = [*read.adapter_failures, *read.problems]
    proposals = [a for a in rule.evaluate(read.snapshot) if a.kind is ActionKind.PROPOSE_CI_CONFIG]
    out: list[str] = []
    for action in proposals:
        repo, path = str(action.params.get("repo")), str(action.params.get("workflow"))
        out.append(f"# {path} in {repo}: {action.reason}")
        if WORKFLOW_PATH.fullmatch(path) is None:
            out += ["(not a workflow file Kelpie edits)", ""]
            continue
        changes = [c for c in action.params.get("changes", []) if isinstance(c, Mapping)]
        try:
            before, _ = read_workflow(client, repo, path, default_branch(client, repo))
            rendered = render(before, changes)
        except (GitHubError, WorkflowError) as exc:
            out += [f"(not rendered: {exc})", ""]
            continue
        diff = rendered.diff(before, path)
        out.append(diff.rstrip("\n") if diff else "(nothing to change in this file)")
        out += [f"left out: {name}: {why}" for name, why in rendered.left_out]
        if "ignore_docs_paths" in rendered.applied:
            out.append(f"note: {DOCS_WARNING}")
        out.append("")
    if not proposals:
        out.append(
            "No pipeline wasted enough minutes for a proposal "
            "(ci_economy propose_min_minutes, within its window)."
        )
    err = "".join(f"warning: {p}\n" for p in problems)
    return "\n".join(out).rstrip("\n") + "\n", err, 0
