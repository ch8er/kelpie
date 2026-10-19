"""The growth baseline of a snooze (#176), end to end: ``kelpie snooze`` stores none, the
first tick of ``kelpie serve`` that sees the snooze counts the work behind its task on the
full board and records it once, and rule 13 compares with the same stable count."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import Settings, SnoozeReader, load_settings, snooze_reader
from kelpie.core.model import (
    Block,
    Condition,
    ConditionKind,
    Dependency,
    Owner,
    OwnerKind,
    Session,
    Snapshot,
    Snooze,
    Status,
    Task,
)
from kelpie.core.snooze import SnoozeSettings
from kelpie.core.storage import SqliteLedgerStore
from kelpie.rules.needs_you import NeedsYou
from kelpie.snooze import snooze

NOW = datetime(2026, 10, 19, 10, tzinfo=UTC)
M = timedelta(minutes=1)
OWNERS = (Owner("ceo", "owner", OwnerKind.HUMAN), Owner("agent-dev-1", "developer"))
HUMAN = Task("H-1", "Sign the vendor contract", Status.READY, NOW - 48 * 60 * M,
             role="owner", owner_id="ceo")  # fmt: skip
PARKED = Task(
    "A-1", "Integrate the vendor API", Status.BLOCKED, NOW - 30 * 60 * M, role="developer",
    owner_id="agent-dev-1", block=Block("waiting_human: H-1", NOW - 30 * 60 * M, "C-1"),
)  # fmt: skip
NEXT = Task("D-1", "Vendor webhooks", Status.BACKLOG, NOW - 60 * M, role="developer")
CONDITION = Condition("C-1", ConditionKind.HUMAN_TASK, "A-1", {"task": "H-1"}, False,
                      checked_at=NOW)  # fmt: skip
NY = NeedsYou()


def board(now: datetime, *extra: Task, next_task: Task = NEXT, **kw: object) -> Snapshot:
    tasks = (HUMAN, PARKED, next_task, *extra)
    deps = tuple(Dependency(t.id, "H-1") for t in (next_task, *extra))
    return replace(
        Snapshot(now, tasks, OWNERS, deps, (CONDITION,)),
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    db = tmp_path / "kelpie.db"
    (tmp_path / "kelpie.toml").write_text(f'[storage]\npath = "{db}"\n')
    SqliteLedgerStore(db).close()  # kelpie serve ran once
    return load_settings(tmp_path / "kelpie.toml")


def stored(settings: Settings) -> Snooze:
    assert settings.db_path is not None
    store = SqliteLedgerStore(settings.db_path)
    try:
        (found,) = store.snoozes()
        return found
    finally:
        store.close()


def human_items(snapshot: Snapshot) -> list[tuple[str, str | None]]:
    return [
        (i["task_id"], i.get("snooze", {}).get("state"))
        for a in NY.evaluate(snapshot)
        for i in a.params["items"]
    ]


def test_snooze_to_serve_to_rule_13_holds_until_a_real_new_dependent(settings: Settings) -> None:
    # The CLI's offline board has no connector conditions: it would count 1, not 2.
    offline = replace(board(NOW), conditions=())
    _, err, code = snooze(
        settings, "H-1", "3d", "after the release", "ceo", clock=lambda: NOW,
        board=lambda s, n: offline,
    )  # fmt: skip
    assert code == 0 and err == "" and stored(settings).behind_at_snooze is None
    assert settings.db_path is not None
    store = SqliteLedgerStore(settings.db_path)
    try:
        reader = snooze_reader(settings, store)
        # tick 1: the baseline is recorded on the full board, and the snooze holds
        first = reader.apply(board(NOW + M))
        assert first.snoozes["H-1"].behind_at_snooze == 2  # A-1 (by condition) and D-1
        assert human_items(first) == []
        assert store.snoozes()[0].behind_at_snooze == 2
        # the dependent starts work with a live session, then stops: no growth, no flap
        working = replace(NEXT, status=Status.IN_PROGRESS)
        session = Session("S-1", "D-1", "agent-dev-1", NOW, last_heartbeat=NOW + 2 * M)
        live = reader.apply(board(NOW + 2 * M, next_task=working, sessions=(session,)))
        assert human_items(live) == []
        stopped = replace(session, ended_at=NOW + 3 * M)
        assert human_items(reader.apply(board(NOW + 3 * M, sessions=(stopped,)))) == []
        # a real new dependent: H-1 comes back, marked as grown
        more = Task("D-2", "Vendor billing sync", Status.BACKLOG, NOW, role="developer")
        assert human_items(reader.apply(board(NOW + 4 * M, more))) == [("H-1", "grown")]
        # the baseline is written once: a restarted serve keeps it
        again = snooze_reader(settings, store).apply(board(NOW + 5 * M, more))
        assert again.snoozes["H-1"].behind_at_snooze == 2
    finally:
        store.close()


def test_no_baseline_from_a_partial_board_a_missing_task_a_marker_or_an_expired_one() -> None:
    written: list[tuple[str, int]] = []

    def write(s: Snooze, behind: int) -> bool:
        written.append((s.task_id, behind))
        return True

    snoozes = {
        "H-1": Snooze("H-1", NOW + 60 * M, None, None, NOW, "cli"),
        "gone": Snooze("gone", NOW + 60 * M, None, None, NOW, "cli"),
        "old": Snooze("old", NOW - M, None, None, NOW - 60 * M, "cli"),
        "A-1": Snooze("A-1", NOW + 60 * M, None, None, NOW, "marker"),
    }
    reader = SnoozeReader(SnoozeSettings(), lambda now: snoozes, baseline=write)
    partial = reader.apply(board(NOW, unobserved=("hub",)))
    assert written == [] and partial.snoozes["H-1"].behind_at_snooze is None
    old = Task("old", "old", Status.BLOCKED, NOW, role="developer")
    merged = reader.apply(board(NOW, old))
    # only H-1: "gone" is not on the board, "old" expired, A-1's is a marker (never one);
    # "old" depends on H-1 here, so three tasks wait behind it
    assert written == [("H-1", 3)] and merged.snoozes["H-1"].behind_at_snooze == 3
    assert merged.snoozes["A-1"].behind_at_snooze is None


def test_a_failed_baseline_write_is_logged_and_tried_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls: list[int] = []

    def write(s: Snooze, behind: int) -> bool:
        calls.append(behind)
        if len(calls) <= 3:
            raise OSError("disk full")
        return True

    snoozes = {"H-1": Snooze("H-1", NOW + 60 * M, None, None, NOW, "cli")}
    reader = SnoozeReader(SnoozeSettings(), lambda now: snoozes, baseline=write)
    first = reader.apply(board(NOW))
    assert first.snoozes["H-1"].behind_at_snooze is None  # rules hold, no growth yet
    for k in (1, 2):
        assert reader.apply(board(NOW + k * M)).snoozes["H-1"].behind_at_snooze is None
    errors = [r for r in caplog.records if r.levelname == "ERROR"]
    assert len(errors) == 1 and "failed to record the baseline of H-1" in errors[0].message
    assert reader.apply(board(NOW + 3 * M)).snoozes["H-1"].behind_at_snooze == 2
    assert calls == [2, 2, 2, 2]
    assert len([r for r in caplog.records if r.levelname == "ERROR"]) == 1


class Board:
    """A local adapter that reads the board, or fails when ``failing``."""

    name = "board"

    def __init__(self) -> None:
        self.failing = False
        self.tasks: tuple[Task, ...] = (HUMAN, PARKED, NEXT)

    def snapshot(self, now: datetime) -> Snapshot:
        if self.failing:
            raise OSError("board unreadable")
        deps = tuple(Dependency(t.id, "H-1") for t in self.tasks if t.id.startswith("D-"))
        return Snapshot(now, self.tasks, OWNERS, deps)


def test_case_a_a_degraded_tick_records_no_baseline(caplog: pytest.LogCaptureFixture) -> None:
    """The connector that attaches A-1's condition fails on the first tick after the snooze:
    the board alone would count 1 (D-1), not 2, and that wrong baseline would stand for
    good. A tick with a failed adapter or connector step records none."""
    from kelpie.app import merged_snapshot
    from kelpie.metrics import Metrics

    written: list[int] = []
    snoozes = {"H-1": Snooze("H-1", NOW + 600 * M, None, None, NOW, "cli")}
    reader = SnoozeReader(
        SnoozeSettings(), lambda now: snoozes, baseline=lambda s, n: bool(written.append(n)) or True
    )
    connector = {"ok": False}

    def attach(snapshot: Snapshot) -> Snapshot:
        if not connector["ok"]:
            raise RuntimeError("connector down")
        return replace(snapshot, conditions=(*snapshot.conditions, CONDITION))

    adapter = Board()
    clock = iter(NOW + k * M for k in range(10))
    source = merged_snapshot(
        [adapter], Metrics(), lambda: next(clock), enrich=[("conditions", attach)], snoozes=reader
    )  # type: ignore[list-item]
    assert source().snoozes["H-1"].behind_at_snooze is None and written == []
    connector["ok"], adapter.failing = True, True
    assert source().snoozes["H-1"].behind_at_snooze is None and written == []
    adapter.failing = False
    assert source().snoozes["H-1"].behind_at_snooze == 2 and written == [2]


def test_case_b_only_the_tasks_own_part_must_be_observed(caplog: pytest.LogCaptureFixture) -> None:
    """One remote agent offline for good does not hold up a baseline for a local task, or
    for another agent's; the offline agent's own task waits, logged once after an hour and
    shown as "baseline pending" by kelpie why; a missing hub holds up everything."""
    written: list[str] = []
    snoozes = {
        "H-1": Snooze("H-1", NOW + 600 * M, None, None, NOW, "cli"),
        "agent-x:T-1": Snooze("agent-x:T-1", NOW + 600 * M, None, None, NOW, "cli"),
        "agent-y:T-2": Snooze("agent-y:T-2", NOW + 600 * M, None, None, NOW, "cli"),
        "gone": Snooze("gone", NOW + 600 * M, None, None, NOW, "cli"),
    }
    remote = [Task(tid, tid, Status.BLOCKED, NOW) for tid in ("agent-x:T-1", "agent-y:T-2")]

    def write(s: Snooze, n: int) -> bool:
        written.append(s.task_id)
        snoozes[s.task_id] = replace(s, behind_at_snooze=n)
        return True

    reader = SnoozeReader(SnoozeSettings(), lambda now: dict(snoozes), baseline=write)
    reader.apply(board(NOW, *remote, unobserved=("hub",)))
    assert written == []
    reader.apply(board(NOW + M, *remote, unobserved=("agent-x/hermes",)))
    assert sorted(written) == ["H-1", "agent-y:T-2"]
    for k in (61, 62, 120):
        reader.apply(board(NOW + k * M, *remote, unobserved=("agent-x",)))
    pending = {
        r.message.split()[2]: r for r in caplog.records if "no growth baseline yet" in r.message
    }
    assert sorted(pending) == ["agent-x:T-1", "gone"]  # once each
    assert len([r for r in caplog.records if "no growth baseline yet" in r.message]) == 2
    assert "its part of the farm was not observed in full" in pending["agent-x:T-1"].message
    assert "the task is not on the board" in pending["gone"].message
    assert {r.levelname for r in pending.values()} == {"WARNING"}


def test_case_c_growth_is_latched_and_announced_once() -> None:
    """A new dependent A-2 waits on H-1 only through a condition its block reason attaches,
    which comes and goes as A-2 flips between blocked and in progress: the count goes
    2, 1, 2, 1. Growth is latched the first time: exactly one "changed", then nothing."""
    from kelpie.core.alerts import Transition
    from kelpie.core.config import EngineConfig, Mode
    from kelpie.core.engine import Engine
    from tests.fakes import FakeAdapter, FakeAlerter

    db = {"H-1": Snooze("H-1", NOW + 600 * M, None, None, NOW - M, "cli")}

    def baseline(s: Snooze, n: int) -> bool:
        db[s.task_id] = replace(db[s.task_id], behind_at_snooze=n)
        return True

    def growth(s: Snooze, at: datetime, more: int) -> bool:
        if db[s.task_id].grown_at is not None:
            return False
        db[s.task_id] = replace(db[s.task_id], grown_at=at, grown_by=more)
        return True

    reader = SnoozeReader(SnoozeSettings(), lambda now: dict(db), None, baseline, growth)
    other = Task(
        "Q-1", "Pick the DNS provider", Status.BLOCKED, NOW - 600 * M, role="developer",
        owner_id="agent-dev-1",
        block=Block("waiting_human: needs_input: which DNS?", NOW - 600 * M),
    )  # fmt: skip
    blocked = Task(
        "A-2", "Vendor billing", Status.BLOCKED, NOW - 60 * M, role="developer",
        owner_id="agent-dev-1", block=Block("waiting_human: H-1", NOW - 60 * M, "C-2"),
    )  # fmt: skip
    attached = Condition("C-2", ConditionKind.HUMAN_TASK, "A-2", {"task": "H-1"}, False,
                         checked_at=NOW)  # fmt: skip
    working = replace(blocked, status=Status.IN_PROGRESS, block=None)

    def tick(k: int, a2: Task | None) -> Snapshot:
        tasks = (HUMAN, PARKED, other) + ((a2,) if a2 is not None else ())
        conditions = (CONDITION,) + ((attached,) if a2 is blocked else ())
        return reader.apply(Snapshot(NOW + k * 20 * M, tasks, OWNERS, (), conditions))

    config = EngineConfig(
        dry_run=False, modes={"needs_you": Mode.SUGGEST}, state_escalations=(30 * 24 * 60 * M,)
    )
    alerter = FakeAlerter()
    inbox = NeedsYou(default_approver="ceo", critical_after_hours=1000)
    engine = Engine([inbox], adapters=[FakeAdapter()], alerter=alerter, config=config)
    sent = []
    for k, a2 in enumerate([None, blocked, working, blocked, working, blocked]):
        board_k = tick(k, a2)
        before = len(alerter.events)
        engine.tick(lambda board_k=board_k: board_k)
        sent.append([e.transition for e in alerter.events[before:]])
    assert db["H-1"].behind_at_snooze == 1 and db["H-1"].grown_by == 1
    assert sent[0] == [Transition.NEW]  # Q-1, with H-1 in the footer
    assert sent[1] == [Transition.CHANGED]  # H-1 is back: A-2 is blocked behind it
    assert sent[2:] == [[], [], [], []]  # latched: A-2 flipping changes nothing


def test_a_store_that_refuses_a_replaced_snooze_writes_nothing(settings: Settings) -> None:
    assert settings.db_path is not None
    store = SqliteLedgerStore(settings.db_path)
    try:
        first = Snooze("H-1", NOW + 60 * M, None, None, NOW, "cli")
        store.put_snooze(first)
        newer = replace(first, created_at=NOW + M)
        store.put_snooze(newer)
        assert store.set_snooze_baseline(first, 3) is False  # not the stored snooze any more
        assert store.set_snooze_baseline(newer, 2) is True
        assert store.set_snooze_baseline(newer, 5) is False  # once
        assert store.snoozes()[0].behind_at_snooze == 2
        assert store.set_snooze_growth(first, NOW + 2 * M, 1) is False  # replaced
        assert store.set_snooze_growth(newer, NOW + 2 * M, 1) is True
        assert store.set_snooze_growth(newer, NOW + 3 * M, 4) is False  # latched once
        (latched,) = store.snoozes()
        assert (latched.grown_at, latched.grown_by) == (NOW + 2 * M, 1)
        store.put_snooze(replace(newer, created_at=NOW + 4 * M, grown_at=None, grown_by=None))
        assert store.snoozes()[0].grown_at is None  # a new snooze starts over
    finally:
        store.close()


def test_snoozing_a_task_parked_behind_a_human_task_warns(settings: Settings) -> None:
    _, err, code = snooze(settings, "A-1", "3d", clock=lambda: NOW, board=lambda s, n: board(NOW))
    assert code == 0
    assert err == (
        "kelpie: warning: snoozing A-1 holds nothing; snooze the human task H-1 instead "
        "(snoozed anyway)\n"
    )
    assert stored(settings).task_id == "A-1"
    _, err, _ = snooze(settings, "H-1", "3d", clock=lambda: NOW, board=lambda s, n: board(NOW))
    assert err == ""


def test_a_v8_snooze_is_read_then_recounted_after_the_upgrade(tmp_path: Path) -> None:
    """A snooze #207's CLI stored kept a baseline counted on its offline board, without the
    connectors' conditions: 0 for H-1, whose waiter A-1 is linked by a condition. Read-only
    readers show it on the old schema; the v9 upgrade drops that baseline, and the first
    complete tick counts 2 and latches no growth."""
    import sqlite3

    from kelpie.core import storage
    from kelpie.snooze import list_snoozes
    from kelpie.why import read_snoozes

    path = tmp_path / "kelpie.db"
    db = sqlite3.connect(path)
    db.executescript(
        "".join(storage.MIGRATIONS[v] for v in range(1, 9)) + "PRAGMA user_version = 8;"
    )
    db.execute(
        "INSERT INTO snoozes (task_id, until_at, reason, snoozed_by, created_at, source,"
        " behind_at_snooze) VALUES ('H-1', ?, 'after the release', 'ceo', ?, 'cli', 0)",
        (storage._ts(NOW + 600 * M), storage._ts(NOW - 60 * M)),
    )
    db.commit()
    db.close()
    (tmp_path / "kelpie.toml").write_text(f'[storage]\npath = "{path}"\n')
    settings = load_settings(tmp_path / "kelpie.toml")
    out, err, code = list_snoozes(settings, clock=lambda: NOW)
    assert code == 0 and err == "" and out.startswith("H-1 until ")  # read on schema 8
    problems: list[str] = []
    (old,) = read_snoozes(path, NOW, problems).values()
    assert problems == [] and old.behind_at_snooze is None and old.grown_at is None
    store = SqliteLedgerStore(path)  # kelpie serve upgrades it
    try:
        (upgraded,) = store.snoozes()
        assert upgraded.behind_at_snooze is None  # the offline count is gone
        reader = snooze_reader(settings, store)
        merged = reader.apply(board(NOW))
        (counted,) = store.snoozes()
        assert (counted.behind_at_snooze, counted.grown_at) == (2, None)
        assert human_items(merged) == []  # held: no false growth
        assert human_items(reader.apply(board(NOW + M))) == []
    finally:
        store.close()


def test_the_snooze_readers_never_read_a_newer_schema(tmp_path: Path) -> None:
    """``oldest`` lets the snooze readers read an older schema, never a newer one (after a
    downgrade the columns may mean something else)."""
    import sqlite3

    from kelpie.core.storage import SCHEMA_VERSION, SNOOZES_SINCE, open_readonly

    path = tmp_path / "kelpie.db"
    SqliteLedgerStore(path).close()
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError, match="newer"):
        open_readonly(path, 1000, oldest=SNOOZES_SINCE)
