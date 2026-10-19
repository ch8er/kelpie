"""Rule 9's CI configuration changes (``ci_economy``'s ``params.pipelines``), rendered into a
GitHub Actions workflow file.

The rule names the changes; this module writes them in GitHub Actions syntax. The file is
edited as text, line by line, so its comments and layout stay as they were (CRLF files
keep CRLF); after each edit the file is parsed again (``yaml.safe_load``), and the edit is
kept only if the workflow now says what the change means and, with the change's own keys
taken out again, exactly what it said before. A change that cannot be placed safely is
left out and said why, never forced. Only fixed text and the repository's default branch
(a checked name) are written; nothing from build data or the rule's params reaches the
file except the docs globs, which are checked and quoted.

- ``cancel_in_progress``: a top-level ``concurrency`` group that cancels a running build
  when a newer one starts for the same pull request, or for a push to the same branch
  other than the default branch. Every other run (pushes to the default branch, manual,
  scheduled and other events) gets a group of its own, so nothing cancels it, as Kelpie's
  own cancels leave the default branch and manual runs alone. Left out when the workflow
  sets ``concurrency`` already.
- ``ignore_docs_paths``: ``paths-ignore`` with the rule's docs globs under the ``push`` and
  ``pull_request`` triggers (``*`` becomes ``**``: in GitHub's filters ``*`` stops at a
  ``/``). Left out for a trigger with ``paths`` or ``paths-ignore`` of its own.
  :data:`DOCS_WARNING` says what to check before merging.
- ``skip_drafts``: each job without an ``if`` skips draft pull requests, and the
  ``pull_request`` trigger lists ``ready_for_review`` so the build runs once the draft is
  ready. Left out when the trigger lists its ``types`` without ``ready_for_review``.
  :data:`DRAFTS_WARNING` says what to check before merging.
- ``skip_wip`` and ``skip_verified``: not rendered (GitHub expressions have no regular
  expressions, and nothing skips a commit another run already verified).

Only block-style mappings are edited; flow style (``pull_request: {branches: [main]}``) is
left as it is. The forms ``on: push``, ``on: [push, pull_request]`` and a block list of
events become a block mapping.
"""

from __future__ import annotations

import copy
import difflib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

MAX_WORKFLOW = 256 * 1024  # bytes: larger files are not edited
DOCS_WARNING = (
    "A workflow skipped by `paths-ignore` leaves its checks pending on the pull request: "
    "if one of them is a required status check, a docs-only pull request cannot merge "
    "until the rule is relaxed for it."
)
DRAFTS_WARNING = (
    "GitHub reports a job skipped by its `if` as successful, also for a required check: a "
    "draft's checks look green until the `ready_for_review` run replaces them. Before "
    "merging this, make sure nothing merges a pull request the moment it is marked ready "
    "(auto-merge, an agent), or keep this change out of workflows that hold required checks."
)
NOT_DRAFT = "github.event_name != 'pull_request' || github.event.pull_request.draft == false"
PR_EVENTS = ("opened", "synchronize", "reopened", "ready_for_review")
PR_TYPES = f"[{', '.join(PR_EVENTS)}]"
# The pull request actions that bring a new head commit: only their runs supersede.
_COMMIT_ACTIONS = '["opened","synchronize","reopened"]'
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
_GLOB = re.compile(r"[A-Za-z0-9._/*?\[\]{}!-]{1,200}")
_KEY = r"(?P<key>\"[^\"\n]{1,100}\"|'[^'\n]{1,100}'|[A-Za-z_][\w.-]*)"
_TOP = re.compile(rf"^{_KEY}\s*:(?P<rest>.*)$")
_CHILD = re.compile(rf"^(?P<indent>[ ]+){_KEY}\s*:(?P<rest>.*)$")
_ITEM = re.compile(r"^(?P<indent>[ ]+)-\s+(?P<name>[\w-]+)\s*(#.*)?$")


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


def concurrency(default: str) -> str:
    """The ``concurrency`` block for a repository whose default branch is ``default``.

    One group per workflow file (``workflow_ref``, not ``name:``, which two files may share)
    and per pull request or branch, so a newer commit cancels the running build. Only runs
    for a commit share it: a pull request's ``opened``, ``synchronize`` and ``reopened``
    runs, and pushes to branches other than the default. Every other run (a label, an edit,
    ``ready_for_review``, the default branch, manual and scheduled runs) has a group of its
    own and neither cancels nor is cancelled: a ``labeled`` run whose jobs skip could
    otherwise cancel the commit's real run and report the required check as passed."""
    group = (
        "${{ github.workflow_ref }}-${{ github.event_name }}-${{ "
        "(github.event_name == 'pull_request' && "
        f"contains(fromJSON('{_COMMIT_ACTIONS}'), github.event.action) && "
        "github.event.pull_request.number) || "
        f"(github.event_name == 'push' && github.ref != 'refs/heads/{default}' && github.ref)"
        " || github.run_id }}"
    )
    return f"concurrency:\n  group: {group}\n  cancel-in-progress: true\n"


def render(text: str, changes: Sequence[Mapping[str, Any]], default: str) -> Rendered:
    """``text`` with ``changes`` (rule 9's ``changes`` of one pipeline) written in, for a
    repository whose default branch is ``default``. Raises :class:`WorkflowError` when the
    file is not a workflow Kelpie edits."""
    if len(text.encode()) > MAX_WORKFLOW:
        raise WorkflowError("workflow file too large to edit")
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
    original = _parse(text)
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
            edited = edit(text, change, default)
        except _Skip as exc:
            left_out.append((name, str(exc)))
            continue
        try:
            data = _parse(edited)
            ok = _CHECKS[name](data, change) and _same(original, data, [*applied, name])
        except WorkflowError:
            ok = False
        if not ok:
            left_out.append((name, "could not be placed safely in this file"))
            continue
        text = edited
        applied.append(name)
    if crlf:
        text = text.replace("\n", "\r\n")
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


# --- the check that nothing else changed ----------------------------------------------------


def _same(before: Mapping[Any, Any], after: Mapping[Any, Any], applied: Sequence[str]) -> bool:
    """Whether ``after`` says what ``before`` said, once the keys the changes add are taken
    out of both (and ``on`` is read in one form)."""
    return _without(before, applied) == _without(after, applied)


def _without(source: Mapping[Any, Any], applied: Sequence[str]) -> Any:
    data: dict[Any, Any] = copy.deepcopy(dict(source))
    on_key = "on" if "on" in data else True
    on = data.get(on_key)
    if isinstance(on, str):
        on = {on: None}
    elif isinstance(on, list):
        on = dict.fromkeys(on)
    if isinstance(on, dict):
        on = {k: (v if v != {} else None) for k, v in on.items()}
        data[on_key] = on
    if "cancel_in_progress" in applied:
        data.pop("concurrency", None)
    for trigger in ("push", "pull_request"):
        value = on.get(trigger) if isinstance(on, dict) else None
        if not isinstance(on, dict) or not isinstance(value, dict):
            continue
        if "ignore_docs_paths" in applied:
            value.pop("paths-ignore", None)
        if (
            "skip_drafts" in applied
            and trigger == "pull_request"
            and value.get("types") == list(PR_EVENTS)
        ):
            value.pop("types")
        if not value:
            on[trigger] = None
    jobs = data.get("jobs")
    if "skip_drafts" in applied and isinstance(jobs, dict):
        for job in jobs.values():
            if isinstance(job, dict) and job.get("if") == NOT_DRAFT:
                job.pop("if")
    return data


# --- cancel_in_progress -------------------------------------------------------------------


def _cancel_in_progress(text: str, change: Mapping[str, Any], default: str) -> str:
    if "concurrency" in _parse(text):
        raise _Skip("the workflow sets concurrency already")
    if _BRANCH.fullmatch(default) is None or ".." in default:
        raise _Skip("the default branch's name is not one Kelpie writes into an expression")
    lines = text.splitlines(keepends=True)
    at = _top_line(lines, "jobs")
    if at is None:
        raise _Skip("no top-level jobs key to place it before")
    while at > 0 and lines[at - 1].lstrip().startswith("#") and _indent(lines[at - 1]) == 0:
        at -= 1  # above the comments that belong to jobs
    return "".join([*lines[:at], concurrency(default), "\n", *lines[at:]])


def _check_cancel(data: Mapping[Any, Any], change: Mapping[str, Any]) -> bool:
    block = data.get("concurrency")
    return isinstance(block, dict) and block.get("cancel-in-progress") is True


# --- ignore_docs_paths --------------------------------------------------------------------


def _ignore_docs_paths(text: str, change: Mapping[str, Any], default: str) -> str:
    globs = _globs(change)
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
    globs = _globs(change)
    triggers = [t for t in ("pull_request", "push") if isinstance(on, dict) and t in on]
    return bool(triggers) and all(
        isinstance(on[t], dict) and on[t].get("paths-ignore") == globs for t in triggers
    )


def _globs(change: Mapping[str, Any]) -> list[str]:
    """The docs globs in GitHub's filter syntax; a glob with anything but path characters
    leaves the change out (it would be written into the file)."""
    paths = change.get("paths")
    if not isinstance(paths, list) or not paths:
        return []
    if not all(isinstance(g, str) and _GLOB.fullmatch(g) for g in paths):
        raise _Skip("a docs path has characters Kelpie does not write into a workflow")
    return [_github_glob(g) for g in paths]


def _github_glob(glob: str) -> str:
    """A path glob as Kelpie matches it (``*`` crosses ``/``) in GitHub's filter syntax."""
    return re.sub(r"\*+", "**", glob)


# --- skip_drafts --------------------------------------------------------------------------


def _skip_drafts(text: str, change: Mapping[str, Any], default: str) -> str:
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
        and bool(jobs)
        and all(isinstance(b, dict) and "if" in b for b in jobs.values())  # every free job too
    )


_EDITS: dict[str, Callable[[str, Mapping[str, Any], str], str]] = {
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


def _key(match: re.Match[str]) -> str:
    return match["key"].strip("\"'")


def _top_line(lines: Sequence[str], key: str) -> int | None:
    for i, line in enumerate(lines):
        match = _TOP.match(line.rstrip("\n"))
        if match and _key(match) == key:
            return i
    return None


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


def _unit(lines: Sequence[str]) -> int:
    """The file's indent step: its smallest indent, 2 when nothing is indented."""
    indents = [
        _indent(line)
        for line in lines
        if line.strip() and not line.lstrip().startswith("#") and _indent(line) > 0
    ]
    return min(indents, default=2)


def _child_indent(lines: Sequence[str], start: int, indent: int) -> int:
    """The indent of the block under ``start``: its first content line's, else one step."""
    for line in lines[start + 1 : _block_end(lines, start, indent)]:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return _indent(line)
    return indent + _unit(lines)


def _block_on(text: str) -> str:
    """``on: push``, ``on: [push, pull_request]`` or a block list of events written as a
    block mapping, so triggers can take settings; other forms as they are."""
    lines = text.splitlines(keepends=True)
    at = _top_line(lines, "on")
    if at is None:
        raise _Skip("no top-level on key")
    match = _TOP.match(lines[at].rstrip("\n"))
    assert match is not None
    key, step = match["key"], _unit(lines)
    rest = match["rest"].split("#", 1)[0].strip()
    end = _block_end(lines, at, 0)
    if not rest:
        body = [line for line in lines[at + 1 : end] if line.strip()]
        items = [
            _ITEM.match(line.rstrip("\n")) for line in body if not line.strip().startswith("#")
        ]
        if not items or not all(items):
            return text  # a block mapping already (or something Kelpie leaves alone)
        events = [m["name"] for m in items if m is not None]
        block = [f"{key}:\n", *(f"{' ' * step}{e}:\n" for e in events)]
        return "".join([*lines[:at], *block, *lines[end:]])
    names = re.fullmatch(r"\[\s*([\w-]+(?:\s*,\s*[\w-]+)*)\s*\]|([\w-]+)", rest)
    if names is None:
        raise _Skip("its on key is not in a form Kelpie edits")
    events = [e.strip() for e in (names[1] or names[2]).split(",")]
    block = [f"{key}:\n", *(f"{' ' * step}{e}:\n" for e in events)]
    return "".join([*lines[:at], *block, *lines[at + 1 :]])


def _add_under_trigger(
    text: str, trigger: str, key: str, value: str, *, inline: bool = False
) -> str:
    """``key`` under ``on.<trigger>`` (a block mapping or empty): ``value`` on the same line
    (``inline``) or as the lines below it."""
    lines = text.splitlines(keepends=True)
    on_at = _top_line(lines, "on")
    if on_at is None:
        raise _Skip("no top-level on key")
    end = _block_end(lines, on_at, 0)
    level = _child_indent(lines, on_at, 0)  # the triggers, not keys nested under them
    step = _unit(lines)
    for i in range(on_at + 1, end):
        match = _CHILD.match(lines[i].rstrip("\n"))
        if match is None or len(match["indent"]) != level or _key(match) != trigger:
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
                f"{' ' * (child + step)}{line}\n" for line in value.splitlines()
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
        if match is None or len(match["indent"]) != level or _key(match) != job:
            continue
        if match["rest"].split("#", 1)[0].strip():
            raise _Skip("a job is not a block Kelpie edits")  # no text from the file
        child = _child_indent(lines, i, len(match["indent"]))
        added = f"{' ' * child}{key}: {value}\n"
        return "".join([*lines[: i + 1], added, *lines[i + 1 :]])
    raise _Skip("a job is not in a form Kelpie edits")  # no text from the file
