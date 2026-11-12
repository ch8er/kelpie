"""POST /conditions (#425 item 6): token with a constant-time compare, idempotent
duplicates, the main listener's Host and body guards, attach-only (nothing on the board
changes)."""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.conditions_api import (
    MAX_BODY,
    ConditionRegistry,
    ConditionSettings,
    load_conditions,
    parse,
    registration_id,
)
from kelpie.core.model import ConditionKind, Snapshot, Status, Task
from kelpie.metrics import Metrics
from kelpie.server import make_server
from tests.test_host_check import started

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
TOKEN = "synthetic-conditions-token-0123456789"
CLOCK = [NOW]
PR = {"task_id": "T-1", "kind": "pr_merged", "params": {"repo": "example/repo", "pr": 12}}


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[ConditionRegistry]:
    CLOCK[:] = [NOW]
    found = ConditionRegistry(
        ConditionSettings(enabled=True), tmp_path / "k.db", lambda: CLOCK[0], TOKEN
    )
    yield found
    found.close()


class Headers(dict[str, str]):
    def get(self, name: str, default: str | None = None) -> str | None:  # type: ignore[override]
        return next((v for k, v in self.items() if k.lower() == name.lower()), default)


def headers(body: bytes = b"{}", token: str | None = TOKEN, **extra: str) -> Headers:
    h = Headers({"Content-Type": "application/json", "Content-Length": str(len(body))})
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    h.update({k.replace("_", "-"): v for k, v in extra.items()})
    return h


def body(data: dict[str, Any]) -> bytes:
    return json.dumps(data).encode()


@pytest.mark.parametrize(
    ("h", "status"),
    [
        (headers(token=None), 401),
        (headers(token="wrong-token-wrong-token-wrong-token"), 401),
        (headers(token=""), 401),
        (Headers({"Authorization": f"Basic {TOKEN}", "Content-Length": "2"}), 401),
        (headers(Origin="https://example.invalid"), 403),
        (Headers({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}), 411),
        (headers(Content_Length="-1"), 400),
        (headers(Content_Length=str(MAX_BODY + 1)), 413),
        (headers(Content_Type="text/plain"), 415),
    ],
)
def test_headers_decide_before_the_body(
    registry: ConditionRegistry, h: Headers, status: int
) -> None:
    answer = registry.precheck(h)
    assert answer is not None and answer.status == status
    assert registry.precheck(headers()) is None


def test_a_registration_is_stored_once_and_attached_to_its_task(
    registry: ConditionRegistry,
) -> None:
    first = registry.register(body(PR))
    assert first.status == 201 and first.body["duplicate"] is False
    CLOCK[0] = NOW + timedelta(hours=1)
    again = registry.register(body({**PR, "params": {"pr": 12, "repo": "example/repo"}}))
    assert again.status == 200 and again.body == {"id": first.body["id"], "duplicate": True}
    task = Task("T-1", "t", Status.IN_PROGRESS, NOW)
    snapshot = Snapshot(now=NOW + timedelta(hours=1), tasks=(task,))
    out = registry(snapshot)
    [condition] = out.conditions
    assert (condition.kind, condition.task_id) == (ConditionKind.PR_MERGED, "T-1")
    assert condition.params == {"pr": 12, "repo": "example/repo"}
    assert condition.registered_at == NOW  # the first registration's time
    assert condition.satisfied is None  # the connectors evaluate it, not the endpoint
    assert out.tasks == snapshot.tasks  # attach-only: the board is untouched


def test_registrations_go_with_a_finished_task_or_their_age(
    registry: ConditionRegistry,
) -> None:
    registry.register(body(PR))
    registry.register(body({**PR, "task_id": "T-2"}))
    done = Task("T-1", "t", Status.DONE, NOW)
    out = registry(Snapshot(now=NOW, tasks=(done,)))
    assert [c.task_id for c in out.conditions] == ["T-2"]
    later = registry(Snapshot(now=NOW + timedelta(days=8)))
    assert later.conditions == ()


def test_at_most_max_registered_are_kept(tmp_path: Path) -> None:
    small = ConditionRegistry(
        ConditionSettings(enabled=True, max_registered=1), tmp_path / "k.db", lambda: NOW, TOKEN
    )
    try:
        assert small.register(body(PR)).status == 201
        assert small.register(body({**PR, "task_id": "T-2"})).status == 507
        assert small.register(body(PR)).status == 200  # a duplicate is still a duplicate
    finally:
        small.close()


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"task_id": "T-1", "kind": "pr_merged", "params": {}, "extra": 1},
        {"task_id": "", "kind": "pr_merged"},
        {"task_id": "T\n1", "kind": "pr_merged"},
        {"task_id": "T" * 201, "kind": "pr_merged"},
        {"task_id": "T-1", "kind": "launch_rockets"},
        {"task_id": "T-1", "kind": "time", "params": {"at": {"nested": 1}}},
        {"task_id": "T-1", "kind": "time", "params": {"x": "y" * 201}},
        {"task_id": "T-1", "kind": "time", "params": {f"k{n}": n for n in range(21)}},
        {"task_id": "T-1", "kind": "time", "params": [1]},
    ],
)
def test_malformed_registrations_are_400(registry: ConditionRegistry, data: Any) -> None:
    assert registry.register(json.dumps(data).encode()).status == 400
    assert registry(Snapshot(now=NOW)).conditions == ()


@pytest.mark.parametrize(
    "raw", [b"{broken", b"[" * 100_000, b'{"task_id": "T", "kind": "time", "params": {"x": NaN}}']
)
def test_bodies_json_cannot_hold_are_400(registry: ConditionRegistry, raw: bytes) -> None:
    assert registry.register(raw).status == 400


def test_the_id_does_not_depend_on_key_order() -> None:
    task, kind, params = parse(body(PR))
    swapped = dict(reversed(list(params.items())))
    assert registration_id(task, kind, params) == registration_id(task, kind, swapped)
    assert registration_id("T-2", kind, params) != registration_id(task, kind, params)


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        ({"enabled": "yes"}, "enabled must be"),
        ({"token_env": "1X"}, "token_env must be"),
        ({"max_age": "10s"}, "max_age must be"),
        ({"max_registered": 0}, "max_registered must be"),
        ({"other": 1}, "unknown [conditions] keys"),
        (3, "must be a table"),
    ],
)
def test_bad_settings_are_refused(raw: Any, error: str) -> None:
    with pytest.raises(ValueError, match=__import__("re").escape(error)):
        load_conditions(raw)


def test_off_by_default_and_a_token_is_required(tmp_path: Path) -> None:
    from kelpie.app import ConfigError, condition_registry, load_settings

    path = tmp_path / "kelpie.toml"
    path.write_text(f'[storage]\npath = "{tmp_path / "k.db"}"\n')
    assert condition_registry(load_settings(path), lambda: NOW) is None
    path.write_text(
        path.read_text() + '[conditions]\nenabled = true\ntoken_env = "KELPIE_TEST_COND"\n'
    )
    settings = load_settings(path)
    with pytest.raises(ConfigError, match=r"needs \$KELPIE_TEST_COND set"):
        condition_registry(settings, lambda: NOW)
    weak = {"KELPIE_TEST_COND": "short"}
    with pytest.raises(ValueError, match="KELPIE_TEST_COND") as exc:
        settings.conditions.token(weak)
    assert "short" not in str(exc.value)
    memory = replace(settings, db_path=None)
    with pytest.raises(ConfigError, match="needs a database"):
        condition_registry(memory, lambda: NOW)


def _post(
    server: Any, payload: bytes, token: str | None = TOKEN, host: str = "127.0.0.1"
) -> tuple[int, bytes]:
    from tests.test_host_check import request

    extra = {"Authorization": f"Bearer {token}"} if token else {}
    return request(server, "POST", "/conditions", host, payload, extra)


def test_the_endpoint_on_the_main_listener(registry: ConditionRegistry) -> None:
    server = make_server("127.0.0.1", 0, Metrics(), None, None, registry)
    for running in started(server):
        status, reply = _post(running, body(PR))
        assert status == 201 and json.loads(reply)["duplicate"] is False
        assert _post(running, body(PR))[0] == 200
        assert _post(running, body(PR), token=None)[0] == 401
        assert _post(running, body(PR), host="attacker.example:9464")[0] == 421  # rebinding


def test_the_token_is_checked_before_the_body_is_read(registry: ConditionRegistry) -> None:
    server = make_server("127.0.0.1", 0, Metrics(), None, None, registry)
    for running in started(server):
        address = running.server_address
        with socket.create_connection((address[0], address[1]), timeout=5) as sock:
            sock.sendall(
                b"POST /conditions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                b"Content-Type: application/json\r\nContent-Length: 1000\r\n\r\n"
            )  # no token, and the body never comes: answered at once
            data = sock.recv(65536)
        assert data.startswith(b"HTTP/1.0 401") or data.startswith(b"HTTP/1.1 401")


def test_without_conditions_the_path_is_not_found() -> None:
    server = make_server("127.0.0.1", 0, Metrics())
    for running in started(server):
        assert _post(running, body(PR))[0] == 404
