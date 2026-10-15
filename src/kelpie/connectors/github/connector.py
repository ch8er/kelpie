"""GitHub condition connector: ``pr_merged`` and ``ci_status`` for configured repositories.

Read-only. A condition names its pull request in ``params``: ``{repo: "owner/name", pr: N}``;
a ``ci_status`` condition may name the commit (``sha``) instead of taking the PR's head, and
``expect`` (``success``, the default, or ``failure``).

- ``pr_merged`` holds once the PR is merged (``satisfied_at`` = ``merged_at``). A closed,
  unmerged PR never satisfies it (``params.pr_state = "closed"``).
- ``ci_status`` looks at the head commit: the combined commit status and the check runs.
  Green means every reported status and check run finished successfully (``neutral`` and
  ``skipped`` count as success) and at least one was reported. ``satisfied_at`` is the
  latest completion.

Only repositories listed in the config are ever queried: the repo a block reason or a
condition names is checked against that list first, so an agent cannot point the token at
another repository. Every failure degrades to an unknown result (``satisfied = None`` and
``params.error = <kind>``) and never raises; ``checked_at`` is the time GitHub last
confirmed the answer, so rules see stale evidence for what it is.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.core.model import Condition, ConditionKind
from kelpie.core.serde import parse_datetime

# ASCII only: agents write block reasons, and Unicode digits ("²", "١٢") must not parse.
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})"
_NUMBER = r"[0-9]{1,9}"
REPO = re.compile(rf"(?P<owner>{_NAME})/(?P<name>{_NAME})", re.ASCII)
_SHA = re.compile(r"[0-9a-fA-F]{7,64}", re.ASCII)
_DIGITS = re.compile(_NUMBER, re.ASCII)
_REF = re.compile(rf"(?:(?P<repo>{_NAME}/{_NAME}))?#(?P<n>{_NUMBER})|(?P<bare>{_NUMBER})", re.ASCII)
_URL_PATH = re.compile(rf"/(?P<repo>{_NAME}/{_NAME})/pull/(?P<n>{_NUMBER})(?:/.*)?", re.ASCII)

GREEN = frozenset({"success", "neutral", "skipped"})
RED = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
)
MAX_PAGE = 100  # statuses and check runs: one page; beyond it the CI state is not known
MAX_CONFIRMED = 10_000  # PRs remembered as existing


def valid_repo(repo: Any) -> bool:
    return (
        isinstance(repo, str)
        and REPO.fullmatch(repo) is not None
        and ".." not in repo
        and not repo.endswith((".", ".git"))
    )


@dataclass(frozen=True, slots=True)
class PrRef:
    repo: str  # owner/name, lower case
    number: int

    def __str__(self) -> str:
        return f"{self.repo}#{self.number}"


def web_host(api_url: str) -> str:
    """The host of PR links for an API URL: github.com for api.github.com, else the API
    host itself (GitHub Enterprise serves both on one host)."""
    host = (urllib.parse.urlsplit(api_url).hostname or "").lower()
    return "github.com" if host == "api.github.com" else host


def parse_pr_ref(text: str, default_repo: str | None, host: str) -> PrRef | None:
    """``#123``, ``123``, ``owner/repo#123`` or ``https://<host>/owner/repo/pull/123``.

    The text comes from agents: anything else, malformed URLs included, gives None."""
    text = text.strip().rstrip(".,;:)]")
    if "://" in text:
        try:
            parts = urllib.parse.urlsplit(text)
            hostname = (parts.hostname or "").lower()
        except ValueError:  # e.g. "https://[oops/pull/1", an NFKC-invalid netloc
            return None
        if parts.scheme not in {"https", "http"} or hostname != host:
            return None
        m = _URL_PATH.fullmatch(parts.path)
        repo, digits = (m["repo"], m["n"]) if m else (None, None)
    else:
        m = _REF.fullmatch(text)
        repo, digits = ((m["repo"] or default_repo), (m["n"] or m["bare"])) if m else (None, None)
    if repo is None or digits is None or not valid_repo(repo) or int(digits) <= 0:
        return None
    return PrRef(repo.lower(), int(digits))


@dataclass(frozen=True, slots=True)
class PullState:
    state: str  # open | closed | merged
    merged_at: datetime | None
    head_sha: str | None
    fetched_at: datetime


@dataclass(frozen=True, slots=True)
class CiState:
    state: str  # success | failure | pending | none | incomplete
    completed_at: datetime | None
    fetched_at: datetime


class GitHubConnector:
    """Implements :class:`kelpie.core.ports.ConditionConnector` for GitHub."""

    name = "github"
    kinds = frozenset({ConditionKind.PR_MERGED, ConditionKind.CI_STATUS})

    def __init__(self, client: GitHubClient, repos: Collection[str]) -> None:
        self.client = client
        self.repos = frozenset(r.lower() for r in repos)
        self.host = web_host(client.api_url)
        self._confirmed: set[PrRef] = set()

    def __repr__(self) -> str:
        return f"GitHubConnector({self.client!r}, repos={sorted(self.repos)})"

    def allows(self, repo: str) -> bool:
        return repo.lower() in self.repos

    def ref_from_params(self, params: Mapping[str, Any]) -> PrRef | None:
        repo, number = params.get("repo"), params.get("pr")
        if not valid_repo(repo) or not self.allows(repo):
            return None
        if isinstance(number, str) and _DIGITS.fullmatch(number):
            number = int(number)
        if isinstance(number, bool) or not isinstance(number, int):
            return None
        if not 0 < number < 10**9:
            return None
        return PrRef(repo.lower(), number)

    def check(self, condition: Condition) -> Condition:
        """Evaluate a ``pr_merged`` or ``ci_status`` condition; never raises.

        A condition this connector cannot address (other kind, repo not configured, no PR)
        comes back unchanged.
        """
        ref = self.ref_from_params(condition.params)
        if condition.kind not in self.kinds or ref is None:
            return condition
        params = {k: v for k, v in condition.params.items() if k != "error"}
        try:
            pull = self.pull(ref)
            params["pr_state"] = pull.state
            if condition.kind is ConditionKind.PR_MERGED:
                merged = pull.state == "merged"
                return replace(
                    condition,
                    params=params,
                    satisfied=merged,
                    satisfied_at=pull.merged_at if merged else None,
                    checked_at=pull.fetched_at,
                )
            sha = condition.params.get("sha") or pull.head_sha
            if not isinstance(sha, str) or not _SHA.fullmatch(sha):
                raise GitHubError("bad_response", f"{ref}: no usable head commit")
            ci = self.ci(ref.repo, sha)
            params.update(sha=sha, ci_state=ci.state)
            expect = condition.params.get("expect", "success")
            satisfied: bool | None = ci.state == expect
            if ci.state == "incomplete":
                satisfied = None
            return replace(
                condition,
                params=params,
                satisfied=satisfied,
                satisfied_at=ci.completed_at if satisfied else None,
                checked_at=min(pull.fetched_at, ci.fetched_at),
            )
        except GitHubError as exc:
            # Unknown now: the condition keeps what it last knew (``satisfied`` and
            # ``checked_at``), and rules judge how old that is.
            params["error"] = exc.kind
            return replace(condition, params=params)

    def confirmed(self, ref: PrRef) -> bool:
        """Whether GitHub has shown this PR to exist (since start-up, until a 404)."""
        return ref in self._confirmed

    def pull(self, ref: PrRef) -> PullState:
        if not self.allows(ref.repo):
            raise GitHubError("not_allowed", f"{ref.repo} is not a configured repository")
        try:
            response = self.client.get(f"/repos/{ref.repo}/pulls/{ref.number}")
        except GitHubError as exc:
            if exc.kind == "not_found":
                self._confirmed.discard(ref)
            raise
        body = _mapping(response.body, f"{ref}")
        if len(self._confirmed) >= MAX_CONFIRMED:
            self._confirmed.clear()  # bounded; refs are confirmed again on the next answer
        self._confirmed.add(ref)
        merged_at = _time(body.get("merged_at"))
        if body.get("merged") is True or merged_at is not None:
            state = "merged"
        else:
            state = "closed" if body.get("state") == "closed" else "open"
        head = body.get("head")
        sha = head.get("sha") if isinstance(head, Mapping) else None
        return PullState(
            state, merged_at, sha if isinstance(sha, str) else None, response.fetched_at
        )

    def ci(self, repo: str, sha: str) -> CiState:
        if not self.allows(repo) or not _SHA.fullmatch(sha):
            raise GitHubError("not_allowed", "commit outside the configured repositories")
        page = {"per_page": str(MAX_PAGE)}
        status = self.client.get(f"/repos/{repo}/commits/{sha}/status", page)
        runs = self.client.get(f"/repos/{repo}/commits/{sha}/check-runs", page)
        fetched_at = min(status.fetched_at, runs.fetched_at)
        combined = _mapping(status.body, "status")
        statuses = _list(combined.get("statuses"))
        body = _mapping(runs.body, "check runs")
        check_runs = _list(body.get("check_runs"))
        # More results than one page: what is not visible may be red or still running.
        incomplete = _more(combined, statuses) or _more(body, check_runs)
        states: list[str] = []
        if statuses and combined.get("state") in {"failure", "error"}:
            states.append("failure")  # GitHub's own verdict over all statuses
        times: list[datetime] = []
        for s in statuses:
            states.append(_verdict(s.get("state"), {"success"}, {"failure", "error"}))
            if s.get("state") == "success" and (t := _time(s.get("updated_at"))):
                times.append(t)
        for run in check_runs:
            if run.get("status") != "completed":
                states.append("pending")
                continue
            states.append(_verdict(run.get("conclusion"), GREEN, RED))
            if t := _time(run.get("completed_at")):
                times.append(t)
        if "failure" in states:
            return CiState("failure", None, fetched_at)
        if incomplete:
            return CiState("incomplete", None, fetched_at)
        if not states:
            return CiState("none", None, fetched_at)
        if "pending" in states:
            return CiState("pending", None, fetched_at)
        return CiState("success", max(times, default=None), fetched_at)


def _more(body: Mapping[str, Any], items: Sequence[Any]) -> bool:
    total = body.get("total_count")
    return isinstance(total, int) and not isinstance(total, bool) and total > len(items)


def _verdict(value: Any, green: Collection[str], red: Collection[str]) -> str:
    if value in green:
        return "success"
    return "failure" if value in red else "pending"


def _mapping(value: Any, what: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise GitHubError("bad_response", f"{what}: unexpected response shape")
    return value


def _list(value: Any) -> list[Mapping[str, Any]]:
    return [v for v in value if isinstance(v, Mapping)] if isinstance(value, list) else []


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_datetime(value.replace("Z", "+00:00"))
    except ValueError:
        return None
