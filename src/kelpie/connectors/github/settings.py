"""``[connectors.github]``: which repositories Kelpie may read, and with which token.

``[connectors.github.ci]`` (optional) adds GitHub Actions runs for rule 9 and, opt-in, the
``cancel_build`` action (see :mod:`kelpie.connectors.github.builds`). ``[connectors.github.pulls]``
(optional) adds the open pull requests for rule 6 (see :mod:`kelpie.connectors.github.pulls`)."""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from kelpie.connectors.github.billing import CiBilling
from kelpie.connectors.github.blocks import DEFAULT_MAX_REQUESTS, DEFAULT_TICK_BUDGET
from kelpie.connectors.github.builds import CiSettings, GitHubCI, load_ci
from kelpie.connectors.github.client import (
    DEFAULT_API_URL,
    DEFAULT_CACHE_TTL,
    DEFAULT_TIMEOUT,
    MIN_CACHE_TTL,
    GitHubClient,
    GitHubObserver,
    check_api_url,
    valid_token,
)
from kelpie.connectors.github.connector import GitHubConnector, valid_repo
from kelpie.connectors.github.proposals import Proposer
from kelpie.connectors.github.pulls import PullSettings, load_pulls
from kelpie.core.serde import parse_duration

log = logging.getLogger(__name__)

DEFAULT_TOKEN_ENV = "GITHUB_TOKEN"
MAX_TIMEOUT = timedelta(seconds=60)
MAX_REQUESTS_PER_TICK = 5000
MAX_CI_WORKFLOWS = 50
MAX_WORKFLOW_NAME = 255
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_KEYS = {
    "repos",
    "token_env",
    "api_url",
    "timeout",
    "cache_ttl",
    "max_requests_per_tick",
    "tick_budget",
    "enabled",
    "ci_workflows",
    "ci",
    "pulls",
}


@dataclass(frozen=True, slots=True)
class GitHubSettings:
    repos: tuple[str, ...]
    api_url: str = DEFAULT_API_URL
    token_env: str = DEFAULT_TOKEN_ENV
    token_env_explicit: bool = False  # set in the config: the variable must then be set
    timeout: timedelta = DEFAULT_TIMEOUT
    cache_ttl: timedelta = DEFAULT_CACHE_TTL
    max_requests_per_tick: int = DEFAULT_MAX_REQUESTS
    tick_budget: timedelta = DEFAULT_TICK_BUDGET
    enabled: bool = True
    # Workflows (names or paths) that must have run for ci_status, read from Actions runs:
    # one list for every repo, or a list per repo (the other repos: every run counts).
    ci_workflows: tuple[str, ...] = ()
    ci_workflows_by_repo: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    ci: CiSettings | None = None  # None: no [connectors.github.ci] table
    pulls: PullSettings | None = None  # None: no [connectors.github.pulls] table

    def token(self, environ: Mapping[str, str] = os.environ) -> str | None:
        """The token from ``$token_env``; None when unset (anonymous, 60 requests an hour).

        Raises ValueError when ``token_env`` was configured explicitly but is not set, or
        when the value cannot go into a header. The message never contains the value.
        """
        token = environ.get(self.token_env, "").strip()
        if not token:
            if self.token_env_explicit and self.enabled:
                raise ValueError(f"[connectors.github] token_env: ${self.token_env} is not set")
            return None
        if not valid_token(token):
            raise ValueError(
                f"[connectors.github] ${self.token_env} contains characters not allowed in "
                "an HTTP header"
            )
        return token

    def build(
        self,
        token: str | None,
        clock: Callable[[], datetime],
        observer: GitHubObserver | None = None,
    ) -> GitHubConnector:
        client = GitHubClient(
            self.api_url,
            token,
            timeout=self.timeout,
            cache_ttl=self.cache_ttl,
            clock=clock,
            observer=observer,
        )
        workflows = self.ci_workflows_by_repo or self.ci_workflows
        return GitHubConnector(client, self.repos, ci_workflows=workflows)

    def build_ci(
        self,
        connector: GitHubConnector,
        clock: Callable[[], datetime],
        environ: Mapping[str, str] = os.environ,
        observer: GitHubObserver | None = None,
    ) -> GitHubCI | None:
        """The CI source sharing ``connector``'s client; None when not configured. With
        ``cancel_token_env`` the cancel request alone goes through a second client that
        holds that token; with ``billing_user``/``billing_org`` the billing usage report is
        read through a third one holding ``billing_token_env``'s token. Raises ValueError
        when one of those tokens is missing or unusable."""
        if self.ci is None or not self.ci.enabled:
            return None
        token = self.ci.cancel_token(environ)
        cancel_client = None
        if token is not None:
            cancel_client = GitHubClient(
                self.api_url,
                token,
                timeout=self.timeout,
                cache_ttl=self.cache_ttl,
                clock=clock,
                observer=observer or connector.client.observer,
            )
        billing = None
        billing_token = self.ci.billing_token(environ)
        if billing_token is not None and self.ci.billing_account is not None:
            others = {self.token_env: environ.get(self.token_env, "").strip()}
            if self.ci.cancel_token_env is not None:  # also while allow_cancel is off
                others[self.ci.cancel_token_env] = environ.get(self.ci.cancel_token_env, "").strip()
            for name, value in others.items():
                if value and value == billing_token:
                    log.warning(
                        "[connectors.github.ci] $%s holds the same token as $%s: one token "
                        "then does both jobs; give billing a token of its own",
                        self.ci.billing_token_env,
                        name,
                    )
            # Its own client, without the main client's metrics: its rate limit and its
            # refusals are not the main token's.
            billing_client = GitHubClient(
                self.api_url,
                billing_token,
                timeout=self.timeout,
                cache_ttl=self.cache_ttl,
                clock=clock,
            )
            billing = CiBilling(
                billing_client,
                self.ci.billing_account,
                organization=self.ci.billing_organization,
                interval=self.ci.billing_interval,
                reset_day=self.ci.quota_reset_day,
                skus=self.ci.billing_skus,
                budget=self.ci.tick_budget,
            )
        proposer = None
        propose_token = self.ci.propose_token(environ)
        if propose_token is not None:
            if propose_token == environ.get(self.token_env, "").strip():
                log.warning(
                    "[connectors.github.ci] $%s holds the same token as $%s: the token that "
                    "reads everything could then change workflows; give it a token of its own",
                    self.ci.propose_token_env,
                    self.token_env,
                )
            proposer = Proposer(
                GitHubClient(
                    self.api_url,
                    propose_token,
                    timeout=self.timeout,
                    cache_ttl=self.cache_ttl,
                    clock=clock,
                ),
                self.ci.propose_repos,
            )
        return GitHubCI(connector.client, self.ci, cancel_client, billing, proposer)


def load_github(section: Any) -> GitHubSettings:
    """Parse ``[connectors.github]``; raises ValueError with the offending key."""
    if not isinstance(section, Mapping):
        raise ValueError("[connectors.github] must be a table")
    if extra := set(section) - _KEYS:
        raise ValueError(f"unknown [connectors.github] keys: {sorted(extra)}")
    repos = section.get("repos")
    if not isinstance(repos, list) or not repos or not all(valid_repo(r) for r in repos):
        raise ValueError(
            '[connectors.github] repos must be a non-empty list of "owner/name" strings'
        )
    token_env = section.get("token_env", DEFAULT_TOKEN_ENV)
    if not isinstance(token_env, str) or not _ENV_NAME.fullmatch(token_env):
        raise ValueError("[connectors.github] token_env must be an environment variable name")
    try:
        api_url = check_api_url(section.get("api_url", DEFAULT_API_URL))
    except ValueError as exc:
        raise ValueError(f"[connectors.github] {exc}") from None
    if api_url != DEFAULT_API_URL and "token_env" not in section:
        # A token meant for one GitHub must not go to another host by default.
        raise ValueError(
            "[connectors.github] token_env must be set explicitly when api_url is not "
            f"{DEFAULT_API_URL}"
        )
    timeout = _duration(section, "timeout", DEFAULT_TIMEOUT)
    if timeout <= timedelta(0) or timeout > MAX_TIMEOUT:
        raise ValueError("[connectors.github] timeout must be positive and at most 60s")
    cache_ttl = _duration(section, "cache_ttl", DEFAULT_CACHE_TTL)
    if cache_ttl < MIN_CACHE_TTL:
        raise ValueError("[connectors.github] cache_ttl must be at least 30s")
    max_requests = section.get("max_requests_per_tick", DEFAULT_MAX_REQUESTS)
    if (
        isinstance(max_requests, bool)
        or not isinstance(max_requests, int)
        or not 1 <= max_requests <= MAX_REQUESTS_PER_TICK
    ):
        raise ValueError(
            f"[connectors.github] max_requests_per_tick must be an integer from 1 to "
            f"{MAX_REQUESTS_PER_TICK}"
        )
    tick_budget = _duration(section, "tick_budget", DEFAULT_TICK_BUDGET)
    if tick_budget <= timedelta(0) or tick_budget > MAX_TIMEOUT:
        raise ValueError("[connectors.github] tick_budget must be positive and at most 60s")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError("[connectors.github] enabled must be true or false")
    repos = list(dict.fromkeys(r.lower() for r in repos))
    ci_workflows, ci_by_repo = _ci_workflows(section.get("ci_workflows", []), repos)
    ci = load_ci(section["ci"], repos) if "ci" in section else None
    if ci is not None and ci.billing_token_env is not None and ci.billing_token_env == token_env:
        raise ValueError(
            "[connectors.github.ci] billing_token_env must not be [connectors.github] "
            "token_env: billing gets a token of its own"
        )
    pulls = load_pulls(section["pulls"]) if "pulls" in section else None
    return GitHubSettings(
        repos=tuple(repos),
        api_url=api_url,
        token_env=token_env,
        token_env_explicit="token_env" in section,
        timeout=timeout,
        cache_ttl=cache_ttl,
        max_requests_per_tick=max_requests,
        tick_budget=tick_budget,
        enabled=enabled,
        ci_workflows=ci_workflows,
        ci_workflows_by_repo=ci_by_repo,
        ci=ci,
        pulls=pulls,
    )


def _ci_workflows(
    value: Any, repos: list[str]
) -> tuple[tuple[str, ...], dict[str, tuple[str, ...]]]:
    """``ci_workflows``: a list for every repo, or a table ``{"owner/name" = [...]}`` whose
    keys are among ``repos``. Names are kept as written: they are matched exactly."""
    if isinstance(value, Mapping):
        by_repo: dict[str, tuple[str, ...]] = {}
        for repo, names in value.items():
            key = repo.lower() if isinstance(repo, str) else repo
            if key not in repos:
                raise ValueError(
                    f"[connectors.github] ci_workflows names {repo!r}, which is not in repos"
                )
            if key in by_repo:
                raise ValueError(f"[connectors.github] ci_workflows names {repo!r} twice")
            by_repo[key] = _workflow_names(names)
        return (), by_repo
    return _workflow_names(value), {}


def _workflow_names(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or len(value) > MAX_CI_WORKFLOWS
        or not all(
            isinstance(v, str)
            and v.strip() == v
            and 0 < len(v) <= MAX_WORKFLOW_NAME
            and v.isprintable()
            for v in value
        )
    ):
        raise ValueError(
            "[connectors.github] ci_workflows must be a list of at most "
            f"{MAX_CI_WORKFLOWS} workflow names or paths (printable, no leading or trailing "
            "space), or a table of such lists per repository"
        )
    return tuple(dict.fromkeys(value))


def _duration(section: Mapping[str, Any], key: str, default: timedelta) -> timedelta:
    try:
        return parse_duration(section.get(key, default))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"[connectors.github] {key}: {exc}") from None
