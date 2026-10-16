"""``[agent]`` (for ``kelpie agent``) and ``[hub]`` (for ``kelpie serve``) configuration.

Tokens are read from environment variables, never from the file. Plain HTTP is refused
unless both sides ask for it and the addresses involved are loopback or private.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from kelpie.core.actions import ActionKind
from kelpie.core.serde import parse_duration
from kelpie.remote.protocol import (
    DEFAULT_PORT,
    MIN_TOKEN,
    PLATFORM_ACTIONS,
    valid_agent_name,
)

AGENT_KEYS = frozenset(
    {
        "name",
        "server",
        "token_env",
        "interval",
        "timeout",
        "ca_file",
        "allow_plain_http",
        "actions",
        "max_body_bytes",
    }
)
HUB_KEYS = frozenset(
    {
        "listen",
        "tls_cert",
        "tls_key",
        "allow_plain_http",
        "silent_after",
        "action_timeout",
        "max_body_bytes",
        "max_connections",
        "max_clock_skew",
        "agents",
    }
)
HUB_AGENT_KEYS = frozenset({"name", "token_env"})
DEFAULT_AGENT_TOKEN_ENV = "KELPIE_AGENT_TOKEN"
DEFAULT_MAX_BODY_BYTES = 16 * 1024 * 1024
MAX_MAX_BODY_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_CONNECTIONS = 64
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PRINTABLE = re.compile(r"[\x21-\x7e]+")
# Shared address space (RFC 6598): Tailscale and carrier-grade NAT. Not ``is_private`` in
# Python 3.12.
_SHARED = ipaddress.ip_network("100.64.0.0/10")


def private_address(host: str) -> bool:
    """A loopback or private IP literal (RFC 1918, RFC 6598, IPv6 ULA and link-local); never
    an unspecified address such as 0.0.0.0, which binds every interface."""
    try:
        ip = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    if ip.is_unspecified:
        return False
    if ip.is_loopback or ip.is_private or ip.is_link_local:
        return True
    return isinstance(ip, ipaddress.IPv4Address) and ip in _SHARED


def read_token(env: str, what: str, environ: Mapping[str, str] = os.environ) -> str:
    """The token in ``$env``; ValueError when it is missing, short or not printable ASCII."""
    token = environ.get(env, "").strip()
    if not token:
        raise ValueError(f"{what}: ${env} is not set")
    if len(token) < MIN_TOKEN or _PRINTABLE.fullmatch(token) is None:
        raise ValueError(
            f"{what}: ${env} must hold at least {MIN_TOKEN} printable ASCII characters "
            "(e.g. the output of `openssl rand -hex 32`)"
        )
    return token


@dataclass(frozen=True, slots=True)
class AgentSettings:
    """``[agent]``: this host's name on the farm and the server it reports to."""

    name: str
    server: str  # https://host:port (http only with allow_plain_http)
    token_env: str = DEFAULT_AGENT_TOKEN_ENV
    interval: timedelta = timedelta(seconds=15)  # between reports
    timeout: timedelta = timedelta(seconds=30)  # one report request
    ca_file: Path | None = None  # trust for the server certificate; None: system store
    allow_plain_http: bool = False
    actions: frozenset[ActionKind] | None = None  # None: every kind the adapters implement
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES

    @property
    def plain_http(self) -> bool:
        return urlsplit(self.server).scheme == "http"

    def token(self, environ: Mapping[str, str] = os.environ) -> str:
        return read_token(self.token_env, "[agent] token_env", environ)

    def check_server(self, resolve: Callable[[str], Iterable[str]] | None = None) -> None:
        """Refuse plain HTTP to a server that resolves to anything but loopback or private
        addresses. ValueError says why."""
        if not self.plain_http:
            return
        host = urlsplit(self.server).hostname or ""
        try:
            addresses = list((resolve or _resolve)(host))
        except OSError as exc:
            raise ValueError(f"[agent] server: cannot resolve {host}: {exc}") from None
        public = sorted({a for a in addresses if not private_address(a)})
        if not addresses or public:
            raise ValueError(
                f"[agent] server: plain http only to loopback or private addresses; {host} "
                f"resolves to {', '.join(public) or 'nothing'}"
            )


def _resolve(host: str) -> list[str]:
    return [str(info[4][0]) for info in socket.getaddrinfo(host, None)]


@dataclass(frozen=True, slots=True)
class HubAgent:
    name: str
    token_env: str


@dataclass(frozen=True, slots=True)
class HubSettings:
    """``[hub]``: where ``kelpie serve`` listens for agents, and who they are."""

    agents: tuple[HubAgent, ...]
    host: str = "0.0.0.0"
    port: int = DEFAULT_PORT
    tls_cert: Path | None = None
    tls_key: Path | None = None
    allow_plain_http: bool = False
    silent_after: timedelta = timedelta(seconds=90)
    action_timeout: timedelta = timedelta(seconds=30)
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_connections: int = DEFAULT_MAX_CONNECTIONS  # open at once; more are closed at once
    max_clock_skew: timedelta = timedelta(minutes=2)  # a report further off is not merged

    @property
    def tls(self) -> bool:
        return self.tls_cert is not None

    def tokens(self, environ: Mapping[str, str] = os.environ) -> dict[str, str]:
        """Agent name -> token. ValueError when one is missing or weak, or two agents share
        a token (the token is the identity)."""
        tokens = {
            a.name: read_token(a.token_env, f"[[hub.agents]] {a.name}", environ)
            for a in self.agents
        }
        if len(set(tokens.values())) != len(tokens):
            raise ValueError("[[hub.agents]]: two agents have the same token; give each its own")
        return tokens


def load_agent(section: Any) -> AgentSettings | None:
    """``[agent]``; None when absent. ValueError names the bad key."""
    if section is None:
        return None
    if not isinstance(section, Mapping):
        raise ValueError("[agent] must be a table")
    if extra := set(section) - AGENT_KEYS:
        raise ValueError(f"unknown [agent] keys: {sorted(extra)}")
    name = section.get("name")
    if not valid_agent_name(name):
        raise ValueError(
            "[agent] name must be 1 to 63 characters of a-z, 0-9 and '-' (it prefixes the "
            "host's task ids)"
        )
    assert isinstance(name, str)
    plain = _bool(section, "allow_plain_http", "[agent]")
    server = section.get("server")
    if not isinstance(server, str):
        raise ValueError("[agent] server is required: the URL of the kelpie serve hub")
    parts = urlsplit(server.strip())
    try:
        port = parts.port
    except ValueError:
        raise ValueError("[agent] server has an invalid port") from None
    if (
        parts.scheme not in ("https", "http")
        or not parts.hostname
        or parts.username
        or parts.password
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise ValueError("[agent] server must be https://host[:port], without a path")
    if parts.scheme == "http" and not plain:
        raise ValueError(
            "[agent] server uses plain http: set allow_plain_http = true (private networks "
            "only: the token crosses the network in clear text), or use https"
        )
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    server = f"{parts.scheme}://{host}:{port or DEFAULT_PORT}"
    ca_file = _path(section, "ca_file", "[agent]")
    if ca_file is not None and parts.scheme == "http":
        raise ValueError("[agent] ca_file only applies to an https server")
    actions = section.get("actions")
    if actions is not None:
        if not isinstance(actions, list) or not all(a in PLATFORM_ACTIONS for a in actions):
            raise ValueError(
                f"[agent] actions must be a list of platform actions among "
                f"{sorted(str(a) for a in PLATFORM_ACTIONS)}"
            )
        actions = frozenset(ActionKind(a) for a in actions)
    interval = _duration(section, "interval", timedelta(seconds=15), "[agent]")
    timeout = _duration(section, "timeout", timedelta(seconds=30), "[agent]")
    if not timedelta(seconds=1) <= interval <= timedelta(minutes=5):
        raise ValueError("[agent] interval must be between 1s and 5m")
    if not timedelta(seconds=1) <= timeout <= timedelta(minutes=5):
        raise ValueError("[agent] timeout must be between 1s and 5m")
    return AgentSettings(
        name=name,
        server=server,
        token_env=_env(section, "token_env", DEFAULT_AGENT_TOKEN_ENV, "[agent]"),
        interval=interval,
        timeout=timeout,
        ca_file=ca_file,
        allow_plain_http=plain,
        actions=actions,
        max_body_bytes=_size(section, "[agent]"),
    )


def load_hub(section: Any) -> HubSettings | None:
    """``[hub]``; None when absent. ValueError names the bad key."""
    if section is None:
        return None
    if not isinstance(section, Mapping):
        raise ValueError("[hub] must be a table")
    if extra := set(section) - HUB_KEYS:
        raise ValueError(f"unknown [hub] keys: {sorted(extra)}")
    host, port = _listen(section.get("listen", f"0.0.0.0:{DEFAULT_PORT}"))
    cert, key = _path(section, "tls_cert", "[hub]"), _path(section, "tls_key", "[hub]")
    if (cert is None) != (key is None):
        raise ValueError("[hub] tls_cert and tls_key go together")
    plain = _bool(section, "allow_plain_http", "[hub]")
    if cert is None:
        if not plain:
            raise ValueError(
                "[hub] needs tls_cert and tls_key, or allow_plain_http = true on a private network"
            )
        if not private_address(host):
            raise ValueError(
                f"[hub] plain http listens only on a loopback or private address, not {host} "
                "(the agent tokens would cross other networks in clear text); use TLS"
            )
    elif plain:
        raise ValueError("[hub] allow_plain_http and tls_cert exclude each other")
    raw = section.get("agents")
    if not isinstance(raw, list) or not raw:
        raise ValueError("[hub] needs at least one [[hub.agents]] entry")
    agents: list[HubAgent] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("[[hub.agents]] entries must be tables")
        if extra := set(item) - HUB_AGENT_KEYS:
            raise ValueError(f"unknown [[hub.agents]] keys: {sorted(extra)}")
        name = item.get("name")
        if not valid_agent_name(name):
            raise ValueError("[[hub.agents]] name must be 1 to 63 characters of a-z, 0-9 and '-'")
        assert isinstance(name, str)
        if "token_env" not in item:
            raise ValueError(f"[[hub.agents]] {name}: token_env is required")
        agents.append(HubAgent(name, _env(item, "token_env", "", f"[[hub.agents]] {name}")))
    if len({a.name for a in agents}) != len(agents):
        raise ValueError("[[hub.agents]] names must be distinct")
    if len({a.token_env for a in agents}) != len(agents):
        raise ValueError("[[hub.agents]] each agent needs its own token_env")
    silent = _duration(section, "silent_after", timedelta(seconds=90), "[hub]")
    if not timedelta(seconds=30) <= silent <= timedelta(hours=1):
        raise ValueError("[hub] silent_after must be between 30s and 1h")
    timeout = _duration(section, "action_timeout", timedelta(seconds=30), "[hub]")
    if not timedelta(seconds=1) <= timeout <= timedelta(minutes=2):
        raise ValueError("[hub] action_timeout must be between 1s and 2m")
    skew = _duration(section, "max_clock_skew", timedelta(minutes=2), "[hub]")
    if not timedelta(seconds=5) <= skew <= timedelta(hours=1):
        raise ValueError("[hub] max_clock_skew must be between 5s and 1h")
    connections = section.get("max_connections", DEFAULT_MAX_CONNECTIONS)
    if isinstance(connections, bool) or not isinstance(connections, int):
        raise ValueError("[hub] max_connections must be an integer")
    if not 2 * len(agents) + 2 <= connections <= 10_000:
        raise ValueError(
            "[hub] max_connections must be at least 2 per agent plus 2 (each agent holds a "
            "poll open and sends reports) and at most 10000"
        )
    return HubSettings(
        agents=tuple(agents),
        host=host,
        port=port,
        tls_cert=cert,
        tls_key=key,
        allow_plain_http=plain,
        silent_after=silent,
        action_timeout=timeout,
        max_body_bytes=_size(section, "[hub]"),
        max_connections=connections,
        max_clock_skew=skew,
    )


def _listen(value: Any) -> tuple[str, int]:
    if not isinstance(value, str) or ":" not in value:
        raise ValueError('[hub] listen must be "host:port" (e.g. "0.0.0.0:9465")')
    host, _, port = value.strip().rpartition(":")
    host = host.strip("[]")
    try:
        number = int(port)
    except ValueError:
        number = -1
    if not host or not 0 < number < 65536:
        raise ValueError('[hub] listen must be "host:port" (e.g. "0.0.0.0:9465")')
    return host, number


def _bool(section: Mapping[str, Any], key: str, where: str) -> bool:
    value = section.get(key, False)
    if not isinstance(value, bool):
        raise ValueError(f"{where} {key} must be true or false")
    return value


def _path(section: Mapping[str, Any], key: str, where: str) -> Path | None:
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} {key} must be a non-empty path")
    return Path(value)


def _env(section: Mapping[str, Any], key: str, default: str, where: str) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or _ENV_NAME.fullmatch(value) is None:
        raise ValueError(f"{where} {key} must name an environment variable")
    return value


def _duration(section: Mapping[str, Any], key: str, default: timedelta, where: str) -> timedelta:
    try:
        return parse_duration(section.get(key, default))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{where} {key}: {exc}") from None


def _size(section: Mapping[str, Any], where: str) -> int:
    value = section.get("max_body_bytes", DEFAULT_MAX_BODY_BYTES)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} max_body_bytes must be an integer")
    if not 64 * 1024 <= value <= MAX_MAX_BODY_BYTES:
        raise ValueError(f"{where} max_body_bytes must be between 64 KiB and 256 MiB")
    return value
