"""Which tasks a pull request belongs to (incident 17), for any code-host connector.

A PR belongs to a task when:

- its head branch is the task's own branch (``Task.branch``), or
- one of the task's codes is a whole token of the PR's title or head branch
  (case-insensitive; ``t_1a`` matches ``hermes/t_1a`` and ``t_1a: fix``, not ``t_1ab``).

A task's codes are its id and, with a ``task_code`` pattern, every match of it in the task's
title. A code must be at least ``MIN_CODE`` characters long and hold a letter and a digit:
a bare number (``42``) or a word (``fix``) would link unrelated PRs. Mentions in a PR's
description or comments never link it: on a live farm 8 of 10 such matches were wrong.
A fork's PR links nothing (its branch names are its own, and its title is not ours to
trust).

On a multi-host farm task ids are namespaced (``laptop:t_1a``, ``Task.host`` = ``laptop``),
and so are they when one adapter watches several boards (``qr:t_1a``, ``Task.board`` =
``qr``): the id without the prefixes is a code too, but only while no other task has the
same local id, so a PR is never linked to look-alike tasks on several hosts or boards.

``task_code`` runs over agent-written titles on every scan, so :func:`compile_task_code`
refuses patterns that can backtrack badly (a repeated group that itself repeats,
backreferences, lookarounds) and keeps them to ``MAX_TASK_CODE`` characters; titles are cut
at ``MAX_TITLE`` characters.
"""

from __future__ import annotations

import re
import re._parser as sre_parse  # the parse tree, to refuse patterns that backtrack badly
from collections import Counter
from collections.abc import Collection, Sequence
from dataclasses import replace
from functools import lru_cache

from kelpie.core.model import PullRequest, Task

MAX_TITLE = 500  # characters of a PR or task title searched for task codes
MAX_TASK_CODE = 200  # characters of a task_code pattern
MIN_CODE = 4  # characters of a task code
_LETTER = re.compile(r"[^\W\d_]")
_DIGIT = re.compile(r"\d")
_REPEATS = {sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT, sre_parse.POSSESSIVE_REPEAT}
_REFUSED = {sre_parse.GROUPREF, sre_parse.GROUPREF_EXISTS, sre_parse.ASSERT, sre_parse.ASSERT_NOT}


def compile_task_code(text: str) -> re.Pattern[str]:
    """The ``task_code`` pattern, or ``ValueError`` if it is unsafe or does not compile."""
    if not text.strip() or len(text) > MAX_TASK_CODE:
        raise ValueError(f"must be a regular expression of 1 to {MAX_TASK_CODE} characters")
    try:
        pattern = re.compile(text)
        tree = sre_parse.parse(text)
    except re.error as exc:
        raise ValueError(str(exc)) from None
    if pattern.groups:
        raise ValueError("must not have capturing groups")
    _check(tree, repeated=False)
    if pattern.fullmatch(""):
        raise ValueError("must not match an empty string")
    return pattern


def _check(items: object, repeated: bool) -> None:
    for op, arg in items:  # type: ignore[attr-defined]
        if op in _REFUSED:
            raise ValueError("must not use backreferences or lookarounds")
        if op in _REPEATS:
            body = arg[2]
            if repeated or _repeats(body):
                raise ValueError("must not repeat a group that itself repeats (backtracking)")
            _check(body, repeated=True)
        elif op is sre_parse.SUBPATTERN:
            _check(arg[3], repeated)
        elif op is sre_parse.BRANCH:
            for branch in arg[1]:
                _check(branch, repeated)
        elif op is sre_parse.ATOMIC_GROUP:
            _check(arg, repeated)


def _repeats(items: object) -> bool:
    for op, arg in items:  # type: ignore[attr-defined]
        if op in _REPEATS:
            return True
        if op is sre_parse.SUBPATTERN and _repeats(arg[3]):
            return True
        if op is sre_parse.BRANCH and any(_repeats(b) for b in arg[1]):
            return True
    return False


def is_code(text: str) -> bool:
    """Long and specific enough to name one task."""
    return len(text) >= MIN_CODE and bool(_LETTER.search(text)) and bool(_DIGIT.search(text))


def task_codes(task: Task, pattern: re.Pattern[str] | None) -> frozenset[str]:
    """A task's codes: its id, and the matches of ``pattern`` in its title."""
    return _codes(task.id, task.title, pattern)


@lru_cache(maxsize=8192)
def _codes(task_id: str, title: str, pattern: re.Pattern[str] | None) -> frozenset[str]:
    codes = {task_id.strip()}
    if pattern is not None:
        codes.update(m.group(0).strip() for m in pattern.finditer(title[:MAX_TITLE]))
    return frozenset(c for c in codes if is_code(c))


@lru_cache(maxsize=8192)
def _token(code: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![^\W_]){re.escape(code)}(?![^\W_])")


def link_tasks(
    pulls: Sequence[PullRequest],
    tasks: Sequence[Task],
    pattern: re.Pattern[str] | None,
    unique_local: Collection[str] | None = None,
) -> tuple[PullRequest, ...]:
    """Each PR with the ids of the tasks it belongs to (``PullRequest.task_ids``).
    ``unique_local`` (part of a multi-host farm is not observed): the local ids (casefolded)
    the last snapshot that saw every machine had exactly once; no other local id links, as a
    look-alike may be on the missing machine. None: every machine is observed."""
    by_code: dict[str, set[str]] = {}
    by_branch: dict[str, set[str]] = {}
    local = {t.id: _local_id(t) for t in tasks}
    shared = Counter(i.casefold() for i in local.values() if i is not None)
    for t in tasks:
        codes = set(task_codes(t, pattern))
        own = local[t.id]
        if (
            own is not None
            and shared[own.casefold()] == 1
            and (unique_local is None or own.casefold() in unique_local)
            and is_code(own)
        ):
            codes.add(own)
        for code in codes:
            by_code.setdefault(code.casefold(), set()).add(t.id)
        if t.branch:
            by_branch.setdefault(t.branch, set()).add(t.id)
    out = []
    for pr in pulls:
        if pr.fork:
            out.append(pr)
            continue
        ids = set(by_branch.get(pr.branch or "", ()))
        text = f"{pr.title[:MAX_TITLE]}\n{pr.branch or ''}".casefold()
        for code, owners in by_code.items():
            if code in text and _token(code).search(text):
                ids |= owners
        out.append(replace(pr, task_ids=tuple(sorted(ids))) if ids else pr)
    return tuple(out)


def unique_local_ids(tasks: Sequence[Task]) -> frozenset[str]:
    """Local ids (casefolded) that exactly one host- or board-namespaced task has."""
    seen = Counter(i.casefold() for t in tasks if (i := _local_id(t)) is not None)
    return frozenset(i for i, n in seen.items() if n == 1)


def _local_id(task: Task) -> str | None:
    """The id on the task's own board when it is namespaced by host or board
    (``<host>:<id>``, ``<board>:<id>``)."""
    local = task.local_id
    return None if local is None else local.strip() or None
