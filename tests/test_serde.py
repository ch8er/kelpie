from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.model import Status
from kelpie.core.serde import parse_datetime, parse_duration, parse_when, snapshot_from_dict


def test_parse_duration() -> None:
    assert parse_duration("1h30m") == timedelta(hours=1, minutes=30)
    assert parse_duration("2d") == timedelta(days=2)
    assert parse_duration(90) == timedelta(seconds=90)
    with pytest.raises(ValueError):
        parse_duration("soon")
    with pytest.raises(ValueError):
        parse_duration(True)


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError):
        parse_datetime("2026-03-02T10:00:00")


def test_snapshot_roundtrip() -> None:
    snap = snapshot_from_dict(
        {
            "now": "2026-03-02T10:00:00+00:00",
            "tasks": [
                {
                    "id": "T-1",
                    "title": "demo",
                    "status": "blocked",
                    "status_since": "2026-03-02T09:00:00+00:00",
                    "block": {"reason": "ci", "since": "2026-03-02T09:00:00+00:00"},
                }
            ],
            "dependencies": [{"task_id": "T-2", "depends_on": "T-1"}],
        }
    )
    task = snap.task("T-1")
    assert task is not None and task.status is Status.BLOCKED
    assert task.block is not None and task.block.reason == "ci"


def test_unknown_field_rejected() -> None:
    with pytest.raises(ValueError, match="unknown fields"):
        snapshot_from_dict({"now": "2026-03-02T10:00:00+00:00", "taks": []})


# --- parse_when (kelpie fix --at) ---------------------------------------------------------

WHEN_NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("now", WHEN_NOW),
        (" NOW ", WHEN_NOW),
        ("-30m", datetime(2026, 3, 1, 11, 30, tzinfo=UTC)),
        ("-2H", datetime(2026, 3, 1, 10, tzinfo=UTC)),
        ("- 2h", datetime(2026, 3, 1, 10, tzinfo=UTC)),
        ("-1d2h", datetime(2026, 2, 28, 10, tzinfo=UTC)),
        ("2026-03-01T08:00:00+00:00", datetime(2026, 3, 1, 8, tzinfo=UTC)),
        ("2026-03-01T17:00", datetime(2026, 3, 1, 8, tzinfo=UTC)),  # Tokyo, as typed
        ("2026-03-01", datetime(2026, 2, 28, 15, tzinfo=UTC)),  # midnight in Tokyo
    ],
)
def test_parse_when(when: str, expected: datetime) -> None:
    from zoneinfo import ZoneInfo

    assert parse_when(when, WHEN_NOW, ZoneInfo("Asia/Tokyo")) == expected


@pytest.mark.parametrize("when", ["yesterday", "-0m", "-", "-2x", "+2h", "2h", "", " "])
def test_parse_when_refuses(when: str) -> None:
    with pytest.raises(ValueError, match="now"):
        parse_when(when, WHEN_NOW, UTC)


@pytest.mark.parametrize(
    ("when", "zone"),
    [
        ("-800000d", "Asia/Tokyo"),
        ("-99999999999d", "Asia/Tokyo"),
        ("0001-01-01T00:00", "Asia/Tokyo"),  # out of range in UTC
        ("0001-01-01T00:00Z", "America/New_York"),  # ...or in [report] timezone
        ("9999-12-31T23:59Z", "Asia/Tokyo"),
    ],
)
def test_parse_when_out_of_range(when: str, zone: str) -> None:
    from zoneinfo import ZoneInfo

    with pytest.raises(ValueError, match="out of range"):
        parse_when(when, WHEN_NOW, ZoneInfo(zone))


def test_parse_when_dst_wall_times_resolve_to_the_first() -> None:
    from zoneinfo import ZoneInfo

    berlin = ZoneInfo("Europe/Berlin")
    skipped = parse_when("2026-03-29T02:30", WHEN_NOW, berlin)  # the clock jumps 02:00 -> 03:00
    assert skipped.astimezone(UTC) == datetime(2026, 3, 29, 1, 30, tzinfo=UTC)
    twice = parse_when("2026-10-25T02:30", WHEN_NOW, berlin)  # 02:00-03:00 happens twice
    assert twice.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
