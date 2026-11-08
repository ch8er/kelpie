"""The read-only farm page (#299): attention order, per board, today's figures, escaping,
no write endpoint, and the demo farm rendered end to end."""

from __future__ import annotations

import base64
import http.client
import io
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.core.actions import Severity
from kelpie.core.farmview import (
    Attention,
    Today,
    attention_items,
    build_view,
    from_dict,
    to_dict,
)
from kelpie.core.model import Block, Owner, OwnerKind, Snapshot, Status, Task
from kelpie.core.states import OpenState
from kelpie.view.config import ViewSettings, load_view
from kelpie.view.page import render_page
from kelpie.view.service import MANIFEST_PATH, PAGE, PATH, ViewServer, ViewService, ViewSource

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
TOKEN = "0123456789abcdef0123456789abcdef"
TODAY = Today(NOW, "2026-03-02", 3.5, 12.25, 40.0, 1_000_000, 0)


def task(tid: str, status: Status, hours: float = 1, **kw: object) -> Task:
    return Task(tid, f"title {tid}", status, NOW - timedelta(hours=hours), **kw)  # type: ignore[arg-type]


def state(task_id: str, rule: str = "stale_blocks", summary: str = "alert") -> OpenState:
    return OpenState(
        f"{rule}:{task_id}", rule, NOW, NOW, NOW, 0, Severity.WARNING, summary, task_id
    )


FARM = Snapshot(
    now=NOW,
    owners=(Owner("sam", "owner", OwnerKind.HUMAN), Owner("dev-1", "dev")),
    tasks=(
        task("T-rest", Status.READY, 9),
        task("T-long", Status.IN_PROGRESS, 3, owner_id="dev-1"),
        task("T-short", Status.IN_PROGRESS, 0.5, owner_id="dev-1"),
        task("T-blocked", Status.BLOCKED, 2, block=Block("waiting for CI", NOW)),
        task("T-alerted", Status.REVIEW, 1),
        task("T-human", Status.READY, 4, owner_id="sam"),
        task("T-ask", Status.WAITING, 1),
        task("T-done", Status.DONE, 1),
    ),
)
STATES = [state("T-alerted"), state("T-ask", "needs_you", "waits for sam")]


def test_attention_order() -> None:
    items = attention_items(FARM, STATES)
    order = [(i.task_id, i.attention) for i in items]
    assert order == [
        ("T-human", "needs_you"),  # oldest first within a group
        ("T-ask", "needs_you"),
        ("T-blocked", "stuck"),
        ("T-alerted", "stuck"),
        ("T-long", "running_long"),
        ("T-rest", "rest"),
        ("T-short", "rest"),
    ]
    by_id = {i.task_id: i for i in items}
    assert by_id["T-blocked"].why == "waiting for CI"
    assert by_id["T-ask"].why == "waits for sam"
    assert "T-done" not in by_id  # finished work is not on the page


def test_long_after_is_configurable() -> None:
    items = attention_items(FARM, (), long_after=timedelta(minutes=10))
    assert {i.task_id for i in items if i.attention == Attention.RUNNING_LONG} == {
        "T-long",
        "T-short",
    }
    assert load_view({"long_after": "30m"}).long_after == timedelta(minutes=30)
    for bad in ("0s", "soon", True):
        with pytest.raises(ValueError):
            load_view({"long_after": bad})


def test_a_healthy_farm_has_nothing_that_needs_you() -> None:
    calm = Snapshot(now=NOW, tasks=(task("T-1", Status.IN_PROGRESS, 0.2),))
    assert [i.attention for i in attention_items(calm)] == ["rest"]
    page = render_page(build_view(NOW, calm))
    assert "Needs you" not in page and "Stuck or blocked" not in page


def test_wire_form_carries_items_and_today() -> None:
    view = build_view(NOW, FARM, STATES, today=TODAY)
    again = from_dict(json.loads(json.dumps(to_dict(view))))
    assert again == view and again.today == TODAY and again.items_total == 7


def test_page_per_board_with_today_and_escaped_text() -> None:
    snapshot = Snapshot(
        now=NOW,
        tasks=(
            Task("a:T-1", "<script>alert(1)</script>", Status.BLOCKED, NOW, board="a"),
            Task("b:T-2", "fine & dandy", Status.READY, NOW, board="b"),
        ),
    )
    page = render_page(build_view(NOW, snapshot, today=TODAY))
    assert "<script>" not in page and "&lt;script&gt;" in page
    assert "fine &amp; dandy" in page
    assert page.index("Board a") < page.index("a:T-1") < page.index("Board b")
    assert "$12.25" in page and "3.5 h" in page and "$40.00" in page
    assert "<form" not in page and "<button" not in page and "<script" not in page


def test_page_without_today_says_so() -> None:
    assert "not computed yet" in render_page(build_view(NOW, FARM))


# --- the listener --------------------------------------------------------------------------


@pytest.fixture
def listener() -> Iterator[ViewServer]:
    source = ViewSource(lambda: FARM, lambda: NOW, None, today=lambda s, now: TODAY)
    source.refresh_today()
    server = ViewServer(ViewSettings(enabled=True, port=0), source, TOKEN)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _get(server: ViewServer, path: str, auth: str | None = None) -> http.client.HTTPResponse:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request("GET", path, headers={"Authorization": auth} if auth else {})
    reply = conn.getresponse()
    reply.body = reply.read()  # type: ignore[attr-defined]
    conn.close()
    return reply


def _basic(password: str) -> str:
    return "Basic " + base64.b64encode(f"me:{password}".encode()).decode()


def test_the_page_needs_the_token_and_a_browser_can_give_it(listener: ViewServer) -> None:
    refused = _get(listener, PAGE)
    assert refused.status == 401 and "Basic" in (refused.getheader("WWW-Authenticate") or "")
    assert _get(listener, PAGE, _basic("wrong")).status == 401
    assert _get(listener, PAGE, "Basic !!!").status == 401
    page = _get(listener, PAGE, _basic(TOKEN))
    assert page.status == 200 and page.getheader("Content-Type", "").startswith("text/html")
    assert "default-src 'none'" in (page.getheader("Content-Security-Policy") or "")
    body = page.body.decode()  # type: ignore[attr-defined]
    assert "T-human" in body and "$12.25" in body
    assert _get(listener, PATH, f"Bearer {TOKEN}").status == 200
    manifest = _get(listener, MANIFEST_PATH, _basic(TOKEN))
    assert manifest.status == 200 and json.loads(manifest.body)["start_url"] == "/"  # type: ignore[attr-defined]


def test_failed_figures_keep_the_page_up() -> None:
    def boom(snapshot: Snapshot, now: datetime) -> Today:
        raise RuntimeError("database locked")

    source = ViewSource(lambda: FARM, lambda: NOW, None, today=boom, cache_seconds=0)
    source.refresh_today()
    view = source.view()
    assert view.today is None and any("could not be computed" in p for p in view.problems)
    assert source.settled


def test_the_service_computes_today_on_its_own_thread() -> None:
    calls: list[datetime] = []

    def figures(snapshot: Snapshot, now: datetime) -> Today:
        calls.append(now)
        return TODAY

    source = ViewSource(lambda: FARM, lambda: NOW, None, today=figures, cache_seconds=0)
    service = ViewService(ViewServer(ViewSettings(enabled=True, port=0), source, None), 60)
    service.start()
    try:
        for _ in range(100):
            if calls:
                break
            threading.Event().wait(0.02)
        assert source.view().today == TODAY
    finally:
        service.stop()


# --- the demo farm ---------------------------------------------------------------------------


def test_the_demo_farm_renders(tmp_path: Path) -> None:
    from kelpie.demo.farm import DemoFarm
    from kelpie.demo.narrate import Narrator
    from kelpie.demo.story import DAY, Story
    from kelpie.report.today import today_figures

    farm = DemoFarm(DAY, tmp_path / "farm")
    try:
        Story(farm, Narrator(io.StringIO(), pace=0)).run()
        app = farm.app
        assert app is not None
        snapshot = app.loop.snapshot_source()
        source = ViewSource(
            lambda: snapshot,
            lambda: farm.now,
            app.settings.db_path,
            today=today_figures(app.settings),
        )
        source.refresh_today()
        view = source.view()
    finally:
        farm.close()
    assert view.today is not None and view.today.chaos_hours > 0
    assert view.alerts_total > 0 and view.items
    page = render_page(view)
    assert "Kelpie farm view" in page and "t_store" in page
    assert page.index("Needs you") < page.index("The rest")
    assert "<form" not in page and "<script" not in page


def test_backlog_of_a_person_and_info_alerts_do_not_jump_the_queue() -> None:
    snapshot = Snapshot(
        now=NOW,
        owners=(Owner("sam", "owner", OwnerKind.HUMAN),),
        tasks=(task("T-later", Status.BACKLOG, owner_id="sam"), task("T-info", Status.READY)),
    )
    info = OpenState("i", "no_progress", NOW, NOW, NOW, 0, Severity.INFO, "fyi", "T-info")
    assert {i.attention for i in attention_items(snapshot, [info])} == {"rest"}


def test_one_challenge_per_reply(listener: ViewServer) -> None:
    assert _get(listener, PAGE).getheader("WWW-Authenticate", "").startswith("Basic realm=")
    assert _get(listener, MANIFEST_PATH).getheader("WWW-Authenticate", "").startswith("Basic")
    assert _get(listener, PATH).getheader("WWW-Authenticate") == "Bearer"
    assert 'crossorigin="use-credentials"' in render_page(build_view(NOW, FARM))


@pytest.mark.parametrize(
    "auth",
    [
        "Basic " + base64.b64encode(TOKEN.encode()).decode(),  # no colon: no password
        "Basic " + base64.b64encode(b"me:").decode(),
        "Bearer ",
        "Bearer",
        "Token " + TOKEN,
    ],
)
def test_malformed_credentials_are_refused(listener: ViewServer, auth: str) -> None:
    assert _get(listener, PAGE, auth).status == 401


@pytest.mark.parametrize("method", ["POST", "HEAD", "PUT", "DELETE"])
def test_the_page_takes_no_writes(listener: ViewServer, method: str) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", listener.server_address[1], timeout=5)
    try:
        conn.request(method, PAGE, headers={"Authorization": _basic(TOKEN)})
        assert conn.getresponse().status == 405
    finally:
        conn.close()


def test_page_headers(listener: ViewServer) -> None:
    page = _get(listener, PAGE, _basic(TOKEN))
    csp = page.getheader("Content-Security-Policy") or ""
    assert "default-src 'none'" in csp and "frame-ancestors 'none'" in csp
    assert "script-src" not in csp
    assert page.getheader("X-Frame-Options") == "DENY"
    assert page.getheader("Referrer-Policy") == "no-referrer"
    assert page.getheader("Cache-Control") == "no-store"
