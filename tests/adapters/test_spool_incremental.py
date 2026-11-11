"""The spool read incrementally (#392): only appended lines are parsed, with caps.

Every tick used to read and parse up to 64 MiB of each spool file again. ``SpoolCache``
remembers each file's offset (by inode, so a rotation keeps it) and its parsed lines, and
caps the lines parsed per tick, the lines kept and the tool calls kept per run.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
import tracemalloc
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters import _spool
from kelpie.adapters.claude_code import adapter as claude_code
from kelpie.adapters.hermes import adapter as hermes
from kelpie.adapters.hermes.boards import HermesBoards
from kelpie.adapters.openclaw import adapter as openclaw

NOW = datetime(2026, 11, 11, 12, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=24)
MAX_LINE = 256


@dataclass(frozen=True, slots=True)
class Item:
    at: datetime
    session_id: str | None
    n: int
    tool: bool = True


class Parser:
    """Parses ``{"at", "s", "n"}`` lines and counts the calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, line: str, cutoff: datetime) -> Item | None:
        self.calls += 1
        try:
            rec = json.loads(line)
            at = datetime.fromtimestamp(rec["at"], UTC)
        except (ValueError, KeyError, TypeError):
            return None
        if at < cutoff:
            return None
        return Item(at, rec.get("s"), rec["n"], rec.get("tool", True))


def text(n: int, at: datetime = NOW, s: str = "r1", **kw: object) -> str:
    return json.dumps({"at": at.timestamp(), "s": s, "n": n, **kw}) + "\n"


def append(path: Path, *lines: str) -> None:
    with path.open("a") as f:
        f.write("".join(lines))


def cache(parse: Parser | None = None, **kw: object) -> _spool.SpoolCache[Item]:
    return _spool.SpoolCache(
        "test",
        parse or Parser(),
        kw.pop("limit", 1 << 20),  # type: ignore[arg-type]
        MAX_LINE,
        capped=lambda i: i.tool,
        **kw,  # type: ignore[arg-type]
    )


def fresh(spool: Path, cutoff: datetime = CUTOFF) -> list[int]:
    """What reading the whole spool again gives (the old way, every tick)."""
    parse = Parser()
    lines = _spool.spool_lines(spool, cutoff, 1 << 20, MAX_LINE)
    return [i.n for line in lines if (i := parse(line, cutoff)) is not None]


def numbers(items: list[Item]) -> list[int]:
    return [i.n for i in items]


@pytest.fixture
def spool(tmp_path: Path) -> Path:
    return tmp_path / "spool.jsonl"


def test_only_appended_lines_are_parsed(spool: Path) -> None:
    parse = Parser()
    c = cache(parse)
    append(spool, *(text(n) for n in range(100)))
    assert numbers(c.items(spool, CUTOFF)) == list(range(100))
    assert parse.calls == 100
    append(spool, text(100), text(101))
    assert numbers(c.items(spool, CUTOFF)) == list(range(102))
    assert parse.calls == 102
    assert numbers(c.items(spool, CUTOFF)) == list(range(102))
    assert parse.calls == 102


def test_rotation_keeps_the_offset(spool: Path) -> None:
    """The hook renames the spool to ``.1``: the file keeps its inode, so lines appended to
    it before the rename are read and nothing is parsed twice."""
    parse = Parser()
    c = cache(parse)
    append(spool, text(0), text(1))
    c.items(spool, CUTOFF)
    append(spool, text(2))  # written after the last tick, before the rotation
    os.replace(spool, spool.with_name(spool.name + ".1"))
    append(spool, text(3))
    assert numbers(c.items(spool, CUTOFF)) == [0, 1, 2, 3] == fresh(spool)
    assert parse.calls == 4
    os.replace(spool, spool.with_name(spool.name + ".1"))  # the old .1 is gone
    append(spool, text(4))
    assert numbers(c.items(spool, CUTOFF)) == [3, 4] == fresh(spool)
    assert parse.calls == 5


def test_truncation_reads_again(spool: Path) -> None:
    c = cache()
    append(spool, *(text(n) for n in range(5)))
    c.items(spool, CUTOFF)
    spool.write_text(text(7))
    assert numbers(c.items(spool, CUTOFF)) == [7]


def test_a_file_rewritten_in_place_is_read_again(spool: Path) -> None:
    """Not shorter, but its first bytes changed (a new file on the same inode)."""
    c = cache()
    append(spool, text(1), text(2))
    c.items(spool, CUTOFF)
    with spool.open("r+") as f:
        f.write(text(8) + text(9) + text(10))
    assert numbers(c.items(spool, CUTOFF)) == [8, 9, 10]


def test_an_unterminated_line_is_parsed_but_kept_only_once_ended(spool: Path) -> None:
    parse = Parser()
    c = cache(parse)
    append(spool, text(1), text(2).rstrip("\n"))
    assert numbers(c.items(spool, CUTOFF)) == [1, 2] == fresh(spool)
    append(spool, "\n", text(3))
    assert numbers(c.items(spool, CUTOFF)) == [1, 2, 3]
    assert parse.calls == 4  # the unterminated line was parsed again once complete


def test_overlong_lines_are_skipped_across_reads(
    spool: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_spool, "READ_BLOCK", 7)  # lines span many reads
    c = cache()
    long = json.dumps({"at": NOW.timestamp(), "n": 99, "pad": "x" * 600}) + "\n"
    append(spool, text(1), long, text(2))
    assert numbers(c.items(spool, CUTOFF)) == [1, 2] == fresh(spool)
    append(spool, long[:300])  # an overlong line still being written
    assert numbers(c.items(spool, CUTOFF)) == [1, 2]
    append(spool, long[300:], text(3))
    assert numbers(c.items(spool, CUTOFF)) == [1, 2, 3] == fresh(spool)


def test_lines_leave_with_the_lookback_and_an_earlier_cutoff_starts_over(spool: Path) -> None:
    parse = Parser()
    c = cache(parse)
    append(spool, text(1, NOW - timedelta(hours=2)), text(2, NOW))
    assert numbers(c.items(spool, CUTOFF)) == [1, 2]
    assert numbers(c.items(spool, NOW - timedelta(hours=1))) == [2]
    assert numbers(c.items(spool, CUTOFF)) == [1, 2]  # read again
    assert parse.calls == 4


@pytest.mark.parametrize("seed", range(20))
def test_matches_a_fresh_read(spool: Path, seed: int) -> None:
    """Random appends, partial lines, overlong and malformed lines, rotations and
    truncations: every tick gives what reading the whole spool again gives."""
    rng = random.Random(seed)
    c = cache()
    rotated = spool.with_name(spool.name + ".1")
    pending = ""
    n = 0
    for _ in range(40):
        roll = rng.random()
        if roll < 0.08 and spool.exists():
            os.replace(spool, rotated)
        elif roll < 0.12:
            spool.write_text("")
            pending = ""
        else:
            chunk = pending
            for _ in range(rng.randrange(6)):
                n += 1
                pick = rng.random()
                if pick < 0.1:
                    chunk += json.dumps({"at": NOW.timestamp(), "n": n, "x": "y" * 400}) + "\n"
                elif pick < 0.2:
                    chunk += "{not json\n"
                else:
                    at = NOW - timedelta(hours=rng.choice([1, 30]))
                    chunk += text(n, at)
            cut = rng.randrange(len(chunk) + 1) if rng.random() < 0.3 else len(chunk)
            append(spool, chunk[:cut])
            pending = chunk[cut:]
        assert numbers(c.items(spool, CUTOFF)) == fresh(spool)


# --- caps ---------------------------------------------------------------------------------


def test_lines_per_tick_keep_the_newest(spool: Path, caplog: pytest.LogCaptureFixture) -> None:
    parse = Parser()
    c = cache(parse, max_lines=10)
    append(spool, *(text(n) for n in range(25)))
    with caplog.at_level(logging.WARNING):
        assert numbers(c.items(spool, CUTOFF)) == list(range(15, 25))
        assert parse.calls == 10
        append(spool, *(text(n) for n in range(25, 30)))
        assert numbers(c.items(spool, CUTOFF)) == list(range(20, 30))  # kept between ticks
    assert c.dropped["lines"] == 15 + 5
    assert sum("spool is over" in r.getMessage() for r in caplog.records) == 1


def test_bytes_per_tick_keep_the_newest(spool: Path) -> None:
    lines = [text(n) for n in range(20)]
    append(spool, *lines)
    five = sum(len(line) for line in lines[-5:])
    c = cache(limit=five + 3)  # five lines and a bit: starts mid-line
    assert numbers(c.items(spool, CUTOFF)) == list(range(15, 20))
    assert c.dropped["bytes"] > 0
    c = cache(limit=five)  # exactly on a line start: no line lost
    assert numbers(c.items(spool, CUTOFF)) == list(range(15, 20))


def test_tool_calls_per_run_keep_the_newest_and_everything_else(spool: Path) -> None:
    c = cache(max_run_calls=3)
    append(
        spool,
        text(0, s="r1", tool=False),  # the run's start: always kept
        *(text(n, s="r1") for n in range(1, 8)),
        text(50, s="r2"),
    )
    assert numbers(c.items(spool, CUTOFF)) == [0, 5, 6, 7, 50]
    assert c.dropped["run_calls"] == 4


def test_a_normal_spool_reaches_no_cap(spool: Path) -> None:
    """Two files of 20 MiB (the hooks rotate there) of a tool line as short as a plugin
    writes one stay under every cap."""
    line = hermes_line(1, NOW.timestamp())
    per_file = 20 * 1024 * 1024 // len(line)
    assert 2 * per_file <= _spool.MAX_LINES
    assert hermes.MAX_SPOOL_READ >= 20 * 1024 * 1024


# --- the adapters -------------------------------------------------------------------------


def hermes_line(i: int, at: float) -> str:
    rec = {"type": "tool", "at": at, "run_id": str(1 + i % 5), "task_id": f"t_{1 + i % 5}"}
    return json.dumps({**rec, "tool": "terminal", "args_hash": f"h{i % 3}"}) + "\n"


def test_hermes_spans_are_the_same_tick_after_tick(spool: Path) -> None:
    now = datetime.now(UTC)
    append(spool, *(hermes_line(i, now.timestamp() - 600 + i) for i in range(200)))
    a = hermes.HermesAdapter(db_path=spool.parent / "kanban.db", spool=spool)
    first = a._spans(now - a.lookback, now + hermes.FUTURE_SKEW)
    append(spool, hermes_line(200, now.timestamp()))
    second = a._spans(now - a.lookback, now + hermes.FUTURE_SKEW)
    again = hermes.HermesAdapter(db_path=spool.parent / "kanban.db", spool=spool)
    assert second == again._spans(now - a.lookback, now + hermes.FUTURE_SKEW)
    assert len(second) == len(first) + 1


def test_hermes_line_dated_ahead_counts_once_it_is_current(spool: Path) -> None:
    now = datetime.now(UTC)
    append(spool, hermes_line(1, (now + timedelta(minutes=10)).timestamp()))
    a = hermes.HermesAdapter(db_path=spool.parent / "kanban.db", spool=spool)
    tools = lambda at: [s for s in a._spans(at - a.lookback, at + hermes.FUTURE_SKEW) if s.tool]  # noqa: E731
    assert tools(now) == []
    assert len(tools(now + timedelta(minutes=6))) == 1


def test_hermes_boards_keep_one_cache(tmp_path: Path) -> None:
    board = hermes.HermesAdapter(db_path=tmp_path / "a.db", spool=None, board="a")
    boards = HermesBoards([board], spool=tmp_path / "spool.jsonl")
    assert boards._spool_reader().spool_cache is board.spool_cache
    assert replace(board).spool_cache is board.spool_cache


def test_claude_code_and_openclaw_parse_only_appended_lines(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime.now(UTC)
    calls = {"n": 0}

    def counting(parse):  # type: ignore[no-untyped-def]
        def wrapped(line: str, cutoff: datetime, until: datetime | None = None):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return parse(line, cutoff, until)

        return wrapped

    sid = "0b5c6f7e-0000-4000-8000-000000000001"
    spool = tmp_path / "cc.jsonl"
    monkeypatch.setattr(claude_code, "parse_line", counting(claude_code.parse_line))
    cc = claude_code.ClaudeCodeAdapter(
        tasks_dir=tmp_path / "tasks", spool=spool, stop_dir=None, process_check=lambda p, r: None
    )
    line = {"type": "tool", "session_id": sid, "v": 2, "tool": "Bash"}
    append(spool, *(json.dumps({**line, "at": now.timestamp() - 60 + i}) + "\n" for i in range(50)))
    assert len([s for s in cc.snapshot(now).spans if s.tool]) == 50
    append(spool, json.dumps({**line, "at": now.timestamp()}) + "\n")
    calls["n"] = 0
    assert len([s for s in cc.snapshot(now).spans if s.tool]) == 51
    assert calls["n"] == 1

    oc_spool = tmp_path / "oc.jsonl"
    monkeypatch.setattr(openclaw, "parse_line", counting(openclaw.parse_line))
    oc = openclaw.OpenClawAdapter(
        state_dir=tmp_path, workboard_db=None, spool=oc_spool, stop_dir=None
    )
    rec = {"type": "tool", "session_id": "s1", "tool": "exec"}
    append(
        oc_spool, *(json.dumps({**rec, "at": now.timestamp() - 60 + i}) + "\n" for i in range(50))
    )
    assert len([s for s in oc.snapshot(now).spans if s.tool]) == 50
    append(oc_spool, json.dumps({**rec, "at": now.timestamp()}) + "\n")
    calls["n"] = 0
    assert len([s for s in oc.snapshot(now).spans if s.tool]) == 51
    assert calls["n"] == 1


def test_a_large_spool_is_bounded_in_time_and_memory(
    spool: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """An agent appending past the rotation: the issue's reproduction, scaled down with the
    caps scaled with it. The first tick parses at most ``max_lines``; a tick after a few
    appended lines parses only those, in a fraction of a second and a few MiB."""
    now = datetime.now(UTC)
    with spool.open("w") as f:
        f.writelines(hermes_line(i, now.timestamp() - 3600 + i / 50) for i in range(120_000))
    a = hermes.HermesAdapter(db_path=spool.parent / "kanban.db", spool=spool)
    assert a.spool_cache is not None
    a.spool_cache.max_lines = 20_000
    a.spool_cache.max_run_calls = 2_000
    parse = Parser()
    real = a.spool_cache.parse

    def counted(line: str, cutoff: datetime) -> object:
        parse.calls += 1
        return real(line, cutoff)

    a.spool_cache.parse = counted  # type: ignore[assignment]
    with caplog.at_level(logging.WARNING):
        spans = a._spans(now - a.lookback, now + hermes.FUTURE_SKEW)
    assert parse.calls == 20_000
    assert sum(1 for s in spans if s.tool) == 5 * 2_000  # five runs, each capped
    assert a.spool_cache.dropped["lines"] == 100_000
    append(spool, *(hermes_line(i, now.timestamp()) for i in range(100)))
    parse.calls = 0
    tracemalloc.start()
    try:
        start = time.perf_counter()
        a._spans(now - a.lookback, now + hermes.FUTURE_SKEW)
        took = time.perf_counter() - start
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert parse.calls == 100
    assert took < 5  # seconds, under tracemalloc on a slow runner; about 0.1 s untraced
    assert peak < 64 * 1024 * 1024
    assert sum("spool is over" in r.getMessage() for r in caplog.records) == 2  # lines, runs
