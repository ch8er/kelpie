"""``kelpie doctor`` checks for ``[adapters.github_issues]``.

Each check is ``(status, name, detail, fix)`` with status ``PASS``, ``WARN``, ``FAIL`` or
``SKIP``; :mod:`kelpie.doctor` renders them. Only GETs are sent: the repositories (with what
the token may do on them), the labels the config names, and one read of the issues as
``kelpie serve`` makes it. Nothing is written.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import datetime

from kelpie.adapters.issues.adapter import IssuesAdapter
from kelpie.adapters.issues.config import SECTION, GitHubIssuesSettings
from kelpie.adapters.issues.github import LABEL_PATH, GitHubIssues
from kelpie.connectors.github.client import GitHubError

CheckTuple = tuple[str, str, str, str]

TOKEN, REPOS, LABELS, READ = (
    "github issues token",
    "github issues repositories",
    "github issues labels",
    "github issues read",
)
QUICKSTART = "docs/quickstart-github-issues.md"
WRITE_ROLES = ("admin", "maintain", "push", "triage")  # may label, assign and comment


def _check(status: str, name: str, detail: str, fix: str = "") -> CheckTuple:
    return (status, name, detail, fix)


def check_token(settings: GitHubIssuesSettings, environ: Mapping[str, str]) -> CheckTuple:
    try:
        token = settings.token(environ)
    except ValueError as exc:
        return _check("FAIL", TOKEN, str(exc), f"set ${settings.token_env} ({QUICKSTART})")
    if token is None:
        return _check(
            "WARN",
            TOKEN,
            f"${settings.token_env} is not set: anonymous, 60 requests an hour, public "
            "repositories only, no actions",
            f"set ${settings.token_env} to a token with Issues and Pull requests: read "
            f"(Issues: write only for rules in act mode; {QUICKSTART}, step 2)",
        )
    return _check("PASS", TOKEN, f"read from ${settings.token_env}")


def _labels(settings: GitHubIssuesSettings) -> dict[str, list[str]]:
    """Per repository, the labels the config relies on."""
    out: dict[str, list[str]] = {}
    for board in settings.boards:
        names = [board.label] if board.label else []
        names += [*board.blocked_labels, *board.ready_labels]
        names += [
            agent.label
            for name, agent in settings.agents.items()
            if agent.label and (not board.agents or name in board.agents)
        ]
        out[board.key] = list(dict.fromkeys(n for n in names if n))
    return out


def check_repos(reader: GitHubIssues) -> CheckTuple:
    """Each repository answers, and the account's role there (the repository's
    ``permissions`` are the account's, not the token's: what a fine-grained token may do
    cannot be read with GETs)."""
    problems: list[str] = []
    read_only: list[str] = []
    broad: list[str] = []
    for board in reader.boards:
        try:
            answer = reader.client.get(f"/repos/{board.key}", fresh=True).body
        except GitHubError as exc:
            problems.append(f"{board.key}: {exc.kind}")
            continue
        permissions = answer.get("permissions") if isinstance(answer, Mapping) else None
        if not isinstance(permissions, Mapping):
            continue  # a GitHub App's token: no role to report
        if not any(permissions.get(role) is True for role in WRITE_ROLES):
            read_only.append(board.key)
        elif permissions.get("admin") is True or permissions.get("maintain") is True:
            broad.append(board.key)
    if problems:
        return _check(
            "FAIL",
            REPOS,
            "; ".join(problems),
            "check repos and that the token can read them (a fine-grained token: Issues and "
            "Pull requests, read; Metadata)",
        )
    if read_only:
        return _check(
            "WARN",
            REPOS,
            f"the account's role on {', '.join(read_only)} allows no labels, assignments or "
            "comments by Kelpie",
            "for rules in act mode: the triage role for the account, and Issues: write for a "
            "fine-grained token (its own permissions cannot be checked here)",
        )
    note = " (the token's own permissions cannot be checked here)"
    if broad:
        return _check(
            "WARN",
            REPOS,
            f"the account administers or maintains {', '.join(broad)}: Kelpie's changes act "
            f"with that standing{note}",
            "use a dedicated account with the triage role (" + QUICKSTART + ", step 2)",
        )
    return _check("PASS", REPOS, ", ".join(b.key for b in reader.boards) + note)


def check_labels(reader: GitHubIssues, settings: GitHubIssuesSettings) -> CheckTuple:
    missing: list[str] = []
    failed: list[str] = []
    unchecked: list[str] = []
    for repo, names in _labels(settings).items():
        for name in names:
            if not LABEL_PATH.fullmatch(name):
                unchecked.append(repr(name))
                continue
            try:
                reader.client.get(f"/repos/{repo}/labels/{name}", fresh=True)
            except GitHubError as exc:
                if exc.kind == "not_found":
                    missing.append(f"{repo}: {name}")
                else:
                    failed.append(f"{repo}: {name} ({exc.kind})")
    skipped = f"; not checked (spaces or symbols): {', '.join(unchecked)}" if unchecked else ""
    if missing:
        return _check(
            "WARN",
            LABELS,
            f"not defined: {', '.join(missing)}{skipped}",
            "create them (or fix the names): a label that does not exist never matches",
        )
    if failed:
        return _check("WARN", LABELS, f"could not be checked: {', '.join(failed)}{skipped}")
    return _check("PASS", LABELS, f"every label checked exists{skipped}")


def check_read(adapter: IssuesAdapter, now: datetime) -> CheckTuple:
    try:
        snapshot = adapter.snapshot(now)
    except Exception as exc:  # the report must go on
        reason = getattr(exc, "public_reason", None) or type(exc).__name__
        return _check("FAIL", READ, str(reason), f"see {QUICKSTART}")
    tasks = snapshot.tasks
    held = sum(1 for t in tasks if t.owner_id and not t.status.is_terminal)
    detail = f"{len(tasks)} tasks, {held} held by an agent or a person"
    problem = adapter.check()
    if problem:
        return _check("WARN", READ, f"{detail}; {problem}", "run kelpie doctor again later")
    if not adapter.agents:
        return _check(
            "WARN",
            READ,
            f"{detail}; no agents configured, so every issue held is a person's",
            f"add [{SECTION[1:-1]}.agents.<name>] ({QUICKSTART})",
        )
    return _check("PASS", READ, detail)


def run(
    settings: GitHubIssuesSettings, now: datetime, environ: Mapping[str, str] = os.environ
) -> list[CheckTuple]:
    token = check_token(settings, environ)
    if token[0] == "FAIL":
        return [token, *skipped("no token")[1:]]
    try:
        adapter = settings.build(environ)
    except Exception as exc:  # the report must go on
        return [token, *skipped(f"the adapter could not be built ({type(exc).__name__})")[1:]]
    reader = adapter.tracker
    if not isinstance(reader, GitHubIssues):
        return [token, *skipped("not a GitHub Issues adapter")[1:]]
    return [
        token,
        _guarded(REPOS, lambda: check_repos(reader)),
        _guarded(LABELS, lambda: check_labels(reader, settings)),
        _guarded(READ, lambda: check_read(adapter, now)),
    ]


def _guarded(name: str, check: Callable[[], CheckTuple]) -> CheckTuple:
    """A check that cannot take ``kelpie doctor`` down: an unexpected error is a FAIL that
    shows its type only."""
    try:
        return check()
    except Exception as exc:  # the report must go on
        return _check("FAIL", name, f"the check failed: {type(exc).__name__}", f"see {QUICKSTART}")


def skipped(why: str) -> list[CheckTuple]:
    return [_check("SKIP", name, why) for name in (TOKEN, REPOS, LABELS, READ)]
