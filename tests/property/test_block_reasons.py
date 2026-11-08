"""Text agents write (block reasons, comments, titles) that Kelpie parses or writes back:
snooze markers, block-storm grouping and the Hermes text cleaner. Pieces that matter to
the parsers are mixed with arbitrary text, so the generated reasons cross every edge."""

from __future__ import annotations

import unicodedata
from datetime import UTC, datetime, timedelta

from hypothesis import assume, example, given
from hypothesis import strategies as st

from kelpie.adapters.hermes import text as hermes_text
from kelpie.core.model import CUT_MARK, MAX_AGENT_TEXT, Block
from kelpie.core.snooze import parse_marker
from kelpie.rules._block_storm import _refs, normalise

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)

PIECES = st.sampled_from(
    [
        "until:2026-03-05",
        "until:2026-03-05T10:00",
        "until:tomorrow",
        "until:",
        "until:9999-99-99",
        'waiting on "',
        '"',
        "“",
        "”",
        "`",
        " ",
        "\n",
        "https://github.com/example/repo/pull/12",
        "https://github.com/example/repo/pull/",
        "example/repo#7",
        "T-101",
        "[kelpie-",
        "kelpie-stop",
        "kelpie-wait",
        "[kelpie-wait:abc]",
        "‮",
        "​",
        "\x00",
        "\ud800",
        "\u2028",
        "1h30m",
        "deadbeefcafe",
        ".",
        ")",
    ]
)
REASONS = st.lists(PIECES | st.text(max_size=12), max_size=12).map("".join)


@given(REASONS)
@example('"' + " until:2026-03-05 " + "x" * MAX_AGENT_TEXT)  # cut inside an open quote
def test_a_marker_is_parsed_or_reported_never_raised(reason: str) -> None:
    capped = Block(reason, NOW).reason  # as every adapter stores it
    at, invalid = parse_marker(capped)
    assert at is None or at.tzinfo is not None
    assert all(token.lower().startswith("until:") for token in invalid)
    if capped.endswith(CUT_MARK) and capped.startswith('"') and capped.count('"') == 1:
        assert at is None  # the rest of a cut quote is someone else's words


@given(REASONS.filter(lambda r: not any(q in r for q in '"\u201c\u201d\u2018\u2019`')))
def test_a_quoted_reason_holds_no_marker(reason: str) -> None:
    """Quoting a reason (as a rule or a human reply does) never snoozes anything."""
    assert parse_marker(f'"{reason}"') == (None, ())


@given(
    REASONS,
    REASONS,
    st.sampled_from([("T-101", "T-102"), ("t_1", "t_2"), ("host-a:T-1", "host-a:T-2")]),
)
def test_block_storm_groups_reasons_that_differ_only_in_their_own_task(
    before: str, after: str, ids: tuple[str, str]
) -> None:
    """Two tasks blocked with the same words, each naming itself, are one storm."""
    one, two = ids
    assume(not any(i.lower() in (before + after).lower() for i in ids))
    first = normalise(f"{before} {one} {after}", one)
    assert first == normalise(f"{before} {two} {after}", two)
    assert first == first.strip() and "  " not in first
    refs = _refs(f"{before} {one} {after}", one)
    assert list(refs) == sorted(set(refs))


@given(REASONS, st.integers(min_value=4, max_value=300), st.booleans())
@example("https://github.com/a/b/pull/https://github.com/c/d/pull/1", 200, False)
@example("[kelpie-[kelpie-stopstop", 100, True)
def test_hermes_text_is_safe_to_write(text: str, limit: int, one_line: bool) -> None:
    out = hermes_text.clean_text(text, limit, one_line=one_line)
    assert len(out) <= limit
    assert hermes_text._PR_URL.search(out) is None
    assert hermes_text._ANY_MARKER.search(out) is None
    keep = set() if one_line else set("\t\n")
    assert all(c in keep or unicodedata.category(c) not in hermes_text._UNSAFE for c in out)
    assert hermes_text.clean_text(out, limit, one_line=one_line) == out


@given(REASONS, st.integers(min_value=1, max_value=400))
def test_a_reason_is_capped_once(reason: str, extra: int) -> None:
    long = reason + "y" * (MAX_AGENT_TEXT + extra)
    capped = Block(long, NOW - timedelta(hours=1)).reason
    assert len(capped) == MAX_AGENT_TEXT and capped.endswith(CUT_MARK)
    assert Block(capped, NOW).reason == capped
