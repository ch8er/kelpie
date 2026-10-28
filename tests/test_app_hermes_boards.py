"""``[[adapters.hermes.boards]]``: several boards of one Hermes home in one Kelpie (#198)."""

from __future__ import annotations

from pathlib import Path

import pytest

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


def test_boards_are_refused_until_the_adapter_reads_them(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not supported"):
        build_adapters(load_settings(write(tmp_path, TWO)))
    off = TWO.replace('human_owners = ["alice"]', "enabled = false")
    assert build_adapters(load_settings(write(tmp_path, off))) == []
