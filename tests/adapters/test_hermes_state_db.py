"""Waits inside Hermes sessions, from each profile's state.db (state_db.py)."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter, state_db
from kelpie.adapters.hermes import adapter as hermes_adapter
from kelpie.adapters.hermes.state_db import read_waits, state_dbs, wait_shape
from tests.adapters.kanban import Board

NOW = datetime(2026, 5, 12, 12, tzinfo=UTC)
M = timedelta(minutes=1)
S = timedelta(seconds=1)

# The columns Kelpie reads, as in Hermes' hermes_state_common.py SCHEMA_SQL.
SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, cwd TEXT, started_at REAL NOT NULL,
    ended_at REAL, last_activity_at REAL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
    timestamp REAL NOT NULL, token_count INTEGER,
    active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX idx_messages_session ON messages(session_id, timestamp);
CREATE INDEX idx_messages_assistant_calls_by_session ON messages(session_id)
    WHERE role = 'assistant' AND tool_calls IS NOT NULL;
"""


class StateDb:
    def __init__(self, path: Path, schema: str = SCHEMA) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(schema)
        self._calls = 0

    def session(
        self,
        sid: str,
        cwd: str | None,
        started: datetime,
        active: datetime,
        source: str = "kanban",
    ) -> None:
        self.conn.execute(
            "INSERT INTO sessions (id, source, cwd, started_at, last_activity_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (sid, source, cwd, started.timestamp(), active.timestamp()),
        )
        self.conn.commit()

    def call(
        self,
        sid: str,
        at: datetime,
        command: str,
        *,
        result: datetime | None = None,
        tool: str = "terminal",
        background: bool = False,
        flat: bool = False,
        call_id: str | None = None,
        state: tuple[int, int] = (1, 0),  # (active, compacted)
    ) -> str:
        self._calls += 1
        call_id = call_id or f"call_{self._calls}"
        args: dict[str, object] = {"command": command}
        if background:
            args["background"] = True
        fn = {"name": tool, "arguments": json.dumps(args)}
        call = (
            {"id": call_id, **fn} if flat else {"id": call_id, "type": "function", "function": fn}
        )
        self.conn.execute(
            "INSERT INTO messages (session_id, role, tool_calls, timestamp, active, compacted)"
            " VALUES (?, 'assistant', ?, ?, ?, ?)",
            (sid, json.dumps([call]), at.timestamp(), *state),
        )
        if result is not None:
            self.conn.execute(
                "INSERT INTO messages (session_id, role, content, tool_call_id, tool_name,"
                " timestamp) VALUES (?, 'tool', 'ok', ?, ?, ?)",
                (sid, call_id.split("|")[0], tool, result.timestamp()),
            )
        self.conn.commit()
        return call_id


def shape(command: str) -> tuple[str, timedelta | None, bool] | None:
    found = wait_shape(command)
    return None if found is None else (found.shape, found.slept, found.whole)


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("sleep 300", ("sleep", 300 * S, True)),
        ("sleep 1m 30", ("sleep", 90 * S, True)),
        ("FOO=1 /bin/sleep 2h", ("sleep", 7200 * S, True)),
        ("sleep $DELAY", ("sleep", None, True)),
        ("sleep", ("sleep", None, True)),
        ("sleep infinity", ("sleep", None, True)),
        ("sleep 99999999999", ("sleep", timedelta(days=7), True)),
        ("sleep 300 && gh pr checks 12", ("sleep", 300 * S, False)),
        ("sleep 1 && make build", ("sleep", 1 * S, False)),
        ("cd /w && gh pr checks 12 --watch", ("gh pr checks --watch", None, True)),
        ("gh run watch 4711 --exit-status | tail -5", ("gh run watch", None, False)),
        ("sleep 60; gh run watch 1", ("gh run watch", 60 * S, True)),
        ("while true; do gh pr checks 12; sleep 30; done", ("sleep loop", None, False)),
        ("for i in 1 2 3; do gh run watch $i; done", ("gh run watch loop", None, True)),
        ("until gh pr checks 1; do sleep 20; done", ("sleep loop", None, False)),
        ("while ! gh run watch 1; do :; done", ("gh run watch loop", None, True)),
        ("for f in a b; do echo $f; done; sleep 600", ("sleep", 600 * S, False)),
        ("if true; then sleep 30; fi", ("sleep", 30 * S, True)),
        # Newlines, comments, redirections
        ("cd /w\nsleep 300", ("sleep", 300 * S, True)),
        ("git push\ngh run watch 5", ("gh run watch", None, False)),
        ("echo start\nwhile true; do sleep 30; done", ("sleep loop", None, False)),
        ("sleep 30 # wait for CI\nsleep 40", ("sleep", 70 * S, True)),
        ("echo '# x'; sleep 10", ("sleep", 10 * S, False)),
        ("sleep 300 2>/dev/null", ("sleep", 300 * S, True)),
        ("sleep 5 >&2", ("sleep", 5 * S, True)),
        # Wrappers and shells
        ("timeout 600 gh run watch 1", ("gh run watch", None, True)),
        ("timeout -k 5 600 sleep 30", ("sleep", 30 * S, True)),
        ("env A=1 nohup sleep 40", ("sleep", 40 * S, True)),
        ("nice -n 5 command sleep 40", ("sleep", 40 * S, True)),
        ("bash -c 'sleep 300'", ("sleep", 300 * S, True)),
        ('bash -lc "while true; do sleep 5; done"', ("sleep loop", None, True)),
        ("bash -c 'make test'", None),
        # gh's global flags
        ("gh -R o/r run watch 5", ("gh run watch", None, True)),
        ("gh --repo o/r pr checks 1 --watch", ("gh pr checks --watch", None, True)),
        ("gh pr checks 1 --watch=true", ("gh pr checks --watch", None, True)),
        ("gh pr checks 1 --watch=false", None),
        # Not waits
        ("sleep 5 &", None),
        ("make serve & sleep 2 && curl x", ("sleep", 2 * S, False)),
        ("cat > ci.sh <<EOF\nwhile true; do sleep 30; done\nEOF", None),
        ("cat > ci.sh <<'EOF'\nsleep 30\nEOF\nsleep 60", ("sleep", 60 * S, False)),
        ("echo sleep 5", None),
        ("grep -rn 'sleep 5' .", None),
        ("gh pr checks 12", None),
        ("while read l; do echo $l; done < f", None),
        ("echo 'unbalanced", None),
        # Groups and loops sent to the background, here-strings, short loops in a script
        ("(sleep 300; make) &", None),
        ("while true; do gh pr checks 1; sleep 60; done >log 2>&1 &", None),
        ("(sleep 300 &)", None),
        ("sleep 40 |& tee x", ("sleep", 40 * S, False)),  # a pipe, not the background
        ("cat <<< hi\nsleep 600", ("sleep", 600 * S, False)),
        ("cat <<\\EOF\nsleep 5\nEOF\nsleep 60", ("sleep", 60 * S, False)),
        ("bash --login -c 'sleep 300'", ("sleep", 300 * S, True)),
        ("until curl -s localhost:8000; do sleep 1; done; pytest", None),
        ("while true; do sleep 5; done", ("sleep loop", None, True)),  # the whole command waits
        # CI polls with short sleeps are waits, whatever runs in the loop
        ("until gh pr checks 1; do sleep 5; done", ("sleep loop", None, False)),
        (
            "for i in $(seq 60); do gh pr checks 1 && break; sleep 5; done",
            ("sleep loop", None, False),
        ),
        # Only the group sent to the background is dropped; what follows still counts
        ("(cd web && npm run dev) & gh run watch 123", ("gh run watch", None, True)),
        ("while true; do sleep 5; done & sleep 300", ("sleep", 300 * S, True)),
        ("(make) & sleep 300", ("sleep", 300 * S, True)),
        ("echo done & sleep 300", ("sleep", 300 * S, True)),  # an argument, not a loop's end
        ("x=(a b) & sleep 30", ("sleep", 30 * S, False)),  # an array, not a group
        ("if true; then sleep 100; fi & sleep 40", ("sleep", 40 * S, True)),
        ("case $x in a) sleep 300;; esac &", None),
        # &> is a redirection, not the background
        ("until gh pr checks 1; do sleep 30; done &>/dev/null", ("sleep loop", None, False)),
        ("{ gh run watch 1; } &>log", ("gh run watch", None, True)),
        ("(gh run watch 1) &>log", ("gh run watch", None, True)),
        (">log & echo done", None),
        (
            'git push && while [ "$(gh run view 1 -q .status)" != done ]; do sleep 5; done',
            ("sleep loop", None, False),
        ),
        ("git push; while true; do st=`gh run view 1`; sleep 5; done", ("sleep loop", None, False)),
        ("npm start & for i in $(seq 30); do curl -s localhost && break; sleep 1; done", None),
        ("> x & while true; do sleep 60; done", ("sleep loop", None, True)),  # no j = -1
    ],
)
def test_wait_shapes(command: str, expected: tuple[str, timedelta | None, bool] | None) -> None:
    assert shape(command) == expected


def test_waits_of_every_profile_active_in_the_window(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    default = StateDb(home / "state.db")
    reviewer = StateDb(home / "profiles" / "reviewer" / "state.db")
    (home / "profiles" / ".trash").mkdir()  # hidden: not a profile
    (home / "profiles" / "empty").mkdir()  # no state.db
    assert [p for p, _ in state_dbs(home)] == ["default", "reviewer"]
    reviewer.session("s1", "/ws/t1", NOW - 60 * M, NOW - M)
    reviewer.call("s1", NOW - 50 * M, "sleep 300", result=NOW - 40 * M)  # at most its sleep
    reviewer.call("s1", NOW - 30 * M, "sleep 120")  # no result before the next call: 2m
    reviewer.call("s1", NOW - 20 * M, "gh pr checks 1 --watch", flat=True, result=NOW - 3 * M)
    reviewer.call("s1", NOW - 2 * M, "sleep 600", background=True)  # holds nothing
    reviewer.call("s1", NOW - 2 * M, "sleep 30", tool="process_manage")  # not a terminal call
    reviewer.call("s1", NOW - 2 * M, "make test")  # not a wait
    reviewer.call("s1", NOW - 1 * M, "sleep 1 && make build", result=NOW)  # 1s, not a minute
    reviewer.call("s1", NOW - 7 * 60 * M, "sleep 300")  # before the window
    reviewer.session("old", "/ws/t2", NOW - 9 * 60 * M, NOW - 8 * 60 * M)
    reviewer.call("old", NOW - 8 * 60 * M, "sleep 300")
    default.session("pm", None, NOW - 30 * M, NOW - 5 * M, source="cron")
    default.call("pm", NOW - 25 * M, "while true; do sleep 60; done")
    default.session("chat", None, NOW - 30 * M, NOW - 5 * M, source="telegram")
    default.call("chat", NOW - 25 * M, "sleep 600")  # a person's own chat: not read
    waits = read_waits(home, NOW - 6 * 60 * M)
    seen = sorted((w.profile, w.session, w.cwd, w.at, w.shape, w.waited) for w in waits)
    assert seen == [
        ("default", "pm", None, NOW - 25 * M, "sleep loop", None),
        ("reviewer", "s1", "/ws/t1", NOW - 50 * M, "sleep", 5 * M),
        ("reviewer", "s1", "/ws/t1", NOW - 30 * M, "sleep", 1 * M),
        ("reviewer", "s1", "/ws/t1", NOW - 20 * M, "gh pr checks --watch", 15 * M),
        ("reviewer", "s1", "/ws/t1", NOW - 1 * M, "sleep", timedelta(0)),
    ]
    chats = read_waits(home, NOW - 6 * 60 * M, sources=("telegram",))
    assert [(w.session, w.shape) for w in chats] == [("chat", "sleep")]


def test_compaction_copies_and_rewound_turns_count_once(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "state.db")
    db.session("s", None, NOW - 60 * M, NOW)
    db.session("child", None, NOW - 30 * M, NOW)  # a compressed session's continuation
    first = db.call("s", NOW - 50 * M, "sleep 300", state=(0, 1))  # archived by compaction
    db.call("s", NOW - 50 * M, "sleep 300", call_id=first)  # the tail re-inserted
    db.call("child", NOW - 50 * M, "sleep 300", call_id=first)  # carried into the child
    db.call("s", NOW - 40 * M, "sleep 300", state=(0, 0))  # rewound: it did not happen
    db.call("s", NOW - 30 * M, "sleep 60", call_id="call_x|fc_1")  # Codex: composite on the call
    db.call("s", NOW - 30 * M, "sleep 60", call_id="call_x")
    waits = read_waits(tmp_path, NOW - 60 * M)
    assert [(w.at, w.shape) for w in waits] == [(NOW - 50 * M, "sleep"), (NOW - 30 * M, "sleep")]


def test_repeated_call_ids_are_separate_calls(tmp_path: Path) -> None:
    """Hermes derives a missing call id from the name and arguments: the same command run
    again has the same id, at another time."""
    db = StateDb(tmp_path / "state.db")
    db.session("s", None, NOW - 60 * M, NOW)
    db.session("t", None, NOW - 60 * M, NOW)
    for i in range(5):
        db.call("s" if i < 4 else "t", NOW - (50 - 5 * i) * M, "sleep 300", call_id="call_abc")
    assert len(read_waits(tmp_path, NOW - 60 * M)) == 5


def test_the_time_until_a_calls_own_result_counts(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "state.db")
    db.session("s", None, NOW - 60 * M, NOW)
    two = [
        {"id": "c1", "function": {"name": "read_file", "arguments": "{}"}},
        {
            "id": "c2",
            "function": {"name": "terminal", "arguments": '{"command": "gh run watch 5"}'},
        },
    ]
    db.conn.execute(
        "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES ('s', 'assistant',"
        " ?, ?)",
        (json.dumps(two), (NOW - 30 * M).timestamp()),
    )
    for cid, at in (("c1", NOW - 30 * M + S), ("c2", NOW - 10 * M)):  # the read first, at once
        db.conn.execute(
            "INSERT INTO messages (session_id, role, tool_call_id, timestamp)"
            " VALUES ('s', 'tool', ?, ?)",
            (cid, at.timestamp()),
        )
    db.conn.commit()
    db.call("s", NOW - 8 * M, "sleep 60; gh run watch 1", result=NOW)  # not capped by the sleep
    found = [(w.shape, w.waited) for w in read_waits(tmp_path, NOW - 60 * M)]
    assert found == [("gh run watch", 15 * M), ("gh run watch", 5 * M)]


def test_a_worker_session_is_read_by_its_origin(tmp_path: Path) -> None:
    schema = SCHEMA.replace("source TEXT NOT NULL,", "source TEXT NOT NULL, created_source TEXT,")
    db = StateDb(tmp_path / "state.db", schema=schema)
    db.session("w", None, NOW - 30 * M, NOW, source="telegram")  # rerouted since
    db.conn.execute("UPDATE sessions SET created_source = 'kanban' WHERE id = 'w'")
    db.conn.commit()
    db.call("w", NOW - 20 * M, "sleep 120")
    assert [w.session for w in read_waits(tmp_path, NOW - 60 * M)] == ["w"]


def test_a_result_is_paired_by_call_id_first(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "state.db")
    db.session("s", None, NOW - 60 * M, NOW)
    call = {"id": "fc_1", "call_id": "call_9", "function": {"name": "terminal",
            "arguments": '{"command": "gh run watch 3"}'}}  # fmt: skip
    db.conn.execute(
        "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES ('s', 'assistant',"
        " ?, ?)",
        (json.dumps([call]), (NOW - 30 * M).timestamp()),
    )
    db.conn.execute(
        "INSERT INTO messages (session_id, role, tool_call_id, timestamp)"
        " VALUES ('s', 'tool', 'call_9', ?)",
        ((NOW - 10 * M).timestamp(),),
    )
    db.conn.commit()
    assert [(w.shape, w.waited) for w in read_waits(tmp_path, NOW - 60 * M)] == [
        ("gh run watch", 15 * M)
    ]


def test_an_older_schema_is_read_with_what_it_has(tmp_path: Path) -> None:
    old = """
    CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, started_at REAL NOT NULL);
    CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, tool_calls TEXT,
                           tool_call_id TEXT, content TEXT, tool_name TEXT, timestamp REAL);
    """
    db = StateDb(tmp_path / "state.db", schema=old)
    db.conn.execute("INSERT INTO sessions VALUES ('s', NULL, ?)", ((NOW - M).timestamp(),))
    call = [{"name": "terminal", "arguments": '{"command": "sleep 40"}'}]
    db.conn.execute(
        "INSERT INTO messages (session_id, role, tool_calls, timestamp) VALUES (?, ?, ?, ?)",
        ("s", "assistant", json.dumps(call), NOW.timestamp()),
    )
    db.conn.commit()
    assert [(w.shape, w.waited) for w in read_waits(tmp_path, NOW - 5 * M)] == [("sleep", 30 * S)]


def test_rows_it_cannot_make_sense_of_are_skipped(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "state.db")
    db.session("s", None, NOW - M, NOW)
    db.call("s", NOW - M, "sleep 40")
    call = json.dumps([{"name": "terminal", "arguments": '{"command": "sleep 9"}'}])
    for at in (1e300, float("inf"), "abc", 1.7e12):  # far future, not finite, text, millis
        db.conn.execute(
            "INSERT INTO messages (session_id, role, tool_calls, timestamp)"
            " VALUES ('s', 'assistant', ?, ?)",
            (call, at),
        )
    deep = "[" * 100_000 + "]" * 100_000
    db.conn.execute(
        "INSERT INTO messages (session_id, role, tool_calls, timestamp)"
        " VALUES ('s', 'assistant', ?, ?)",
        (json.dumps([{"name": "terminal", "arguments": deep}]), NOW.timestamp()),
    )
    db.conn.execute(
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES ('s', 'tool', 'x', ?)",
        (1e300,),
    )
    db.conn.commit()
    assert [(w.shape, w.waited) for w in read_waits(tmp_path, NOW - 5 * M)] == [("sleep", 30 * S)]


def test_an_unreadable_state_db_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "hermes"
    (home / "profiles" / "broken").mkdir(parents=True)
    (home / "profiles" / "broken" / "state.db").write_bytes(b"not a database" * 100)
    ok = StateDb(home / "profiles" / "ok" / "state.db")
    ok.session("s", None, NOW - M, NOW)
    ok.call("s", NOW - M, "sleep 50")
    assert [(w.profile, w.shape) for w in read_waits(home, NOW - 60 * M)] == [("ok", "sleep")]

    def boom(*a: object) -> list[object]:
        raise RuntimeError("anything")

    monkeypatch.setattr(state_db, "_wait_calls", boom)
    assert read_waits(home, NOW - 60 * M) == []  # whatever goes wrong: skipped, not raised
    with caplog.at_level(logging.WARNING):
        assert read_waits(tmp_path / "nowhere", NOW) == []
    assert "no state.db under" in caplog.text


def test_odd_tool_calls_are_skipped(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "state.db")
    db.session("s", None, NOW - M, NOW)
    term = "terminal"
    odd = [
        "not json",
        json.dumps({"a": 1}),
        json.dumps([1, {"function": "x"}]),
        json.dumps([{"function": {"name": term, "arguments": "{bad"}}]),
        json.dumps([{"function": {"name": term, "arguments": {"command": 5}}}]),
        json.dumps([{"function": {"name": term, "arguments": {"command": "sleep 1" * 999}}}]),
        "x" * (state_db.MAX_TOOL_CALLS + 1),
    ]
    for raw in odd:
        db.conn.execute(
            "INSERT INTO messages (session_id, role, tool_calls, timestamp)"
            " VALUES ('s', 'assistant', ?, ?)",
            (raw, NOW.timestamp()),
        )
    db.conn.commit()
    assert read_waits(tmp_path, NOW - M) == []


def test_the_adapter_puts_each_hermes_session_on_its_run(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    board = Board(home / "kanban.db")
    ws = tmp_path / "ws"
    for tid in ("t1", "t2", "t3"):
        board.task(tid, "blocked", NOW - 120 * M, assignee="reviewer")
        board.update(tid, workspace_kind="worktree", workspace_path=str(ws / tid))
    board.update("t3", workspace_path=str(ws / "t1"))  # t1 and t3 share a path: neither's
    board.update("t2", workspace_kind="scratch")
    run = board.run("t2", "reviewer", NOW - 60 * M, ended=NOW - 5 * M, outcome="blocked")
    db = StateDb(home / "profiles" / "Reviewer-2" / "state.db")
    db.session("w2", str(ws / "t2"), NOW - 59 * M, NOW - 5 * M)
    db.call("w2", NOW - 30 * M, "sleep 300", result=NOW - 25 * M)
    db.session("w1", str(ws / "t1" / "sub"), NOW - 59 * M, NOW - 5 * M)
    db.call("w1", NOW - 20 * M, "gh run watch 1")
    a = HermesAdapter(
        db_path=board.path,
        spool=None,
        git_state=False,
        state_home=home,
        roles={"reviewer-2": "rev"},
    )
    snap = a.snapshot(NOW)
    assert [(w.session_id, w.task_id, w.role, w.shape, w.waited) for w in snap.waits] == [
        ("hermes-session-reviewer-2-w2", "t2", "rev", "sleep", 5 * M),
        ("hermes-session-reviewer-2-w1", None, "rev", "gh run watch", None),
    ]
    assert {w.source for w in snap.waits} == {"hermes"}
    # The run's profile is the session's: then all its waits are on the run, also the one
    # recorded after the run ended.
    db2 = StateDb(home / "profiles" / "reviewer" / "state.db")
    db2.session("w3", str(ws / "t2"), NOW - 59 * M, NOW - 3 * M)
    db2.call("w3", NOW - 10 * M, "sleep 60")
    db2.call("w3", NOW - 4 * M, "sleep 60")  # after the run ended
    a = HermesAdapter(db_path=board.path, spool=None, git_state=False, state_home=home)
    snap = a.snapshot(NOW)
    found = {(w.session_id, w.at) for w in snap.waits if "reviewer-2" not in w.session_id}
    assert found == {(f"hermes-run-{run}", NOW - 10 * M), (f"hermes-run-{run}", NOW - 4 * M)}


def test_a_failed_read_of_waits_leaves_the_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    board = Board(home / "kanban.db")
    board.task("t1", "ready", NOW - M)

    def boom(*a: object) -> list[object]:
        raise ValueError("odd row")

    monkeypatch.setattr(hermes_adapter, "read_waits", boom)
    a = HermesAdapter(db_path=board.path, spool=None, git_state=False, state_home=home)
    snap = a.snapshot(NOW)
    assert [t.id for t in snap.tasks] == ["t1"] and snap.waits == ()


def test_state_db_is_read_at_most_once_a_minute(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    board = Board(home / "kanban.db")
    reads: list[tuple[datetime, tuple[str, ...]]] = []

    def read(home: Path, since: datetime, sources: tuple[str, ...]) -> list[object]:
        reads.append((since, sources))
        return []

    monkeypatch.setattr(hermes_adapter, "read_waits", read)
    a = HermesAdapter(db_path=board.path, spool=None, git_state=False, state_home=home)
    for at in (NOW, NOW + 30 * S, NOW + 2 * M, NOW - M):
        a.snapshot(at)
    window = 6 * 60 * M
    assert [r[0] for r in reads] == [NOW - window, NOW + 2 * M - window, NOW - M - window]
    assert reads[0][1] == ("kanban", "subagent", "cron")
    a = HermesAdapter(db_path=board.path, spool=None, git_state=False)
    assert a.snapshot(NOW).waits == ()  # no state_home: not read


# --- token usage per session and model (item 1 of #197) ------------------------------------

USAGE_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, source TEXT NOT NULL, cwd TEXT, model TEXT, started_at REAL NOT NULL,
    ended_at REAL, last_activity_at REAL, input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0, cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0, reasoning_tokens INTEGER DEFAULT 0,
    api_call_count INTEGER DEFAULT 0
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, tool_calls TEXT,
    tool_call_id TEXT, content TEXT, tool_name TEXT, timestamp REAL
);
CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL, model TEXT NOT NULL, billing_provider TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '', api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0
);
"""


def usage_session(
    db: StateDb,
    sid: str,
    cwd: str | None,
    *,
    source: str = "kanban",
    started: datetime = NOW - 60 * M,
    last: datetime = NOW - M,
    totals: tuple[int, ...] = (0, 0, 0, 0, 0, 0),
    model: str = "m-main",
) -> None:
    db.conn.execute(
        "INSERT INTO sessions (id, source, cwd, model, started_at, last_activity_at, input_tokens,"
        " output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens, api_call_count)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, source, cwd, model, started.timestamp(), last.timestamp(), *totals),
    )
    db.conn.commit()


def per_model(db: StateDb, sid: str, model: str, *counts: int, task: str = "") -> None:
    db.conn.execute(
        "INSERT INTO session_model_usage (session_id, model, task, api_call_count, input_tokens,"
        " output_tokens, cache_read_tokens, cache_write_tokens, reasoning_tokens)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (sid, model, task, *counts),
    )
    db.conn.commit()


def test_usage_per_session_and_model(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "profiles" / "dev-1" / "state.db", schema=USAGE_SCHEMA)
    usage_session(db, "s1", "/ws/t1", totals=(1, 1, 1, 1, 1, 1))
    per_model(db, "s1", "m-main", 10, 1_000, 200, 50_000, 300, 40)
    per_model(db, "s1", "m-main", 2, 100, 20, 0, 0, 0, task="title")  # another billing row
    per_model(db, "s1", "m-small", 3, 500, 50, 0, 0, 0)
    usage_session(db, "s2", None, totals=(400, 30, 9_000, 0, 0, 4))  # no per-model rows
    usage_session(db, "chat", None, source="telegram", totals=(9, 9, 9, 9, 9, 9))
    usage_session(db, "old", None, started=NOW - 30 * 60 * M, last=NOW - 25 * 60 * M,
                  totals=(9, 9, 9, 9, 9, 9))  # fmt: skip
    usage_session(db, "idle", None)  # nothing used
    found = sorted(
        (u.session, u.model, u.input_tokens, u.output_tokens, u.cache_read_tokens,
         u.cache_write_tokens, u.reasoning_tokens, u.calls)
        for u in state_db.read_usage(tmp_path, NOW - 24 * 60 * M)
    )  # fmt: skip
    assert found == [
        ("s1", "m-main", 1_100, 220, 50_000, 300, 40, 12),
        ("s1", "m-small", 500, 50, 0, 0, 0, 3),
        ("s2", "m-main", 400, 30, 9_000, 0, 0, 4),
    ]


def test_usage_reconciles_session_totals_with_per_model_rows(tmp_path: Path) -> None:
    """As Hermes' Insights: what a session's totals hold beyond all its rows (cumulative
    updates write no row) is the session model's."""
    db = StateDb(tmp_path / "state.db", schema=USAGE_SCHEMA)
    usage_session(db, "s1", None, totals=(1_000, 10, 0, 0, 0, 5))
    per_model(db, "s1", "m-aux", 1, 7, 1, 0, 0, 0, task="vision")  # nothing else has rows
    usage_session(db, "s2", None, totals=(300, 30, 0, 0, 0, 3))
    per_model(db, "s2", "m-main", 2, 200, 20, 0, 0, 0)  # the rest: an absolute update
    usage_session(db, "s3", None, totals=(0, 0, 0, 0, 0, 0))
    per_model(db, "s3", "unknown", 1, 5, 5, 0, 0, 0)  # Hermes' seeded rows
    per_model(db, "s3", "m-main", 1, float("inf"), 4, 0, 0, 0, task="title")  # corrupt
    found = sorted(
        (u.session, u.model or "", u.input_tokens, u.output_tokens, u.calls)
         for u in state_db.read_usage(tmp_path, NOW - 60 * M)
    )  # fmt: skip
    assert found == [
        ("s1", "m-aux", 7, 1, 1),
        ("s1", "m-main", 993, 9, 4),
        ("s2", "m-main", 300, 30, 3),
        ("s3", "", 5, 5, 1),
        ("s3", "m-main", 0, 4, 1),
    ]


def test_usage_of_an_older_schema(tmp_path: Path) -> None:
    old = """
    CREATE TABLE sessions (id TEXT PRIMARY KEY, cwd TEXT, started_at REAL NOT NULL,
                           input_tokens INTEGER, output_tokens INTEGER);
    CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, tool_calls TEXT,
                           tool_call_id TEXT, content TEXT, tool_name TEXT, timestamp REAL);
    """
    db = StateDb(tmp_path / "state.db", schema=old)
    db.conn.execute("INSERT INTO sessions VALUES ('s', NULL, ?, 70, 7)", ((NOW - M).timestamp(),))
    db.conn.commit()
    (u,) = state_db.read_usage(tmp_path, NOW - 60 * M)
    assert (u.model, u.input_tokens, u.output_tokens, u.calls, u.last) == (
        None,
        70,
        7,
        0,
        u.started,
    )


def test_the_adapter_puts_usage_on_the_run_in_kelpies_token_convention(tmp_path: Path) -> None:
    home = tmp_path / "hermes"
    home.mkdir()
    board = Board(home / "kanban.db")
    ws = tmp_path / "ws"
    board.task("t1", "running", NOW - 120 * M, assignee="dev-1")
    board.update("t1", workspace_kind="worktree", workspace_path=str(ws / "t1"))
    run = board.run("t1", "dev-1", NOW - 90 * M)
    db = StateDb(home / "profiles" / "dev-1" / "state.db", schema=USAGE_SCHEMA)
    usage_session(db, "h1", str(ws / "t1"), started=NOW - 80 * M, last=NOW - 2 * M)
    per_model(db, "h1", "m-main", 10, 1_000, 200, 50_000, 300, 40)
    usage_session(db, "h2", None, totals=(5, 5, 0, 0, 0, 1))  # an orchestrator's session
    a = HermesAdapter(db_path=board.path, spool=None, git_state=False, state_home=home)
    snap = a.snapshot(NOW)
    on_run, own = sorted(snap.usage, key=lambda u: u.platform_session_id)
    assert (on_run.session_id, on_run.platform_session_id, on_run.task_id) == (
        f"hermes-run-{run}",
        "h1",
        "t1",
    )
    assert (on_run.role, on_run.owner_id, on_run.model, on_run.source) == (
        "dev",
        "dev-1",
        "m-main",
        "hermes",
    )
    assert (on_run.input_tokens, on_run.cached_tokens, on_run.cache_write_tokens) == (
        51_300,
        50_000,
        300,  # input includes the cached tokens, as Span.input_tokens
    )
    assert (on_run.output_tokens, on_run.reasoning_tokens, on_run.calls) == (200, 40, 10)
    assert on_run.per_session_total and on_run.last_at == NOW - 2 * M
    assert (own.session_id, own.task_id) == ("hermes-session-dev-1-h2", None)


def test_usage_goes_to_the_latest_run_started_by_then(tmp_path: Path) -> None:
    """A never-closed older run of the same task and profile takes no later session."""
    home = tmp_path / "hermes"
    home.mkdir()
    board = Board(home / "kanban.db")
    ws = tmp_path / "ws"
    board.task("t1", "running", NOW - 300 * M, assignee="dev-1")
    board.update("t1", workspace_kind="worktree", workspace_path=str(ws / "t1"))
    old = board.run("t1", "dev-1", NOW - 240 * M, status="crashed")  # never closed
    new = board.run("t1", "dev-1", NOW - 90 * M)
    db = StateDb(home / "profiles" / "dev-1" / "state.db", schema=USAGE_SCHEMA)
    usage_session(db, "h0", str(ws / "t1"), started=NOW - 230 * M, last=NOW - 200 * M,
                  totals=(5, 5, 0, 0, 0, 1))  # fmt: skip
    usage_session(db, "h1", str(ws / "t1"), started=NOW - 80 * M, totals=(5, 5, 0, 0, 0, 1))
    a = HermesAdapter(db_path=board.path, spool=None, git_state=False, state_home=home)
    found = {u.platform_session_id: u.session_id for u in a.snapshot(NOW).usage}
    assert found == {"h0": f"hermes-run-{old}", "h1": f"hermes-run-{new}"}


def test_a_wait_belongs_to_the_run_its_session_started_in(tmp_path: Path) -> None:
    """A worker of a never-closed older run that waits after a newer run started is still
    the older run's, with or without usage of its own."""
    home = tmp_path / "hermes"
    home.mkdir()
    board = Board(home / "kanban.db")
    ws = tmp_path / "ws"
    board.task("t1", "running", NOW - 300 * M, assignee="dev-1")
    board.update("t1", workspace_kind="worktree", workspace_path=str(ws / "t1"))
    old = board.run("t1", "dev-1", NOW - 240 * M, status="crashed")  # never closed
    new = board.run("t1", "dev-1", NOW - 90 * M)
    db = StateDb(home / "profiles" / "dev-1" / "state.db")  # no token columns: no usage
    db.session("w0", str(ws / "t1"), NOW - 230 * M, NOW - 5 * M)
    db.call("w0", NOW - 20 * M, "sleep 300")
    db.session("w1", str(ws / "t1"), NOW - 80 * M, NOW - 5 * M)
    db.call("w1", NOW - 10 * M, "sleep 300")
    a = HermesAdapter(db_path=board.path, spool=None, git_state=False, state_home=home)
    snap = a.snapshot(NOW)
    assert snap.usage == ()
    assert {(w.session_id, w.at) for w in snap.waits} == {
        (f"hermes-run-{old}", NOW - 20 * M),
        (f"hermes-run-{new}", NOW - 10 * M),
    }


def test_corrupt_counts_do_not_drop_the_profile(tmp_path: Path) -> None:
    db = StateDb(tmp_path / "state.db", schema=USAGE_SCHEMA)
    usage_session(db, "s1", None)
    per_model(db, "s1", "m-main", 1, 2**62, 1, 0, 0, 0)
    per_model(db, "s1", "m-main", 1, 2**62, 1, 0, 0, 0, task="title")  # SUM would overflow
    usage_session(db, "s2", None, totals=(10, 1, 0, 0, 0, 1))
    found = sorted((u.session, u.input_tokens) for u in state_db.read_usage(tmp_path, NOW - 60 * M))
    assert found == [("s1", state_db.MAX_COUNT), ("s2", 10)]
