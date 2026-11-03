"""Time spans as sorted, merged lists of ``(start, end)``: the report's arithmetic for
counting an hour once (a snooze or an outage over a wait, overlapping chaos states on a
task). Pure."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

type Spans = list[tuple[datetime, datetime]]


def merged(spans: Iterable[tuple[datetime, datetime]]) -> Spans:
    """Non-empty spans, sorted and merged where they overlap."""
    out: Spans = []
    for lo, hi in sorted(x for x in spans if x[1] > x[0]):
        if out and lo <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], hi))
        else:
            out.append((lo, hi))
    return out


def hours(spans: Spans) -> float:
    return sum((hi - lo).total_seconds() for lo, hi in spans) / 3600


def minus(x: Spans, y: Spans) -> Spans:
    """The parts of merged spans ``x`` outside merged spans ``y``."""
    out: Spans = []
    for lo, hi in x:
        start = lo
        for lo2, hi2 in y:
            if hi2 <= start or lo2 >= hi:
                continue
            if lo2 > start:
                out.append((start, lo2))
            start = max(start, hi2)
        if hi > start:
            out.append((start, hi))
    return out
