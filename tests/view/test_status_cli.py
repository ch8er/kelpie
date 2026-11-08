"""``kelpie status`` shows the farm, read-only: the board read now plus the stored history,
and ``kelpie serve``'s [view] listener serves the same view from its last tick."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path

import pytest

from kelpie.app import build_app, find_config, load_settings
from kelpie.cli import main
from kelpie.core.farmview import from_dict
from kelpie.view.cli import view_service
from kelpie.view.service import PATH
from tests.test_smoke import NOW, TICK, farm  # noqa: F401  (the fixture)


def _tick(times: int = 2) -> None:
    clock = [NOW]
    app = build_app(load_settings(find_config(None)), clock=lambda: clock[0])
    try:
        for _ in range(times):
            app.loop.tick_once()
            clock[0] += TICK
    finally:
        app.close()


def _state(db: Path) -> tuple[int, int] | None:
    return None if not db.exists() else (os.stat(db).st_mtime_ns, os.stat(db).st_size)


def test_status_shows_the_farm(farm: Path, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    _tick()
    db = farm / "kelpie.db"
    before = _state(db)
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert "observe" in out  # the rules, as before
    assert "farm: read from the board now" in out
    assert "blocked 1" in out and "in_progress 2" in out
    assert "open alerts: 1" in out and "t_blocked" in out
    assert "last actions per rule:" in out and "exit_contract:" in out
    assert _state(db) == before  # read-only


def test_status_json_is_the_farm_view(farm: Path, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    _tick()
    assert main(["status", "--json"]) == 0
    view = from_dict(json.loads(capsys.readouterr().out))
    assert view.source == "board" and view.tasks == 3
    assert [a.task_id for a in view.alerts] == ["t_blocked"]
    assert {r.rule for r in view.rules} >= {"exit_contract"}


def test_status_before_serve_ran(farm: Path, capsys: pytest.CaptureFixture[str]) -> None:  # noqa: F811
    assert main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["tasks"] == 3 and data["alerts"] == [] and data["rules"] == []
    assert any("no database" in p for p in data["problems"])
    assert not (farm / "kelpie.db").exists()


def test_status_from_stored_statuses_without_a_board(
    farm: Path,  # noqa: F811
    capsys: pytest.CaptureFixture[str],
) -> None:
    _tick()
    (farm / "kelpie.toml").write_text("[engine]\ndry_run = true\n")  # no adapter here
    assert main(["status", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["source"] == "stored" and data["tasks"] == 3


def test_serve_view_is_off_by_default(farm: Path) -> None:  # noqa: F811
    app = build_app(load_settings(find_config(None)), clock=lambda: NOW)
    try:
        assert view_service(app) is None
    finally:
        app.close()


def test_serve_view_serves_the_last_tick(farm: Path) -> None:  # noqa: F811
    config = farm / "kelpie.toml"
    config.write_text(config.read_text() + "\n[view]\nenabled = true\nport = 0\n")
    app = build_app(load_settings(find_config(None)), clock=lambda: NOW)
    service = view_service(app, clock=lambda: NOW)
    assert service is not None
    service.start()
    try:
        port = service.server.server_address[1]

        def get() -> dict[str, object]:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", PATH)
            body = json.loads(conn.getresponse().read())
            conn.close()
            return body

        assert get()["problems"] == ["kelpie serve has not finished a tick yet"]
        app.loop.tick_once()
        service.server.source._cached = None  # past the cache interval
        view = from_dict(get())
        assert view.source == "server" and view.tasks == 3
        assert [a.task_id for a in view.alerts] == ["t_blocked"]
    finally:
        service.stop()
        app.close()
