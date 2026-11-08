"""``kelpie status``'s farm view: read here (the board offline and the database read-only)
or from a running ``kelpie serve`` (``--server``, its ``[view]`` listener).

Read-only either way: nothing is written, not even a migration. Exit codes: 0; 1 when the
server cannot be read (not running, ``[view]`` off, token refused, not a farm view).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import datetime
from urllib.parse import urlsplit

from kelpie.app import App, Settings, build_adapters, utc_now
from kelpie.core.farmview import FarmView, build_view, from_dict, to_dict
from kelpie.view.read import read_stored
from kelpie.view.service import PATH, ViewServer, ViewService, ViewSource
from kelpie.view.text import render

MAX_BODY = 8 * 1024 * 1024
TIMEOUT = 10.0


def local_view(settings: Settings, clock: Callable[[], datetime] = utc_now) -> FarmView:
    """The board read now, offline (as ``kelpie why`` reads it), and the stored history;
    with no adapter here (a hub, or nothing configured), the last observed statuses."""
    from kelpie.why import read_snapshot  # the snapshot as a tick assembles it, read-only

    snapshot = None
    problems: list[str] = []
    if build_adapters(settings):
        read = read_snapshot(settings, clock)
        snapshot = read.snapshot
        problems += read.adapter_failures + read.problems
    stored = read_stored(settings.db_path, statuses=snapshot is None)
    return build_view(
        snapshot.now if snapshot is not None else clock(),
        snapshot,
        stored.states,
        stored.entries,
        stored=stored.statuses,
        problems=problems + stored.problems,
    )


def server_url(settings: Settings, given: str) -> str:
    """``--server`` as given, else the configured ``[view]`` address (a wildcard bind is
    asked on loopback), with the view's path."""
    if not given:
        host = settings.view.host
        if host in ("0.0.0.0", "::", "[::]", ""):
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        given = f"http://{host}:{settings.view.port}"
    parts = urlsplit(given)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(
            f"--server must be an http(s) URL such as http://127.0.0.1:9465, got {given!r}"
        )
    return given.rstrip("/") + PATH if parts.path in ("", "/") else given


def fetch_view(url: str, token: str | None, timeout: float = TIMEOUT) -> FarmView:
    """The view a running server serves; OSError or ValueError with a readable message."""
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    # No proxy from the environment: the token goes to the view and nowhere else.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as reply:
            body = reply.read(MAX_BODY + 1)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise OSError(
                "the server refused the token: set the [view] token_env variable"
            ) from None
        if exc.code == 404:
            raise OSError(f"no farm view at {url}: is [view] enabled?") from None
        raise OSError(f"the server answered {exc.code}") from None
    except urllib.error.URLError as exc:
        raise OSError(f"cannot reach {url}: {exc.reason}") from None
    if len(body) > MAX_BODY:
        raise ValueError("the farm view is larger than 8 MB")
    try:
        data = json.loads(body)
    except ValueError:
        raise ValueError(f"{url} did not answer with JSON") from None
    return from_dict(data)


def run(
    settings: Settings,
    as_json: bool,
    server: str | None,
    environ: Mapping[str, str] = os.environ,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[str, str, int]:
    """The farm view as (stdout, stderr, exit code)."""
    if server is None:
        view = local_view(settings, clock)
    else:
        try:
            url = server_url(settings, server)
            view = fetch_view(url, settings.view.token(environ))
        except (OSError, ValueError) as exc:
            return "", f"kelpie: {exc}\n", 1
    if as_json:
        return json.dumps(to_dict(view), indent=2, ensure_ascii=False) + "\n", "", 0
    return render(view), "", 0


def view_service(app: App, clock: Callable[[], datetime] = utc_now) -> ViewService | None:
    """``[view]``'s listener for ``kelpie serve``, or None when it is off. OSError when the
    address cannot be bound."""
    settings = app.settings
    if not settings.view.enabled:
        return None
    source = ViewSource(
        lambda: app.loop.last_snapshot,
        clock,
        settings.db_path,
        states=app.engine.state_store,
        store=app.store,
    )
    server = ViewServer(settings.view, source, settings.view.token(), settings.server)
    return ViewService(server)
