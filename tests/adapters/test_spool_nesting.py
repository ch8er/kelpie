"""A JSON value nested too deep to decode is skipped like any other malformed line.

Python's JSON decoder raises ``RecursionError`` (not ``ValueError``) at roughly 10,000
levels, which fits in one 16 KiB spool line. Agents can write the spools, the Claude Code
task files and the Hermes board, so one such line must not take an adapter's whole
snapshot (and every rule watching it) offline.
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
from kelpie.traces import semconv
from kelpie.traces.args import args_hash, args_hash_of_attribute
from kelpie.traces.scrub import scrub_line

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=1)
DEEP = "[" * 15_000  # nested past the decoder's limit, under every line cap


def test_deep_line_fits_the_line_caps() -> None:
    assert len(DEEP.encode()) < hermes.MAX_SPOOL_LINE
    assert len(DEEP.encode()) < claude_code.MAX_LINE
    assert len(DEEP.encode()) < openclaw.MAX_LINE
    with pytest.raises(RecursionError):
        json.loads(DEEP)


@pytest.mark.parametrize(
    "parse",
    [
        lambda line: hermes._event_from_line(line, CUTOFF),
        lambda line: claude_code.parse_line(line, CUTOFF),
        lambda line: openclaw.parse_line(line, CUTOFF),
    ],
    ids=["hermes", "claude-code", "openclaw"],
)
def test_deep_spool_line_is_skipped(parse: Callable[[str], object]) -> None:
    assert parse(DEEP) is None


def test_hermes_spool_keeps_the_good_lines(tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    good = {
        "type": "tool",
        "at": NOW.timestamp(),
        "task_id": "t_1",
        "run_id": "1",
        "tool": "terminal",
        "args_hash": "a1",
    }
    spool.write_text(DEEP + "\n" + json.dumps(good) + "\n")
    adapter = hermes.HermesAdapter(db_path=tmp_path / "kanban.db", spool=spool)
    assert [s.tool for s in adapter._spans(CUTOFF) if s.tool] == ["terminal"]


def test_hermes_last_spool_at_skips_a_deep_line(tmp_path: Path) -> None:
    spool = tmp_path / "spool.jsonl"
    spool.write_text(json.dumps({"type": "tool", "at": NOW.timestamp()}) + "\n" + DEEP + "\n")
    adapter = hermes.HermesAdapter(db_path=tmp_path / "kanban.db", spool=spool)
    assert adapter.last_spool_at() == NOW


def test_hermes_event_payload_nested_too_deep_is_empty() -> None:
    assert hermes._payload(DEEP) == {}


def test_claude_code_task_file_nested_too_deep_is_skipped(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks" / "list-a"
    tasks.mkdir(parents=True)
    (tasks / "1.json").write_text(DEEP)
    adapter = claude_code.ClaudeCodeAdapter(
        tasks_dir=tmp_path / "tasks", task_lists=["list-a"], spool=None, stop_dir=None
    )
    assert adapter._read_task("list-a", tasks / "1.json") is None


def test_scrub_drops_a_deep_line() -> None:
    assert scrub_line(DEEP) == (None, False)


def test_otlp_arguments_nested_too_deep_hash_as_text() -> None:
    """Tool arguments that do not decode are hashed as their text, and the span is kept
    (it used to be counted malformed, out of rule 10's sight)."""
    assert args_hash_of_attribute(DEEP) == args_hash(DEEP)
    assert semconv._raw_command({semconv.ARGUMENTS: DEEP}, True) is None
    assert semconv._raw_command({semconv.CC_PARAMETERS: DEEP}, True) is None
