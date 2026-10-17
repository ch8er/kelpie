"""GitHub condition connector: ``pr_merged`` and ``ci_status`` for configured repositories.

Read-only. A condition names its pull request in ``params``: ``{repo: "owner/name", pr: N}``;
a ``ci_status`` condition may name the commit (``sha``) instead of taking the PR's head (the
connector never writes ``sha`` itself; the PR's head it saw is ``head_sha``), and
``expect`` (``success``, the default, or ``failure``).

- ``pr_merged`` holds once the PR is merged (``satisfied_at`` = ``merged_at``). A closed,
  unmerged PR never satisfies it (``params.pr_state = "closed"``, ``params.terminal = True``).
- ``ci_status`` looks at the head commit: the combined commit status and the check runs.
  Green means every reported status and check run finished successfully (``neutral`` and
  ``skipped`` count as success) and at least one was reported. ``satisfied_at`` is the
  latest completion. Where the token may not read one of the two (HTTP 403: a fine-grained
  token has no Checks permission, statuses need ``Commit statuses: read``), the commit's
  GitHub Actions runs are read as well, see :mod:`kelpie.connectors.github.ci_runs`. That a
  repository needs this is remembered, but only when the Actions runs could be read in the
  same probe, and asked again every ``reprobe`` (an hour), so a tick does not repeat calls
  that fail. A 403 for a rate limit or an organization policy (SAML, IP allow list) is not a
  missing permission: it is an unknown result and teaches nothing.

Only repositories listed in the config are ever queried: the repo a block reason or a
condition names is checked against that list first, so an agent cannot point the token at
another repository. Every failure degrades to an unknown result (``satisfied = None`` and
``params.error = <kind>``) and never raises; ``checked_at`` is the time GitHub last
confirmed the answer, so rules see stale evidence for what it is.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from collections import OrderedDict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from kelpie.connectors.github import ci_runs
from kelpie.connectors.github.client import GitHubClient, GitHubError, Response
from kelpie.core.model import Condition, ConditionKind

log = logging.getLogger(__name__)

# ASCII only: agents write block reasons, and Unicode digits ("²", "١٢") must not parse.
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,99})"
_NUMBER = r"[0-9]{1,9}"
REPO = re.compile(rf"(?P<owner>{_NAME})/(?P<name>{_NAME})", re.ASCII)
_SHA = re.compile(r"[0-9a-fA-F]{7,64}", re.ASCII)
_DIGITS = re.compile(_NUMBER, re.ASCII)
_REF = re.compile(rf"(?:(?P<repo>{_NAME}/{_NAME}))?#(?P<n>{_NUMBER})|(?P<bare>{_NUMBER})", re.ASCII)
_URL_PATH = re.compile(rf"/(?P<repo>{_NAME}/{_NAME})/pull/(?P<n>{_NUMBER})(?:/.*)?", re.ASCII)

GREEN = frozenset({"success"}) | ci_runs.NEUTRAL
RED = ci_runs.RED
MAX_PAGE = 100  # statuses and check runs: one page; beyond it the CI state is not known
MAX_CONFIRMED = 10_000  # PRs remembered as existing
REPROBE = timedelta(hours=1)  # how long "this token may not read X" is believed
MAX_RUN_PAGES = 3  # pages of workflow runs of one commit (of MAX_PAGE) before it is unknown

# CI sources, as named in ``CiState.sources`` and ``params.ci_source``.
STATUSES = "statuses"
CHECK_RUNS = "check-runs"
ACTIONS = "actions"
# The permission whose absence a 403 on each source means.
PERMISSIONS = {
    STATUSES: "Commit statuses: read",
    CHECK_RUNS: "Checks: read (not offered for fine-grained personal access tokens)",
    ACTIONS: "Actions: read",
}
PULLS = "Pull requests: read"


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
    sources: tuple[str, ...] = ()  # what it was read from: STATUSES, CHECK_RUNS, ACTIONS
    refused: tuple[str, ...] = ()  # sources the token may not read (HTTP 403)
    missing: tuple[str, ...] = ()  # ``ci_workflows`` entries without a result on the commit


@dataclass(frozen=True, slots=True)
class _Access:
    """What the token could read of a repository's commit CI, and when that was asked."""

    statuses: bool
    check_runs: bool
    probed_at: datetime


@dataclass(frozen=True, slots=True)
class Sample:
    """A commit to probe CI with (for ``kelpie doctor``), and what could not be read."""

    sha: str | None  # None: no pull request and no workflow run, or neither is readable
    pulls_refused: bool  # HTTP 403 on the pull requests
    runs_refused: bool  # HTTP 403 on the workflow runs, asked for because no PR gave a commit
    pr: int | None = None  # the pull request the commit is the head of


@dataclass(frozen=True, slots=True)
class Workflow:
    name: str
    path: str
    state: str  # active | disabled_manually | disabled_inactivity | ...


@dataclass(frozen=True, slots=True)
class _Runs:
    items: list[Mapping[str, Any]]
    more: bool  # the commit has runs that were not read
    fetched_at: datetime


class Forbidden(GitHubError):
    """HTTP 403 for a missing permission (not a rate limit), naming the permission."""

    def __init__(self, path: str, permission: str) -> None:
        super().__init__("forbidden", f"GET {path}: HTTP 403 (needs {permission})", 403)
        self.permission = permission


class _Evidence:
    """What the sources said about one commit, and the verdict over all of it."""

    def __init__(self) -> None:
        self.states: list[str] = []  # success | failure | pending, one per status or run
        self.times: list[datetime] = []
        self.fetched: list[datetime] = []
        self.incomplete = False  # more results than one page: the rest may be red or running
        self.missing: tuple[str, ...] = ()  # listed workflows without a result

    def add_statuses(self, response: Response) -> None:
        self.fetched.append(response.fetched_at)
        combined = _mapping(response.body, "status")
        statuses = _list(combined.get("statuses"))
        self.incomplete = self.incomplete or _more(combined, statuses)
        if statuses and combined.get("state") in {"failure", "error"}:
            self.states.append("failure")  # GitHub's own verdict over all statuses
        for s in statuses:
            self.states.append(_verdict(s.get("state"), {"success"}, {"failure", "error"}))
            if s.get("state") == "success" and (t := _time(s.get("updated_at"))):
                self.times.append(t)

    def add_check_runs(self, response: Response) -> None:
        self.fetched.append(response.fetched_at)
        body = _mapping(response.body, "check runs")
        check_runs = _list(body.get("check_runs"))
        self.incomplete = self.incomplete or _more(body, check_runs)
        for run in check_runs:
            if run.get("status") != "completed":
                self.states.append("pending")
                continue
            self.states.append(_verdict(run.get("conclusion"), GREEN, RED))
            if t := _time(run.get("completed_at")):
                self.times.append(t)

    def add_runs(
        self, runs: _Runs, workflows: Collection[str], pr: int | None
    ) -> ci_runs.RunsVerdict:
        """Workflow runs judged by :func:`kelpie.connectors.github.ci_runs.judge`. No counted
        run says nothing (``none``): the caller decides whether another source may stand."""
        self.fetched.append(runs.fetched_at)
        verdict = ci_runs.judge(runs.items, workflows, more=runs.more, pr=pr)
        self.missing = verdict.missing
        if verdict.state == "incomplete":
            self.incomplete = True
        elif verdict.state != "none":
            self.states.append(verdict.state)
        if verdict.completed_at is not None:
            self.times.append(verdict.completed_at)
        return verdict

    def state(self, sources: tuple[str, ...], refused: tuple[str, ...]) -> CiState:
        at = min(self.fetched)
        m = self.missing if "failure" not in self.states and not self.incomplete else ()
        if "failure" in self.states:
            return CiState("failure", None, at, sources, refused, m)
        if self.incomplete:
            return CiState("incomplete", None, at, sources, refused, m)
        if not self.states:
            return CiState("none", None, at, sources, refused, m)
        if "pending" in self.states:
            return CiState("pending", None, at, sources, refused, m)
        return CiState("success", max(self.times, default=None), at, sources, refused, m)


class GitHubConnector:
    """Implements :class:`kelpie.core.ports.ConditionConnector` for GitHub."""

    name = "github"
    kinds = frozenset({ConditionKind.PR_MERGED, ConditionKind.CI_STATUS})

    def __init__(
        self,
        client: GitHubClient,
        repos: Collection[str],
        *,
        ci_workflows: Collection[str] | Mapping[str, Collection[str]] = (),
        reprobe: timedelta = REPROBE,
    ) -> None:
        self.client = client
        self.repos = frozenset(r.lower() for r in repos)
        # Workflows that must have run (names or paths): for every repo, or per repo.
        self.ci_workflows: tuple[str, ...] | dict[str, tuple[str, ...]] = (
            {r.lower(): tuple(w) for r, w in ci_workflows.items()}
            if isinstance(ci_workflows, Mapping)
            else tuple(ci_workflows)
        )
        self.reprobe = reprobe
        self._access: dict[str, _Access] = {}  # per configured repository
        # repo -> (workflows, all of them, when asked)
        self._workflows: dict[str, tuple[list[Workflow], bool, datetime]] = {}
        self._warned: set[str] = set()
        self.host = web_host(client.api_url)
        self._confirmed: OrderedDict[PrRef, None] = OrderedDict()  # LRU of existing PRs

    def __repr__(self) -> str:
        return f"GitHubConnector({self.client!r}, repos={sorted(self.repos)})"

    def allows(self, repo: str) -> bool:
        return repo.lower() in self.repos

    def workflows_for(self, repo: str) -> tuple[str, ...]:
        """The workflows that must have run for ``repo`` (empty: every run counts)."""
        if isinstance(self.ci_workflows, dict):
            return self.ci_workflows.get(repo.lower(), ())
        return self.ci_workflows

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
        params = {k: v for k, v in condition.params.items() if k not in {"error", "terminal"}}
        try:
            pull = self.pull(ref)
            params["pr_state"] = pull.state
            if pull.head_sha:
                params["head_sha"] = pull.head_sha  # a result key: dispatch_gate drops it
            else:
                params.pop("head_sha", None)  # always this check's result, never a stale one
            if condition.kind is ConditionKind.PR_MERGED and pull.state == "closed":
                params["terminal"] = True  # closed unmerged: it can never be met
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
            ci = self.ci(ref.repo, sha, ref.number)
            # ``sha`` is the condition's own binding and is never written back: a condition
            # that had none follows the PR's head (``head_sha``, a result key) wherever it
            # is parked and registered again.
            params.update(ci_state=ci.state, ci_source="+".join(ci.sources))
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

    def age(self, ref: PrRef) -> timedelta | None:
        """How old the cached answer about this PR is; None if GitHub was never asked."""
        return self.client.cache_age(_pull_path(ref))

    def pull(self, ref: PrRef) -> PullState:
        if not self.allows(ref.repo):
            raise GitHubError("not_allowed", f"{ref.repo} is not a configured repository")
        try:
            response = self.client.get(_pull_path(ref))
        except GitHubError as exc:
            if exc.kind == "not_found":
                self._confirmed.pop(ref, None)
            raise
        body = _mapping(response.body, f"{ref}")
        self._confirmed[ref] = None
        self._confirmed.move_to_end(ref)
        while len(self._confirmed) > MAX_CONFIRMED:
            self._confirmed.popitem(last=False)  # the least recently confirmed
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

    def ci(self, repo: str, sha: str, pr: int | None = None) -> CiState:
        """The CI state of a commit; ``pr``: the pull request it is asked for (see
        :func:`kelpie.connectors.github.ci_runs.judge`)."""
        return self._ci(repo, sha, pr, force=False)

    def probe_ci(self, repo: str, sha: str, pr: int | None = None) -> CiState:
        """Like :meth:`ci`, but every source is asked again, whatever was remembered (for
        ``kelpie doctor``)."""
        return self._ci(repo, sha, pr, force=True)

    def _ci(self, repo: str, sha: str, pr: int | None, force: bool) -> CiState:
        if not self.allows(repo) or not _SHA.fullmatch(sha):
            raise GitHubError("not_allowed", "commit outside the configured repositories")
        key, now = repo.lower(), self.client.clock()
        known = self._access.get(key)
        due = force or known is None or now - known.probed_at >= self.reprobe
        page = {"per_page": str(MAX_PAGE)}
        status = runs = None
        if due or (known is not None and known.statuses):
            status = self._get_ci(f"/repos/{repo}/commits/{sha}/status", page)
        if due or (known is not None and known.check_runs):
            runs = self._get_ci(f"/repos/{repo}/commits/{sha}/check-runs", page)
        probed_at = known.probed_at if known is not None and not due else now
        access = _Access(status is not None, runs is not None, probed_at)
        found = _Evidence()
        sources: list[str] = []
        if status is not None:
            sources.append(STATUSES)
            found.add_statuses(status)
        if runs is not None:
            sources.append(CHECK_RUNS)
            found.add_check_runs(runs)
        refused = tuple(n for n, got in ((STATUSES, status), (CHECK_RUNS, runs)) if got is None)
        required = self.workflows_for(repo)
        if not refused:
            self._access[key] = access
            if not required:
                return found.state(tuple(sources), refused)
        # The workflow runs are read when the token may not read everything (they stand in
        # for what is out of reach) and whenever workflows are required (those must have run,
        # whatever the other sources say). All of it is judged together: all must be green.
        try:
            listed = self._runs(repo, sha, page)
        except GitHubError as exc:
            if "failure" not in found.states:
                raise  # unknown; and nothing is learned about the token: a policy, or a blip
            # A readable source already shows red, which does not depend on the runs.
            denied = (*refused, ACTIONS) if isinstance(exc, Forbidden) else refused
            return found.state(tuple(sources), denied)
        if refused:
            self._access[key] = access  # remembered only now that the runs could be read
        if STATUSES in refused and key not in self._warned:
            self._warned.add(key)
            log.warning(
                "[connectors.github] %s: commit statuses are refused (needs %s): CI that only "
                "posts statuses (Jenkins, Buildkite, CircleCI) is not seen; Actions runs are "
                "read instead",
                repo,
                PERMISSIONS[STATUSES],
            )
        sources.append(ACTIONS)
        verdict = found.add_runs(listed, required, pr)
        if (
            verdict.state == "none"
            and CHECK_RUNS in refused  # otherwise the check runs already include Actions'
            and found.states
            and "failure" not in found.states
            and self._has_workflows(repo, fresh=force)
        ):
            # The repository has workflows but none counted ran for this commit: a green
            # status alone may be a fast legacy check while the real CI has not started.
            found.states.append("pending")
        return found.state(tuple(sources), refused)

    def workflow_list(self, repo: str, fresh: bool = False) -> tuple[list[Workflow], bool]:
        """The repository's workflows (the first page; the flag says it is all of them),
        remembered for ``reprobe``."""
        key, now = repo.lower(), self.client.clock()
        known = self._workflows.get(key)
        if not fresh and known is not None and now - known[2] < self.reprobe:
            return known[0], known[1]
        path = f"/repos/{repo}/actions/workflows"
        response = self._needing(PERMISSIONS[ACTIONS], path, {"per_page": str(MAX_PAGE)})
        body = _mapping(response.body, "workflows")
        items = _list(body.get("workflows"))
        found = [
            Workflow(_str(w.get("name")), _str(w.get("path")), _str(w.get("state"))) for w in items
        ]
        self._workflows[key] = (found, not _more(body, items), now)
        return found, not _more(body, items)

    def _has_workflows(self, repo: str, fresh: bool = False) -> bool:
        """Whether the repository has an active workflow of its own. GitHub's built-in
        ``dynamic/...`` ones (Dependabot updates, CodeQL default setup, pages builds) do not
        run on pull requests and do not count."""
        found, complete = self.workflow_list(repo, fresh)
        active = any(w.state == "active" and not w.path.startswith("dynamic/") for w in found)
        return active or not complete

    def _get_ci(self, path: str, query: Mapping[str, str]) -> Response | None:
        """One statuses or check-runs call: None on HTTP 403, a missing permission (the
        client tells it from a rate limit, which raises like every other failure)."""
        try:
            return self._needing("", path, query)
        except Forbidden:
            return None

    def _runs(self, repo: str, sha: str, page: Mapping[str, str]) -> _Runs:
        """The workflow runs of a commit, up to ``MAX_RUN_PAGES`` pages. A later page that
        cannot be read leaves ``more`` set: the result is then unknown, not wrong."""
        if len(sha) not in {40, 64}:  # head_sha matches whole commit ids only
            raise GitHubError("bad_response", f"{repo}: the Actions runs need a full commit id")
        path, query = f"/repos/{repo}/actions/runs", {**page, "head_sha": sha.lower()}
        first = self._needing(PERMISSIONS[ACTIONS], path, query)
        body = _mapping(first.body, "workflow runs")
        items, fetched = _list(body.get("workflow_runs")), first.fetched_at
        total, last = _total(body), len(items)
        for number in range(2, MAX_RUN_PAGES + 1):
            if total <= len(items) or last < MAX_PAGE:
                break
            try:
                more = self.client.get(path, {**query, "page": str(number)})
            except GitHubError:
                break
            later = _mapping(more.body, "workflow runs")
            got = _list(later.get("workflow_runs"))
            # A run started between two pages shifts the next one: the same run may come
            # twice (counted once), and the total that grew says some run is still unseen.
            known = {_run_id(r) for r in items} - {None}
            items = items + [r for r in got if _run_id(r) is None or _run_id(r) not in known]
            total, last = max(total, _total(later)), len(got)
            fetched = min(fetched, more.fetched_at)
        return _Runs(items, total > len(items), fetched)

    def head_commit(self, repo: str) -> Sample:
        """A recent commit to probe with (for ``kelpie doctor``): the head of the newest pull
        request, else of the newest workflow run."""
        if not self.allows(repo):
            raise GitHubError("not_allowed", f"{repo} is not a configured repository")
        pulls_refused = False
        try:
            pulls = self._needing(PULLS, f"/repos/{repo}/pulls", {"per_page": "1", "state": "all"})
            for pull in _list(pulls.body):
                head = pull.get("head")
                sha = head.get("sha") if isinstance(head, Mapping) else None
                number = pull.get("number")
                if isinstance(sha, str) and _SHA.fullmatch(sha):
                    return Sample(sha, False, False, number if isinstance(number, int) else None)
        except Forbidden:  # the runs may still be readable, and they are what CI needs
            pulls_refused = True
        try:
            runs = self._needing(
                PERMISSIONS[ACTIONS], f"/repos/{repo}/actions/runs", {"per_page": "1"}
            )
        except Forbidden:
            return Sample(None, pulls_refused, True)
        for run in _list(_mapping(runs.body, "workflow runs").get("workflow_runs")):
            sha = run.get("head_sha")
            if isinstance(sha, str) and _SHA.fullmatch(sha):
                return Sample(sha, pulls_refused, False)
        return Sample(None, pulls_refused, False)

    def _needing(self, permission: str, path: str, query: Mapping[str, str]) -> Response:
        """A GET that needs ``permission``: HTTP 403 raises ``forbidden`` naming it."""
        try:
            return self.client.get(path, query)
        except GitHubError as exc:
            if exc.kind != "forbidden":
                raise
            raise Forbidden(path, permission) from None


def _more(body: Mapping[str, Any], items: Sequence[Any]) -> bool:
    return _total(body) > len(items)


def _total(body: Mapping[str, Any]) -> int:
    total = body.get("total_count")
    return total if isinstance(total, int) and not isinstance(total, bool) else 0


def _run_id(run: Mapping[str, Any]) -> int | None:
    value = run.get("id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _str(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _pull_path(ref: PrRef) -> str:
    return f"/repos/{ref.repo}/pulls/{ref.number}"


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


_time = ci_runs.parse_time
