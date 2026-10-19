"""Rule 9's CI configuration changes (``ci_economy``'s ``params.pipelines``), rendered into a
GitHub Actions workflow file.

The rule names the changes; this module writes them in GitHub Actions syntax. The file is
edited as text, line by line, so its comments and layout stay as they were; after each edit
the file is parsed again (``yaml.safe_load``) and the edit is kept only if the workflow now
says what the change means. A change that cannot be placed safely is left out and said
why, never forced:

- ``cancel_in_progress``: a top-level ``concurrency`` group per workflow and ref that
  cancels a running build when a newer one starts, except on the default branch (as
  Kelpie's own cancels). Left out when the workflow sets ``concurrency`` already.
- ``ignore_docs_paths``: ``paths-ignore`` with the rule's docs globs under the ``push`` and
  ``pull_request`` triggers (``*`` becomes ``**``: in GitHub's filters ``*`` stops at a
  ``/``). Left out for a trigger with ``paths`` or ``paths-ignore`` of its own. A check
  skipped this way stays "expected" on the pull request: if it is required, docs-only pull
  requests cannot merge (said in :data:`DOCS_WARNING`).
- ``skip_drafts``: each job without an ``if`` skips draft pull requests, and the
  ``pull_request`` trigger lists ``ready_for_review`` so the build runs once the draft is
  ready. Left out when the trigger lists its ``types`` without ``ready_for_review``.
- ``skip_wip`` and ``skip_verified``: not rendered (GitHub expressions have no regular
  expressions, and nothing skips a commit another run already verified).

Only block-style mappings are edited; flow style (``pull_request: {branches: [main]}``) is
left as it is. The forms ``on: push`` and ``on: [push, pull_request]`` become a block.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

MAX_WORKFLOW = 256 * 1024  # bytes: larger files are not edited
DOCS_WARNING = (
    "A workflow skipped by `paths-ignore` leaves its checks pending on the pull request: "
    "if one of them is a required status check, a docs-only pull request cannot merge "
    "until the rule is relaxed for it."
)
CONCURRENCY = (
    "concurrency:\n"
    "  group: ${{ github.workflow }}-${{ github.event.pull_request.number || github.ref }}\n"
    "  cancel-in-progress: ${{ github.ref != format('refs/heads/{0}', "
    "github.event.repository.default_branch) }}\n"
)
NOT_DRAFT = "github.event_name != 'pull_request' || github.event.pull_request.draft == false"
PR_TYPES = "[opened, synchronize, reopened, ready_for_review]"
_TOP = re.compile(r"^(?P<key>[\"']?[A-Za-z_][\w-]*[\"']?)\s*:(?P<rest>.*)$")
_CHILD = re.compile(r"^(?P<indent>[ ]+)(?P<key>[\"']?[A-Za-z_][\w-]*[\"']?)\s*:(?P<rest>.*)$")


@dataclass(frozen=True, slots=True)
class Rendered:
    """The workflow after the changes that could be placed; ``left_out`` says why for the
    others (``(change, why)``)."""

    text: str
    applied: tuple[str, ...]
    left_out: tuple[tuple[str, str], ...]

    def diff(self, before: str, path: str) -> str:
        return "".join(
            difflib.unified_diff(
                before.splitlines(keepends=True),
                self.text.splitlines(keepends=True),
                f"a/{path}",
                f"b/{path}",
            )
        )


class WorkflowError(ValueError):
    """The file is no workflow Kelpie edits (too large, not YAML, not a mapping)."""


def render(text: str, changes: Sequence[Mapping[str, Any]]) -> Rendered:
    """``text`` with ``changes`` (rule 9's ``changes`` of one pipeline) written in. Raises
    :class:`WorkflowError` when the file is not a workflow Kelpie edits."""
    if len(text.encode()) > MAX_WORKFLOW:
        raise WorkflowError("workflow file too large to edit")
    _parse(text)
    applied: list[str] = []
    left_out: list[tuple[str, str]] = []
    for change in changes:
        name = change.get("change")
        if not isinstance(name, str):
            continue
        edit = _EDITS.get(name)
        if edit is None:
            left_out.append((name, _NOT_RENDERED.get(name, "not a change Kelpie renders")))
            continue
        try:
            edited = edit(text, change)
        except _Skip as exc:
            left_out.append((name, str(exc)))
            continue
        try:
            ok = _CHECKS[name](_parse(edited), change)
        except WorkflowError:
            ok = False
        if not ok:
            left_out.append((name, "could not be placed safely in this file"))
            continue
        text = edited
        applied.append(name)
    return Rendered(text, tuple(applied), tuple(left_out))


class _Skip(Exception):
    """This change does not fit this workflow; the message says why."""


def _parse(text: str) -> dict[Any, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise WorkflowError("workflow file is not valid YAML") from exc
    if not isinstance(data, dict):
        raise WorkflowError("workflow file is not a mapping")
    return data


def _on(data: Mapping[Any, Any]) -> Any:
    # YAML 1.1 reads a bare ``on`` key as True.
    return data.get("on", data.get(True))


# --- cancel_in_progress -------------------------------------------------------------------


def _cancel_in_progress(text: str, change: Mapping[str, Any]) -> str:
    if "concurrency" in _parse(text):
        raise _Skip("the workflow sets concurrency already")
    lines = text.splitlines(keepends=True)
    at = _top_line(lines, "jobs")
    if at is None:
        raise _Skip("no top-level jobs key to place it before")
    return "".join([*lines[:at], CONCURRENCY, "\n", *lines[at:]])


def _check_cancel(data: Mapping[Any, Any], change: Mapping[str, Any]) -> bool:
    concurrency = data.get("concurrency")
    return isinstance(concurrency, dict) and "cancel-in-progress" in concurrency


# --- ignore_docs_paths --------------------------------------------------------------------


def _ignore_docs_paths(text: str, change: Mapping[str, Any]) -> str:
    globs = [_github_glob(g) for g in change.get("paths") or () if isinstance(g, str)]
    if not globs:
        raise _Skip("no docs paths given")
    text = _block_on(text)
    on = _on(_parse(text))
    triggers = [t for t in ("pull_request", "push") if isinstance(on, dict) and t in on]
    if not triggers:
        raise _Skip("the workflow runs on neither push nor pull_request")
    for trigger in triggers:
        value = on[trigger]
        if isinstance(value, dict) and ("paths" in value or "paths-ignore" in value):
            raise _Skip(f"its {trigger} trigger filters paths already")
    for trigger in triggers:
        listed = "".join(f"- '{g}'\n" for g in globs)
        text = _add_under_trigger(text, trigger, "paths-ignore", listed)
    return text


def _check_docs(data: Mapping[Any, Any], change: Mapping[str, Any]) -> bool:
    on = _on(data)
    globs = [_github_glob(g) for g in change.get("paths") or () if isinstance(g, str)]
    triggers = [t for t in ("pull_request", "push") if isinstance(on, dict) and t in on]
    return bool(triggers) and all(
        isinstance(on[t], dict) and on[t].get("paths-ignore") == globs for t in triggers
    )


def _github_glob(glob: str) -> str:
    """A path glob as Kelpie matches it (``*`` crosses ``/``) in GitHub's filter syntax."""
    return re.sub(r"\*+", "**", glob)


# --- skip_drafts --------------------------------------------------------------------------


def _skip_drafts(text: str, change: Mapping[str, Any]) -> str:
    text = _block_on(text)
    data = _parse(text)
    on = _on(data)
    if not isinstance(on, dict) or "pull_request" not in on:
        raise _Skip("the workflow does not run on pull_request")
    trigger = on["pull_request"]
    types = trigger.get("types") if isinstance(trigger, dict) else None
    if types is not None and (not isinstance(types, list) or "ready_for_review" not in types):
        raise _Skip("its pull_request types leave out ready_for_review")
    jobs = data.get("jobs")
    if not isinstance(jobs, dict) or not jobs:
        raise _Skip("no jobs")
    free = [j for j, body in jobs.items() if isinstance(body, dict) and "if" not in body]
    if not free:
        raise _Skip("every job has a condition of its own")
    if types is None:
        text = _add_under_trigger(text, "pull_request", "types", PR_TYPES, inline=True)
    for job in free:
        text = _add_to_job(text, str(job), "if", NOT_DRAFT)
    return text


def _check_drafts(data: Mapping[Any, Any], change: Mapping[str, Any]) -> bool:
    on = _on(data)
    trigger = on.get("pull_request") if isinstance(on, dict) else None
    jobs = data.get("jobs")
    return (
        isinstance(trigger, dict)
        and "ready_for_review" in (trigger.get("types") or ())
        and isinstance(jobs, dict)
        and any(isinstance(b, dict) and b.get("if") == NOT_DRAFT for b in jobs.values())
    )


_EDITS = {
    "cancel_in_progress": _cancel_in_progress,
    "ignore_docs_paths": _ignore_docs_paths,
    "skip_drafts": _skip_drafts,
}
_CHECKS = {
    "cancel_in_progress": _check_cancel,
    "ignore_docs_paths": _check_docs,
    "skip_drafts": _check_drafts,
}
_NOT_RENDERED = {
    "skip_wip": "GitHub expressions cannot match the WIP patterns (no regular expressions)",
    "skip_verified": "GitHub Actions has no setting that skips an already verified commit",
}


# --- text editing ---------------------------------------------------------------------------


def _top_line(lines: Sequence[str], key: str) -> int | None:
    for i, line in enumerate(lines):
        match = _TOP.match(line.rstrip("\n"))
        if match and match["key"].strip("\"'") == key:
            return i
    return None


def _on_line(lines: Sequence[str]) -> int | None:
    return _top_line(lines, "on")


def _block_end(lines: Sequence[str], start: int, indent: int) -> int:
    """The index after the block that starts at ``start`` (lines indented deeper than
    ``indent``, blank and comment lines included)."""
    end = start + 1
    while end < len(lines):
        stripped = lines[end].strip()
        if stripped and not stripped.startswith("#") and _indent(lines[end]) <= indent:
            break
        end += 1
    return end


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _child_indent(lines: Sequence[str], start: int, indent: int) -> int:
    """The indent of the block under ``start``: its first content line's, else two more."""
    for line in lines[start + 1 : _block_end(lines, start, indent)]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return _indent(line)
    return indent + 2


def _block_on(text: str) -> str:
    """``on: push`` or ``on: [push, pull_request]`` written as a block mapping, so triggers
    can take settings; other forms as they are."""
    lines = text.splitlines(keepends=True)
    at = _on_line(lines)
    if at is None:
        raise _Skip("no top-level on key")
    match = _TOP.match(lines[at].rstrip("\n"))
    assert match is not None
    rest = match["rest"].split("#", 1)[0].strip()
    if not rest:
        return text  # a block already
    names = re.fullmatch(r"\[\s*([\w-]+(?:\s*,\s*[\w-]+)*)\s*\]|([\w-]+)", rest)
    if names is None:
        raise _Skip("its on key is not in a form Kelpie edits")
    events = [e.strip() for e in (names[1] or names[2]).split(",")]
    key = match["key"]
    block = [f"{key}:\n", *(f"  {e}:\n" for e in events)]
    return "".join([*lines[:at], *block, *lines[at + 1 :]])


def _add_under_trigger(
    text: str, trigger: str, key: str, value: str, *, inline: bool = False
) -> str:
    """``key`` under ``on.<trigger>`` (a block mapping or empty): ``value`` on the same line
    (``inline``) or as the lines below it."""
    lines = text.splitlines(keepends=True)
    on_at = _on_line(lines)
    if on_at is None:
        raise _Skip("no top-level on key")
    end = _block_end(lines, on_at, 0)
    level = _child_indent(lines, on_at, 0)  # the triggers, not keys nested under them
    for i in range(on_at + 1, end):
        match = _CHILD.match(lines[i].rstrip("\n"))
        if match is None or len(match["indent"]) != level:
            continue
        if match["key"].strip("\"'") != trigger:
            continue
        rest = match["rest"].split("#", 1)[0].strip()
        if rest and rest not in ("null", "~"):
            raise _Skip(f"its {trigger} trigger is not a block Kelpie edits")
        indent = len(match["indent"])
        child = _child_indent(lines, i, indent)
        head = lines[i] if not rest else f"{match['indent']}{match['key']}:\n"
        if inline:
            added = [f"{' ' * child}{key}: {value}\n"]
        else:
            added = [f"{' ' * child}{key}:\n"] + [
                f"{' ' * (child + 2)}{line}\n" for line in value.splitlines()
            ]
        return "".join([*lines[:i], head, *added, *lines[i + 1 :]])
    raise _Skip(f"no {trigger} trigger found in the file")


def _add_to_job(text: str, job: str, key: str, value: str) -> str:
    lines = text.splitlines(keepends=True)
    jobs_at = _top_line(lines, "jobs")
    if jobs_at is None:
        raise _Skip("no top-level jobs key")
    end = _block_end(lines, jobs_at, 0)
    level = _child_indent(lines, jobs_at, 0)  # the jobs, not keys nested under them
    for i in range(jobs_at + 1, end):
        match = _CHILD.match(lines[i].rstrip("\n"))
        if match is None or len(match["indent"]) != level:
            continue
        if match["key"].strip("\"'") != job:
            continue
        if match["rest"].split("#", 1)[0].strip():
            raise _Skip(f"job {job} is not a block Kelpie edits")
        child = _child_indent(lines, i, len(match["indent"]))
        added = f"{' ' * child}{key}: {value}\n"
        return "".join([*lines[: i + 1], added, *lines[i + 1 :]])
    raise _Skip(f"job {job} not found in the file")
