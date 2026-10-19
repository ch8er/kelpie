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
   ``.github/workflows/`` whose name is also a safe branch name (``Build.workflow`` from the
   run list, never a path the rule made up);
2. an open pull request from Kelpie's branch for this file (``kelpie/ci-config/<file>``)
   means it was proposed already: nothing to do;
3. the file on the default branch is read and the changes are rendered for that branch
   (:mod:`kelpie.connectors.github.workflow_config`); nothing to render is nothing to do.
   The commit and the description are made before anything is written;
4. the branch is created from the default branch's head, the file is committed on it with
   the read file's blob sha (GitHub refuses the commit if the file changed meanwhile), and a
   draft pull request is opened. It lists each change with the minutes it would have saved,
   the changes left out and why, and what to check before merging. When anything fails
   after the branch was made, the branch is deleted again, but only while it still points
   at Kelpie's own work. A branch of that name that already exists: from a proposal a human
   closed, nothing to do (Kelpie does not push to it again); left half-made by an earlier
   attempt (at the default head, or holding exactly this change), Kelpie carries on from
   there; anything else is someone else's branch and refuses.

Answers that retrying will not change refuse (a human is told): no access (403, or an
organization policy), the file or repository gone (404), a request GitHub rejects (422,
e.g. drafts not available on the plan). Rate limits, back-off, 5xx and network trouble
raise, so the engine records a failure and retries later.
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
from kelpie.connectors.github.workflow_config import (
    DOCS_WARNING,
    DRAFTS_WARNING,
    WorkflowError,
    render,
)
from kelpie.core.actions import Action, ActionKind, ActionResult

log = logging.getLogger(__name__)

# A workflow file Kelpie edits: under .github/workflows/, a name that is a safe ref component
# too (ASCII, no "..", no ".lock"), as the branch is named after it.
WORKFLOW_PATH = re.compile(r"\.github/workflows/(?P<file>[A-Za-z0-9][A-Za-z0-9_-]{0,99}\.ya?ml)")
BRANCH_PREFIX = "kelpie/ci-config/"
_LABELS = {
    "cancel_in_progress": "Cancel a running build when a newer commit is pushed",
    "ignore_docs_paths": "Skip changes that touch only docs",
    "skip_drafts": "Skip draft pull requests",
    "skip_wip": "Skip WIP commits",
    "skip_verified": "Skip builds of a commit that already passed",
}
# GitHub's answers that will not change by retrying: no access (403 forbidden or an
# organization policy), the file or repository gone (404), a request it rejects (422).
_REFUSED_KINDS = frozenset({"forbidden", "policy", "not_found"})


@dataclass(slots=True)
class Proposer:
    """Opens the pull request for one ``propose_ci_config`` action."""

    client: GitHubClient  # with the propose token
    repos: tuple[str, ...]  # propose_repos

    def execute(self, action: Action) -> ActionResult:
        target = _target(action, self.repos)
        if target is None:
            return ActionResult.REFUSED
        repo, path, file, changes = target
        try:
            return self._propose(repo, path, file, changes)
        except GitHubError as exc:
            if exc.kind in _REFUSED_KINDS or exc.status == 422:
                log.warning("ci-config: GitHub refused %s %s: %s", repo, path, exc)
                return ActionResult.REFUSED
            raise  # rate limits, back-off, 5xx, network: the engine retries later

    def _propose(
        self, repo: str, path: str, file: str, changes: list[Mapping[str, Any]]
    ) -> ActionResult:
        default = default_branch(self.client, repo)
        branch = f"{BRANCH_PREFIX}{file}"
        if self._proposed(repo, branch):
            return ActionResult.NOTHING_TO_DO
        text, blob = read_workflow(self.client, repo, path, default)
        try:
            rendered = render(text, changes, default)
        except WorkflowError as exc:
            log.warning("ci-config: %s %s: %s", repo, path, exc)
            return ActionResult.REFUSED
        if not rendered.applied:
            return ActionResult.NOTHING_TO_DO
        # Everything the writes need, before the first write.
        content = base64.b64encode(rendered.text.encode()).decode()
        description = body(path, changes, rendered.applied, rendered.left_out)
        head = self.client.get(f"/repos/{repo}/git/ref/heads/{quote(default)}", fresh=True)
        sha = (head.body.get("object") or {}).get("sha") if isinstance(head.body, Mapping) else None
        if not isinstance(sha, str):
            raise GitHubError("bad_response", f"{repo}: no head of {default}")
        state: str | None = "empty"
        try:
            self.client.send(
                "POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}
            )
            left = None
        except GitHubError as exc:
            left = self._branch_sha(repo, branch) if exc.status == 422 else None
            if left is None:
                raise  # a 422 without such a branch: refused (an invalid name, a rule)
            if self._closed_before(repo, branch):
                return ActionResult.NOTHING_TO_DO  # a human closed this proposal: no again
            state = self._left_by_kelpie(repo, path, branch, default, rendered.text)
            if state is None:
                log.warning(
                    "ci-config: %s: branch %s exists without a proposal; Kelpie leaves it alone",
                    repo,
                    branch,
                )
                return ActionResult.REFUSED  # someone else's branch
        # The branch may be deleted only while it points at Kelpie's own work.
        ours = {sha} if left is None else {left}
        try:
            if state == "empty":
                commit = self.client.send(
                    "PUT",
                    f"/repos/{repo}/contents/{quote(path)}",
                    {
                        "message": f"ci: cut wasted builds in {path} (proposed by Kelpie)",
                        "content": content,
                        "sha": blob,
                        "branch": branch,
                    },
                )
                made = commit.get("commit") if isinstance(commit, Mapping) else None
                made = made.get("sha") if isinstance(made, Mapping) else None
                if isinstance(made, str):
                    ours.add(made)
            self.client.send(
                "POST",
                f"/repos/{repo}/pulls",
                {
                    "title": f"ci: cut wasted builds in {path}",
                    "head": branch,
                    "base": default,
                    "draft": True,
                    "body": description,
                },
            )
        except Exception:
            # No half-made proposal: the branch Kelpie just made goes, so a later attempt
            # starts over; only while it still points at Kelpie's own work.
            self._take_back(repo, branch, ours)
            raise
        return ActionResult.DONE

    def _proposed(self, repo: str, branch: str) -> bool:
        owner = repo.split("/", 1)[0]
        open_ = self.client.get(
            f"/repos/{repo}/pulls", {"head": f"{owner}:{branch}", "state": "open"}, fresh=True
        ).body
        return isinstance(open_, list) and bool(open_)

    def _branch_sha(self, repo: str, branch: str) -> str | None:
        try:
            ref = self.client.get(f"/repos/{repo}/git/ref/heads/{quote(branch)}", fresh=True)
        except GitHubError as exc:
            if exc.kind == "not_found":
                return None
            raise
        sha = (ref.body.get("object") or {}).get("sha") if isinstance(ref.body, Mapping) else None
        return sha if isinstance(sha, str) else ""

    def _closed_before(self, repo: str, branch: str) -> bool:
        owner = repo.split("/", 1)[0]
        closed = self.client.get(
            f"/repos/{repo}/pulls", {"head": f"{owner}:{branch}", "state": "closed"}, fresh=True
        ).body
        return isinstance(closed, list) and bool(closed)

    def _left_by_kelpie(
        self, repo: str, path: str, branch: str, default: str, text: str
    ) -> str | None:
        """What an earlier attempt left half-made on the branch (it could not be deleted,
        e.g. while rate limited): ``"empty"`` (no commit of its own yet), ``"made"`` (one
        commit, to this file only, holding exactly this change), or None (someone else's)."""
        diff = self.client.get(
            f"/repos/{repo}/compare/{quote(default)}...{quote(branch)}", fresh=True
        ).body
        if not isinstance(diff, Mapping):
            return None
        ahead, files = diff.get("ahead_by"), diff.get("files")
        if ahead == 0:
            return "empty"
        if ahead != 1 or not isinstance(files, list) or len(files) != 1:
            return None
        if not isinstance(files[0], Mapping) or files[0].get("filename") != path:
            return None
        there, _ = read_workflow(self.client, repo, path, branch)
        return "made" if there == text else None

    def _take_back(self, repo: str, branch: str, ours: set[str]) -> None:
        try:
            if self._branch_sha(repo, branch) not in ours:
                log.warning("ci-config: %s: branch %s moved on; left as it is", repo, branch)
                return
            self.client.send("DELETE", f"/repos/{repo}/git/refs/heads/{quote(branch)}")
        except GitHubError as exc:
            log.warning("ci-config: %s: branch %s left behind: %s", repo, branch, exc)


def preview(client: GitHubClient, action: Action) -> str:
    """What :class:`Proposer` would commit for ``action``, as a diff of the workflow file on
    the default branch, read with ``client`` (read access suffices); writes nothing."""
    target = _target(action, None)
    if target is None:
        return "(not a workflow file Kelpie edits)"
    repo, path, _, changes = target
    try:
        default = default_branch(client, repo)
        before, _ = read_workflow(client, repo, path, default)
        rendered = render(before, changes, default)
    except (GitHubError, WorkflowError) as exc:
        return f"(not rendered: {exc})"
    lines = [rendered.diff(before, path).rstrip("\n") or "(nothing to change in this file)"]
    lines += [f"left out: {name}: {why}" for name, why in rendered.left_out]
    if "ignore_docs_paths" in rendered.applied:
        lines.append(f"note: {DOCS_WARNING}")
    if "skip_drafts" in rendered.applied:
        lines.append(f"note: {DRAFTS_WARNING}")
    return "\n".join(lines)


def _target(
    action: Action, repos: tuple[str, ...] | None
) -> tuple[str, str, str, list[Mapping[str, Any]]] | None:
    """The action's repository, workflow path, file name and changes, when it is one Kelpie
    may propose for (in ``repos``, unless None)."""
    if action.kind is not ActionKind.PROPOSE_CI_CONFIG:
        return None
    params = action.params
    repo, workflow, changes = params.get("repo"), params.get("workflow"), params.get("changes")
    if not isinstance(repo, str):
        return None
    if repos is not None:
        # The repository as configured: never a spelling the action brought along.
        configured = {r.lower(): r for r in repos}
        if not repo.isascii() or repo.lower() not in configured:
            log.warning("ci-config: %r is not in propose_repos", repo)
            return None
        repo = configured[repo.lower()]
    match = WORKFLOW_PATH.fullmatch(workflow) if isinstance(workflow, str) else None
    if match is None or not isinstance(changes, list):
        log.warning("ci-config: %r is not a workflow file Kelpie edits", workflow)
        return None
    return repo, match.group(0), match["file"], [c for c in changes if isinstance(c, Mapping)]


def body(
    path: str,
    changes: Sequence[Any],
    applied: Sequence[str],
    left_out: Sequence[tuple[str, str]],
) -> str:
    """The pull request's description: what changes, what each would have saved, what was
    left out and why. Kelpie's own words and the rule's numbers only."""
    by_name = {c.get("change"): c for c in changes if isinstance(c, Mapping) and "change" in c}
    lines = [f"Kelpie (rule 9, CI economy) proposes these changes to `{path}`:", ""]
    for name in applied:
        c = by_name.get(name, {})
        minutes, builds = _number(c.get("minutes")), int(_number(c.get("builds")))
        lines.append(
            f"- **{_LABELS.get(name, name)}**: would have saved {minutes:.0f} min "
            f"({builds} build{'' if builds == 1 else 's'})."
        )
    if left_out:
        lines += ["", "Not changed here:", ""]
        lines += [f"- {_LABELS.get(name, name)}: {why}." for name, why in left_out]
    if "ignore_docs_paths" in applied:
        lines += ["", f"**Check before merging:** {DOCS_WARNING}"]
    if "skip_drafts" in applied:
        lines += ["", f"**Check before merging:** {DRAFTS_WARNING}"]
    lines += ["", "Review the workflow before marking this ready; Kelpie never merges it."]
    return "\n".join(lines)


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
        return 0.0
    return max(0.0, min(float(value), 1e9))


def default_branch(client: GitHubClient, repo: str) -> str:
    info = client.get(f"/repos/{repo}", fresh=True).body
    default = info.get("default_branch") if isinstance(info, Mapping) else None
    if not isinstance(default, str) or not default:
        raise GitHubError("bad_response", f"{repo}: no default branch")
    return default


def read_workflow(client: GitHubClient, repo: str, path: str, ref: str) -> tuple[str, str]:
    """The workflow file at ``ref``, and its blob sha."""
    file = client.get(f"/repos/{repo}/contents/{quote(path)}", {"ref": ref}, fresh=True).body
    return _decode(file)


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
