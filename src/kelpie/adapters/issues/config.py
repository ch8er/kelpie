"""``[adapters.github_issues]``: which repositories' issues are tasks, which agents work them.

Where to reach GitHub and with which token default to ``[connectors.github]`` (its ``repos``,
``api_url``, ``token_env``, ``timeout`` and ``cache_ttl``); the adapter makes its own
client from them, so its reads have their own per-tick budget and cache.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from kelpie.adapters.issues.adapter import IssuesAdapter
from kelpie.adapters.issues.github import GitHubIssues
from kelpie.adapters.issues.mapping import (
    DEFAULT_BLOCKED_LABELS,
    DEFAULT_ROLE,
    Agent,
    Board,
    login_key,
)
from kelpie.connectors.github.client import (
    DEFAULT_API_URL,
    DEFAULT_CACHE_TTL,
    DEFAULT_TIMEOUT,
    MIN_CACHE_TTL,
    GitHubClient,
    check_api_url,
    valid_token,
)
from kelpie.connectors.github.connector import valid_repo
from kelpie.connectors.github.settings import DEFAULT_TOKEN_ENV, MAX_TIMEOUT, GitHubSettings
from kelpie.core.serde import parse_duration

log = logging.getLogger(__name__)

SECTION = "[adapters.github_issues]"
NAME = "github-issues"
KEYS = frozenset(
    {
        *("enabled", "repos", "api_url", "token_env", "timeout", "cache_ttl"),
        *("lookback", "max_requests_per_tick", "tick_budget"),
        *("label", "blocked_labels", "ready_labels", "role", "agents", "boards"),
    }
)
BOARD_KEYS = frozenset({"label", "blocked_labels", "ready_labels", "role", "agents"})
AGENT_KEYS = frozenset({"role", "assignee", "label", "mention", "author"})
DEFAULT_LOOKBACK = timedelta(days=7)
DEFAULT_MAX_REQUESTS = 60
DEFAULT_TICK_BUDGET = timedelta(seconds=15)
MAX_REQUESTS = 5000
MAX_LABEL = 50  # GitHub's limit on a label name
MAX_AGENTS = 32
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*", re.ASCII)
_LOGIN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?")


@dataclass(frozen=True, slots=True)
class GitHubIssuesSettings:
    boards: tuple[Board, ...]
    agents: Mapping[str, Agent] = field(default_factory=dict)
    api_url: str = DEFAULT_API_URL
    token_env: str = DEFAULT_TOKEN_ENV
    token_env_explicit: bool = False
    timeout: timedelta = DEFAULT_TIMEOUT
    cache_ttl: timedelta = DEFAULT_CACHE_TTL
    lookback: timedelta = DEFAULT_LOOKBACK
    max_requests_per_tick: int = DEFAULT_MAX_REQUESTS
    tick_budget: timedelta = DEFAULT_TICK_BUDGET
    enabled: bool = True

    def token(self, environ: Mapping[str, str] = os.environ) -> str | None:
        """The token from ``$token_env``; None when unset. Raises ValueError when it was set
        explicitly but is missing, or cannot go into a header (never showing the value)."""
        token = environ.get(self.token_env, "").strip()
        if not token:
            if self.token_env_explicit:
                raise ValueError(f"{SECTION} token_env: ${self.token_env} is not set")
            return None
        if not valid_token(token):
            raise ValueError(f"{SECTION} ${self.token_env} contains characters not allowed")
        return token

    def build(self, environ: Mapping[str, str] = os.environ) -> IssuesAdapter:
        """The adapter. Raises ValueError for a missing explicit token."""
        token = self.token(environ)
        if token is None:
            log.warning(
                "%s $%s is not set: issues are read anonymously (60 requests an hour, public "
                "repositories only)",
                SECTION,
                self.token_env,
            )
        client = GitHubClient(self.api_url, token, timeout=self.timeout, cache_ttl=self.cache_ttl)
        reader = GitHubIssues(
            client,
            self.boards,
            self.lookback,
            max_requests=self.max_requests_per_tick,
            tick_budget=self.tick_budget,
        )
        return IssuesAdapter(reader, self.boards, self.agents, name=NAME, where=self.api_url)


def _string(value: Any, where: str, limit: int = 200) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"{where} must be a non-empty string of at most {limit} characters")
    return value.strip()


def _label(value: Any, where: str) -> str:
    label = _string(value, where, MAX_LABEL)
    if "," in label:  # GitHub's labels= filter splits on commas
        raise ValueError(f"{where}: a label name with a comma cannot be filtered on")
    return label


def _labels(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list of label names")
    return tuple(_label(v, where) for v in value)


def _duration(section: Mapping[str, Any], key: str, default: timedelta) -> timedelta:
    if key not in section:
        return default
    try:
        value = parse_duration(section[key])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{SECTION} {key}: {exc}") from exc
    if value <= timedelta(0):
        raise ValueError(f"{SECTION} {key} must be positive")
    return value


def _agent(name: str, section: Any) -> Agent:
    where = f"{SECTION} agents.{name}"
    if not _NAME.fullmatch(name):
        raise ValueError(f"{where}: an agent name is 1-64 letters, digits, '.', '_' or '-'")
    if not isinstance(section, Mapping):
        raise ValueError(f"{where} must be a table")
    if extra := set(section) - AGENT_KEYS:
        raise ValueError(f"unknown {where} keys: {sorted(extra)}")
    fields: dict[str, str] = {}
    for key in ("assignee", "author"):
        if key in section:
            value = _string(section[key], f"{where} {key}", 60)
            if not _LOGIN.fullmatch(value.removeprefix("@")):
                raise ValueError(f"{where} {key} must be a GitHub login")
            fields[key] = value.removeprefix("@")
    if "label" in section:
        fields["label"] = _label(section["label"], f"{where} label")
    if "mention" in section:
        fields["mention"] = _string(section["mention"], f"{where} mention")
    if not {"assignee", "label", "author"} & set(fields):
        raise ValueError(
            f"{where} needs assignee, label or author: how Kelpie sees the agent on an issue"
        )
    role = _string(section.get("role", DEFAULT_ROLE), f"{where} role", 64)
    return Agent(name=name, role=role, **fields)


def _board(
    repo: str, section: Mapping[str, Any], defaults: Board, agents: Mapping[str, Agent]
) -> Board:
    where = f"{SECTION} boards.{repo!r}"
    if extra := set(section) - BOARD_KEYS:
        raise ValueError(f"unknown {where} keys: {sorted(extra)}")
    label = defaults.label
    if "label" in section:
        label = None if section["label"] is False else _label(section["label"], where)
    names = defaults.agents
    if "agents" in section:
        value = section["agents"]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{where} agents must be a list of agent names")
        if unknown := [v for v in value if v not in agents]:
            raise ValueError(f"{where} agents: unknown agents {unknown}")
        names = tuple(value)
    return Board(
        key=repo,
        label=label,
        blocked_labels=_labels(section["blocked_labels"], f"{where} blocked_labels")
        if "blocked_labels" in section
        else defaults.blocked_labels,
        ready_labels=_labels(section["ready_labels"], f"{where} ready_labels")
        if "ready_labels" in section
        else defaults.ready_labels,
        role=_string(section["role"], f"{where} role", 64) if "role" in section else defaults.role,
        agents=names,
    )


def _check_agents(agents: Mapping[str, Agent], boards: Sequence[Board]) -> None:
    """Each account and label names one agent, and no agent's label means something else."""
    seen: dict[str, str] = {}
    for agent in agents.values():
        for kind, value in (("assignee", agent.assignee), ("author", agent.author)):
            if value is None:
                continue
            key = f"login {login_key(value)}"
            if seen.setdefault(key, agent.name) != agent.name:
                raise ValueError(
                    f"{SECTION} agents.{agent.name} {kind}: {value} is already agents.{seen[key]}'s"
                )
        if agent.label is None:
            continue
        key = f"label {agent.label.casefold()}"
        if seen.setdefault(key, agent.name) != agent.name:
            raise ValueError(
                f"{SECTION} agents.{agent.name} label {agent.label!r} is already agents."
                f"{seen[key]}'s"
            )
        for board in boards:
            other = {board.label or "", *board.blocked_labels, *board.ready_labels}
            if agent.label.casefold() in {o.casefold() for o in other}:
                raise ValueError(
                    f"{SECTION} agents.{agent.name} label {agent.label!r} is also a board "
                    f"label of {board.key} (label, blocked_labels or ready_labels)"
                )


def load_github_issues(section: Any, github: GitHubSettings | None) -> GitHubIssuesSettings:
    """Parse the section (``github``: ``[connectors.github]``, for the defaults); raises
    ValueError with a message naming the bad key."""
    if not isinstance(section, Mapping):
        raise ValueError(f"{SECTION} must be a table")
    if extra := set(section) - KEYS:
        raise ValueError(f"unknown {SECTION} keys: {sorted(extra)}")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{SECTION} enabled must be true or false")
    repos_value = section.get("repos", list(github.repos) if github is not None else None)
    if repos_value is None:
        raise ValueError(f"{SECTION} repos is required (or set [connectors.github] repos)")
    if not isinstance(repos_value, list) or not repos_value:
        raise ValueError(f"{SECTION} repos must be a non-empty list of owner/name")
    repos: list[str] = []
    for repo in repos_value:
        if not valid_repo(repo):
            raise ValueError(f"{SECTION} repos: {repo!r} is not owner/name")
        if repo.lower() not in repos:
            repos.append(repo.lower())
    api_url = section.get("api_url", github.api_url if github is not None else DEFAULT_API_URL)
    try:
        api_url = check_api_url(api_url)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{SECTION} api_url: {exc}") from exc
    explicit = "token_env" in section
    token_env = section.get("token_env", github.token_env if github is not None else None)
    token_env = token_env or DEFAULT_TOKEN_ENV
    if not isinstance(token_env, str) or not _ENV_NAME.fullmatch(token_env):
        raise ValueError(f"{SECTION} token_env must be an environment variable name")
    # A token meant for one GitHub must not go to another host: the inherited token belongs
    # to the connector's api_url (or github.com).
    inherited_for = github.api_url if github is not None else DEFAULT_API_URL
    if api_url != inherited_for and not explicit:
        raise ValueError(
            f"{SECTION} token_env is required with an api_url other than the one its token "
            f"is for ({inherited_for})"
        )
    explicit = explicit or (github is not None and github.token_env_explicit)
    base_timeout = github.timeout if github is not None else DEFAULT_TIMEOUT
    timeout = _duration(section, "timeout", base_timeout)
    if timeout > MAX_TIMEOUT:
        raise ValueError(f"{SECTION} timeout must be at most {MAX_TIMEOUT.seconds}s")
    cache_ttl = _duration(
        section, "cache_ttl", github.cache_ttl if github is not None else DEFAULT_CACHE_TTL
    )
    if cache_ttl < MIN_CACHE_TTL:
        raise ValueError(f"{SECTION} cache_ttl must be at least {MIN_CACHE_TTL.seconds}s")
    max_requests = section.get("max_requests_per_tick", DEFAULT_MAX_REQUESTS)
    if (
        not isinstance(max_requests, int)
        or isinstance(max_requests, bool)
        or not 1 <= max_requests <= MAX_REQUESTS
    ):
        raise ValueError(f"{SECTION} max_requests_per_tick must be 1 to {MAX_REQUESTS}")
    agents_section = section.get("agents", {})
    if not isinstance(agents_section, Mapping) or len(agents_section) > MAX_AGENTS:
        raise ValueError(f"{SECTION} agents must be a table of at most {MAX_AGENTS} agents")
    agents = {name: _agent(name, value) for name, value in agents_section.items()}
    label = section.get("label")
    defaults = Board(
        key="",
        label=None if label is None else _label(label, f"{SECTION} label"),
        blocked_labels=_labels(section["blocked_labels"], f"{SECTION} blocked_labels")
        if "blocked_labels" in section
        else DEFAULT_BLOCKED_LABELS,
        ready_labels=_labels(section["ready_labels"], f"{SECTION} ready_labels")
        if "ready_labels" in section
        else (),
        role=_string(section.get("role", DEFAULT_ROLE), f"{SECTION} role", 64),
    )
    boards_section = section.get("boards", {})
    if not isinstance(boards_section, Mapping):
        raise ValueError(f"{SECTION} boards must be a table of repositories")
    overrides: dict[str, Mapping[str, Any]] = {}
    for repo, value in boards_section.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"{SECTION} boards.{repo!r} must be a table")
        if repo.lower() not in repos:
            raise ValueError(f"{SECTION} boards.{repo!r} is not in repos")
        overrides[repo.lower()] = value
    boards = tuple(_board(r, overrides.get(r, {}), defaults, agents) for r in repos)
    _check_agents(agents, boards)
    return GitHubIssuesSettings(
        boards=boards,
        agents=agents,
        api_url=api_url,
        token_env=token_env,
        token_env_explicit=explicit,
        timeout=timeout,
        cache_ttl=cache_ttl,
        lookback=_duration(section, "lookback", DEFAULT_LOOKBACK),
        max_requests_per_tick=max_requests,
        tick_budget=_duration(section, "tick_budget", DEFAULT_TICK_BUDGET),
        enabled=enabled,
    )
