"""POST /conditions with per-agent tokens (#442): each agent's [[traces.tokens]] token
registers only for its own tasks (the board decides, as for spans); another agent's task
and a task the board does not show get the same 403, a ledger line and a counter; a
removed agent's token is refused and what it registered stops attaching; a flood from one
token leaves the others alone. The shared token keeps working, deprecated."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.conditions_api import (
    AUDIT_RULE,
    FOREIGN,
    SHARED,
    Answer,
    Caller,
    ConditionRegistry,
    ConditionSettings,
    load_conditions,
)
from kelpie.core.actions import ActionKind
from kelpie.core.ledger import MemoryLedgerStore
from kelpie.core.model import Owner, Session, Snapshot, Status, Task
from kelpie.metrics import Metrics
from kelpie.server import make_server
from kelpie.traces.ingest import ScopedToken
from kelpie.traces.scope import TokenScope
from tests.test_host_check import request, started

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
CLOCK = [NOW]
SHARED_TOKEN = "synthetic-shared-conditions-token-0123456789"
TOKEN_A = "synthetic-agent-a-conditions-token-0123456789"
TOKEN_B = "synthetic-agent-b-conditions-token-0123456789"
SCOPES = {
    "agent-a": TokenScope("agent-a", owners=frozenset({"agent-a"})),
    "agent-b": TokenScope("agent-b", owners=frozenset({"agent-b"})),
}
TOKENS = (ScopedToken("agent-a", TOKEN_A), ScopedToken("agent-b", TOKEN_B))
A, B = Caller("agent-a"), Caller("agent-b")
OWNERS = (Owner("agent-a", "dev"), Owner("agent-b", "dev"))
TASKS = (
    Task("T-A", "a's task", Status.WAITING, NOW, owner_id="agent-a"),
    Task("T-B", "b's task", Status.WAITING, NOW, owner_id="agent-b"),
)
BOARD = Snapshot(now=NOW, tasks=TASKS, owners=OWNERS)


def body(task: str = "T-A", pr: int = 12) -> bytes:
    return json.dumps(
        {"task_id": task, "kind": "pr_merged", "params": {"repo": "example/repo", "pr": pr}}
    ).encode()


class Headers(dict[str, str]):
    def get(self, name: str, default: str | None = None) -> str | None:  # type: ignore[override]
        return next((v for k, v in self.items() if k.lower() == name.lower()), default)


def headers(token: str) -> Headers:
    return Headers(
        {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Content-Length": "2",
        }
    )


def make(
    tmp_path: Path,
    settings: ConditionSettings | None = None,
    tokens: tuple[ScopedToken, ...] = TOKENS,
    scopes: dict[str, TokenScope] | None = None,
    shared: str = "",
    **extra: Any,
) -> ConditionRegistry:
    return ConditionRegistry(
        settings or ConditionSettings(enabled=True),
        tmp_path / "k.db",
        lambda: CLOCK[0],
        shared,
        scoped=tokens,
        scopes=SCOPES if scopes is None else scopes,
        **extra,
    )


@pytest.fixture
def ledger() -> MemoryLedgerStore:
    return MemoryLedgerStore()


@pytest.fixture
def metrics() -> Metrics:
    found = Metrics()
    found.set_conditions(True, ("out_of_scope", "rate_limited", "token_cap"))
    return found


@pytest.fixture
def registry(
    tmp_path: Path, ledger: MemoryLedgerStore, metrics: Metrics
) -> Iterator[ConditionRegistry]:
    CLOCK[:] = [NOW]
    found = make(tmp_path, audit=ledger, observer=metrics)
    found(BOARD)  # the first tick reads the board
    yield found
    found.close()


def attached(registry: ConditionRegistry, board: Snapshot = BOARD) -> list[str]:
    return [c.task_id or "" for c in registry(board).conditions]


def test_each_token_is_its_own_caller(registry: ConditionRegistry) -> None:
    assert registry.precheck(headers(TOKEN_A)) == A
    assert registry.precheck(headers(TOKEN_B)) == B
    missing = registry.precheck(headers(SHARED_TOKEN))  # no shared token configured
    assert isinstance(missing, Answer) and missing.status == 401


def test_own_task_is_registered_and_attached(registry: ConditionRegistry) -> None:
    answer = registry.register(body("T-A"), A)
    assert answer.status == 201 and answer.body["duplicate"] is False
    assert registry.register(body("T-A"), A).status == 200
    assert attached(registry) == ["T-A"]


def test_foreign_and_unknown_tasks_are_refused_alike(
    registry: ConditionRegistry, ledger: MemoryLedgerStore, metrics: Metrics
) -> None:
    foreign = registry.register(body("T-B"), A)
    unknown = registry.register(body("T-NOWHERE"), A)
    assert (foreign.status, foreign.body) == (403, {"error": FOREIGN})
    assert (unknown.status, unknown.body) == (foreign.status, foreign.body)  # tells nothing
    assert attached(registry) == []  # nothing stored
    lines = ledger.since(NOW - timedelta(hours=1))
    assert [(e.rule, e.kind, e.task_id) for e in lines] == [
        (AUDIT_RULE, ActionKind.REGISTER, "T-B"),
        (AUDIT_RULE, ActionKind.REGISTER, "T-NOWHERE"),
    ]
    assert lines[0].params == {"token": "agent-a", "kind": "pr_merged", "status": 403}
    assert TOKEN_A not in lines[0].reason
    assert 'kelpie_conditions_refused_total{reason="out_of_scope"} 2' in metrics.render()


def test_refusals_repeat_on_one_ledger_line_an_hour(
    registry: ConditionRegistry, ledger: MemoryLedgerStore
) -> None:
    for _ in range(3):
        assert registry.register(body("T-B"), A).status == 403
    [line] = ledger.since(NOW - timedelta(hours=1))
    assert line.suppressed == 2
    CLOCK[0] = NOW + timedelta(hours=2)
    registry.register(body("T-B"), A)
    assert len(ledger.since(NOW - timedelta(hours=1))) == 2


def test_a_task_by_its_session_or_role_is_the_tokens_own(tmp_path: Path) -> None:
    CLOCK[:] = [NOW]
    scopes = {
        "runs": TokenScope("runs", sessions=("run-a-*",)),
        "devs": TokenScope("devs", roles=frozenset({"dev"})),
    }
    tokens = (ScopedToken("runs", TOKEN_A), ScopedToken("devs", TOKEN_B))
    registry = make(tmp_path, tokens=tokens, scopes=scopes)
    try:
        session = Session("run-a-7", "T-X", "someone", NOW)
        task = Task("T-X", "x", Status.IN_PROGRESS, NOW, owner_id="someone")
        board = Snapshot(now=NOW, tasks=(task, *TASKS), owners=OWNERS, sessions=(session,))
        registry(board)
        assert registry.register(body("T-X"), Caller("runs")).status == 201
        assert registry.register(body("T-A"), Caller("runs")).status == 403
        assert registry.register(body("T-B"), Caller("devs")).status == 201  # role dev
        assert registry.register(body("T-X"), Caller("devs")).status == 403
        assert sorted(attached(registry, board)) == ["T-B", "T-X"]
    finally:
        registry.close()


def test_a_removed_agents_token_is_refused_and_its_registrations_stop(
    tmp_path: Path,
) -> None:
    CLOCK[:] = [NOW]
    registry = make(tmp_path)
    registry(BOARD)
    assert registry.register(body("T-B"), B).status == 201
    assert attached(registry) == ["T-B"]
    registry.close()
    # agent-b's entry is removed; kelpie serve restarts with agent-a's only.
    only_a = make(tmp_path, tokens=TOKENS[:1], scopes={"agent-a": SCOPES["agent-a"]})
    try:
        refused = only_a.precheck(headers(TOKEN_B))
        assert isinstance(refused, Answer) and refused.status == 401
        assert attached(only_a) == []  # what it registered no longer attaches
        assert only_a.register(body("T-B"), B).status == 403  # nor may its name register
    finally:
        only_a.close()


def test_a_task_moved_to_another_agent_drops_the_old_agents_registration(
    registry: ConditionRegistry,
) -> None:
    assert registry.register(body("T-A"), A).status == 201
    moved = Task("T-A", "a's task", Status.WAITING, NOW, owner_id="agent-b")
    board = replace(BOARD, tasks=(moved, TASKS[1]))
    assert attached(registry, board) == []
    assert registry.register(body("T-A", pr=13), B).status == 201  # the new owner's own
    assert attached(registry, board) == ["T-A"]


def test_a_flood_from_one_token_does_not_affect_another(tmp_path: Path, metrics: Metrics) -> None:
    CLOCK[:] = [NOW]
    settings = load_conditions({"enabled": True, "max_per_minute": 5, "max_per_token": 3})
    registry = make(tmp_path, settings, observer=metrics)
    try:
        registry(BOARD)
        rated = [registry.precheck(headers(TOKEN_A)) for _ in range(8)]
        assert rated[:5] == [A] * 5
        limited = rated[5]
        assert isinstance(limited, Answer) and limited.status == 429
        assert int(limited.headers["Retry-After"]) >= 1
        assert registry.precheck(headers(TOKEN_B)) == B  # its own bucket
        answers = [registry.register(body("T-A", pr=n), A).status for n in range(5)]
        assert answers == [201, 201, 201, 507, 507]  # agent-a's share is full
        assert registry.register(body("T-B"), B).status == 201  # agent-b's is not
        CLOCK[0] = NOW + timedelta(minutes=1)
        assert registry.precheck(headers(TOKEN_A)) == A  # the bucket refills
        text = metrics.render()
        assert 'kelpie_conditions_refused_total{reason="rate_limited"} 3' in text
        assert 'kelpie_conditions_refused_total{reason="token_cap"} 2' in text
    finally:
        registry.close()


def test_the_shared_token_still_registers_for_any_task(tmp_path: Path) -> None:
    CLOCK[:] = [NOW]
    registry = make(tmp_path, shared=SHARED_TOKEN)
    try:
        registry(BOARD)
        assert registry.precheck(headers(SHARED_TOKEN)) == SHARED
        assert registry.register(body("T-B"), SHARED).status == 201
        assert registry.register(body("T-NOWHERE"), SHARED).status == 201
        assert attached(registry) == ["T-B"]
    finally:
        registry.close()


def test_a_task_by_its_id_on_its_board_is_stored_by_its_full_id(tmp_path: Path) -> None:
    CLOCK[:] = [NOW]
    registry = make(tmp_path)
    try:
        task = Task("qr:t_1a", "x", Status.WAITING, NOW, owner_id="agent-a", board="qr")
        board = Snapshot(now=NOW, tasks=(task,), owners=OWNERS)
        registry(board)
        assert registry.register(body("t_1a"), A).status == 201  # as Hermes names it
        assert attached(registry, board) == ["qr:t_1a"]
        assert registry.register(body("t_1a"), B).status == 403
    finally:
        registry.close()


def test_a_table_from_before_per_agent_tokens_gets_the_column(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "k.db") as db:
        db.execute(
            "CREATE TABLE registered_conditions (id TEXT PRIMARY KEY, task_id TEXT NOT NULL,"
            " kind TEXT NOT NULL, params TEXT NOT NULL, registered_at TEXT NOT NULL,"
            " bound_to TEXT)"
        )
        db.execute(
            "INSERT INTO registered_conditions VALUES ('registered:old', 'T-B', 'pr_merged',"
            " '{}', ?, NULL)",
            (NOW.isoformat(),),
        )
    reader = make(tmp_path, tokens=(), readonly=True, shared_rows=True)
    try:
        assert attached(reader) == ["T-B"]  # read as the shared token's, column or not
    finally:
        reader.close()
    CLOCK[:] = [NOW]
    registry = make(tmp_path, shared=SHARED_TOKEN)  # the shared token is still set
    try:
        registry(BOARD)
        assert registry.register(body("T-A"), A).status == 201
        assert sorted(attached(registry)) == ["T-A", "T-B"]
    finally:
        registry.close()
    # The shared token unset: its (and older) registrations stop attaching, and go.
    registry = make(tmp_path)
    try:
        assert attached(registry) == ["T-A"]
        with sqlite3.connect(tmp_path / "k.db") as db:
            rows = db.execute("SELECT task_id, token FROM registered_conditions").fetchall()
        assert rows == [("T-A", "agent-a")]
    finally:
        registry.close()


def test_registrations_of_a_removed_token_are_deleted(tmp_path: Path) -> None:
    CLOCK[:] = [NOW]
    registry = make(tmp_path)
    registry(BOARD)
    assert registry.register(body("T-B"), B).status == 201
    registry.close()
    only_a = make(tmp_path, tokens=TOKENS[:1], scopes={"agent-a": SCOPES["agent-a"]})
    try:
        only_a(BOARD)
        with sqlite3.connect(tmp_path / "k.db") as db:
            assert db.execute("SELECT COUNT(*) FROM registered_conditions").fetchone()[0] == 0
    finally:
        only_a.close()


def test_before_the_first_tick_a_token_is_asked_to_retry(
    tmp_path: Path, ledger: MemoryLedgerStore, metrics: Metrics
) -> None:
    CLOCK[:] = [NOW]
    registry = make(tmp_path, audit=ledger, observer=metrics)
    try:
        answer = registry.register(body("T-A"), A)  # no tick read the board yet
        assert answer.status == 503 and int(answer.headers["Retry-After"]) > 0
        assert ledger.since(NOW - timedelta(hours=1)) == []  # not a refusal
        assert 'reason="out_of_scope"} 0' in metrics.render()
        registry(BOARD)
        assert registry.register(body("T-A"), A).status == 201
    finally:
        registry.close()


def test_made_up_task_ids_fold_into_one_ledger_line_per_token(
    registry: ConditionRegistry, ledger: MemoryLedgerStore
) -> None:
    from kelpie.conditions_api import AUDIT_PER_TOKEN

    for n in range(AUDIT_PER_TOKEN + 30):
        assert registry.register(body(f"T-MADE-UP-{n}"), A).status == 403
    lines = ledger.since(NOW - timedelta(hours=1))
    assert len(lines) == AUDIT_PER_TOKEN + 1
    assert lines[-1].task_id is None and lines[-1].suppressed == 29
    assert registry.register(body("T-A"), B).status == 403  # another token: its own lines
    assert ledger.since(NOW - timedelta(hours=1))[-1].task_id == "T-A"


def test_settings_of_per_agent_tokens() -> None:
    assert load_conditions({}).max_per_token == 100
    assert load_conditions({"max_registered": 5}).max_per_token == 5
    assert load_conditions({"token_env": "X_TOKEN"}).token_env_explicit
    assert not load_conditions({}).token_env_explicit
    with pytest.raises(ValueError, match="max_per_token must be 1 to max_registered"):
        load_conditions({"max_registered": 5, "max_per_token": 6})
    with pytest.raises(ValueError, match="max_per_minute must be"):
        load_conditions({"max_per_minute": 0})
    with pytest.raises(ValueError, match="needs a token"):
        ConditionRegistry(ConditionSettings(enabled=True), "k.db", lambda: NOW)
    with pytest.raises(ValueError, match="needs its scope"):
        ConditionRegistry(
            ConditionSettings(enabled=True), "k.db", lambda: NOW, scoped=TOKENS, scopes={}
        )


def config(tmp_path: Path, conditions: str = "") -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(
        f'[storage]\npath = "{tmp_path / "k.db"}"\n'
        f"[conditions]\nenabled = true\n{conditions}"
        '[[traces.tokens]]\nname = "agent-a"\ntoken_env = "KELPIE_TEST_TOKEN_A"\n'
        'owners = ["agent-a"]\n'
    )
    return path


def test_serve_takes_the_traces_tokens_and_refuses_bad_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kelpie.app import ConfigError, condition_registry, load_settings

    monkeypatch.delenv("KELPIE_CONDITIONS_TOKEN", raising=False)
    settings = load_settings(config(tmp_path))
    with pytest.raises(ConfigError, match="KELPIE_TEST_TOKEN_A is not set"):
        condition_registry(settings, lambda: NOW)
    monkeypatch.setenv("KELPIE_TEST_TOKEN_A", "short")
    with pytest.raises(ConfigError, match="agent-a") as exc:
        condition_registry(settings, lambda: NOW)
    assert "short" not in str(exc.value)
    monkeypatch.setenv("KELPIE_TEST_TOKEN_A", TOKEN_A)
    registry = condition_registry(settings, lambda: NOW)
    assert registry is not None and not registry.token
    assert [t.name for t in registry.scoped] == ["agent-a"]
    registry.close()
    # also with [traces] off: the entries are the agents' tokens
    path = config(tmp_path)
    path.write_text(path.read_text() + "[traces]\nenabled = false\n")
    off = load_settings(path)
    registry = condition_registry(off, lambda: NOW)
    assert registry is not None and len(registry.scoped) == 1
    registry.close()
    monkeypatch.setenv("KELPIE_CONDITIONS_TOKEN", TOKEN_A)  # the same as agent-a's
    with pytest.raises(ConfigError, match="same as the shared \\[conditions\\] token") as exc:
        condition_registry(settings, lambda: NOW)
    assert TOKEN_A not in str(exc.value)
    monkeypatch.setenv("KELPIE_CONDITIONS_TOKEN", SHARED_TOKEN)
    registry = condition_registry(settings, lambda: NOW)
    assert registry is not None and registry.token == SHARED_TOKEN and registry.scoped
    registry.close()


def test_without_any_token_serve_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from kelpie.app import ConfigError, condition_registry, load_settings

    monkeypatch.delenv("KELPIE_CONDITIONS_TOKEN", raising=False)
    path = tmp_path / "kelpie.toml"
    path.write_text(f'[storage]\npath = "{tmp_path / "k.db"}"\n[conditions]\nenabled = true\n')
    with pytest.raises(ConfigError, match=r"needs per-agent tokens \(\[\[traces.tokens\]\]"):
        condition_registry(load_settings(path), lambda: NOW)


def test_doctor_warns_while_the_shared_token_is_set(tmp_path: Path) -> None:
    from kelpie.app import load_settings
    from kelpie.doctor import CONDITIONS_CHECK, check_conditions_tokens

    path = tmp_path / "kelpie.toml"
    path.write_text(f'[storage]\npath = "{tmp_path / "k.db"}"\n')
    check = check_conditions_tokens(load_settings(path), {})
    assert (check.status, check.name) == ("SKIP", CONDITIONS_CHECK)
    settings = load_settings(config(tmp_path))
    env = {"KELPIE_TEST_TOKEN_A": TOKEN_A}
    assert check_conditions_tokens(settings, env).status == "PASS"
    check = check_conditions_tokens(settings, {**env, "KELPIE_CONDITIONS_TOKEN": SHARED_TOKEN})
    assert check.status == "WARN" and "deprecated" in check.detail
    assert SHARED_TOKEN not in check.detail + check.fix
    explicit = load_settings(config(tmp_path, 'token_env = "KELPIE_TEST_SHARED"\n'))
    check = check_conditions_tokens(explicit, {**env, "KELPIE_TEST_SHARED": SHARED_TOKEN})
    assert check.status == "WARN" and "KELPIE_TEST_SHARED" in check.detail
    assert check_conditions_tokens(settings, {}).status == "FAIL"  # agent-a's is unset
    weak = check_conditions_tokens(settings, {"KELPIE_TEST_TOKEN_A": "short"})
    assert weak.status == "FAIL" and "short" not in weak.detail + weak.fix
    path.write_text(f'[storage]\npath = "{tmp_path / "k.db"}"\n[conditions]\nenabled = true\n')
    assert check_conditions_tokens(load_settings(path), {}).status == "FAIL"  # no token


def test_per_agent_tokens_on_the_main_listener(registry: ConditionRegistry) -> None:
    metrics = Metrics()
    metrics.set_conditions(True)
    server = make_server("127.0.0.1", 0, metrics, None, None, registry)

    def post(running: Any, payload: bytes, token: str) -> int:
        extra = {"Authorization": f"Bearer {token}"}
        return request(running, "POST", "/conditions", "127.0.0.1", payload, extra)[0]

    for running in started(server):
        assert post(running, body("T-A"), TOKEN_A) == 201
        assert post(running, body("T-B"), TOKEN_A) == 403
        assert post(running, body("T-NOWHERE"), TOKEN_A) == 403
        assert post(running, body("T-B"), TOKEN_B) == 201
        assert post(running, body("T-B"), SHARED_TOKEN) == 401
    text = metrics.render()
    assert 'kelpie_conditions_requests_total{code="201"} 2' in text
    assert 'kelpie_conditions_requests_total{code="403"} 2' in text
    assert TOKEN_A not in text


def test_why_reads_the_shared_tokens_rows_serve_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kelpie.app import connector_steps, load_settings

    monkeypatch.delenv("KELPIE_CONDITIONS_TOKEN", raising=False)  # set only for serve
    CLOCK[:] = [NOW]
    writer = make(tmp_path, shared=SHARED_TOKEN)
    writer(BOARD)
    assert writer.register(body("T-B"), SHARED).status == 201
    writer.close()
    settings = load_settings(config(tmp_path))
    steps, _ = connector_steps(settings, Metrics(), lambda: NOW, network=False)
    reader = dict(steps)["registered_conditions"]
    assert isinstance(reader, ConditionRegistry) and reader.readonly
    try:
        assert attached(reader) == ["T-B"]  # as serve, which still holds the shared token
    finally:
        reader.close()


def test_a_repeat_keeps_or_moves_its_owner(tmp_path: Path) -> None:
    CLOCK[:] = [NOW]
    settings = load_conditions({"enabled": True, "max_per_token": 1})
    registry = make(tmp_path, settings, shared=SHARED_TOKEN)
    try:
        registry(BOARD)
        assert registry.register(body("T-A"), A).status == 201
        assert registry.register(body("T-A"), SHARED).status == 200  # stays agent-a's
        with sqlite3.connect(tmp_path / "k.db") as db:
            assert db.execute("SELECT token FROM registered_conditions").fetchall() == [
                ("agent-a",)
            ]
        assert registry.register(body("T-B"), SHARED).status == 201
        assert registry.register(body("T-B", pr=13), B).status == 201  # agent-b's one
        assert registry.register(body("T-B"), B).status == 507  # taking more: over its share
    finally:
        registry.close()
