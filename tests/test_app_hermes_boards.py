"""``[[adapters.hermes.boards]]``: several boards of one Hermes home in one Kelpie (#198)."""

from __future__ import annotations

from pathlib import Path

import pytest

from kelpie.adapters.hermes.boards import HermesBoards
from kelpie.adapters.hermes.park import DEFAULT_WAIT_KEY
from kelpie.app import (
    ConfigError,
    HermesBoardSettings,
    HermesSettings,
    _with_wait_key,
    build_adapters,
    load_settings,
)

TWO = """
[adapters.hermes]
spool = "/data/s.jsonl"
human_owners = ["alice"]

[[adapters.hermes.boards]]
name = "qr"
db_path = "/data/boards/qr/kanban.db"

[[adapters.hermes.boards]]
name = "sign"
db_path = "/data/boards/sign/kanban.db"
wait_key = "/keys/sign.key"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return path


def test_two_boards_share_the_home_settings_and_get_a_key_each(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, TWO))
    hermes = settings.hermes
    assert hermes is not None and hermes.spool == Path("/data/s.jsonl")
    assert hermes.human_owners == frozenset({"alice"})
    ledger = (settings.db_path or Path()).parent
    assert hermes.boards == (
        HermesBoardSettings("qr", Path("/data/boards/qr/kanban.db"), ledger / "hermes-wait-qr.key"),
        HermesBoardSettings("sign", Path("/data/boards/sign/kanban.db"), Path("/keys/sign.key")),
    )


def test_one_board_form_is_unchanged(tmp_path: Path) -> None:
    hermes = load_settings(write(tmp_path, '[adapters.hermes]\ndb_path = "/d/k.db"\n')).hermes
    assert hermes is not None and hermes.boards == () and hermes.db_path == Path("/d/k.db")
    assert hermes.wait_key is not None and hermes.wait_key.name == "hermes-wait.key"


def board(name: str = "qr", db: str = "/d/qr.db", extra: str = "") -> str:
    return f'[[adapters.hermes.boards]]\nname = "{name}"\ndb_path = "{db}"\n{extra}'


@pytest.mark.parametrize(
    ("text", "match"),
    [
        ("[adapters.hermes]\nboards = []\n", "one or more tables"),
        ("[adapters.hermes]\nboards = 1\n", "one or more tables"),
        ('[adapters.hermes]\ndb_path = "/d/k.db"\n' + board(), "db_path cannot be set beside"),
        ('[adapters.hermes]\nwait_key = "/k"\n' + board(), "wait_key cannot be set beside"),
        (board(extra='roles = {"dev-1" = "dev"}\n'), r"unknown \[\[adapters.hermes.boards\]\]"),
        (board(name="QR"), "need a name"),
        (board(name="q:r"), "need a name"),
        (board(name="a/b"), "need a name"),
        ('[[adapters.hermes.boards]]\nname = "qr"\n', "qr: db_path must be a non-empty path"),
        (board(extra='wait_key = ""\n'), "qr: wait_key must be a non-empty path"),
        (board() + board(db="/d/other.db"), "names qr twice"),
        (board() + board(name="sign"), "lists a db_path twice"),
        (
            board(extra='wait_key = "/k"\n') + board("sign", "/d/s.db", 'wait_key = "/k"\n'),
            "share a wait_key",
        ),
    ],
)
def test_bad_boards_stop_kelpie(tmp_path: Path, text: str, match: str) -> None:
    with pytest.raises(ConfigError, match=match):
        load_settings(write(tmp_path, text))


def test_in_memory_ledger_keeps_board_keys_in_kelpie_home() -> None:
    unkeyed = HermesSettings(boards=(HermesBoardSettings("qr", Path("/d/qr.db")),))
    keyed = _with_wait_key(unkeyed, None)
    assert keyed is not None
    assert keyed.boards[0].wait_key == DEFAULT_WAIT_KEY.parent / "hermes-wait-qr.key"


def test_an_explicit_key_may_not_be_another_boards_default(tmp_path: Path) -> None:
    ledger = load_settings(None).db_path
    assert ledger is not None
    taken = ledger.parent / "hermes-wait-qr.key"
    text = board() + board("sign", "/d/s.db", f'wait_key = "{taken}"\n')
    with pytest.raises(ConfigError, match="share a wait_key"):
        load_settings(write(tmp_path, text))


def test_boards_build_one_adapter_over_the_home(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, TWO))
    (adapter,) = build_adapters(settings)
    assert isinstance(adapter, HermesBoards) and adapter.name == "hermes"
    assert adapter.spool == Path("/data/s.jsonl") and adapter.host is None
    qr, sign = adapter.boards
    assert (qr.board, qr.name, qr.db_path) == ("qr", "hermes:qr", Path("/data/boards/qr/kanban.db"))
    assert qr.spool is None and qr.tools is None and qr.human_owners == frozenset({"alice"})
    assert sign.wait_key == Path("/keys/sign.key") and qr.wait_key.name == "hermes-wait-qr.key"
    off = TWO.replace('human_owners = ["alice"]', "enabled = false")
    assert build_adapters(load_settings(write(tmp_path, off))) == []


def test_tool_processes_are_read_by_one_host_reader(tmp_path: Path) -> None:
    text = TWO + '[adapters.hermes.tools]\nnames = ["xcodebuild"]\n'
    (adapter,) = build_adapters(load_settings(write(tmp_path, text)))
    assert isinstance(adapter, HermesBoards) and adapter.host is not None
    assert adapter.host.tools is not None and adapter.host.tools.names == ("xcodebuild",)
    assert all(b.tools is None for b in adapter.boards)


def test_state_db_is_read_by_one_reader_for_every_board(tmp_path: Path) -> None:
    (adapter,) = build_adapters(load_settings(write(tmp_path, TWO)))
    assert isinstance(adapter, HermesBoards) and adapter.state is not None
    assert adapter.state.state_home is not None and adapter.state.tools is None
    assert all(b.state_home is None for b in adapter.boards)
    off = TWO.replace('human_owners = ["alice"]', 'human_owners = ["alice"]\nstate_home = false')
    (none,) = build_adapters(load_settings(write(tmp_path, off)))
    assert isinstance(none, HermesBoards) and none.state is None


def test_status_and_act_guard_know_the_boards(tmp_path: Path) -> None:
    from kelpie.app import ActBlocker, act_blockers, render_adapters

    settings = load_settings(write(tmp_path, TWO))
    adapters = build_adapters(settings)
    lines = render_adapters(adapters)
    assert "  hermes: NOT readable: qr: Hermes kanban database not found" in lines[2]
    assert any("board sign: /data/boards/sign/kanban.db" in x for x in lines)
    blockers = act_blockers(settings, adapters)
    assert isinstance(blockers.get("ping_pong"), ActBlocker) and blockers["ping_pong"].refused


def test_otlp_spans_of_a_board_task_link_by_the_id_on_the_board() -> None:
    from datetime import UTC, datetime

    from kelpie.app import _board_tasks
    from kelpie.core.model import Span, SpanKind, Status, Task

    now = datetime(2026, 3, 2, tzinfo=UTC)

    def task(tid: str, board: str | None = None, host: str | None = None) -> Task:
        return Task(tid, "x", Status.IN_PROGRESS, now, board=board, host=host)

    tasks = [
        task("qr:t_1", "qr"),
        task("sign:t_2", "sign"),
        task("qr:t_3", "qr"),
        task("sign:t_3", "sign"),
        task("vm-1:qr:t_4", "qr", "vm-1"),
        task("t_5"),
    ]

    def span(tid: str | None) -> Span:
        return Span("tr", f"s-{tid}", "x", SpanKind.TOOL, now, task_id=tid)

    raw = (span("t_1"), span("t_2"), span("t_3"), span("vm-1:t_4"), span("t_5"), span(None))
    out = [s.task_id for s in _board_tasks(raw, tasks)]
    assert out == ["qr:t_1", "sign:t_2", "t_3", "vm-1:qr:t_4", "t_5", None]


def test_a_broken_board_is_unobserved_in_the_merged_snapshot(tmp_path: Path) -> None:
    from datetime import UTC, datetime

    from kelpie.adapters.hermes import HermesAdapter
    from kelpie.app import merged_snapshot
    from kelpie.metrics import Metrics
    from tests.adapters.kanban import Board

    now = datetime(2026, 3, 2, 12, tzinfo=UTC)
    board = Board(tmp_path / "qr.db")
    board.task("t_1", "ready", now)
    kids = [
        HermesAdapter(db_path=p, spool=None, git_state=False, board=n, name=f"hermes:{n}")
        for n, p in (("qr", board.path), ("sign", tmp_path / "gone.db"))
    ]
    snap = merged_snapshot([HermesBoards(kids)], Metrics(), clock=lambda: now)()
    board.close()
    assert [t.id for t in snap.tasks] == ["qr:t_1"] and snap.unobserved == ("sign",)
