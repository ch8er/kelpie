"""``propose_ci_config``: rule 9's CI configuration changes for one pipeline, opened as a
draft pull request that edits its GitHub Actions workflow file.

Off unless ``[connectors.github.ci] allow_propose`` is set, and then only for repositories in
``propose_repos``, with a token of its own (``propose_token_env``: Contents, Pull requests
and Workflows write on those repositories; GitHub refuses a token without the workflow
scope to change ``.github/workflows``). It runs only through the engine, so the rule's mode
and the global dry-run apply; ``kelpie ci-config`` prints the same change as a diff without
writing anything.

Steps, each re-checked against GitHub (no cache):

1. a pipeline with no file under ``.github/workflows/`` (CodeQL default setup, a dynamic
   workflow) has nothing to propose; otherwise the repository is in ``propose_repos`` and
   the workflow is a file there with an ASCII name without ``..`` (``Build.workflow`` from
   the run list, never a path the rule made up);
2. an open pull request from Kelpie's branch for this file (``kelpie/ci-config/<file>``)
   means it was proposed already: nothing to do;
3. the file on the default branch is read and the changes are rendered for that branch
   (:mod:`kelpie.connectors.github.workflow_config`); nothing to render is nothing to do.
   A change a closed pull request from that branch named (the description's hidden mark)
   is left out: unmerged, a human turned it down; merged, it is in the file, or a human
   undid it. Nothing left is nothing to do. The commit and the description are made
   before anything is written;
4. the branch is created from the default branch's head, the file is committed on it with
   the read file's blob sha (GitHub refuses the commit if the file changed meanwhile), and a
   draft pull request is opened. It lists each change with the minutes it would have saved,
   the changes left out and why, and what to check before merging. When anything fails
   after the branch was made, the branch is deleted again, but only while it still points
   at Kelpie's own work. A branch of that name that already exists: the head of an earlier
   proposal of Kelpie's, merged or closed, is deleted and made afresh; left half-made by an
   earlier attempt (no commit of its own, fast-forwarded when behind; or one commit to this
   file holding exactly this change), Kelpie carries on from there; anything else is
   someone else's branch and refuses.

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

# A workflow file Kelpie edits: under .github/workflows/, an ASCII name without ".." (it
# names the branch too, and a ref allows no ".."; ending in .yml, it never ends in ".lock").
WORKFLOW_DIR = ".github/workflows/"
WORKFLOW_PATH = re.compile(
    r"\.github/workflows/(?P<file>(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml)"
)
BRANCH_PREFIX = "kelpie/ci-config/"
# In every description Kelpie writes, naming the changes it proposes: a closed pull request
# with the same mark was turned down, and one with any mark was Kelpie's.
MARKER = "<!-- kelpie-ci-config:"
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
        workflow = action.params.get("workflow")
        if isinstance(workflow, str) and not workflow.startswith(WORKFLOW_DIR):
            # A pipeline with no file to edit (CodeQL default setup, a dynamic workflow).
            log.info("ci-config: %r is no workflow file; nothing to propose", workflow)
            return ActionResult.NOTHING_TO_DO
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
        default, owner = repository(self.client, repo)
        branch = branch_for(file)
        head_filter = f"{owner}:{branch}"  # the owner as GitHub spells it
        if self._open(repo, head_filter):
            return ActionResult.NOTHING_TO_DO
        closed = self._closed(repo, head_filter)
        # A change a closed proposal of Kelpie's named is never proposed again for this
        # file, alone or with others: closed unmerged, a human turned it down; merged, it
        # is in the file (rendering leaves it out), unless a human undid it since.
        turned = {
            name: (pr.get("number"), bool(pr.get("merged_at")))
            for pr in closed
            for name in proposed_in(str(pr.get("body") or ""))
        }
        text, blob = read_workflow(self.client, repo, path, default)
        try:
            rendered = render(text, [c for c in changes if c.get("change") not in turned], default)
        except WorkflowError as exc:
            log.warning("ci-config: %s %s: %s", repo, path, exc)
            return ActionResult.REFUSED
        if not rendered.applied:
            if turned:
                log.info("ci-config: %s %s: what is left was turned down before", repo, path)
            return ActionResult.NOTHING_TO_DO
        left_out = (
            *rendered.left_out,
            *(
                (name, _turned_down(*turned[name]))
                for name in (c.get("change") for c in changes)
                if isinstance(name, str) and name in turned
            ),
        )
        # Everything the writes need, before the first write.
        content = base64.b64encode(rendered.text.encode()).decode()
        description = body(path, changes, rendered.applied, left_out)
        head = self.client.get(f"/repos/{repo}/git/ref/heads/{quote(default)}", fresh=True)
        sha = (head.body.get("object") or {}).get("sha") if isinstance(head.body, Mapping) else None
        if not isinstance(sha, str):
            raise GitHubError("bad_response", f"{repo}: no head of {default}")
        state: str | None = "empty"
        made_here = True  # only a branch this attempt made is ever taken back
        try:
            self._branch(repo, branch, sha)
        except GitHubError as exc:
            left = self._branch_sha(repo, branch) if exc.status == 422 else None
            if left is None:
                raise  # a 422 without such a branch: refused (an invalid name, a rule)
            if any(_head(pr) == left and MARKER in str(pr.get("body") or "") for pr in closed):
                # The branch of an earlier proposal, merged or turned down for other
                # changes: it is done with (GitHub can restore it from that pull request).
                if not self._delete(repo, head_filter, branch, {left}):
                    return ActionResult.REFUSED
                self._branch(repo, branch, sha)
            else:
                state = self._left_by_kelpie(repo, path, branch, default, sha, rendered.text)
                if state is None:
                    log.warning(
                        "ci-config: %s: branch %s exists without a proposal; Kelpie leaves "
                        "it alone",
                        repo,
                        branch,
                    )
                    return ActionResult.REFUSED  # someone else's branch
                made_here = False  # carried on from: never deleted by this attempt
        ours = {sha}  # the branch may be deleted only while it points at this attempt's work
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
        except Exception as exc:
            if (
                isinstance(exc, GitHubError)
                and exc.status == 422
                and self._open_now(repo, head_filter)
            ):
                return ActionResult.NOTHING_TO_DO  # opened meanwhile (another Kelpie, a human)
            if made_here:
                # No half-made proposal: the branch this attempt made goes, so a later
                # attempt starts over.
                self._delete(repo, head_filter, branch, ours)
            raise
        return ActionResult.DONE

    def _branch(self, repo: str, branch: str, sha: str) -> None:
        self.client.send(
            "POST", f"/repos/{repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": sha}
        )

    def _open(self, repo: str, head_filter: str) -> bool:
        found = self.client.get(
            f"/repos/{repo}/pulls", {"head": head_filter, "state": "open"}, fresh=True
        ).body
        return isinstance(found, list) and bool(found)

    def _open_now(self, repo: str, head_filter: str) -> bool | None:
        """Whether a pull request from the branch is open; None when GitHub cannot say."""
        try:
            return self._open(repo, head_filter)
        except GitHubError as exc:
            log.warning("ci-config: %s: open pull requests unknown: %s", repo, exc)
            return None

    def _branch_sha(self, repo: str, branch: str) -> str | None:
        try:
            ref = self.client.get(f"/repos/{repo}/git/ref/heads/{quote(branch)}", fresh=True)
        except GitHubError as exc:
            if exc.kind == "not_found":
                return None
            raise
        sha = (ref.body.get("object") or {}).get("sha") if isinstance(ref.body, Mapping) else None
        return sha if isinstance(sha, str) else ""

    def _closed(self, repo: str, head_filter: str) -> list[Mapping[str, Any]]:
        """Closed pull requests from Kelpie's branch for this file, merged ones too."""
        closed = self.client.get(
            f"/repos/{repo}/pulls", {"head": head_filter, "state": "closed"}, fresh=True
        ).body
        return [pr for pr in closed if isinstance(pr, Mapping)] if isinstance(closed, list) else []

    def _left_by_kelpie(
        self, repo: str, path: str, branch: str, default: str, head: str, text: str
    ) -> str | None:
        """What an earlier attempt left half-made on the branch (it could not be deleted,
        e.g. while rate limited): ``"empty"`` (no commit of its own yet; moved up to the
        default branch's ``head`` when it fell behind), ``"made"`` (one commit, to this file
        only, holding exactly this change), or None (someone else's)."""
        diff = self.client.get(
            f"/repos/{repo}/compare/{quote(default)}...{quote(branch)}", fresh=True
        ).body
        if not isinstance(diff, Mapping):
            return None
        ahead, files = diff.get("ahead_by"), diff.get("files")
        if ahead == 0:
            behind = diff.get("behind_by")
            if isinstance(behind, int) and behind > 0:
                # No commit of its own: a fast-forward loses nothing (GitHub refuses
                # anything else without force).
                self.client.send(
                    "PATCH",
                    f"/repos/{repo}/git/refs/heads/{quote(branch)}",
                    {"sha": head, "force": False},
                )
            return "empty"
        if ahead != 1 or not isinstance(files, list) or len(files) != 1:
            return None
        if not isinstance(files[0], Mapping) or files[0].get("filename") != path:
            return None
        there, _ = read_workflow(self.client, repo, path, branch)
        return "made" if there == text else None

    def _delete(self, repo: str, head_filter: str, branch: str, ours: set[str]) -> bool:
        """Delete Kelpie's branch while it still points at ``ours`` and no pull request from
        it is open (deleting it would close that pull request). Whether it is gone."""
        try:
            if self._open_now(repo, head_filter) is not False:
                log.warning("ci-config: %s: branch %s may head a pull request; kept", repo, branch)
                return False
            if self._branch_sha(repo, branch) not in ours:
                log.warning("ci-config: %s: branch %s moved on; left as it is", repo, branch)
                return False
            self.client.send("DELETE", f"/repos/{repo}/git/refs/heads/{quote(branch)}")
            return True
        except GitHubError as exc:
            log.warning("ci-config: %s: branch %s left behind: %s", repo, branch, exc)
            return False


def preview(client: GitHubClient, action: Action) -> str:
    """What :class:`Proposer` would commit for ``action``, as a diff of the workflow file on
    the default branch, read with ``client`` (read access suffices); writes nothing."""
    target = _target(action, None)
    if target is None:
        return "(not a workflow file Kelpie edits)"
    repo, path, _, changes = target
    try:
        default, _ = repository(client, repo)
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
    lines += ["", marker(applied)]
    return "\n".join(lines)


def marker(applied: Sequence[str]) -> str:
    """The hidden line naming the changes a description proposes."""
    return f"{MARKER} {','.join(sorted(applied))} -->"


_MARKED = re.compile(re.escape(MARKER) + r" (?P<names>[a-z_,]{1,200}) -->")


def proposed_in(description: str) -> set[str]:
    """The changes a description of Kelpie's proposed (its :func:`marker`)."""
    return {
        name
        for found in _MARKED.finditer(description)
        for name in found["names"].split(",")
        if name in _LABELS
    }


def _turned_down(number: Any, merged: bool) -> str:
    n = number if isinstance(number, int) and not isinstance(number, bool) else 0
    return f"merged in #{n} and undone since" if merged else f"turned down in #{n}"


def branch_for(file: str) -> str:
    """Kelpie's branch for the workflow file ``file`` (a :data:`WORKFLOW_PATH` file name)."""
    if WORKFLOW_PATH.fullmatch(WORKFLOW_DIR + file) is None:
        raise ValueError(f"not a workflow file name: {file!r}")
    return f"{BRANCH_PREFIX}{file}"


def _head(pr: Mapping[str, Any]) -> str | None:
    head = pr.get("head")
    sha = head.get("sha") if isinstance(head, Mapping) else None
    return sha if isinstance(sha, str) else None


def _number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value != value:
        return 0.0
    return max(0.0, min(float(value), 1e9))


def repository(client: GitHubClient, repo: str) -> tuple[str, str]:
    """The repository's default branch, and its owner's login as GitHub spells it."""
    info = client.get(f"/repos/{repo}", fresh=True).body
    info = info if isinstance(info, Mapping) else {}
    default = info.get("default_branch")
    owner = info.get("owner")
    login = owner.get("login") if isinstance(owner, Mapping) else None
    if not isinstance(default, str) or not default:
        raise GitHubError("bad_response", f"{repo}: no default branch")
    return default, login if isinstance(login, str) and login else repo.split("/", 1)[0]


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
