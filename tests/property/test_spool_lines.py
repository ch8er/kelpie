"""Spool lines: any process that can write a spool can write any line into it. A parser
gives an event or ``None``, never an exception (which would leave the adapter's whole
snapshot out of the tick), and never an event dated outside the window it was asked for."""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hypothesis import example, given
from hypothesis import strategies as st

from kelpie.adapters.claude_code import adapter as claude_code
from kelpie.adapters.hermes import adapter as hermes
from kelpie.adapters.openclaw import adapter as openclaw
from kelpie.traces.hooks import spans_from_hook_events
from kelpie.traces.scrub import scrub_line
from tests.property.strategies import lines, records

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=24)
UNTIL = NOW + timedelta(minutes=5)

# around the window, so both edges are crossed
TIMES = st.floats(
    min_value=(CUTOFF - timedelta(hours=2)).timestamp(),
    max_value=(UNTIL + timedelta(hours=2)).timestamp(),
)
IDS = st.text(max_size=12) | st.integers(min_value=-5, max_value=10**6)
SMALL = st.integers(min_value=-(2**64), max_value=2**64) | st.floats()
COMMON: dict[str, st.SearchStrategy[Any]] = {
    "at": TIMES,
    "session_id": IDS,
    "tool": st.sampled_from(["terminal", "Bash", "shell", "read_file", ""]),
    "args_hash": st.text(max_size=20),
    "duration_ms": SMALL,
    "outcome": st.sampled_from(["ok", "error", "timeout", ""]),
    "tool_call_id": IDS,
    "command": st.text(max_size=80),
    "runners": st.lists(st.text(max_size=10), max_size=4),
    "output_chars": SMALL,
    "pid": SMALL,
    "pid_start": st.text(max_size=30),
    "exit_code": SMALL,
    "reason": st.text(max_size=40),
    "v": st.integers(min_value=-1, max_value=5),
}
HERMES = records(
    {
        **COMMON,
        "type": st.sampled_from([hermes.TOOL, hermes.SESSION_END, "x"]),
        "run_id": IDS,
        "task_id": IDS,
        "background": st.booleans(),
        "blocked": st.booleans(),
        "command_whole": st.booleans(),
    }
)
CLAUDE_CODE = records(
    {
        **COMMON,
        "type": st.sampled_from(sorted(claude_code._EVENT_TYPES)),
        "agent_id": IDS,
        "task": IDS,
        "task_list": IDS,
        "task_status": st.text(max_size=12),
        "uid": SMALL,
        "boot": st.text(max_size=12),
        "pidns": st.text(max_size=12),
        "host": st.text(max_size=12),
    }
)
OPENCLAW = records(
    {
        **COMMON,
        "type": st.sampled_from(sorted(openclaw._EVENT_TYPES)),
        "session_key": IDS,
        "run_id": IDS,
        "agent": IDS,
        "model": st.text(max_size=12),
        "provider": st.text(max_size=12),
        "input": SMALL,
        "output": SMALL,
        "cacheRead": SMALL,
        "cacheWrite": SMALL,
        "success": st.booleans(),
        "gate": st.booleans(),
    }
)


@given(lines(HERMES))
@example('{"at": 1772366400.0, "duration_ms": 63907963200001.0, "type": "tool"}')  # overflow
def test_hermes_line_parses_or_is_skipped(line: str) -> None:
    event = hermes._event_from_line(line, CUTOFF, UNTIL)
    if event is not None:
        assert CUTOFF <= event.at <= UNTIL
        assert event.at.tzinfo is not None
        spans_from_hook_events([event], prefix="hermes")


@given(lines(CLAUDE_CODE))
@example('{"type": []}')  # an unhashable type
def test_claude_code_line_parses_or_is_skipped(line: str) -> None:
    event = claude_code.parse_line(line, CUTOFF, UNTIL)
    if event is not None:
        assert CUTOFF <= event.at <= UNTIL


@given(lines(OPENCLAW))
@example('{"type": {}}')  # an unhashable type
def test_openclaw_line_parses_or_is_skipped(line: str) -> None:
    event = openclaw.parse_line(line, CUTOFF, UNTIL)
    if event is not None:
        assert CUTOFF <= event.at <= UNTIL


@given(lines(HERMES | CLAUDE_CODE))
def test_scrub_keeps_or_drops_a_line(line: str) -> None:
    out, _ = scrub_line(line)
    if out is not None:
        assert out.endswith("\n") and "\n" not in out[:-1]  # one whole line
        assert scrub_line(out.rstrip("\n"))[0] == out  # scrubbing twice changes nothing more


SPOOLS = st.lists(lines(HERMES), max_size=8)


def _write(directory: Path, rows: list[str]) -> Path:
    spool = directory / "spool.jsonl"
    spool.write_text("".join(f"{r}\n" for r in rows if "\n" not in r and "\r" not in r))
    return spool


@given(SPOOLS)
def test_hermes_spool_of_any_lines_gives_spans(rows: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        spool = _write(Path(tmp), rows)
        adapter = hermes.HermesAdapter(db_path=Path(tmp) / "kanban.db", spool=spool)
        for span in adapter._spans(CUTOFF, UNTIL):
            assert span.end is None or span.start <= span.end


@given(st.lists(lines(CLAUDE_CODE), max_size=8))
@example(['{"type": "tool", "at": 1772366340, "session_id": "s1", "duration_ms": 1e17}'])
@example(
    ['{"type": "tool", "at": 1772366340, "session_id": "s1", "duration_ms": ' + str(10**17) + "}"]
)
def test_claude_code_spool_of_any_lines_gives_a_snapshot(rows: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = claude_code.ClaudeCodeAdapter(
            tasks_dir=Path(tmp) / "tasks",
            spool=_write(Path(tmp), rows),
            stop_dir=None,
            process_check=lambda pid, rec: None,
        )
        adapter.snapshot(NOW)


@given(st.lists(lines(OPENCLAW), max_size=8))
def test_openclaw_spool_of_any_lines_gives_a_snapshot(rows: list[str]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        adapter = openclaw.OpenClawAdapter(
            state_dir=Path(tmp), spool=_write(Path(tmp), rows), stop_dir=None, cli=None
        )
        adapter.snapshot(NOW)
