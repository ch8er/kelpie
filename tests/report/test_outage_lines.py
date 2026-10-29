"""The report's list of the day's outages (``kelpie.core.incidents``).

The day is test_waits' (2026-03-02, UTC): an outage's waiting time comes from its tasks
R1-R3, which wait on CI in ``example/app`` from 08:00.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest

from kelpie.core.incidents import FOLDED_UNDER
from kelpie.core.ledger import LedgerEntry, Outcome, Suppression
from kelpie.report.daily import build_report, render_markdown
from kelpie.report.store import StateRow
from tests.report.test_waits import END, SID, at, inputs, ledger, outage_sample, outage_state

SIGNATURE = "ci.yml > build > Select toolchain"
OTHER = "ci_economy:outage:ffffffffffff"


def folded(when: float, fingerprint: str, under: str = SID, **kw: Any) -> LedgerEntry:
    e = replace(ledger("stale_blocks", **{FOLDED_UNDER: under}), at=at(when))
    fields = {"outcome": Outcome.SUPPRESSED, "suppression": Suppression.INCIDENT, **kw}
    return replace(e, fingerprint=fingerprint, **fields)


def closed(row: StateRow, at_: datetime | None) -> StateRow:
    return replace(row, closed_at=at_)


def test_the_report_names_each_outage_with_its_folded_alerts() -> None:
    state = closed(outage_state(SID, at(2), at(20)), at(20.5))
    sample = outage_sample(at(3), repos=("example/app",), prs=("example/app#1", "example/app#2"))
    entries = (sample, folded(4, "a"), folded(5, "a"), folded(6, "b"), folded(21, "late"))
    report = build_report(inputs(states=(state,), actions=entries), sections={})
    [line] = report.outages
    assert (line.signature, line.scope, line.folded, line.open) == (
        SIGNATURE,
        2,
        2,  # alert "a" twice (hourly repeat) counts once; "late" is after the outage
        False,
    )
    assert report.outage_wait_hours == pytest.approx(3 * 12)  # R1-R3 from 08:00 to 20:00
    markdown = render_markdown(report)
    assert "**Outages** (36.0 task-hours of waiting fell inside them" in markdown
    assert f"`{SIGNATURE}` since 2026-03-02 02:00 UTC, until 2026-03-02 20:00 UTC" in markdown
    assert "2 PR(s), 2 alert(s) folded under it" in markdown


@pytest.mark.parametrize("closed_at", [None, END + timedelta(hours=1)])
def test_an_outage_open_at_the_end_of_the_day_is_still_open(closed_at: datetime | None) -> None:
    # its last tick came before midnight (the daemon was down, or the report ran early)
    state = closed(outage_state(SID, at(2), END - timedelta(seconds=30)), closed_at)
    report = build_report(inputs(states=(state,), actions=(outage_sample(at(3)),)), sections={})
    [line] = report.outages
    assert line.open
    assert f"`{SIGNATURE}` since 2026-03-02 02:00 UTC, still open:" in render_markdown(report)


def test_an_outage_from_yesterday_counts_only_the_days_prs_and_folds() -> None:
    state = closed(outage_state(SID, at(-5), at(6)), at(6.5))
    entries = (
        outage_sample(at(-4.5), prs=("example/app#9",)),  # superseded before midnight
        outage_sample(at(-4), prs=("example/app#1", "example/app#2")),  # its scope at midnight
        outage_sample(at(1), prs=("example/app#2", "Example/App#3")),
        folded(-3, "yesterday"),
        folded(2, "a"),
    )
    report = build_report(inputs(states=(state,), actions=entries), sections={})
    [line] = report.outages
    assert line.since == at(-5) and (line.scope, line.folded) == (3, 1)
    assert "since 2026-03-01 19:00 UTC, until 2026-03-02 06:00 UTC" in render_markdown(report)


def test_two_outages_in_order_with_one_total() -> None:
    late = closed(outage_state(SID, at(12), at(14)), at(15))
    early = closed(outage_state(OTHER, at(9), at(10)), at(11))
    entries = (outage_sample(at(12), repos=("example/app",)),)  # OTHER has no sample
    report = build_report(inputs(states=(late, early), actions=entries), sections={})
    assert [o.signature for o in report.outages] == [OTHER, SIGNATURE]
    assert report.outage_wait_hours == pytest.approx(3 * 2)  # only SID names a repository
    markdown = render_markdown(report)
    assert markdown.count("task-hours of waiting fell inside them") == 1
    assert markdown.index(OTHER) < markdown.index(SIGNATURE)


def test_only_alerts_folded_under_this_outage_count() -> None:
    state = closed(outage_state(SID, at(2), at(20)), at(21))
    entries = (
        outage_sample(at(3)),
        folded(4, "other", under=OTHER),
        folded(5, "cooldown", suppression=Suppression.COOLDOWN),
        folded(6, "alerted", outcome=Outcome.ALERTED, suppression=None),
        folded(7, "kept"),
    )
    report = build_report(inputs(states=(state,), actions=entries), sections={})
    assert [o.folded for o in report.outages] == [1]


def test_an_outage_outside_the_day_is_not_listed() -> None:
    before = closed(outage_state(SID, at(-6), at(-1)), at(-0.5))
    after = outage_state(OTHER, END, END + timedelta(hours=1))
    report = build_report(inputs(states=(before, after), actions=()), sections={})
    assert report.outages == () and report.outage_wait_hours == 0


def test_no_outage_no_list() -> None:
    report = build_report(inputs(), sections={})
    assert report.outages == () and "**Outages**" not in render_markdown(report)


def test_folds_after_the_last_sighting_count_until_the_outage_closes() -> None:
    # not seen after 10:00, closed at 11:00: the engine kept folding meanwhile
    state = closed(outage_state(SID, at(2), at(10)), at(11))
    entries = (outage_sample(at(3)), folded(4, "a"), folded(10.25, "b"), folded(11.5, "late"))
    report = build_report(inputs(states=(state,), actions=entries), sections={})
    assert [o.folded for o in report.outages] == [2]


@pytest.mark.parametrize("closed_at", [None, END + timedelta(hours=1)])
def test_folds_after_the_last_sighting_count_while_still_open(closed_at: datetime | None) -> None:
    state = closed(outage_state(SID, at(2), at(10)), closed_at)
    entries = (outage_sample(at(3)), folded(4, "a"), folded(23, "b"))
    report = build_report(inputs(states=(state,), actions=entries), sections={})
    assert [(o.folded, o.open) for o in report.outages] == [(2, True)]
