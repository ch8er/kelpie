"""[view]: the read-only farm view listener and ``kelpie status`` reading it."""

from __future__ import annotations

import http.client
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kelpie.app import ConfigError, Settings, check_exposure
from kelpie.core.farmview import from_dict
from kelpie.core.model import Snapshot, Status, Task
from kelpie.view.cli import fetch_view, run, server_url
from kelpie.view.config import ViewSettings, load_view
from kelpie.view.service import PATH, ViewServer, ViewSource

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
TOKEN = "0123456789abcdef0123456789abcdef"


def test_defaults_are_off_and_on_loopback() -> None:
    view = load_view(None)
    assert view == ViewSettings() and not view.enabled
    assert view.host == "127.0.0.1" and view.port == 9465 and view.loopback


@pytest.mark.parametrize(
    "raw",
    [
        {"enabled": "yes"},
        {"port": 70000},
        {"port": True},
        {"host": " "},
        {"token_env": ""},
        {"listen": "0.0.0.0"},
        "on",
    ],
)
def test_bad_config_is_refused(raw: object) -> None:
    with pytest.raises(ValueError):
        load_view(raw)


def test_off_loopback_needs_a_strong_token() -> None:
    view = load_view({"enabled": True, "host": "0.0.0.0"})
    assert "without a token" in (view.exposure_problem({}) or "")
    assert "at least" in (view.exposure_problem({"KELPIE_VIEW_TOKEN": "short"}) or "")
    assert view.exposure_problem({"KELPIE_VIEW_TOKEN": TOKEN}) is None
    # Loopback, or the view off: nothing to refuse.
    assert load_view({"enabled": True}).exposure_problem({}) is None
    assert load_view({"host": "0.0.0.0"}).exposure_problem({}) is None


def test_serve_refuses_an_open_view_off_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELPIE_VIEW_TOKEN", raising=False)
    settings = Settings(view=ViewSettings(enabled=True, host="0.0.0.0"))
    with pytest.raises(ConfigError, match=r"\[view\]"):
        check_exposure(settings, "127.0.0.1")
    monkeypatch.setenv("KELPIE_VIEW_TOKEN", TOKEN)
    check_exposure(settings, "127.0.0.1")


def _source() -> ViewSource:
    snapshot = Snapshot(now=NOW, tasks=(Task("T-1", "t", Status.BLOCKED, NOW),))
    return ViewSource(lambda: snapshot, lambda: NOW, None)


@pytest.fixture
def listener(request: pytest.FixtureRequest) -> Iterator[ViewServer]:
    token = getattr(request, "param", None)
    server = ViewServer(ViewSettings(enabled=True, port=0), _source(), token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _request(
    server: ViewServer, method: str, path: str, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, object]]:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    try:
        conn.request(method, path, body=b"{}" if method != "GET" else None, headers=headers or {})
        reply = conn.getresponse()
        return reply.status, json.loads(reply.read())
    finally:
        conn.close()


def test_get_serves_the_view(listener: ViewServer) -> None:
    code, body = _request(listener, "GET", PATH)
    assert code == 200
    view = from_dict(body)
    assert view.source == "server" and [c.status for c in view.statuses] == ["blocked"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_no_write_endpoint(listener: ViewServer, method: str) -> None:
    code, _ = _request(listener, method, PATH)
    assert code == 405


def test_other_paths_are_not_found(listener: ViewServer) -> None:
    assert _request(listener, "GET", "/metrics")[0] == 404


@pytest.mark.parametrize("listener", [TOKEN], indirect=True)
def test_token_is_required_when_set(listener: ViewServer) -> None:
    assert _request(listener, "GET", PATH)[0] == 401
    assert _request(listener, "GET", PATH, {"Authorization": "Bearer wrong"})[0] == 401
    assert _request(listener, "GET", PATH, {"Authorization": f"Bearer {TOKEN}"})[0] == 200
    url = f"http://127.0.0.1:{listener.server_address[1]}{PATH}"
    assert fetch_view(url, TOKEN).tasks == 1
    with pytest.raises(OSError, match="refused the token"):
        fetch_view(url, None)


def test_status_reads_a_running_server(listener: ViewServer) -> None:
    port = listener.server_address[1]
    settings = Settings(view=ViewSettings(enabled=True, port=port))
    out, err, code = run(settings, True, "")
    assert code == 0 and not err
    assert json.loads(out)["tasks"] == 1
    out, _, code = run(settings, False, f"http://127.0.0.1:{port}")
    assert code == 0 and "blocked 1" in out


def test_status_says_when_no_server_answers(tmp_path: Path) -> None:
    settings = Settings(view=ViewSettings(port=1))
    out, err, code = run(settings, False, "")
    assert code == 1 and not out and "cannot reach" in err
    _, err, code = run(settings, False, "ftp://example.org")
    assert code == 1 and "http(s) URL" in err


def test_server_url() -> None:
    settings = Settings(view=ViewSettings(host="0.0.0.0", port=9999))
    assert server_url(settings, "") == f"http://127.0.0.1:9999{PATH}"
    assert server_url(settings, "http://host-a:1/") == f"http://host-a:1{PATH}"
    assert server_url(Settings(view=ViewSettings(host="::1")), "") == f"http://[::1]:9465{PATH}"
