"""One copy of the helpers the adapters share (#248 D13), and their behaviour."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters import _common
from kelpie.adapters.claude_code import adapter as claude_code
from kelpie.adapters.hermes import adapter as hermes
from kelpie.adapters.hermes import stop as hermes_stop
from kelpie.adapters.issues import github, linear, mapping
from kelpie.adapters.openclaw import adapter as openclaw
from kelpie.adapters.openclaw import stop as openclaw_stop
from kelpie.core import actlock
from kelpie.core.actlock import ActLock


def test_the_twins_are_one_copy() -> None:
    assert hermes.format_age is claude_code.format_age is openclaw.format_age
    assert hermes.format_age is _common.format_age
    assert claude_code._utc is openclaw._utc is _common.epoch_utc
    assert hermes_stop._kill_group is hermes_stop.kill_group is openclaw_stop._kill_group
    assert linear._time is github._time is mapping.parse_time


@pytest.mark.parametrize(
    ("age", "shown"),
    [(-5, "0s"), (59, "59s"), (60, "1m"), (3599, "59m"), (3600, "1h"), (86400 * 3 + 5, "3d")],
)
def test_format_age(age: int, shown: str) -> None:
    assert _common.format_age(timedelta(seconds=age)) == shown


@pytest.mark.parametrize("value", [True, "1700000000", None, 1e30, float("nan")])
def test_epoch_utc_refuses_what_is_no_time(value: object) -> None:
    assert _common.epoch_utc(value) is None


def test_epoch_utc() -> None:
    assert _common.epoch_utc(0) == datetime(1970, 1, 1, tzinfo=UTC)


def test_warn_once(caplog: pytest.LogCaptureFixture) -> None:
    warned: set[str] = set()
    logger = logging.getLogger("kelpie.test")
    with caplog.at_level(logging.WARNING, "kelpie.test"):
        _common.warn_once(logger, warned, "k", "x %s", 1)
        _common.warn_once(logger, warned, "k", "x %s", 2)
    assert [r.getMessage() for r in caplog.records] == ["x 1"]


def test_spool_act_locks(tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    assert _common.spool_act_locks("claude-code", None, tmp_path) == ()
    assert _common.spool_act_locks("claude-code", spool, None) == ()
    (lock,) = _common.spool_act_locks("claude-code", spool, tmp_path)
    assert lock == ActLock(f"claude-code ({spool})", actlock.beside(spool), True)
