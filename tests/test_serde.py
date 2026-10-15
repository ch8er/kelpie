from datetime import timedelta

import pytest

from kelpie.core.model import Status
from kelpie.core.serde import parse_datetime, parse_duration, snapshot_from_dict


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
