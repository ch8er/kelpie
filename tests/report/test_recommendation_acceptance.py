"""Top 3 recommendations: proposed or accepted (a fix noted for it with ``kelpie fix`` or an
equivalent), the accepted ones verified before/after even when older than the day before,
and the day's bottleneck competing for the top 3."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from kelpie.core.model import Owner, Status, Task
from kelpie.report.daily import ReportInputs, accepts, build_report, render_markdown
from kelpie.report.fixes import CONFIG, FILTER, MANUAL, MODE, FixEvent
from kelpie.report.sections import FixKey
from kelpie.report.sections.throughput import bottleneck_advice
from kelpie.report.store import StateRow
from kelpie.report.throughput import Bottleneck
from tests.report.test_daily import at, day_inputs, opened

T = datetime(2026, 3, 1, 12, tzinfo=UTC)


@pytest.mark.parametrize(
    ("key", "event"),
    [
        (FixKey(rule="stale_blocks"), FixEvent(T, MANUAL, "n", rule="stale_blocks")),
        (FixKey(rule="bottlenecks"), FixEvent(T, MANUAL, "n", rule="bottlenecks:hold")),
        (
            FixKey(rule="orphan_status", mode="act"),
            FixEvent(T, MODE, "orphan_status: observe -> act", "orphan_status"),
        ),
        (FixKey(command="xcodebuild", role="dev"), FixEvent(T, FILTER, "f", command="xcodebuild")),
        (
            FixKey(command="xcodebuild", role="dev"),
            FixEvent(T, MANUAL, "n", command="xcodebuild", role="dev"),
        ),
        (FixKey(role="reviewer"), FixEvent(T, MANUAL, "n", role="reviewer")),
        (FixKey(rule="bottlenecks", role="dev"), FixEvent(T, MANUAL, "n", rule="bottlenecks")),
        (
            FixKey(rule="bottlenecks", role="dev"),
            FixEvent(T, MANUAL, "n", rule="bottlenecks", role="dev"),
        ),
    ],
)
def test_a_fix_that_names_the_recommendation_accepts_it(key: FixKey, event: FixEvent) -> None:
    assert accepts(key, event)


@pytest.mark.parametrize(
    ("key", "event"),
    [
        (FixKey(rule="stale_blocks"), FixEvent(T, MANUAL, "n", rule="orphan_status")),
        (FixKey(rule="stale_blocks"), FixEvent(T, CONFIG, "config changed")),
        (FixKey(rule="stale_blocks"), FixEvent(T, MANUAL, "unscoped")),
        (
            FixKey(rule="orphan_status", mode="act"),
            FixEvent(T, MODE, "orphan_status: act -> observe", "orphan_status"),
        ),
        (
            FixKey(command="xcodebuild", role="dev"),
            FixEvent(T, MANUAL, "n", command="xcodebuild", role="reviewer"),
        ),
        (FixKey(command="xcodebuild"), FixEvent(T, MANUAL, "n", command="gradle")),
        (FixKey(role="reviewer"), FixEvent(T, MANUAL, "n", role="developer")),
        (FixKey(), FixEvent(T, MANUAL, "n", rule="stale_blocks")),
        # A role-only key is not accepted by a fix aimed at a rule or a command of the role.
        (FixKey(role="dev"), FixEvent(T, FILTER, "f", command="xcodebuild", role="dev")),
        (FixKey(role="dev"), FixEvent(T, MANUAL, "n", rule="stale_blocks", role="dev")),
        (FixKey(role="dev"), FixEvent(T, MANUAL, "n", command="gradle", role="dev")),
        # A mode change accepts only a key asking for that mode: never a demotion.
        (
            FixKey(rule="orphan_status"),
            FixEvent(T, MODE, "orphan_status: act -> observe", "orphan_status"),
        ),
        (
            FixKey(rule="orphan_status", mode="act"),
            FixEvent(T, MODE, "orphan_status: observe -> suggest", "orphan_status"),
        ),
        (
            FixKey(rule="bottlenecks", role="dev"),
            FixEvent(T, MANUAL, "n", rule="bottlenecks", role="ops"),
        ),
    ],
)
def test_a_fix_that_does_not_name_it_accepts_nothing(key: FixKey, event: FixEvent) -> None:
    assert not accepts(key, event)


def test_fix_flags_are_quoted_for_a_shell() -> None:
    assert FixKey(rule="stale_blocks").flags() == "--rule stale_blocks"
    key = FixKey(command="npm test; rm -rf x", role="dev ops")
    assert key.flags() == "--command 'npm test; rm -rf x' --role 'dev ops'"
    assert FixKey(role="unknown") == FixKey()  # no fix can name tasks of no known role


@pytest.mark.parametrize(
    ("stage", "expected"),
    [("queue", "add an agent"), ("review", "changes since the last round")],
)
def test_a_queue_or_review_bottleneck_competes_for_the_top_3(stage: str, expected: str) -> None:
    advice = bottleneck_advice(
        Bottleneck("developer", stage, 6.5, 0.6, 0.9, ("ops", "review", 2.5))
    )
    assert advice is not None and expected in advice.detail
    assert advice.hours == 4.0  # the constraint's excess over the next role and stage
    assert advice.accept == FixKey(rule="bottlenecks", role="developer")
    alone = bottleneck_advice(Bottleneck("developer", stage, 6.5, 1.0, 0.9))
    assert alone is not None and alone.hours == 6.5


@pytest.mark.parametrize(
    "found",
    [
        Bottleneck("developer", "waiting", 6.5, 0.6, None),
        Bottleneck("developer", "ownerless", 6.5, 0.6, None),
        Bottleneck("unknown", "queue", 6.5, 0.6, None),
        Bottleneck("developer", "queue", 3.0, 0.5, None, ("ops", "review", 3.0)),  # a tie
        None,
    ],
)
def test_bottlenecks_left_to_other_advice_or_without_excess_give_none(
    found: Bottleneck | None,
) -> None:
    assert bottleneck_advice(found) is None


def _queue_day(*states: StateRow) -> str:
    start = datetime(2026, 3, 8, tzinfo=UTC)
    queued = Task("T-9", "queued", Status.READY, start - timedelta(hours=2), role="developer")
    review = Task("T-8", "in review", Status.REVIEW, start + timedelta(hours=18), role="ops")
    inputs = ReportInputs(
        day=date(2026, 3, 8),
        tz=UTC,
        tasks=(queued, review),
        owners=(Owner("dev-1", "developer"),),
        states=states,
    )
    top = render_markdown(build_report(inputs)).split("## Top 3 recommendations")[1]
    return top.split("\n## ")[0]


def test_the_days_bottleneck_reaches_the_top_3() -> None:
    top = _queue_day()
    assert "1. **Relieve the developer bottleneck in queue** (18.0 hours lost)" in top
    assert '`kelpie fix --rule bottlenecks --role developer "<what changed>"`' in top


def test_the_bottleneck_gives_way_to_rule_6s_own_line() -> None:
    day = datetime(2026, 3, 8, tzinfo=UTC)
    idle = opened("bottlenecks:idle-ready", "bottlenecks", day, day, day + timedelta(hours=20))
    assert "Relieve the developer bottleneck" not in _queue_day(idle)


def _stale_day(*fixes: FixEvent) -> str:
    state = opened("stale_blocks:T-9", "stale_blocks", at(0, day=1), at(0, day=1), at(23))
    report = build_report(day_inputs(states=[state], fixes=fixes))
    return render_markdown(report)


def test_a_recommendation_nobody_acted_on_is_marked_proposed() -> None:
    text = _stale_day(FixEvent(at(9, day=1), MANUAL, "other", rule="orphan_status"))
    top = text.split("## Top 3 recommendations")[1].split("\n## ")[0]
    assert "*Proposed*: not accepted yet (note it with " in top
    assert '`kelpie fix --rule stale_blocks "<what changed>"`' in top
    assert "A *proposed* recommendation has no fix noted" in top


def test_an_accepted_recommendation_is_verified_even_from_before_the_day_before() -> None:
    # Older than the day before (not verified otherwise) but within the days the report reads.
    fix = FixEvent(
        datetime(2026, 2, 28, 12, tzinfo=UTC), MANUAL, "tied blocks to PRs", "stale_blocks"
    )
    state = opened("stale_blocks:T-9", "stale_blocks", at(0, day=1), at(0, day=1), at(23))
    report = build_report(day_inputs(states=[state], fixes=(fix,)))
    assert report.accepted == (fix,)
    assert [v.event for v in report.verifications] == [fix]
    text = render_markdown(report)
    top = text.split("## Top 3 recommendations")[1].split("\n## ")[0]
    assert (
        "*Accepted* 2026-02-28 12:00 (`kelpie fix`: tied blocks to PRs); before/after below." in top
    )
    assert "A *proposed* recommendation" not in top  # every one is accepted
    assert "2026-02-28 12:00 UTC, manual" in text.split("## Before/after of recent fixes")[1]


def test_an_older_fix_that_accepts_nothing_is_not_verified() -> None:
    fix = FixEvent(datetime(2026, 2, 28, 12, tzinfo=UTC), MANUAL, "unrelated", "orphan_status")
    state = opened("stale_blocks:T-9", "stale_blocks", at(0, day=1), at(0, day=1), at(23))
    report = build_report(day_inputs(states=[state], fixes=(fix,)))
    assert report.accepted == (None,) and report.verifications == ()


def test_a_long_command_is_named_by_its_options_not_cut() -> None:
    from kelpie.report.daily import _acceptance
    from kelpie.report.sections import Advice

    long = Advice("t", "d", accept=FixKey(command="x" * 90, role="dev"))
    text = _acceptance(long, None, UTC)
    assert "`--command`, `--role` exactly as the recommendation names them" in text
    spaced = Advice("t", "d", accept=FixKey(command="make  test"))  # two spaces: shown as one
    assert "`--command` exactly" in _acceptance(spaced, None, UTC)
    tick = Advice("t", "d", accept=FixKey(command="run `x`"))
    assert _acceptance(tick, None, UTC).count("``") >= 2  # a code span the value cannot close


def test_a_mode_change_to_act_reads_as_accepted() -> None:
    from kelpie.report.daily import _acceptance
    from kelpie.report.sections import Advice

    fix = FixEvent(T, MODE, "orphan_status: observe -> act", "orphan_status")
    text = _acceptance(Advice("Let orphan_status act", "d"), fix, UTC)
    assert text == "*Accepted* 2026-03-01 12:00 (its mode change); before/after below."
