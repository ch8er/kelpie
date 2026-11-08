"""Spool values of the wrong type or range are skipped or cut, never raised.

Found by property-based tests of the spool parsers: a line whose ``type`` is a JSON list
or object (unhashable) and a tool call lasting thousands of years each raised out of the
adapter's snapshot, leaving every rule blind to that adapter until the spool rotated.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.claude_code import adapter as claude_code
from kelpie.adapters.hermes import adapter as hermes
from kelpie.adapters.openclaw import adapter as openclaw

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=1)
AT = (NOW - timedelta(minutes=1)).timestamp()
EONS = 10**17  # milliseconds: past timedelta's range


@pytest.mark.parametrize("kind", [[], {}, [1], {"a": 1}])
@pytest.mark.parametrize("parse", [claude_code.parse_line, openclaw.parse_line])
def test_a_type_that_is_not_text_is_skipped(
    parse: Callable[[str, datetime], object], kind: object
) -> None:
    assert parse(json.dumps({"type": kind, "at": AT, "session_id": "s1"}), CUTOFF) is None


def _spool(tmp_path: Path, **rec: object) -> Path:
    spool = tmp_path / "spool.jsonl"
    spool.write_text(json.dumps({"at": AT, **rec}) + "\n")
    return spool


@pytest.mark.parametrize("millis", [10**16, EONS, 1e300, float("inf")])
def test_hermes_cuts_a_huge_duration(tmp_path: Path, millis: float) -> None:
    spool = _spool(tmp_path, type="tool", run_id="1", task_id="t_1", tool="x", duration_ms=millis)
    adapter = hermes.HermesAdapter(db_path=tmp_path / "kanban.db", spool=spool)
    (span,) = [s for s in adapter._spans(CUTOFF) if s.tool]
    assert span.end is not None
    assert span.end - span.start == timedelta(milliseconds=hermes.MAX_DURATION_MS)


def test_claude_code_cuts_a_huge_duration(tmp_path: Path) -> None:
    spool = _spool(tmp_path, type="tool", session_id="s1", tool="Bash", duration_ms=EONS)
    event = claude_code.parse_line(spool.read_text().strip(), CUTOFF)
    assert event is not None and event.duration_ms == claude_code.MAX_DURATION_MS
    adapter = claude_code.ClaudeCodeAdapter(
        tasks_dir=tmp_path / "tasks", spool=spool, stop_dir=None, process_check=lambda p, r: None
    )
    adapter.snapshot(NOW)  # used to raise OverflowError
