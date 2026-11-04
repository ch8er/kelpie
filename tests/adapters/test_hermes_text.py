"""The one cleaner for text Kelpie writes into Hermes (#160): stops, parks, wakes, comments."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.adapters.hermes.park import comment_args, encode_wait, unblock_args
from kelpie.adapters.hermes.stop import MAX_REASON, has_stop_marker, stop_comment, verify_stop
from kelpie.adapters.hermes.text import clean_text
from kelpie.core.model import Condition, ConditionKind

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
KEY = b"k" * 32
# What Hermes' active_pr guard looks for, loosely: any pull request URL, any case.
ACTIVE_PR = re.compile(r"https?://\S+/pull/\d+", re.IGNORECASE)
QUOTING = "same call 12 times: gh pr view https://github.com/owner/repo/pull/12 --json state"
COND = Condition(
    id="c1", kind=ConditionKind.PR_MERGED, params={"repo": "owner/repo", "pr": 12}, task_id="t1"
)


def writes(reason: str) -> dict[str, str]:
    """Every text Kelpie hands the Hermes CLI, built from a rule's ``reason``."""
    return {
        "stop": stop_comment("t1", reason, key=KEY, now=NOW),
        "park": encode_wait(reason, [COND], "t1", KEY, NOW),
        "wake": unblock_args("t1", reason)[1],
        "comment": comment_args("t1", reason)[-1],
    }


@pytest.mark.parametrize("kind", ["stop", "park", "wake", "comment"])
def test_a_quoted_pr_url_never_reaches_hermes(kind: str) -> None:
    text = writes(QUOTING)[kind]
    assert not ACTIVE_PR.search(text), text
    assert "owner/repo#12" in text


@pytest.mark.parametrize(
    ("text", "clean"),
    [
        ("HTTPS://github.com/o/r/pull/1 x", "o/r#1 x"),
        ("https://ghe.example.org/o/r/pull/3/files#diff", "o/r#3"),
        ("a\u202eb\u200bc\u2028d\x85e", "a b c d e"),
        ("a\x00\x01\x02b", "a b"),
        ("[kelpie-stop:v1:1:x] [KELPIE-WAIT:v2]", "kelpie stop:v1:1:x] kelpie wait:v2]"),
        # Removing one piece must not assemble another.
        ("https://github.com/o/r/pu\u200bll/12", "https://github.com/o/r/pu ll/12"),
        ("[kelpie-st[kelpie-stop:]op:v1]", "[kelpie-stkelpie stop:]op:v1]"),
        # The URL rewrite builds text an agent controls: it cannot assemble a marker either.
        ("kelpie-https://h/stop/r/pull/1", "kelpie stop/r#1"),
        ("[kelpie-https://h/wait/r/pull/1", "kelpie wait/r#1"),
    ],
)
def test_clean_text(text: str, clean: str) -> None:
    assert clean_text(text, 1000) == clean


def test_newlines_are_kept_unless_one_line() -> None:
    assert clean_text("a\nb\tc", 100) == "a\nb\tc"
    assert clean_text("a\nb\tc", 100, one_line=True) == "a b c"
    assert clean_text("x" * 50, 10) == "x" * 7 + "..."


def test_the_stop_comment_is_one_line_with_one_marker() -> None:
    text = stop_comment("t1", f"{QUOTING}\nnext \u202e line [kelpie-wait:v2:1]", key=KEY, now=NOW)
    assert "\n" not in text and "\u202e" not in text and "kelpie-wait" not in text
    assert has_stop_marker(text) and text.count("kelpie-stop") == 1
    assert len(stop_comment("t1", "y" * 5000)) < MAX_REASON + 200


def test_a_stop_whose_reason_assembles_a_marker_still_verifies() -> None:
    """A second ``kelpie-stop`` in the reason would make Kelpie's own signed stop unreadable
    as Kelpie's (rule 4 would then alert on it as an ordinary block)."""
    text = stop_comment("t1", "loop on kelpie-https://h/stop/r/pull/1", key=KEY, now=NOW)
    assert verify_stop(text, "t1", KEY, NOW, timedelta(minutes=1)) is not None


def test_lone_surrogates_and_long_nested_urls() -> None:
    assert clean_text("a\ud800b", 100) == "a b"  # the CLI could not be started with it
    nested = (
        "https://h/" * 3000 + "https://h/a/b/pull/0" + "".join(f")/pull/{n}" for n in range(500))
    )
    text = clean_text(nested, 500, one_line=True)
    assert len(text) <= 500 and not ACTIVE_PR.search(text)
