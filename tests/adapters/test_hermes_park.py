"""Hermes ``wait`` (park in ``scheduled``), wake (``unblock``) and ``comment``, through a fake
Hermes CLI that does to a synthetic ``kanban.db`` what the real commands do."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes import park as park_module
from kelpie.adapters.hermes.adapter import HermesError
from kelpie.adapters.hermes.park import (
    MAX_TEXT,
    clean_text,
    comment_args,
    decode_wait,
    encode_wait,
    kanban_env,
    load_key,
    unblock_args,
)
from kelpie.adapters.hermes.stop import HermesCliError
from kelpie.connectors.github.client import GitHubClient
from kelpie.connectors.github.connector import GitHubConnector
from kelpie.core.conditions import check_task_status
from kelpie.core.executors import wait_conditions
from kelpie.core.model import Condition, ConditionKind, Snapshot, Status, Task
from kelpie.core.ports import SupportsWait, UnsupportedAction
from kelpie.rules.dispatch_gate import DispatchGate
from kelpie.rules.wait_condition import WaitCondition
from tests.adapters.kanban import Board
from tests.connectors.fake_github import TOKEN, Clock, serve

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)

FAKE_CLI = """\
#!{python}
import json, os, sqlite3, sys, time

with open({log!r}, "a") as fh:
    fh.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
try:
    mode = open({log!r} + ".mode").read().strip()
except OSError:
    mode = "ok"
if mode == "fail":
    print("kanban: nope", file=sys.stderr)
    sys.exit(1)
verb = sys.argv[2]
rest = sys.argv[sys.argv.index("--") + 1:]
task = rest[0]
conn = sqlite3.connect(os.environ["HERMES_KANBAN_DB"])
now = int(time.time())
if verb == "schedule":
    reason = " ".join(rest[1:])
    if mode == "mangle":  # a reason Hermes stored differently than Kelpie wrote it
        reason = reason[:40]
    # Hermes writes the "SCHEDULED: <reason>" comment first; it stays even if it then refuses.
    conn.execute("INSERT INTO task_events (task_id, kind, created_at) VALUES (?, 'commented', ?)",
                 (task, now))
    conn.commit()
    raw = os.environ.get("HERMES_KANBAN_RUN_ID")
    expected = int(raw) if os.environ.get("HERMES_KANBAN_TASK") == task and raw else None
    sql = ("UPDATE tasks SET status='scheduled' WHERE id=?"
           " AND status IN ('todo','ready','running','blocked')")
    params = [task]
    if expected is not None:
        sql += " AND current_run_id=?"
        params.append(expected)
    if mode == "noop" or conn.execute(sql, params).rowcount != 1:
        print("cannot schedule " + task, file=sys.stderr)
        sys.exit(1)
    ended = conn.execute("UPDATE task_runs SET ended_at=?, outcome='scheduled'"
                         " WHERE task_id=? AND ended_at IS NULL", (now, task)).rowcount
    if not ended and reason:  # a ready task: Hermes records a zero-length run (synthesize)
        who = conn.execute("SELECT assignee FROM tasks WHERE id=?", (task,)).fetchone()[0]
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at,"
                     " outcome, summary) VALUES (?, ?, 'scheduled', ?, ?, 'scheduled', ?)",
                     (task, who, now, now, reason))
    conn.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (task,))
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at)"
                 " VALUES (?, 'scheduled', ?, ?)",
                 (task, json.dumps({{"reason": reason}}), now))
elif verb == "unblock":
    if os.environ.get("HERMES_KANBAN_TASK"):
        print("kanban unblock is orchestrator-only", file=sys.stderr)
        sys.exit(1)
    sql = "UPDATE tasks SET status='ready' WHERE id=? AND status IN ('blocked','scheduled')"
    if conn.execute(sql, (task,)).rowcount != 1:
        print("cannot unblock " + task, file=sys.stderr)
        sys.exit(1)
    conn.execute("INSERT INTO task_events (task_id, kind, created_at)"
                 " VALUES (?, 'unblocked', ?)", (task, now))
elif verb == "comment":
    pass
conn.commit()
"""


@pytest.fixture
def board(tmp_path: Path) -> Iterator[Board]:
    b = Board(tmp_path / "kanban.db")
    yield b
    b.close()


@pytest.fixture
def cli(tmp_path: Path) -> tuple[str, Path]:
    log = tmp_path / "cli.jsonl"
    exe = tmp_path / "bin" / "hermes"
    exe.parent.mkdir()
    exe.write_text(FAKE_CLI.format(python=sys.executable, log=str(log)))
    exe.chmod(0o755)
    return str(exe), log


def calls(log: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def make(board: Board, cli: str) -> HermesAdapter:
    key = board.path.parent / "wait.key"
    a = HermesAdapter(db_path=board.path, spool=None, git_state=False, cli=cli, wait_key=key)
    a.snapshot(datetime.now(UTC))  # the statuses a park is checked against
    return a


KEY = b"k" * 32


CONDS = (
    Condition(
        "dispatch_gate:t1:p1", ConditionKind.TASK_STATUS, "t1", {"task": "p1", "status": "done"}
    ),
    Condition(
        "ready:t1:pr_merged:example/repo#7",
        ConditionKind.PR_MERGED,
        "t1",
        {"repo": "example/repo", "pr": 7},
    ),
)


def status(board: Board, task: str = "t1") -> str:
    return board.conn.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()[0]


# --- the marker ----------------------------------------------------------------------------


W = timedelta(seconds=10)  # how late Hermes may record a park after Kelpie issued it


def enc(text: str, conds: tuple[Condition, ...], task: str = "t1", key: bytes = KEY) -> str:
    return encode_wait(text, conds, task, key, NOW)


def dec(reason: str, task: str = "t1", key: bytes | None = KEY, at: datetime = NOW) -> object:
    return decode_wait(reason, task, key, at, W)


def test_marker_round_trip_and_clean_text() -> None:
    reason = enc("Prereq https://github.com/example/repo/pull/7 red\x07", CONDS)
    assert "https://" not in reason and "example/repo#7" in reason and "\x07" not in reason
    assert dec(reason) == [(c.id, c.kind, c.params) for c in CONDS]
    assert dec("no marker") is None
    assert dec(reason + " " + reason) is None  # two markers: not ours
    assert dec(reason + " trailing words") is None  # the marker must end the reason
    assert dec(reason, key=None) is None  # no key: nothing can be verified
    assert len(clean_text("x" * 5000)) == MAX_TEXT
    assert clean_text("a [kelpie-wait:v1:abc.def] b") == "a kelpie wait:v1:abc.def] b"


def test_a_dispatch_gate_park_carries_no_commit_the_connector_checked() -> None:
    """Issue #147: the marker of a sha-less CI condition must not pin the head seen at park
    time; a condition the platform registered with a sha keeps it. The input is what
    ``GitHubConnector.check`` returns, so this fails if it writes a ``sha`` back."""
    head, pinned = "a" * 40, "b" * 40
    repo = "example/repo"
    free = Condition(
        "ready:t1:ci_green:free", ConditionKind.CI_STATUS, "t1", {"repo": repo, "pr": 7}
    )
    bound = replace(free, id="ready:t1:ci_green:bound", params={**free.params, "sha": pinned})
    with serve() as gh:
        gh.pull(repo, 7, sha=head)
        for sha in (head, pinned):
            gh.ci(repo, sha, statuses=[{"state": "failure", "updated_at": "2026-03-02T11:50:00Z"}])
        connector = GitHubConnector(GitHubClient(gh.url, TOKEN, clock=Clock(NOW)), [repo])
        checked = tuple(
            replace(connector.check(c), registered_at=NOW - timedelta(hours=1))
            for c in (free, bound)
        )
    assert checked[0].params["head_sha"] == head and "sha" not in checked[0].params
    task = Task("t1", "Redo it", Status.READY, NOW - timedelta(hours=1), role="developer")
    [wait] = DispatchGate().evaluate(Snapshot(now=NOW, tasks=(task,), conditions=checked))
    parked = enc("Prerequisites are not met", tuple(wait_conditions(wait)))
    assert dec(parked) == [
        (
            "ready:t1:ci_green:bound",
            ConditionKind.CI_STATUS,
            {"repo": repo, "pr": 7, "sha": pinned},
        ),
        ("ready:t1:ci_green:free", ConditionKind.CI_STATUS, {"repo": repo, "pr": 7}),
    ]


def test_a_marker_recorded_later_is_a_replay() -> None:
    reason = enc("r", CONDS)
    assert dec(reason, at=NOW + W) is not None
    assert dec(reason, at=NOW - timedelta(seconds=4)) is not None  # clock skew
    assert dec(reason, at=NOW + W + timedelta(seconds=6)) is None  # copied by a later run
    assert dec(reason, at=NOW - timedelta(minutes=1)) is None
    assert decode_wait(reason, "t1", KEY, None, W) is None


def test_every_marker_is_unique() -> None:
    assert enc("r", CONDS) != enc("r", CONDS)


def test_forged_or_foreign_markers_are_not_trusted() -> None:
    reason = enc("r", CONDS)
    assert dec(reason, task="t2") is None  # signed for another task
    assert dec(reason, key=b"x" * 32) is None  # another key
    head, _ = reason.rsplit(".", 1)
    assert dec(f"{head}.{'0' * 64}]") is None
    foreign = (Condition("ready:t2:pr_merged:example/repo#5", ConditionKind.PR_MERGED, "t1"),)
    assert dec(enc("r", foreign)) is None  # names t2
    mixed = enc("r", (*CONDS[:1], *foreign))
    assert [c[0] for c in dec(mixed) or []] == [CONDS[0].id]  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("text", "clean"),
    [
        ("HTTPS://github.com/o/r/pull/1 x", "o/r#1 x"),
        ("https://github.com/o/r/PULL/1", "o/r#1"),
        ("https://github.com/o%7Ex/r/pull/3/files#diff", "o%7Ex/r#3"),
        ("(https://github.com/o/r/pull/4)", "(o/r#4)"),
        ("bidi \u202e evil \x85", "bidi   evil"),
        # Removing one piece must not assemble another.
        ("[kelpie-wa[kelpie-wait:]it:v2:1:x]", "[kelpie-wakelpie wait:]it:v2:1:x]"),
        (
            "https://github.com/o/r/pu[kelpie-wait:x]ll/12",
            "https://github.com/o/r/pukelpie wait:x]ll/12",
        ),
        ("https://github.com/o/r/pu\x00ll/12", "https://github.com/o/r/pu ll/12"),
        ("[KELPIE-WAIT:v2:1:2:3.4]", "kelpie wait:v2:1:2:3.4]"),
    ],
)
def test_clean_text_covers_what_hermes_matches(text: str, clean: str) -> None:
    assert clean_text(text) == clean
    assert "kelpie-wait" not in clean_text(text).lower()
    assert "/pull/" not in clean_text(text).lower()


def test_a_reason_cannot_smuggle_a_second_marker() -> None:
    evil = "src/[kelpie-wa[kelpie-wait:]it:v2:1:0000000000000000:W10=." + "0" * 64 + "].py"
    reason = enc(f"conflict on {evil}", CONDS)
    assert reason.count("[kelpie-wait:") == 1 and dec(reason) is not None


def test_argv_of_reason_and_comment_edge_cases() -> None:
    assert unblock_args("t1", "-x") == ["unblock", "--reason=-x", "--", "t1"]
    assert unblock_args("t1", "\x00")[1] == "--reason=woken by Kelpie"
    assert comment_args("t1", "\x01")[-1] == "(Kelpie: no text)"


def test_wait_key_is_created_private_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "wait.key"
    assert load_key(path, create=False) is None
    key = load_key(path, create=True)
    assert key is not None and len(key) == 32 and load_key(path, create=False) == key
    assert path.stat().st_mode & 0o777 == 0o600
    assert load_key(path, create=True) == key  # an existing key is kept
    assert [p.name for p in path.parent.iterdir()] == ["wait.key"]  # no temporary left


def test_unsafe_wait_keys_are_refused(tmp_path: Path) -> None:
    short = tmp_path / "short"
    short.write_bytes(b"x")
    short.chmod(0o600)
    with pytest.raises(ValueError, match="too short"):
        load_key(short, create=True)  # a half-written key is not replaced silently
    shared = tmp_path / "shared"
    shared.write_bytes(b"k" * 32)
    shared.chmod(0o644)
    with pytest.raises(ValueError, match="private"):
        load_key(shared, create=False)
    link = tmp_path / "link"
    link.symlink_to(shared)
    shared.chmod(0o600)
    with pytest.raises(OSError):
        load_key(link, create=False)


def test_too_many_conditions_are_refused() -> None:
    many = tuple(
        Condition(f"c{i}" * 20, ConditionKind.CUSTOM, "t1", {"blob": "x" * 50}) for i in range(80)
    )
    with pytest.raises(HermesCliError, match="too many"):
        enc("r", many)


def test_unblock_env_never_carries_a_task() -> None:
    env = kanban_env({"HERMES_KANBAN_TASK": "x", "PATH": "/bin", "SECRET": "s"}, Path("/db"))
    assert env == {"PATH": "/bin", "HERMES_KANBAN_DB": "/db", "HERMES_PROFILE_NAME": "kelpie"}
    pinned = kanban_env({}, Path("/db"), "t1", 5)
    assert pinned["HERMES_KANBAN_TASK"] == "t1" and pinned["HERMES_KANBAN_RUN_ID"] == "5"


# --- park and read back ----------------------------------------------------------------------


def test_ready_task_is_parked_and_read_back_as_waiting(board: Board, cli: tuple[str, Path]) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    assert isinstance(a, SupportsWait)
    a.wait("t1", CONDS, "Prerequisites not met: PR example/repo#7 is not merged")
    assert status(board) == "scheduled"
    (call,) = calls(log)
    argv = call["argv"]
    assert isinstance(argv, list) and argv[:3] == ["kanban", "schedule", "--"] and argv[3] == "t1"
    env = call["env"]
    assert isinstance(env, dict) and "HERMES_KANBAN_TASK" not in env  # ready: nothing to pin
    snap = a.snapshot(datetime.now(UTC))
    (task,) = snap.tasks
    assert task.status is Status.WAITING
    assert [(c.id, c.kind, c.task_id) for c in snap.conditions] == [
        (c.id, c.kind, "t1") for c in CONDS
    ]
    assert all(c.registered_at == task.status_since for c in snap.conditions)


def test_running_task_is_pinned_to_its_run(board: Board, cli: tuple[str, Path]) -> None:
    exe, log = cli
    board.task("t1", "running", NOW - timedelta(hours=2), assignee="dev-1")
    rid = board.run("t1", "dev-1", NOW - timedelta(minutes=20))
    board.update("t1", current_run_id=rid)
    make(board, exe).wait("t1", CONDS[:1], "conflict")
    env = calls(log)[0]["env"]
    assert isinstance(env, dict)
    assert env["HERMES_KANBAN_TASK"] == "t1" and env["HERMES_KANBAN_RUN_ID"] == str(rid)
    assert status(board) == "scheduled"


@pytest.mark.parametrize("hermes_status", ["todo", "review", "blocked", "done", "triage"])
def test_other_statuses_are_not_parked(
    board: Board, cli: tuple[str, Path], hermes_status: str
) -> None:
    exe, log = cli
    board.task("t1", hermes_status, NOW - timedelta(hours=1), assignee="dev-1")
    with pytest.raises(UnsupportedAction):
        make(board, exe).wait("t1", CONDS, "r")
    assert calls(log) == []


def test_refused_or_ineffective_park_is_an_error(board: Board, cli: tuple[str, Path]) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    with pytest.raises(HermesError, match="unknown"):
        a.wait("nope", CONDS, "r")
    Path(f"{log}.mode").write_text("fail")
    with pytest.raises(HermesError, match="exit 1"):
        a.wait("t1", CONDS, "r")
    Path(f"{log}.mode").write_text("noop")
    with pytest.raises(HermesError):
        a.wait("t1", CONDS, "r")


def test_a_scheduled_task_kelpie_did_not_park_stays_backlog(board: Board) -> None:
    board.task("t1", "scheduled", NOW - timedelta(hours=1), assignee="dev-1")
    board.event("t1", "scheduled", NOW - timedelta(minutes=30), {"reason": "wait for Monday"})
    a = HermesAdapter(db_path=board.path, spool=None, git_state=False)
    snap = a.snapshot(NOW)
    assert snap.tasks[0].status is Status.BACKLOG and snap.conditions == ()


# --- wake ------------------------------------------------------------------------------------


def test_wake_unblocks_only_tasks_kelpie_parked(board: Board, cli: tuple[str, Path]) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    board.task("t2", "scheduled", NOW - timedelta(hours=1), assignee="dev-1")
    board.event("t2", "scheduled", NOW - timedelta(minutes=30), {"reason": "a human's"})
    a = make(board, exe)
    a.wait("t1", CONDS, "r")
    a.requeue("t1", "Every condition holds: woken by https://github.com/example/repo/pull/7")
    assert status(board) == "ready"
    argv = calls(log)[-1]["argv"]
    assert isinstance(argv, list) and argv[:2] == ["kanban", "unblock"]
    assert "https://" not in " ".join(argv) and argv[-2:] == ["--", "t1"]
    with pytest.raises(UnsupportedAction):
        a.requeue("t2", "r")
    a.requeue("t1", "r")  # already woken: nothing to do, no error


def test_a_failed_wake_of_a_task_still_parked_is_an_error(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    a.wait("t1", CONDS, "r")
    Path(f"{log}.mode").write_text("fail")
    with pytest.raises(HermesError):
        a.requeue("t1", "r")  # still scheduled: a real failure


def test_park_then_condition_holds_then_rule_2_wakes(board: Board, cli: tuple[str, Path]) -> None:
    exe, _ = cli
    board.task("p1", "running", NOW - timedelta(hours=3), assignee="dev-2")
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    a.wait("t1", CONDS[:1], "parent p1 not done")
    board.update("p1", status="done", completed_at=datetime.now(UTC))
    snap = check_task_status(a.snapshot(datetime.now(UTC) + timedelta(seconds=5)))
    [wake] = WaitCondition().evaluate(snap)
    assert wake.task_id == "t1"
    a.requeue("t1", wake.reason)
    assert status(board) == "ready"


# --- comment ---------------------------------------------------------------------------------


def test_comment_is_authored_by_kelpie_without_pr_urls(board: Board, cli: tuple[str, Path]) -> None:
    exe, log = cli
    board.task("t1", "running", NOW - timedelta(hours=1), assignee="dev-1")
    make(board, exe).comment("t1", "Heads-up: see https://github.com/example/repo/pull/9 too")
    argv = calls(log)[0]["argv"]
    assert argv == [
        "kanban",
        "comment",
        "--author",
        "kelpie",
        "--",
        "t1",
        "Heads-up: see example/repo#9 too",
    ]
    Path(f"{log}.mode").write_text("fail")
    with pytest.raises(HermesError):
        make(board, exe).comment("t1", "x")


def test_every_command_is_signed_with_the_author(board: Board, cli: tuple[str, Path]) -> None:
    """Hermes signs a schedule's or unblock's comment with $HERMES_PROFILE_NAME, never with
    the profile Kelpie's environment names."""
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    key = board.path.parent / "wait.key"
    a = HermesAdapter(
        db_path=board.path, spool=None, git_state=False, cli=exe, wait_key=key, author="ops-bot"
    )
    a.snapshot(datetime.now(UTC))
    a.wait("t1", CONDS[:1], "conflict")
    a.requeue("t1", "done")
    a.comment("t1", "hello")
    done = calls(log)
    assert [c["argv"][1] for c in done] == ["schedule", "unblock", "comment"]
    assert {c["env"]["HERMES_PROFILE_NAME"] for c in done} == {"ops-bot"}  # type: ignore[index]
    assert done[-1]["argv"][2:4] == ["--author", "ops-bot"]


def test_commands_start_from_the_given_environment(board: Board, cli: tuple[str, Path]) -> None:
    """``cli_environ`` (the self-test's throwaway home) replaces Kelpie's own environment as
    the base of every command; the allow-list and the pinned variables still apply."""
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    base = {"PATH": os.environ["PATH"], "HERMES_HOME": "/throwaway/home", "SECRET": "x"}
    a.cli_environ = base
    a.wait("t1", CONDS[:1], "conflict")
    a.requeue("t1", "done")
    a.comment("t1", "hello")
    done = calls(log)
    assert [c["argv"][1] for c in done] == ["schedule", "unblock", "comment"]
    for call in done:
        env = call["env"]
        assert env["HERMES_HOME"] == "/throwaway/home" and "SECRET" not in env  # type: ignore[operator,index]
        assert env["HERMES_KANBAN_DB"] == str(board.path)  # type: ignore[index]


def test_park_survives_events_hermes_writes_afterwards(board: Board, cli: tuple[str, Path]) -> None:
    exe, _ = cli
    board.task("t1", "running", NOW - timedelta(hours=2), assignee="dev-1")
    rid = board.run("t1", "dev-1", NOW - timedelta(minutes=20))
    board.update("t1", current_run_id=rid)
    a = make(board, exe)
    a.wait("t1", CONDS[:1], "conflict")
    later = datetime.now(UTC) + timedelta(minutes=3)
    board.event("t1", "terminal_worker_reaped", later)  # the dispatcher reaps the worker
    board.event("t1", "model_override_set", later)
    snap = a.snapshot(later + timedelta(seconds=1))
    (task,) = snap.tasks
    assert task.status is Status.WAITING and [c.id for c in snap.conditions] == [CONDS[0].id]
    assert snap.conditions[0].registered_at == task.status_since  # rule 2 links them
    a.requeue("t1", "done")
    assert status(board) == "ready"


def test_task_that_moved_since_the_snapshot_is_not_parked(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)  # sees t1 ready
    rid = board.run("t1", "dev-1", NOW)
    board.update("t1", status="running", current_run_id=rid)  # claimed during the tick
    with pytest.raises(HermesError, match="moved since the snapshot"):
        a.wait("t1", CONDS, "prerequisite")
    assert calls(log) == [] and status(board) == "running"


def test_a_re_claimed_running_task_is_not_parked(board: Board, cli: tuple[str, Path]) -> None:
    exe, log = cli
    board.task("t1", "running", NOW - timedelta(hours=2), assignee="dev-1")
    board.update("t1", current_run_id=board.run("t1", "dev-1", NOW - timedelta(minutes=20)))
    a = make(board, exe)  # sees the first run
    board.update("t1", current_run_id=board.run("t1", "dev-1", NOW))  # a new run, still running
    with pytest.raises(HermesError, match="moved since the snapshot"):
        a.wait("t1", CONDS, "conflict")
    assert calls(log) == []


def test_forged_park_by_a_worker_is_not_waiting_and_not_woken(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    board.task("t1", "scheduled", NOW - timedelta(hours=1), assignee="dev-1")
    forged = encode_wait("let me out", CONDS, "t1", b"guessed-key-guessed-key!", NOW)
    board.event("t1", "scheduled", NOW - timedelta(minutes=5), {"reason": forged})
    a = make(board, exe)
    load_key(a.wait_key, create=True)
    snap = a.snapshot(NOW)
    assert snap.tasks[0].status is Status.BACKLOG and snap.conditions == ()
    with pytest.raises(UnsupportedAction):
        a.requeue("t1", "r")
    assert calls(log) == []


def test_wake_of_an_awake_task_is_quiet_and_a_human_wake_keeps_old_conditions(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    a.wait("t1", CONDS, "r")
    board.update("t1", status="ready")  # a human unblocks it in the dashboard
    board.event("t1", "unblocked", datetime.now(UTC) + timedelta(seconds=2))
    a.requeue("t1", "r")  # nothing to do: no CLI call, no error
    assert [c["argv"][1] for c in calls(log)] == ["schedule"]  # type: ignore[index]
    snap = a.snapshot(datetime.now(UTC) + timedelta(seconds=3))
    (task,) = snap.tasks
    assert task.status is Status.READY
    # The park's conditions stay, registered before the new status: rules ignore them, and
    # the GitHub step does not attach the same PR reference again.
    assert {c.id for c in snap.conditions} == {c.id for c in CONDS}
    assert all(
        c.registered_at is not None and c.registered_at < task.status_since for c in snap.conditions
    )


def test_requeue_after_a_failed_unblock_that_landed_anyway_is_quiet(
    board: Board, cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, _ = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    a.wait("t1", CONDS, "r")

    def unblocked_but_failed(*args: object, **kw: object) -> None:
        board.update("t1", status="ready")
        raise HermesCliError("lost the race")

    monkeypatch.setattr("kelpie.adapters.hermes.adapter.run_kanban", unblocked_but_failed)
    a.requeue("t1", "r")  # woken in between: nothing left to do
    assert status(board) == "ready"


def test_park_that_did_not_take_is_an_error(
    board: Board, cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, _ = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    monkeypatch.setattr("kelpie.adapters.hermes.adapter.run_kanban", lambda *a, **k: None)
    with pytest.raises(HermesError, match="not parked"):
        a.wait("t1", CONDS, "r")


def test_a_park_that_cannot_be_read_back_is_undone(board: Board, cli: tuple[str, Path]) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    Path(f"{log}.mode").write_text("mangle")
    with pytest.raises(HermesError, match="not parked"):
        a.wait("t1", CONDS, "r")
    assert [c["argv"][1] for c in calls(log)] == ["schedule", "unblock"]  # type: ignore[index]
    assert {c["env"]["HERMES_PROFILE_NAME"] for c in calls(log)} == {"kelpie"}  # type: ignore[index]
    assert status(board) == "ready"


def test_a_replayed_park_is_not_kelpies(
    board: Board, cli: tuple[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    exe, _ = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    a.wait("t1", CONDS, "r")
    reason = board.conn.execute(
        "SELECT payload FROM task_events WHERE kind='scheduled'"
    ).fetchone()[0]
    a.requeue("t1", "woken")
    # A later run copies the reason from the SCHEDULED: comment and parks itself with it.
    board.update("t1", status="scheduled")
    later = datetime.now(UTC) + timedelta(hours=1)
    board.event("t1", "scheduled", later, json.loads(reason))
    snap = a.snapshot(later + timedelta(seconds=1))
    assert snap.tasks[0].status is Status.BACKLOG
    assert "does not verify" in caplog.text
    with pytest.raises(UnsupportedAction):
        a.requeue("t1", "r")


def test_requeue_of_a_task_kelpie_never_parked_is_unsupported(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    board.task("t1", "blocked", NOW - timedelta(hours=1), assignee="dev-1")
    with pytest.raises(UnsupportedAction, match="not parked by Kelpie"):
        make(board, exe).requeue("t1", "r")
    assert calls(log) == []


def test_a_missing_key_leaves_parks_unrecognised_with_a_warning(
    board: Board, cli: tuple[str, Path], caplog: pytest.LogCaptureFixture
) -> None:
    exe, _ = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    a.wait("t1", CONDS, "r")
    a.wait_key.unlink()
    snap = a.snapshot(datetime.now(UTC) + timedelta(seconds=1))
    assert snap.tasks[0].status is Status.BACKLOG
    assert "does not verify" in caplog.text and str(a.wait_key) in caplog.text


def test_a_parked_ready_task_does_not_make_its_assignee_look_active(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, _ = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    a.wait("t1", CONDS, "r")
    synthesized = board.conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0]
    assert synthesized == 1  # Hermes' zero-length run for the scheduled ready task
    assert a.snapshot(datetime.now(UTC)).sessions == ()


def test_a_task_moved_out_of_scheduled_by_a_human_counts_as_woken(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    a.wait("t1", CONDS, "r")
    board.update("t1", status="todo")  # dragged in the dashboard: no unblocked event
    snap = a.snapshot(datetime.now(UTC) + timedelta(seconds=2))
    assert snap.tasks[0].status is Status.BACKLOG
    assert {c.id for c in snap.conditions} == {c.id for c in CONDS}  # kept, not re-derived
    a.requeue("t1", "r")  # already awake: quiet
    assert [c["argv"][1] for c in calls(log)] == ["schedule"]  # type: ignore[index]


def test_a_task_missing_from_the_snapshot_is_not_parked(
    board: Board, cli: tuple[str, Path]
) -> None:
    exe, log = cli
    a = make(board, exe)  # snapshot before the task exists
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    with pytest.raises(HermesError, match="not in the last snapshot"):
        a.wait("t1", CONDS, "r")
    assert calls(log) == []


def test_a_failed_undo_says_to_unblock_by_hand(
    board: Board, cli: tuple[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    Path(f"{log}.mode").write_text("mangle")
    real = park_module.run_kanban

    def unblock_fails(cli: str, db: Path, args: list[str], **kw: object) -> None:
        if args[0] == "unblock":
            raise HermesCliError("kanban: nope")
        real(cli, db, args, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr("kelpie.adapters.hermes.adapter.run_kanban", unblock_fails)
    with pytest.raises(HermesError, match="unblock it by hand"):
        a.wait("t1", CONDS, "r")


def test_a_malformed_blob_with_a_good_mac_is_not_a_park() -> None:
    import hashlib
    import hmac as hmac_module

    blob = "bm90LWpzb24="  # base64 of "not-json"
    issued, nonce = str(int(NOW.timestamp())), "0" * 16
    msg = f"kelpie-wait:v2\nt1\n{issued}\n{nonce}\n{blob}".encode()
    mac = hmac_module.new(KEY, msg, hashlib.sha256).hexdigest()
    assert dec(f"r [kelpie-wait:v2:{issued}:{nonce}:{blob}.{mac}]") is None


def test_a_key_of_another_user_or_not_a_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = tmp_path / "k"
    key.write_bytes(b"k" * 32)
    key.chmod(0o600)
    monkeypatch.setattr("os.geteuid", lambda: 12345)
    with pytest.raises(ValueError, match="belong to the user"):
        load_key(key, create=False)
    monkeypatch.undo()
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(ValueError, match="regular file"):
        load_key(fifo, create=False)  # does not hang


def test_key_creation_is_logged_and_works_without_hard_links(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def no_links(src: object, dst: object) -> None:
        raise OSError("hard links not supported")

    monkeypatch.setattr("os.link", no_links)
    key = load_key(tmp_path / "k", create=True)
    assert key is not None and len(key) == 32
    assert "created the key that signs Kelpie's parks" in caplog.text
    assert [p.name for p in tmp_path.iterdir()] == ["k"]


def test_cli_missing_or_slow_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(HermesCliError, match="not found"):
        park_module.run_kanban("no-such-hermes-cli", tmp_path / "db", ["comment"], timeout=1)
    slow = tmp_path / "slow"
    slow.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(5)\n")
    slow.chmod(0o755)
    with pytest.raises(HermesCliError, match="timed out"):
        park_module.run_kanban(str(slow), tmp_path / "db", ["comment"], timeout=0.2)


def test_an_unusable_key_makes_the_park_an_error(board: Board, cli: tuple[str, Path]) -> None:
    exe, log = cli
    board.task("t1", "ready", NOW - timedelta(hours=1), assignee="dev-1")
    a = make(board, exe)
    a.wait_key.write_bytes(b"x")
    a.wait_key.chmod(0o600)
    with pytest.raises(HermesError, match="wait key"):
        a.wait("t1", CONDS, "r")
    assert calls(log) == []
