"""``[view]``: the read-only farm view listener of ``kelpie serve``. Off by default.

It listens on its own address (``host``, ``port``; 127.0.0.1:9465 by default), apart from
the main port, so OTLP ingest can be opened to agents while the view stays on loopback. Off
loopback it needs a token (``token_env``, default ``KELPIE_VIEW_TOKEN``): ``kelpie serve``
refuses to start without a strong one, and ``kelpie doctor`` warns. ``long_after``: a
task in progress this long is shown as "running long" on the page.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.farmview import LONG_AFTER
from kelpie.core.serde import parse_duration
from kelpie.traces.config import is_loopback, weak_token

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9465
DEFAULT_TOKEN_ENV = "KELPIE_VIEW_TOKEN"
_KEYS = frozenset({"enabled", "host", "port", "token_env", "long_after"})


@dataclass(frozen=True, slots=True)
class ViewSettings:
    enabled: bool = False
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token_env: str = DEFAULT_TOKEN_ENV
    long_after: timedelta = LONG_AFTER

    def token(self, environ: Mapping[str, str] = os.environ) -> str | None:
        """The view token, or None when the variable is unset or empty."""
        return environ.get(self.token_env) or None

    @property
    def loopback(self) -> bool:
        return is_loopback(self.host)

    def exposure_problem(self, environ: Mapping[str, str] = os.environ) -> str | None:
        """Why this listener must not start as configured, or None."""
        if not self.enabled or self.loopback:
            return None
        token = self.token(environ)
        if token is None:
            return (
                f"[view] listens on {self.host}, off loopback, without a token: set "
                f"${self.token_env} to a random token (`openssl rand -hex 32`), or bind "
                f"[view] host to 127.0.0.1"
            )
        if (why := weak_token(token)) is not None:
            return (
                f"[view] ${self.token_env} {why}, and the view listens on {self.host}: "
                "replace it with a random token (`openssl rand -hex 32`)"
            )
        return None


def load_view(raw: Any) -> ViewSettings:
    """``[view]`` from the config; ValueError naming the key on a bad value."""
    if raw is None:
        return ViewSettings()
    if not isinstance(raw, Mapping):
        raise ValueError("[view] must be a table")
    if extra := set(raw) - _KEYS:
        raise ValueError(f"unknown [view] keys: {sorted(extra)}")
    enabled = raw.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("[view] enabled must be true or false")
    host = raw.get("host", DEFAULT_HOST)
    if not isinstance(host, str) or not host.strip():
        raise ValueError("[view] host must be a non-empty string")
    port = raw.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65535:
        raise ValueError("[view] port must be an integer from 0 to 65535")
    token_env = raw.get("token_env", DEFAULT_TOKEN_ENV)
    if not isinstance(token_env, str) or not token_env.strip():
        raise ValueError("[view] token_env must be the name of an environment variable")
    try:
        long_after = parse_duration(raw.get("long_after", LONG_AFTER))
    except (TypeError, ValueError):
        raise ValueError("[view] long_after must be a duration such as 2h") from None
    if long_after <= timedelta(0):
        raise ValueError("[view] long_after must be positive")
    return ViewSettings(enabled, host.strip(), port, token_env.strip(), long_after)
