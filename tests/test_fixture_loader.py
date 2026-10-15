from datetime import timedelta

from kelpie.core.serde import parse_datetime
from tests.incidents import expand_repeats


def test_expand_repeats_shifts_copies() -> None:
    spans = expand_repeats(
        [
            {"span_id": "a", "start": "2026-03-02T00:00:00+00:00"},
            {
                "span_id": "hb",
                "start": "2026-03-02T00:00:00+00:00",
                "end": "2026-03-02T00:00:05+00:00",
                "repeat": {"count": 3, "every": "40s"},
            },
        ]
    )
    assert [s["span_id"] for s in spans] == ["a", "hb-0", "hb-1", "hb-2"]
    assert all("repeat" not in s for s in spans)
    starts = [parse_datetime(s["start"]) for s in spans[1:]]
    assert starts[2] - starts[0] == timedelta(seconds=80)
    assert parse_datetime(spans[3]["end"]) - starts[2] == timedelta(seconds=5)
