"""[view]: the read-only farm view listener and ``kelpie status`` reading it."""

from __future__ import annotations

import http.client
import http.server
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kelpie.app import ConfigError, Settings, check_exposure
from kelpie.core.farmview import from_dict
from kelpie.core.model import Snapshot, Status, Task
from kelpie.doctor import check_view
from kelpie.view.cli import fetch_view, plain_http_warning, run, server_url
from kelpie.view.config import ViewSettings, load_view
from kelpie.view.service import PATH, ViewServer, ViewSource
from kelpie.view.text import render

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
TOKEN = "0123456789abcdef0123456789abcdef"


def test_defaults_are_off_and_on_loopback() -> None:
    view = load_view(None)
    assert view == ViewSettings() and not view.enabled
    assert view.host == "127.0.0.1" and view.port == 9466 and view.loopback


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


@pytest.mark.parametrize(
    ("host", "code"),
    [
        ("attacker.example:9465", 421),  # a page that rebound its name to 127.0.0.1
        ("attacker.example", 421),
        ("127.0.0.1.attacker.example", 421),
        ("localhost:9465", 200),
        ("LOCALHOST", 200),
        ("127.0.0.1", 200),
        ("[::1]:9465", 200),
    ],
)
def test_on_loopback_only_a_loopback_host_is_served(
    listener: ViewServer, host: str, code: int
) -> None:
    """DNS rebinding: a web page the operator opens cannot read the farm (#395)."""
    assert _request(listener, "GET", PATH, {"Host": host})[0] == code


def test_a_token_with_blanks_around_it_still_matches() -> None:
    settings = ViewSettings(enabled=True)
    assert settings.token({settings.token_env: f" {TOKEN}\r\n"}) == TOKEN
    assert settings.token({settings.token_env: "  "}) is None


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
    assert server_url(Settings(view=ViewSettings(host="::1")), "") == f"http://[::1]:9466{PATH}"


@pytest.mark.parametrize("method", ["HEAD", "OPTIONS", "TRACE"])
def test_other_methods_are_refused_too(listener: ViewServer, method: str) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", listener.server_address[1], timeout=5)
    try:
        conn.request(method, PATH)
        assert conn.getresponse().status == 405
    finally:
        conn.close()


def test_the_token_never_follows_a_redirect() -> None:
    seen: list[str | None] = []

    class Target(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    target = http.server.HTTPServer(("127.0.0.1", 0), Target)

    class Redirect(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/x")
            self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    redirect = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    servers = [target, redirect]
    for s in servers:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    try:
        with pytest.raises(OSError, match="redirected"):
            fetch_view(f"http://127.0.0.1:{redirect.server_address[1]}{PATH}", TOKEN)
        assert seen == []
    finally:
        for s in servers:
            s.shutdown()
            s.server_close()


def test_plain_http_off_loopback_warns() -> None:
    assert plain_http_warning("http://host-a:9465/v1/farm", TOKEN)
    assert plain_http_warning("https://host-a:9465/v1/farm", TOKEN) is None
    assert plain_http_warning("http://127.0.0.1:9465/v1/farm", TOKEN) is None
    assert plain_http_warning("http://host-a:9465/v1/farm", None) is None


def test_escapes_from_a_server_never_reach_the_terminal() -> None:
    evil = "\x1b[31mX\x07"
    data = {
        "schema_version": 1,
        "now": NOW.isoformat(),
        "source": evil,
        "boards": [evil],
        "statuses": [{"status": evil, "count": 1, "board": evil}],
        "waiting_total": 1,
        "waiting": [
            {
                "task_id": evil,
                "condition_id": evil,
                "kind": evil,
                "satisfied": None,
                "target": evil,
                "task_status": evil,
                "board": evil,
            }
        ],
        "alerts_total": 1,
        "alerts": [
            {
                "id": evil,
                "rule": evil,
                "severity": evil,
                "since": NOW.isoformat(),
                "opened_at": NOW.isoformat(),
                "summary": evil,
                "mode": evil,
                "task_id": evil,
            }
        ],
        "rules": [
            {
                "rule": evil,
                "actions": [
                    {
                        "at": NOW.isoformat(),
                        "kind": evil,
                        "mode": evil,
                        "outcome": evil,
                        "reason": evil,
                        "task_id": evil,
                    }
                ],
            }
        ],
        "problems": [evil],
    }
    out = render(from_dict(data))
    assert "\x1b" not in out and "\x07" not in out


def test_doctor_warns_about_an_open_view() -> None:
    off = Settings(view=ViewSettings(enabled=True, host="0.0.0.0"))
    assert check_view(off, {}).status == "WARN"
    assert check_view(off, {"KELPIE_VIEW_TOKEN": TOKEN}).status == "PASS"
    assert check_view(Settings(view=ViewSettings(enabled=True)), {}).status == "PASS"
