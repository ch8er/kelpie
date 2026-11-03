"""``propose_ci_config``: rule 9's CI configuration changes for one pipeline, written into
its GitHub Actions workflow file.

The rule names the changes in no CI's syntax (``params.changes`` of a ``propose_ci_config``
action: ``cancel_in_progress``, ``ignore_docs_paths``, ``skip_drafts``, ``skip_wip``,
``skip_verified``); :mod:`kelpie.connectors.github.workflow_config` renders them for GitHub
Actions. :func:`preview` is the read-only half that ``kelpie ci-config`` uses: it reads the
workflow file on the default branch (``Contents: read`` suffices) and returns the diff, the
changes left out and why, and what to check before merging. It writes nothing.

Only a file under ``.github/workflows/`` with an ASCII name without ``..`` is rendered
(``Build.workflow`` from the run list, never a path the rule made up). Opening the change as
a pull request is not part of this module yet.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.connectors.github.workflow_config import (
    CANCEL_WARNING,
    DOCS_WARNING,
    DRAFTS_WARNING,
    WorkflowError,
    render,
)
from kelpie.core.actions import Action, ActionKind

log = logging.getLogger(__name__)

# A workflow file Kelpie edits: under .github/workflows/, an ASCII name without ".." (it
# will name a branch too, and a ref allows no ".."; ending in .yml, it never ends in ".lock").
WORKFLOW_DIR = ".github/workflows/"
WORKFLOW_PATH = re.compile(
    r"\.github/workflows/(?P<file>(?!.*\.\.)[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml)"
)


def preview(client: GitHubClient, action: Action, repos: tuple[str, ...]) -> str:
    """The changes of ``action`` as a diff of the workflow file on the default branch, read
    with ``client`` (read access suffices), for a repository in ``repos`` (the configured
    ones) only; writes nothing."""
    target = _target(action, repos)
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
    if "cancel_in_progress" in rendered.applied:
        lines.append(f"note: {CANCEL_WARNING}")
    if "ignore_docs_paths" in rendered.applied:
        lines.append(f"note: {DOCS_WARNING}")
    if "skip_drafts" in rendered.applied:
        lines.append(f"note: {DRAFTS_WARNING}")
    return "\n".join(lines)


def _target(
    action: Action, repos: tuple[str, ...]
) -> tuple[str, str, str, list[Mapping[str, Any]]] | None:
    """The action's repository (spelled as in ``repos``), workflow path, file name and
    changes, when the repository is in ``repos`` and the workflow a file Kelpie edits."""
    if action.kind is not ActionKind.PROPOSE_CI_CONFIG:
        return None
    params = action.params
    repo, workflow, changes = params.get("repo"), params.get("workflow"), params.get("changes")
    if not isinstance(repo, str):
        return None
    # The repository as configured: never a spelling the action brought along.
    configured = {r.lower(): r for r in repos}
    if not repo.isascii() or repo.lower() not in configured:
        log.warning("ci-config: %r is not a configured repository", repo)
        return None
    repo = configured[repo.lower()]
    match = WORKFLOW_PATH.fullmatch(workflow) if isinstance(workflow, str) else None
    if match is None or not isinstance(changes, list):
        log.warning("ci-config: %r is not a workflow file Kelpie edits", workflow)
        return None
    return repo, match.group(0), match["file"], [c for c in changes if isinstance(c, Mapping)]


def repository(client: GitHubClient, repo: str) -> tuple[str, str]:
    """The repository's default branch, and its owner's login as GitHub spells it (for the
    writes that open a proposal)."""
    info = client.get(f"/repos/{repo}", fresh=True).body
    info = info if isinstance(info, Mapping) else {}
    default = info.get("default_branch")
    owner = info.get("owner")
    login = owner.get("login") if isinstance(owner, Mapping) else None
    if not isinstance(default, str) or not default:
        raise GitHubError("bad_response", f"{repo}: no default branch")
    return default, login if isinstance(login, str) and login else repo.split("/", 1)[0]


def read_workflow(client: GitHubClient, repo: str, path: str, ref: str) -> tuple[str, str]:
    """The workflow file at ``ref``, and its blob sha (a commit on top of it names it)."""
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
