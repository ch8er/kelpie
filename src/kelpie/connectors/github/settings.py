"""``[connectors.github]``: which repositories Kelpie may read, and with which token.

``[connectors.github.ci]`` (optional) adds GitHub Actions runs for rule 9 and, opt-in, the
``cancel_build`` action (see :mod:`kelpie.connectors.github.builds`). ``[connectors.github.pulls]``
(optional) adds the open pull requests for rule 6 (see :mod:`kelpie.connectors.github.pulls`)."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
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
from kelpie.connectors.github.pulls import PullSettings, load_pulls
from kelpie.core.serde import parse_duration

DEFAULT_TOKEN_ENV = "GITHUB_TOKEN"
MAX_TIMEOUT = timedelta(seconds=60)
MAX_REQUESTS_PER_TICK = 5000
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
        return GitHubConnector(client, self.repos)

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
            billing_client = GitHubClient(
                self.api_url,
                billing_token,
                timeout=self.timeout,
                cache_ttl=self.cache_ttl,
                clock=clock,
                observer=observer or connector.client.observer,
            )
            billing = CiBilling(
                billing_client,
                self.ci.billing_account,
                organization=self.ci.billing_organization,
                interval=self.ci.billing_interval,
                reset_day=self.ci.quota_reset_day,
            )
        return GitHubCI(connector.client, self.ci, cancel_client, billing)


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
    ci = load_ci(section["ci"], repos) if "ci" in section else None
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
        ci=ci,
        pulls=pulls,
    )


def _duration(section: Mapping[str, Any], key: str, default: timedelta) -> timedelta:
    try:
        return parse_duration(section.get(key, default))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"[connectors.github] {key}: {exc}") from None
