"""Snoozes: the model, ``until:`` markers, ``--until`` values, ``[snooze]`` and the store."""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from kelpie.app import ConfigError, SnoozeReader, load_settings, merged_snapshot
from kelpie.core import storage
from kelpie.core.model import Block, Snapshot, Snooze, Status, Task, active_snooze
from kelpie.core.serde import snapshot_from_dict
from kelpie.core.snooze import (
    EXPIRED_KEPT,
    PRUNE_AFTER,
    SnoozeSettings,
    format_when,
    load_snooze,
    marker_snooze,
    marker_snoozes,
    parse_marker,
    parse_until,
    snooze_json,
    until_problem,
    with_snoozes,
)
from kelpie.core.storage import (
    SchemaOutdated,
    SqliteLedgerStore,
    load_snoozes,
    open_readonly,
    read_snoozes,
)
from kelpie.metrics import Metrics

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
BERLIN = ZoneInfo("Europe/Berlin")
ON = SnoozeSettings(marker=True)  # markers are off by default


def snooze(task_id: str = "T-101", until: datetime | None = None, **kw: object) -> Snooze:
    fields: dict[str, object] = {
        "reason": "deferred to the release",
        "by": "alice",
        "created_at": NOW,
    }
    fields.update(kw)
    return Snooze(task_id, until or NOW + timedelta(days=3), **fields)  # type: ignore[arg-type]


def blocked(task_id: str, reason: str, since: datetime = NOW - timedelta(hours=2)) -> Task:
    return Task(
        task_id, "Synthetic task", Status.BLOCKED, since, block=Block(reason=reason, since=since)
    )


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)  # type: ignore[misc]


# -- model ------------------------------------------------------------------------------------


def test_active_snooze_ends_exactly_at_until() -> None:
    until = NOW + timedelta(hours=1)
    snap = Snapshot(now=NOW, snoozes={"T-101": snooze(until=until)})
    assert active_snooze(snap, "T-101") is not None
    early = Snapshot(now=until - timedelta(microseconds=1), snoozes=snap.snoozes)
    assert active_snooze(early, "T-101") is not None
    assert active_snooze(Snapshot(now=until, snoozes=snap.snoozes), "T-101") is None
    assert active_snooze(snap, "T-102") is None
    assert Snapshot(now=NOW).snoozes == {}


def _fixture(snoozes: object) -> Snapshot:
    return snapshot_from_dict({"now": "2026-10-19T12:00:00+00:00", "snoozes": snoozes})


ENTRY = {
    "task_id": "T-101",
    "until": "2026-10-25T00:00:00+00:00",
    "reason": None,
    "by": "alice",
    "created_at": "2026-10-19T09:00:00+00:00",
    "behind_at_snooze": 2,
}


def test_fixtures_can_carry_snoozes() -> None:
    s = _fixture({"T-101": ENTRY}).snoozes["T-101"]
    assert s.until == utc(2026, 10, 25) and s.behind_at_snooze == 2
    assert s.source == "cli"
    assert _fixture({"T-101": {**ENTRY, "source": "marker"}}).snoozes["T-101"].source == "marker"


@pytest.mark.parametrize(
    ("snoozes", "message"),
    [
        ([ENTRY], "must be a mapping by task id"),
        ({"T-101": "2026-10-25"}, "T-101: must be a mapping"),
        ({"T-101": {**ENTRY, "source": "agent"}}, "must be one of"),
        ({"T-999": ENTRY}, "T-999: task_id is 'T-101', not the key"),
    ],
)
def test_fixture_snoozes_are_validated(snoozes: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _fixture(snoozes)


# -- markers ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "until"),
    [
        # A bare date: through that day.
        ("CEO deferred R2 backups until:2026-10-25", utc(2026, 10, 26)),
        ("manual QA pass, until:2026-10-25T09:30.", utc(2026, 10, 25, 9, 30)),
        ("until:2026-10-25T09:30Z", utc(2026, 10, 25, 9, 30)),
        ("(until:2026-10-25T09:30:15)", utc(2026, 10, 25, 9, 30, 15)),
        (
            "until:2026-10-25T09:30+02:00",
            datetime(2026, 10, 25, 9, 30, tzinfo=timezone(timedelta(hours=2))),
        ),
        (
            "until:2026-10-25T09:30-0500;",
            datetime(2026, 10, 25, 9, 30, tzinfo=timezone(timedelta(hours=-5))),
        ),
        ("UNTIL:2026-10-26", utc(2026, 10, 27)),
    ],
)
def test_valid_markers(reason: str, until: datetime) -> None:
    assert parse_marker(reason) == (until, ())


def test_offset_marker_is_compared_in_utc() -> None:
    (at, _) = parse_marker("until:2026-10-25T09:30+02:00")
    assert at is not None and at.astimezone(UTC) == utc(2026, 10, 25, 7, 30)


def test_markers_without_an_offset_are_in_report_timezone() -> None:
    (day, _) = parse_marker("until:2026-10-25", BERLIN)
    assert day == datetime(2026, 10, 26, tzinfo=BERLIN)  # 2026-10-25 23:00 UTC (CET)
    assert day is not None and day.astimezone(UTC) == utc(2026, 10, 25, 23)
    (at, _) = parse_marker("until:2026-10-20T09:00", BERLIN)
    assert at is not None and at.astimezone(UTC) == utc(2026, 10, 20, 7)  # CEST
    (fixed, _) = parse_marker("until:2026-10-25T09:00Z", BERLIN)
    assert fixed == utc(2026, 10, 25, 9)


def test_the_latest_of_several_markers_wins() -> None:
    reason = "until:2026-10-27 for QA, until:2026-10-25 for the backups, until:2026-02-30"
    assert parse_marker(reason) == (utc(2026, 10, 28), ("until:2026-02-30",))


@pytest.mark.parametrize(
    ("reason", "invalid"),
    [
        ("until:2026-13-01", ("until:2026-13-01",)),
        ("until:2026-10-25T25:00", ("until:2026-10-25T25:00",)),
        ("until:release", ("until:release",)),
        ("until:25.10.2026", ("until:25.10.2026",)),
        ("until:2026-10-25+02:00", ("until:2026-10-25+02:00",)),
        ("until:9999-12-31", ("until:9999-12-31",)),  # the day after is out of range
        ("until:9999-12-31T23:00-05:00", ("until:9999-12-31T23:00-05:00",)),
        ("no marker: until the release", ()),
        ("wait_until:2026-10-25 and x.until:2026-10-25", ()),
    ],
)
def test_invalid_or_absent_markers(reason: str, invalid: tuple[str, ...]) -> None:
    assert parse_marker(reason) == (None, invalid)


@pytest.mark.parametrize(
    "reason",
    [
        'test failed: "AssertionError: valid until:2026-12-01"',
        "the issue says “hold until:2026-12-01”",
        "the comment said ‘hold until:2026-12-01’",  # noqa: RUF001
        "log line `cert valid until:2026-12-01`",
    ],
)
def test_a_marker_in_quoted_text_is_not_read(reason: str) -> None:
    assert parse_marker(reason) == (None, ())


def test_a_marker_outside_the_quotes_still_counts() -> None:
    reason = 'CEO’s call: until:2026-10-25; error was "token valid until:2026-12-01"'  # noqa: RUF001
    assert parse_marker(reason) == (utc(2026, 10, 26), ())


def test_marker_snoozes_from_the_board() -> None:
    since = NOW - timedelta(hours=5)
    snap = Snapshot(
        now=NOW,
        tasks=(
            blocked("T-1", "R2 for backups, deferred until:2026-10-25", since),
            blocked("T-2", "until:2026-13-40"),
            blocked("T-3", f"until:{(NOW + timedelta(days=40)).date()}"),
            blocked("T-4", f"until:{(NOW - EXPIRED_KEPT - timedelta(days=2)).date()}"),
            blocked("T-5", f"until:{(NOW - timedelta(days=2)).date()}"),
            Task("T-6", "until:2026-10-25 in a title", Status.READY, NOW),
        ),
    )
    found, problems = marker_snoozes(snap, ON)
    assert sorted(found) == ["T-1", "T-5"]  # T-4 expired too long ago, T-6 is not blocked
    t1 = found["T-1"]
    assert (t1.source, t1.created_at, t1.reason, t1.by) == ("marker", since, None, None)
    assert t1.until == utc(2026, 10, 26) and t1.behind_at_snooze is None
    assert not found["T-5"].active(NOW)
    assert [(p.task_id, p.why) for p in problems] == [
        ("T-2", "not a valid date"),
        (
            "T-3",
            "ends more than 30 days after the block was set ([snooze] marker_max_days); "
            "the last date allowed is 2026-11-18",
        ),
    ]


def test_markers_are_off_by_default() -> None:
    snap = Snapshot(now=NOW, tasks=(blocked("T-1", "until:2026-10-25"), blocked("T-2", "until:x")))
    assert SnoozeSettings().marker is False
    assert marker_snoozes(snap, SnoozeSettings()) == ({}, ())


def test_the_marker_cap_counts_from_when_the_block_was_set() -> None:
    since = NOW - timedelta(days=190)
    far = blocked("T-1", f"until:{(since + timedelta(days=200)).date()}", since)
    # Ten days left now, inside any window counted from today: still never honoured.
    for now in (NOW, NOW + timedelta(days=5)):
        snooze_, problems = marker_snooze(far, now, ON)
        assert snooze_ is None and "marker_max_days" in problems[0].why
    near = blocked("T-2", f"until:{(NOW + timedelta(days=18)).date()}", NOW - timedelta(days=1))
    snooze_, problems = marker_snooze(near, NOW, ON)
    assert snooze_ is not None and problems == ()
    wide = SnoozeSettings(marker=True, max_days=365, marker_max_days=365)
    assert marker_snooze(far, NOW, wide)[0] is not None


def test_a_stored_snooze_beats_a_marker_even_expired() -> None:
    snap = Snapshot(
        now=NOW,
        tasks=(blocked("T-1", "until:2026-10-25"), blocked("T-2", "until:2026-10-26")),
    )
    stored = {
        "T-1": snooze("T-1", until=NOW - timedelta(days=1)),
        "T-9": snooze("T-9"),  # not on the board: kept (the board may be partial)
        "T-old": snooze("T-old", until=NOW - EXPIRED_KEPT - timedelta(seconds=1)),
    }
    merged, problems = with_snoozes(snap, stored, ON)
    assert problems == ()
    assert sorted(merged.snoozes) == ["T-1", "T-2", "T-9"]
    assert merged.snoozes["T-1"].source == "cli" and not merged.snoozes["T-1"].active(NOW)
    assert merged.snoozes["T-2"].source == "marker"
    with pytest.raises(TypeError):
        merged.snoozes["T-3"] = snooze("T-3")  # type: ignore[index]


# -- --until, formatting and [snooze] -----------------------------------------------------------


def test_parse_until() -> None:
    assert parse_until("3d", NOW) == NOW + timedelta(days=3)
    assert parse_until(" 1d12h ", NOW) == NOW + timedelta(days=1, hours=12)
    assert parse_until("2026-10-25", NOW) == utc(2026, 10, 26)  # through that day
    assert parse_until("2026-10-25", NOW, BERLIN) == datetime(2026, 10, 26, tzinfo=BERLIN)
    assert parse_until("2026-10-25T09:00", NOW, BERLIN) == datetime(2026, 10, 25, 9, tzinfo=BERLIN)
    assert parse_until("2026-10-25T09:00+00:00", NOW, BERLIN) == utc(2026, 10, 25, 9)
    for bad in ("tomorrow", "3", "3w", "2026-10-32", ""):
        with pytest.raises(ValueError, match="--until must be"):
            parse_until(bad, NOW)


@pytest.mark.parametrize("value", ["9999-12-31T23:00-05:00", "9999-12-31", "999999999d"])
def test_an_extreme_until_is_out_of_range(value: str) -> None:
    with pytest.raises(ValueError, match="out of range"):
        parse_until(value, NOW)


def test_the_cli_and_a_marker_agree_on_a_date() -> None:
    tz = ZoneInfo("America/New_York")
    (marker, _) = parse_marker("until:2026-11-01", tz)  # a DST change ends that night there
    assert parse_until("2026-11-01", NOW, tz) == marker == datetime(2026, 11, 2, tzinfo=tz)
    assert marker is not None and marker.astimezone(UTC) == utc(2026, 11, 2, 5)  # EST
    (at, _) = parse_marker("until:2026-11-01T09:00", tz)
    assert parse_until("2026-11-01T09:00", NOW, tz) == at


def test_format_when_and_json() -> None:
    at = utc(2026, 10, 25, 23)
    assert format_when(at) == "2026-10-25 23:00 UTC"
    assert format_when(at, BERLIN) == "2026-10-26 00:00 CET (2026-10-25 23:00 UTC)"
    assert format_when(at, ZoneInfo("Europe/London")) == "2026-10-25 23:00 UTC"
    data = snooze_json(snooze(until=at, behind_at_snooze=1), NOW, BERLIN)
    assert data["until_text"] == "2026-10-26 00:00 CET (2026-10-25 23:00 UTC)"
    assert (data["task_id"], data["active"], data["source"]) == ("T-101", True, "cli")


def test_a_date_exactly_max_days_ahead_is_allowed() -> None:
    settings = SnoozeSettings()  # NOW is 2026-10-19; 90 days later is 2027-01-17
    last = parse_until("2027-01-17", NOW)
    assert until_problem(last, NOW, settings, through_day=True) is None
    assert until_problem(last, NOW, settings) is not None  # as an instant it is 90d 12h away
    assert until_problem(parse_until("2027-01-18", NOW), NOW, settings, through_day=True) == (
        "is more than 90 days ahead ([snooze] max_days); the last date allowed is 2027-01-17"
    )
    assert until_problem(NOW + timedelta(days=91), NOW, settings) == (
        "is more than 90 days ahead ([snooze] max_days); the latest allowed is 2027-01-17 12:00 UTC"
    )
    # Dates are counted in [report] timezone: there it is already 2026-10-20.
    late = utc(2026, 10, 19, 23, 30)
    berlin = SnoozeSettings(tz=BERLIN)
    assert until_problem(parse_until("2027-01-18", late, BERLIN), late, berlin, True) is None
    assert "2027-01-17" in str(until_problem(parse_until("2027-01-18", late), late, settings, True))


def test_a_marker_on_the_last_allowed_day_is_honoured() -> None:
    since = NOW - timedelta(hours=2)  # the block was set on 2026-10-19, at 10:00 UTC
    snooze_, problems = marker_snooze(blocked("T-1", "until:2026-11-18", since), NOW, ON)
    assert snooze_ is not None and problems == ()
    _, (problem,) = marker_snooze(blocked("T-2", "until:2026-11-19", since), NOW, ON)
    assert problem.token == "until:2026-11-19"
    assert problem.why.endswith("; the last date allowed is 2026-11-18")
    exact = blocked("T-3", "until:2026-11-18T10:00Z", since)
    snooze_, problems = marker_snooze(exact, NOW, ON)  # exactly 30 days after the block
    assert snooze_ is not None and problems == ()
    _, (problem,) = marker_snooze(blocked("T-4", "until:2026-11-18T10:01Z", since), NOW, ON)
    assert problem.why.endswith("; the latest allowed is 2026-11-18 10:00 UTC")


def test_until_problem_bounds() -> None:
    settings = SnoozeSettings()
    assert until_problem(NOW, NOW, settings) == "is not in the future"
    assert until_problem(NOW + timedelta(days=90), NOW, settings) is None
    assert "more than 90 days" in str(
        until_problem(NOW + timedelta(days=90, seconds=1), NOW, settings)
    )
    assert "more than 7 days" in str(
        until_problem(NOW + timedelta(days=8), NOW, SnoozeSettings(max_days=7))
    )


def test_load_snooze() -> None:
    assert load_snooze(None) == SnoozeSettings(90, False, 30, UTC)
    assert load_snooze(None, BERLIN).tz is BERLIN
    assert load_snooze({"max_days": 30, "marker": True}) == SnoozeSettings(30, True, 30)
    assert load_snooze({"max_days": 14}).marker_max_days == 14  # the default follows it
    assert load_snooze({"max_days": 60, "marker_max_days": 60}).marker_max_days == 60
    for raw, message in (
        ([], "must be a table"),
        ({"days": 3}, "unknown"),
        ({"max_days": "30"}, "whole number"),
        ({"max_days": True}, "whole number"),
        ({"max_days": 0}, "between 1 and 365"),
        ({"max_days": 366}, "between 1 and 365"),
        ({"marker_max_days": 0}, "marker_max_days must be between 1 and 90"),
        ({"max_days": 20, "marker_max_days": 21}, "between 1 and 20"),
        ({"marker": "yes"}, "true or false"),
    ):
        with pytest.raises(ValueError, match=message):
            load_snooze(raw)


def test_snooze_section_in_the_config(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text(
        '[report]\ntimezone = "Europe/Berlin"\n[snooze]\nmax_days = 14\nmarker = true\n'
    )
    settings = load_settings(path).snooze
    assert (settings.max_days, settings.marker, settings.marker_max_days) == (14, True, 14)
    assert settings.tz == BERLIN
    path.write_text("[snooze]\nmax_days = 0\n")
    with pytest.raises(ConfigError, match="max_days"):
        load_settings(path)


# -- store ------------------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> SqliteLedgerStore:
    s = SqliteLedgerStore(tmp_path / "kelpie.db")
    yield s  # type: ignore[misc]
    s.close()


def test_store_round_trip_and_replace(store: SqliteLedgerStore) -> None:
    first = snooze(behind_at_snooze=2)
    store.put_snooze(first)
    assert store.snoozes() == [first]
    later = snooze(until=NOW + timedelta(days=10), reason="  new  ", by=" bob ", source="chat")
    store.put_snooze(later)
    (saved,) = store.snoozes()
    assert (saved.until, saved.reason, saved.by, saved.source) == (
        later.until,
        "new",
        "bob",
        "chat",
    )
    assert saved.behind_at_snooze is None and saved.created_at == NOW


def test_store_trims_and_empties(store: SqliteLedgerStore) -> None:
    store.put_snooze(snooze(reason="x" * 600, by=""))
    (saved,) = store.snoozes()
    assert saved.reason == "x" * 500 and saved.by is None


def test_store_refuses_markers_and_naive_times(store: SqliteLedgerStore) -> None:
    with pytest.raises(ValueError, match="markers"):
        store.put_snooze(snooze(source="marker"))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.put_snooze(snooze(until=datetime(2026, 10, 25)))


def test_store_delete(store: SqliteLedgerStore) -> None:
    store.put_snooze(snooze("T-1"))
    store.put_snooze(snooze("T-2"))
    assert store.delete_snooze("T-1") is True
    assert store.delete_snooze("T-1") is False
    assert [s.task_id for s in store.snoozes()] == ["T-2"]


def test_store_load_keeps_recently_expired_and_prune_drops_old(store: SqliteLedgerStore) -> None:
    store.put_snooze(snooze("active", until=NOW + timedelta(days=1)))
    store.put_snooze(snooze("recent", until=NOW - EXPIRED_KEPT + timedelta(seconds=1)))
    store.put_snooze(snooze("week", until=NOW - EXPIRED_KEPT))
    store.put_snooze(snooze("month", until=NOW - PRUNE_AFTER - timedelta(seconds=1)))
    assert sorted(store.load_snoozes(NOW)) == ["active", "recent"]
    assert [s.task_id for s in store.snoozes(NOW)] == ["active"]
    assert store.prune_snoozes(NOW) == 1
    assert [s.task_id for s in store.snoozes()] == ["week", "recent", "active"]


def test_an_existing_database_is_migrated(tmp_path: Path) -> None:
    path = tmp_path / "v7.db"
    db = sqlite3.connect(path)
    db.executescript(
        "".join(storage.MIGRATIONS[v] for v in range(1, 8)) + "PRAGMA user_version = 7;"
    )
    db.execute(
        "INSERT INTO states (id, rule, since, opened_at, last_seen, level, severity, summary,"
        " mode) VALUES ('s1', 'r', ?, ?, ?, 0, 'warning', 'x', 'observe')",
        (storage._ts(NOW), storage._ts(NOW), storage._ts(NOW)),
    )
    db.commit()
    db.close()
    with pytest.raises(SchemaOutdated):
        open_readonly(path, 1000)  # a reader never migrates
    s = SqliteLedgerStore(path)
    s.put_snooze(snooze())
    assert [st.id for st in s.open_states()] == ["s1"]
    s.close()
    conn = open_readonly(path, 1000)
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == storage.SCHEMA_VERSION == 9
        assert list(load_snoozes(conn, NOW)) == ["T-101"]
    finally:
        conn.close()


def test_read_only_connection_reads_snoozes(store: SqliteLedgerStore, tmp_path: Path) -> None:
    store.put_snooze(snooze("T-1", until=NOW + timedelta(days=2)))
    store.put_snooze(snooze("T-2", until=NOW + timedelta(days=1)))
    conn = open_readonly(tmp_path / "kelpie.db", 1000)
    try:
        assert [s.task_id for s in read_snoozes(conn)] == ["T-2", "T-1"]
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM snoozes")
    finally:
        conn.close()


# -- snapshot assembly --------------------------------------------------------------------------


class Board:
    name = "board"

    def __init__(self, *tasks: Task) -> None:
        self.tasks = tasks

    def snapshot(self, now: datetime) -> Snapshot:
        return Snapshot(now=now, tasks=self.tasks)


def test_assembly_fills_snoozes_and_a_cli_snooze_beats_a_marker(
    store: SqliteLedgerStore, caplog: pytest.LogCaptureFixture
) -> None:
    store.put_snooze(snooze("T-1", until=NOW + timedelta(days=2), reason="person"))
    board = Board(
        blocked("T-1", "until:2026-10-30"),
        blocked("T-2", "until:2026-10-30"),
        blocked("T-3", "until:2026-99-99"),
    )
    reader = SnoozeReader(ON, store.load_snoozes, store.prune_snoozes)
    source = merged_snapshot([board], Metrics(), lambda: NOW, snoozes=reader)  # type: ignore[list-item]
    with caplog.at_level(logging.WARNING, logger="kelpie.app"):
        snap = source()
        source()
    assert snap.snoozes["T-1"].reason == "person" and snap.snoozes["T-1"].source == "cli"
    assert snap.snoozes["T-2"].source == "marker"
    assert "T-3" not in snap.snoozes
    warned = [r for r in caplog.records if "snooze marker" in r.getMessage()]
    assert len(warned) == 1 and "T-3" in warned[0].getMessage()  # logged once
    off = SnoozeReader(SnoozeSettings(), store.load_snoozes)
    assert list(merged_snapshot([board], Metrics(), lambda: NOW, snoozes=off)().snoozes) == ["T-1"]  # type: ignore[list-item]


def test_assembly_without_a_reader_has_no_snoozes() -> None:
    snap = merged_snapshot([Board(blocked("T-1", "until:2026-10-30"))], Metrics(), lambda: NOW)()  # type: ignore[list-item]
    assert snap.snoozes == {}


def test_a_failing_read_keeps_the_last_snoozes_and_logs_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = {"n": 0}

    def flaky(now: datetime) -> dict[str, Snooze]:
        calls["n"] += 1
        if calls["n"] in (2, 3):
            raise sqlite3.OperationalError("disk I/O error")
        return {"T-9": snooze("T-9", until=NOW + timedelta(days=1), reason=str(calls["n"]))}

    reader = SnoozeReader(ON, flaky)
    board = Board(blocked("T-1", "until:2026-10-30"))
    source = merged_snapshot([board], Metrics(), lambda: NOW, snoozes=reader)  # type: ignore[list-item]
    with caplog.at_level(logging.ERROR, logger="kelpie.app"):
        snaps = [source() for _ in range(4)]
    assert [sorted(s.snoozes) for s in snaps] == [["T-1", "T-9"]] * 4
    assert [s.snoozes["T-9"].reason for s in snaps] == ["1", "1", "1", "4"]
    assert caplog.text.count("snooze store failed to read") == 1


def test_a_failing_prune_does_not_stop_the_read(caplog: pytest.LogCaptureFixture) -> None:
    def prune(now: datetime) -> int:
        raise sqlite3.OperationalError("database is locked")

    reader = SnoozeReader(ON, lambda now: {"T-9": snooze("T-9")}, prune)
    with caplog.at_level(logging.ERROR, logger="kelpie.app"):
        snap = reader.apply(Snapshot(now=NOW))
    assert list(snap.snoozes) == ["T-9"]
    assert "snooze store failed to prune" in caplog.text


def test_prune_runs_at_most_hourly() -> None:
    pruned: list[datetime] = []

    def prune(now: datetime) -> int:
        pruned.append(now)
        return 0

    reader = SnoozeReader(ON, lambda now: {}, prune)
    for minutes in (0, 30, 61, 90):
        reader.apply(Snapshot(now=NOW + timedelta(minutes=minutes)))
    assert pruned == [NOW, NOW + timedelta(minutes=61)]
