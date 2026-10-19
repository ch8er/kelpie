"""``propose_ci_config``: rule 9's CI configuration changes for one pipeline, opened as a
draft pull request that edits its GitHub Actions workflow file.

Off unless ``[connectors.github.ci] allow_propose`` is set, and then only for repositories in
``propose_repos``, with a token of its own (``propose_token_env``: Contents, Pull requests
and Workflows write on those repositories; GitHub refuses a token without the workflow
scope to change ``.github/workflows``). It runs only through the engine, so the rule's mode
and the global dry-run apply; ``kelpie ci-config`` prints the same change as a diff without
writing anything.

Steps, each re-checked against GitHub (no cache):

1. the repository is in ``propose_repos`` and the workflow is a file under
   ``.github/workflows/`` (``Build.workflow`` from the run list, never a path the rule made
   up);
2. the file on the default branch is read and the changes are rendered
   (:mod:`kelpie.connectors.github.workflow_config`); nothing to render is nothing to do;
3. an open pull request from Kelpie's branch for this workflow (``kelpie/ci-config/<file>``)
   means it was proposed already: nothing to do. So does a branch of that name left from an
   earlier proposal a human closed: Kelpie does not push to it again;
4. the branch is created from the default branch's head, the file is committed on it with
   the read file's blob sha (GitHub refuses the commit if the file changed meanwhile), and
   a draft pull request is opened: it lists each change with the minutes it would have
   saved, the changes left out and why, and the warning about required checks. When the
   commit or the pull request fails, the branch is deleted again.

A 403 (the token lacks a permission) refuses: a human is told. Network trouble, rate
limits and 5xx raise, so the engine records a failure and retries later.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.connectors.github.workflow_config import DOCS_WARNING, WorkflowError, render
from kelpie.core.actions import Action, ActionKind, ActionResult

log = logging.getLogger(__name__)

WORKFLOW_PATH = re.compile(r"\.github/workflows/(?P<file>[\w.-]{1,100})\.ya?ml")
BRANCH_PREFIX = "kelpie/ci-config/"
_LABELS = {
    "cancel_in_progress": "Cancel a running build when a newer commit is pushed",
    "ignore_docs_paths": "Skip changes that touch only docs",
    "skip_drafts": "Skip draft pull requests",
    "skip_wip": "Skip WIP commits",
    "skip_verified": "Skip builds of a commit that already passed",
}


@dataclass(slots=True)
class Proposer:
    """Opens the pull request for one ``propose_ci_config`` action."""

    client: GitHubClient  # with the propose token
    repos: tuple[str, ...]  # propose_repos

    def execute(self, action: Action) -> ActionResult:
        if action.kind is not ActionKind.PROPOSE_CI_CONFIG:
            return ActionResult.REFUSED
        params = action.params
        repo, workflow = params.get("repo"), params.get("workflow")
        changes = params.get("changes")
        if not isinstance(repo, str) or repo.lower() not in {r.lower() for r in self.repos}:
            log.warning("ci-config: %r is not in propose_repos", repo)
            return ActionResult.REFUSED
        path = workflow if isinstance(workflow, str) else ""
        match = WORKFLOW_PATH.fullmatch(path)
        if match is None or not isinstance(changes, list):
            log.warning("ci-config: %r is not a workflow file Kelpie edits", workflow)
            return ActionResult.REFUSED
        try:
            return self._propose(repo, path, match["file"], changes, action.reason)
        except GitHubError as exc:
            if exc.status == 403:
                log.warning("ci-config: GitHub refused %s (403): %s", repo, exc)
                return ActionResult.REFUSED
            raise

    def _propose(
        self, repo: str, path: str, stem: str, changes: Sequence[Any], reason: str
    ) -> ActionResult:
        info = self.client.get(f"/repos/{repo}", fresh=True).body
        default = info.get("default_branch") if isinstance(info, Mapping) else None
        if not isinstance(default, str) or not default:
            raise GitHubError("bad_response", f"{repo}: no default branch")
        branch = f"{BRANCH_PREFIX}{stem}"
        if self._proposed(repo, branch):
            return ActionResult.NOTHING_TO_DO
        file = self.client.get(
            f"/repos/{repo}/contents/{quote(path)}", {"ref": default}, fresh=True
        ).body
        text, blob = _decode(file)
        try:
            rendered = render(text, [c for c in changes if isinstance(c, Mapping)])
        except WorkflowError as exc:
            log.warning("ci-config: %s %s: %s", repo, path, exc)
            return ActionResult.REFUSED
        if not rendered.applied:
            return ActionResult.NOTHING_TO_DO
        head = self.client.get(f"/repos/{repo}/git/ref/heads/{quote(default)}", fresh=True)
        sha = (head.body.get("object") or {}).get("sha") if isinstance(head.body, Mapping) else None
        if not isinstance(sha, str):
            raise GitHubError("bad_response", f"{repo}: no head of {default}")
        try:
            self.client.send(
                "POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}
            )
        except GitHubError as exc:
            if exc.status == 422:  # the branch exists: an earlier proposal a human closed
                return ActionResult.NOTHING_TO_DO
            raise
        try:
            self.client.send(
                "PUT",
                f"/repos/{repo}/contents/{quote(path)}",
                {
                    "message": f"ci: cut wasted builds in {path} (proposed by Kelpie)",
                    "content": base64.b64encode(rendered.text.encode()).decode(),
                    "sha": blob,
                    "branch": branch,
                },
            )
            self.client.send(
                "POST",
                f"/repos/{repo}/pulls",
                {
                    "title": f"ci: cut wasted builds in {path}",
                    "head": branch,
                    "base": default,
                    "draft": True,
                    "body": body(path, changes, rendered.applied, rendered.left_out, reason),
                },
            )
        except GitHubError:
            # No half-made proposal: the branch goes, so a later attempt starts over.
            try:
                self.client.send("DELETE", f"/repos/{repo}/git/refs/heads/{quote(branch)}")
            except GitHubError as exc:
                log.warning("ci-config: %s: branch %s left behind: %s", repo, branch, exc)
            raise
        return ActionResult.DONE

    def _proposed(self, repo: str, branch: str) -> bool:
        owner = repo.split("/", 1)[0]
        open_ = self.client.get(
            f"/repos/{repo}/pulls", {"head": f"{owner}:{branch}", "state": "open"}, fresh=True
        ).body
        return isinstance(open_, list) and bool(open_)


def body(
    path: str,
    changes: Sequence[Any],
    applied: Sequence[str],
    left_out: Sequence[tuple[str, str]],
    reason: str,
) -> str:
    """The pull request's description: what changes, what each would have saved, what was
    left out and why. Kelpie's own words and the rule's numbers only."""
    minutes = {c.get("change"): c for c in changes if isinstance(c, Mapping) and "change" in c}
    lines = [f"Kelpie (rule 9, CI economy) proposes these changes to `{path}`:", ""]
    for name in applied:
        c = minutes.get(name, {})
        lines.append(
            f"- **{_LABELS.get(name, name)}**: would have saved {c.get('minutes', 0):g} min "
            f"({c.get('builds', 0)} builds)."
        )
    if left_out:
        lines += ["", "Not changed here:", ""]
        lines += [f"- {_LABELS.get(name, name)}: {why}." for name, why in left_out]
    if "ignore_docs_paths" in applied:
        lines += ["", f"**Check before merging:** {DOCS_WARNING}"]
    lines += ["", "Review the workflow before marking this ready; Kelpie never merges it."]
    return "\n".join(lines)


def _decode(file: Any) -> tuple[str, str]:
    if not isinstance(file, Mapping) or file.get("encoding") != "base64":
        raise GitHubError("bad_response", "workflow file: not a base64 file")
    blob, content = file.get("sha"), file.get("content")
    if not isinstance(blob, str) or not isinstance(content, str):
        raise GitHubError("bad_response", "workflow file: no content")
    try:
        return base64.b64decode(content).decode(), blob
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise GitHubError("bad_response", "workflow file: not text") from exc
