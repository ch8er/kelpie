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
from kelpie.adapters.issues.linear import DEFAULT_API_URL as LINEAR_API_URL
from kelpie.adapters.issues.linear import DEFAULT_MAX_REQUESTS as LINEAR_MAX_REQUESTS
from kelpie.adapters.issues.linear import DEFAULT_REFRESH as LINEAR_REFRESH
from kelpie.adapters.issues.linear import LinearClient, LinearIssues
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
MIN_LINEAR_REFRESH = timedelta(seconds=30)
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


def _duration(
    section: Mapping[str, Any], key: str, default: timedelta, sect: str = SECTION
) -> timedelta:
    if key not in section:
        return default
    try:
        value = parse_duration(section[key])
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{sect} {key}: {exc}") from exc
    if value <= timedelta(0):
        raise ValueError(f"{sect} {key} must be positive")
    return value


def _agent(
    name: str,
    section: Any,
    sect: str = SECTION,
    account: re.Pattern[str] = _LOGIN,
    account_kind: str = "a GitHub login",
) -> Agent:
    where = f"{sect} agents.{name}"
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
            if not account.fullmatch(value.removeprefix("@")):
                raise ValueError(f"{where} {key} must be {account_kind}")
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
    repo: str,
    section: Mapping[str, Any],
    defaults: Board,
    agents: Mapping[str, Agent],
    sect: str = SECTION,
    keys: frozenset[str] = BOARD_KEYS,
) -> Board:
    where = f"{sect} boards.{repo!r}"
    if extra := set(section) - keys:
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
        ready_states=_states(section["ready_states"], f"{where} ready_states")
        if "ready_states" in section
        else defaults.ready_states,
        role=_string(section["role"], f"{where} role", 64) if "role" in section else defaults.role,
        agents=names,
    )


def _states(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError(f"{where} must be a list of workflow state names")
    return tuple(_string(v, where, 64) for v in value)


def _check_agents(
    agents: Mapping[str, Agent], boards: Sequence[Board], sect: str = SECTION
) -> None:
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
                f"{sect} agents.{agent.name} label {agent.label!r} is already agents.{seen[key]}'s"
            )
        for board in boards:
            other = {board.label or "", *board.blocked_labels, *board.ready_labels}
            if agent.label.casefold() in {o.casefold() for o in other}:
                raise ValueError(
                    f"{sect} agents.{agent.name} label {agent.label!r} is also a board "
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


# --- [adapters.linear] ----------------------------------------------------------------------

LINEAR_SECTION = "[adapters.linear]"
LINEAR_NAME = "linear"
LINEAR_TOKEN_ENV = "LINEAR_API_KEY"
LINEAR_KEYS = frozenset(
    {
        *("enabled", "teams", "api_url", "token_env", "timeout", "refresh"),
        *("lookback", "max_requests_per_tick", "pull_repos"),
        *("label", "blocked_labels", "ready_labels", "ready_states", "role", "agents", "boards"),
    }
)
LINEAR_BOARD_KEYS = BOARD_KEYS | {"ready_states"}
_TEAM = re.compile(r"[A-Z][A-Z0-9_]{0,15}")
_PULL_REPO = re.compile(r"(?:(?P<host>[a-z0-9.-]{1,255})/)?(?P<repo>[\w.-]{1,100}/[\w.-]{1,100})")
MAX_PULL_REPOS = 100
_DISPLAY_NAME = re.compile(r"[^\x00-\x1f\x7f@,]{1,64}")


@dataclass(frozen=True, slots=True)
class LinearSettings:
    boards: tuple[Board, ...]
    agents: Mapping[str, Agent] = field(default_factory=dict)
    api_url: str = LINEAR_API_URL
    token_env: str = LINEAR_TOKEN_ENV
    timeout: timedelta = DEFAULT_TIMEOUT
    refresh: timedelta = LINEAR_REFRESH
    lookback: timedelta = DEFAULT_LOOKBACK
    max_requests_per_tick: int = LINEAR_MAX_REQUESTS
    pull_repos: frozenset[str] = frozenset()  # "host/owner/repo"; empty: github.com, any repo
    enabled: bool = True

    def token(self, environ: Mapping[str, str] = os.environ) -> str:
        """The API key from ``$token_env`` (Linear has no anonymous access). Raises
        ValueError, never showing the value."""
        token = environ.get(self.token_env, "").strip()
        if not token:
            raise ValueError(f"{LINEAR_SECTION} token_env: ${self.token_env} is not set")
        if not valid_token(token):
            raise ValueError(f"{LINEAR_SECTION} ${self.token_env} contains characters not allowed")
        return token

    def build(self, environ: Mapping[str, str] = os.environ) -> IssuesAdapter:
        client = LinearClient(self.api_url, self.token(environ), timeout=self.timeout)
        reader = LinearIssues(
            client,
            self.boards,
            self.lookback,
            refresh=self.refresh,
            max_requests=self.max_requests_per_tick,
            pull_repos=self.pull_repos,
            agents=self.agents,
        )
        return IssuesAdapter(reader, self.boards, self.agents, name=LINEAR_NAME, where=self.api_url)


def load_linear(section: Any) -> LinearSettings:
    """Parse ``[adapters.linear]``; raises ValueError naming the bad key."""
    sect = LINEAR_SECTION
    if not isinstance(section, Mapping):
        raise ValueError(f"{sect} must be a table")
    if extra := set(section) - LINEAR_KEYS:
        raise ValueError(f"unknown {sect} keys: {sorted(extra)}")
    enabled = section.get("enabled", True)
    if not isinstance(enabled, bool):
        raise ValueError(f"{sect} enabled must be true or false")
    teams_value = section.get("teams")
    if not isinstance(teams_value, list) or not teams_value:
        raise ValueError(f"{sect} teams must be a non-empty list of team keys (ENG)")
    teams: list[str] = []
    for team in teams_value:
        if not isinstance(team, str) or not _TEAM.fullmatch(team.strip().upper()):
            raise ValueError(f"{sect} teams: {team!r} is not a team key")
        if team.strip().upper() not in teams:
            teams.append(team.strip().upper())
    try:
        api_url = check_api_url(section.get("api_url", LINEAR_API_URL))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"{sect} api_url: {exc}") from exc
    token_env = section.get("token_env", LINEAR_TOKEN_ENV)
    if not isinstance(token_env, str) or not _ENV_NAME.fullmatch(token_env):
        raise ValueError(f"{sect} token_env must be an environment variable name")
    if api_url != LINEAR_API_URL and "token_env" not in section:
        raise ValueError(f"{sect} token_env is required with a non-default api_url")
    timeout = _duration(section, "timeout", DEFAULT_TIMEOUT, sect)
    if timeout > MAX_TIMEOUT:
        raise ValueError(f"{sect} timeout must be at most {MAX_TIMEOUT.seconds}s")
    refresh = _duration(section, "refresh", LINEAR_REFRESH, sect)
    if refresh < MIN_LINEAR_REFRESH:
        raise ValueError(f"{sect} refresh must be at least {MIN_LINEAR_REFRESH.seconds}s")
    max_requests = section.get("max_requests_per_tick", LINEAR_MAX_REQUESTS)
    if (
        not isinstance(max_requests, int)
        or isinstance(max_requests, bool)
        or not 0 <= max_requests <= MAX_REQUESTS
    ):
        raise ValueError(f"{sect} max_requests_per_tick must be 0 to {MAX_REQUESTS}")
    agents_section = section.get("agents", {})
    if not isinstance(agents_section, Mapping) or len(agents_section) > MAX_AGENTS:
        raise ValueError(f"{sect} agents must be a table of at most {MAX_AGENTS} agents")
    agents = {
        name: _agent(name, value, sect, _DISPLAY_NAME, "a Linear display name")
        for name, value in agents_section.items()
    }
    for agent in agents.values():
        if agent.author is not None:
            raise ValueError(
                f"{sect} agents.{agent.name} author: Linear shows no pull request author; "
                "name the agent's assignee or label"
            )
        # Names Kelpie never takes from Linear (they could pass for another's): refuse them
        # here rather than have the agent silently never match.
        name = agent.assignee or ""
        if not name.isprintable() or name.casefold().endswith("[bot]"):
            raise ValueError(
                f"{sect} agents.{agent.name} assignee: a Linear display name, printable and "
                "not ending in [bot]"
            )
    label = section.get("label")
    defaults = Board(
        key="",
        label=None if label is None else _label(label, f"{sect} label"),
        blocked_labels=_labels(section["blocked_labels"], f"{sect} blocked_labels")
        if "blocked_labels" in section
        else DEFAULT_BLOCKED_LABELS,
        ready_labels=_labels(section["ready_labels"], f"{sect} ready_labels")
        if "ready_labels" in section
        else (),
        ready_states=_states(section["ready_states"], f"{sect} ready_states")
        if "ready_states" in section
        else (),
        role=_string(section.get("role", DEFAULT_ROLE), f"{sect} role", 64),
    )
    boards_section = section.get("boards", {})
    if not isinstance(boards_section, Mapping):
        raise ValueError(f"{sect} boards must be a table of team keys")
    overrides: dict[str, Mapping[str, Any]] = {}
    for team, value in boards_section.items():
        if not isinstance(value, Mapping):
            raise ValueError(f"{sect} boards.{team!r} must be a table")
        if team.upper() not in teams:
            raise ValueError(f"{sect} boards.{team!r} is not in teams")
        overrides[team.upper()] = value
    boards = tuple(
        _board(t, overrides.get(t, {}), defaults, agents, sect, LINEAR_BOARD_KEYS) for t in teams
    )
    _check_agents(agents, boards, sect)
    return LinearSettings(
        boards=boards,
        agents=agents,
        api_url=api_url,
        token_env=token_env,
        timeout=timeout,
        refresh=refresh,
        lookback=_duration(section, "lookback", DEFAULT_LOOKBACK, sect),
        max_requests_per_tick=max_requests,
        pull_repos=_pull_repos(section.get("pull_repos", []), f"{sect} pull_repos"),
        enabled=enabled,
    )


def _pull_repos(value: Any, where: str) -> frozenset[str]:
    """Repositories as ``owner/repo`` (on github.com) or ``host/owner/repo``."""
    if not isinstance(value, list) or len(value) > MAX_PULL_REPOS:
        raise ValueError(f"{where} must be a list of at most {MAX_PULL_REPOS} repositories")
    repos: set[str] = set()
    for entry in value:
        match = _PULL_REPO.fullmatch(entry.strip().lower()) if isinstance(entry, str) else None
        if match is None:
            raise ValueError(f"{where}: {entry!r} is not owner/repo or host/owner/repo")
        repos.add(f"{match['host'] or 'github.com'}/{match['repo']}")
    return frozenset(repos)
