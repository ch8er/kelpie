"""People's acceptance criteria and the evidence that answers them (rule 1, incident 18)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import ActionKind, Severity
from kelpie.core.model import (
    Artifact,
    Build,
    BuildStatus,
    Comment,
    Owner,
    OwnerKind,
    PullRequest,
    Session,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules._acceptance import Criterion, criteria, unanswered
from kelpie.rules.exit_contract import ExitContract, build

NOW = datetime(2026, 3, 2, 15, tzinfo=UTC)
H = timedelta(hours=1)
PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b\d+\s+(?:runs?|times)\b",
        r"\b(\d+)\s*(?:of|/)\s*\1\b",
        r"\bbefore\s+(?:auto)?merg",
    )
)
ASK = Comment("lead", "Before automerge: 20 runs in a row, 20 of 20.", NOW - 5 * H, human=True)
REF = Criterion(ASK.text, "lead", ASK.at).ref  # how evidence names the criterion


def task(*comments: Comment, status: Status = Status.REVIEW, **kw: object) -> Task:
    return Task("T-1", "Fix flaky test", status, NOW - 2 * H, comments=comments, **kw)  # type: ignore[arg-type]


def test_criteria_come_from_people_only_and_once() -> None:
    agent = Comment("dev-1", "Plan: 20 runs of the suite.", NOW - 4 * H)
    again = Comment(
        "lead", "Reminder: before automerge: 20 runs in a row, 20 of 20.", NOW - 3 * H, human=True
    )
    found = criteria(task(ASK, agent, ASK), PATTERNS)
    assert [c.text for c in found] == ["Before automerge: 20 runs in a row, 20 of 20."]
    assert found[0].asked_by == "lead" and found[0].at == ASK.at
    assert len(criteria(task(ASK, again), PATTERNS)) == 1  # a reminder is the same criterion
    text = "Looks good overall.\nCheck it on a tablet before merge. Thanks!"
    [c] = criteria(task(Comment("lead", text, NOW - H, human=True)), PATTERNS)
    assert c.text == "Check it on a tablet before merge."
    # An abbreviation or a version does not split a sentence.
    [c] = criteria(
        task(Comment("lead", "Run it 20 times on iOS 17.2 e.g. on iPad.", NOW - H, human=True)),
        PATTERNS,
    )
    assert c.text.endswith("on iPad.")
    # A human owner of the snapshot is a person too.
    plain = Comment("ops-lead", "Before merge: 5 runs in a row.", NOW - H)
    assert criteria(task(plain), PATTERNS, frozenset({"ops-lead"}))


def test_a_description_by_a_person_holds_criteria_from_its_creation() -> None:
    body = "Fix the login test.\nNeeds 10 runs in a row before merge."
    made = NOW - 10 * H
    ref = Criterion("Needs 10 runs in a row before merge.", "lead", made).ref
    report = Comment("dev-1", f"{ref}: met, 10 runs in a row, all green", NOW - 6 * H)
    t = task(report, body=body, body_author="lead", created_at=made)
    [c] = criteria(t, PATTERNS)
    assert (c.text, c.asked_by, c.at) == ("Needs 10 runs in a row before merge.", "lead", made)
    assert unanswered(t, Snapshot(NOW), PATTERNS) == []  # the agent's report came after
    assert criteria(task(body=body), PATTERNS) == []  # an agent wrote it


@pytest.mark.parametrize(
    ("criterion", "evidence", "answered"),
    [
        ("20 runs in a row, 20 of 20", "Ran it 20 times in a row: 20/20 passed", True),
        ("20 runs in a row, 20 of 20", "CI green, merges cleanly", False),
        ("20 runs in a row, 20 of 20", "CI green at 20:14, merges cleanly.", False),
        ("20 runs in a row, 20 of 20", "Pushed a1b20c3, CI green.", False),
        ("20 runs in a row, 20 of 20", "Ran 20 times: 19/20 passed", False),
        ("20 runs in a row, 20 of 20", "I'll do 20 runs, 20/20 expected", False),
        ("20 runs in a row, 20 of 20", "20 runs in a row, 20 of 20", False),  # repeated back
        ("Needs 10 runs in a row", "ran it 10 times, all green", True),
        ("Needs 10 runs in a row", "iOS 10 build green", False),
        ("Check it on a tablet before merge", "Checked on a tablet: layout fine", True),
        ("Check it on a tablet before merge", "All CI checks green, merges cleanly.", False),
        ("Check it on a tablet before merge", "Will test on a tablet later.", False),
        ("Check it on a tablet before merge", "Fixed the layout", False),
        ("must be merged", "merged", False),  # only common words: nothing to look for
        ("Needs 20 runs in a row before merge", "CI green at 20:14, merges cleanly.", False),
        ("Needs 20 runs in a row before merge", "Run 20 of the nightly is green", False),
        ("Needs 20 runs in a row", "Ran it 20 times: failed on run 7.", False),
        ("Needs 20 runs in a row", "20 runs: 3 failed, still flaky.", False),
        ("20 runs in a row, 20 of 20", "Note: lead requires 20/20 before automerge.", False),
        ("20 runs in a row, 20 of 20", "Still waiting for 20/20 before merge", False),
        ("Check it on a tablet before merge", "Not checked on a tablet: no device.", False),
        ("Check it on a tablet before merge", "remember the tablet check", False),
        (
            "20 runs in a row, 20 of 20",
            "Ran it 20 times: 20/20 passed. Next: remove the retry decorator?",
            False,  # a word of failure or retry anywhere vetoes: one comment per criterion
        ),
        (
            "20 runs in a row, 20 of 20",
            "> Before automerge: 20 runs in a row, 20 of 20.\nDone: ran 20 times, 20/20 passed.",
            True,
        ),
        ("Before merge: 10 runs on Android 14.", "Ran it 10 times on Android 14: all green.", True),
        (
            "Before merge: 10 runs on Android 14.",
            "Ran it 10 times on Android 13: all green.",
            False,
        ),
        ("Before merge: run e2e on iOS", "Ran e2e on iOS: green", True),
        (
            "20 runs in a row, 20 of 20",
            "Re 20 runs in a row, 20 of 20: CI green, merges cleanly.",
            False,
        ),
        (
            "20 runs in a row, 20 of 20",
            "Noted the 20 runs, 20 of 20 ask - CI green and merges cleanly.",
            False,
        ),
        (
            "20 runs in a row, 20 of 20",
            "20 runs, 20 of 20: CI is green, merges cleanly, done.",
            False,
        ),
        ("20 runs in a row, 20 of 20", "20 runs, 20/20 passed, 0 failures.", True),
        ("20 runs in a row, 20 of 20", "20 runs in a row, 20/20 passed, no flakes.", True),
        (
            "20 runs in a row, 20 of 20",
            "Ran it 20 times: 20/20 passed, not a single failure.",
            True,
        ),
        (
            "20 runs in a row, 20 of 20",
            "Ran 20 times, 20/20 green (it was red before the fix).",
            False,  # asides count
        ),
        ("20 runs in a row, 20 of 20", "20/20 passed.", True),
        ("20 runs in a row, 20 of 20", "20 of 20 green.", True),
        ("20 runs in a row, 20 of 20", "Looped it 20x: 20 of 20 passed.", True),
        ("Needs 20 runs in a row", "Looped it 20x, all green.", True),
    ],
)
def test_evidence_reports_the_result(criterion: str, evidence: str, answered: bool) -> None:
    c = Criterion(criterion, "lead", NOW)
    assert (c.answered_by(f"{c.ref}: met, {evidence}") is True) is answered
    assert c.answered_by(evidence) is None or "20 runs in a row, 20 of 20" in evidence


def test_evidence_must_come_after_and_report_a_result() -> None:
    before = Comment("dev-1", f"{REF}: met, 20 runs, 20/20 green", NOW - 6 * H)
    question = Comment("lead", "Is it 20/20 now?", NOW - H, human=True)
    assert len(unanswered(task(ASK, before, question), Snapshot(NOW), PATTERNS)) == 1
    after = Comment("dev-1", f"{REF}: met, 20 runs in a row, 20/20 green", NOW - H)
    assert unanswered(task(ASK, after), Snapshot(NOW), PATTERNS) == []
    own = Comment("lead", f"{REF}: met, did the 20 runs myself, 20/20 passed.", NOW - H, human=True)
    assert unanswered(task(ASK, own), Snapshot(NOW), PATTERNS) == []  # a person's confirmation
    s = Session(
        "S-1", "T-1", "dev-1", NOW - 3 * H, ended_at=NOW - H, summary=f"{REF}: met, 20/20 green"
    )
    assert unanswered(task(ASK), Snapshot(NOW, sessions=(s,)), PATTERNS) == []
    log = Artifact(f"logs/{REF}-met-20-of-20-passed.txt", NOW - H)
    art = Session("S-1", "T-1", "dev-1", NOW - 3 * H, ended_at=NOW - H, artifacts=(log,))
    assert unanswered(task(ASK), Snapshot(NOW, sessions=(art,)), PATTERNS) == []
    ci = Build("b-1", workflow=f"{REF} login 20 runs", branch="b",
               status=BuildStatus.SUCCESS, finished_at=NOW - H)  # fmt: skip
    assert unanswered(task(ASK, branch="b"), Snapshot(NOW, builds=(ci,)), PATTERNS) == []
    failed = Build("b-2", workflow=f"{REF} login 20 runs", branch="b",
                   status=BuildStatus.FAILURE, finished_at=NOW - H)  # fmt: skip
    assert unanswered(task(ASK, branch="b"), Snapshot(NOW, builds=(failed,)), PATTERNS)


def rule(**kw: object) -> ExitContract:
    return ExitContract(acceptance_patterns=PATTERNS, **kw)  # type: ignore[arg-type]


def pr(**kw: object) -> PullRequest:
    return PullRequest("example/app", 5, "T-1: fix", "b", task_ids=("T-1",), **kw)  # type: ignore[arg-type]


def alerts(*tasks: Task, **kw: object) -> list:
    found = rule().evaluate(Snapshot(NOW, tasks=tasks, **kw))  # type: ignore[arg-type]
    return [a for a in found if a.kind is ActionKind.ALERT]


def test_alerted_only_once_the_work_lands() -> None:
    t = task(ASK)
    assert alerts(t) == []  # in review: not yet
    # Set to merge by itself: critical before the merge, whatever the checks say now.
    [a] = alerts(t, pull_requests=(pr(labels=("automerge",)),))
    assert a.kind is ActionKind.ALERT and a.severity is Severity.CRITICAL and a.once
    assert a.params["notify"] == ["lead", "reviewer"] and "merge by itself" in a.reason
    [a] = alerts(t, pull_requests=(pr(mergeable=False), pr(labels=("AutoMerge",), mergeable=True)))
    assert a.severity is Severity.CRITICAL
    [w] = alerts(t, pull_requests=(pr(mergeable=True),))
    assert w.severity is Severity.WARNING and "can be merged now" in w.params["landing"]
    assert w.key != a.key  # a later merge label is told again, as critical
    busy = task(ASK, status=Status.IN_PROGRESS)
    assert alerts(busy, pull_requests=(pr(mergeable=True),)) == []  # not handed on yet
    assert alerts(t, pull_requests=(pr(draft=True, mergeable=True),)) == []
    merged = pr(closed_at=NOW - H, merged_at=NOW - H)
    [m] = alerts(busy, closed_pull_requests=(merged,))
    assert m.severity is Severity.CRITICAL and "was merged" in m.reason
    [a] = alerts(task(ASK, status=Status.ACCEPTED))
    assert a.params["landing"] == "it was accepted"
    assert alerts(Task("T-1", "x", Status.ACCEPTED, NOW - 30 * H, comments=(ASK,))) == []
    assert alerts(task(ASK, subtasks=("T-2",), status=Status.ACCEPTED)) == []  # a parent


def test_done_is_judged_only_when_an_agent_completed_it() -> None:
    done = task(ASK, status=Status.DONE)
    assert alerts(done) == []  # nobody's session declared done: a person closed it
    s = Session("S-1", "T-1", "dev-1", NOW - 3 * H, ended_at=NOW - 2 * H, final_status=Status.DONE)
    [a] = alerts(done, sessions=(s,))
    assert a.params["landing"] == "it was moved to done"
    assert list(ExitContract().evaluate(Snapshot(NOW, tasks=(done,), sessions=(s,)))) == []


def test_build_acceptance_config() -> None:
    r = build({"acceptance_patterns": [r"\bmust\b"], "acceptance_merge_labels": ["ship-it"],
               "acceptance_notify_roles": ["qa"]})  # fmt: skip
    assert r.acceptance_patterns[0].search("You MUST") and r.acceptance_merge_labels == ("ship-it",)
    for bad, message in [
        ({"acceptance_patterns": "x"}, "list of regular expressions"),
        ({"acceptance_patterns": ["("]}, "invalid pattern"),
        ({"acceptance_patterns": ["x?"]}, "empty line"),
        ({"acceptance_notify_roles": [""]}, "list of names"),
    ]:
        with pytest.raises(ValueError, match=message):
            build(bad)


def test_owners_known_as_people_are_people() -> None:
    plain = Comment("ops-lead", "Before merge: 5 runs in a row.", NOW - 3 * H)
    owners = (Owner("ops-lead", "lead", OwnerKind.HUMAN),)
    [a] = alerts(task(plain, status=Status.ACCEPTED), owners=owners)
    assert a.params["asked_by"] == "ops-lead"


def test_a_stricter_follow_up_is_a_criterion_of_its_own() -> None:
    first = Comment("lead", "Before merge: 10 runs in a row.", NOW - 5 * H, human=True)
    more = Comment("lead", "10 runs is not enough, make it 50 runs before merge.", NOW - 4 * H,
                   human=True)  # fmt: skip
    found = criteria(task(first, more), PATTERNS)
    assert len(found) == 2
    ref = Criterion(first.text, "lead", first.at).ref
    ten = Comment("dev-1", f"{ref}: met, ran it 10 times, all green.", NOW - H)
    [left] = unanswered(task(first, more, ten), Snapshot(NOW), PATTERNS)
    assert "50 runs" in left.text


def test_criteria_asked_after_the_work_landed_are_not_judged() -> None:
    merged = pr(closed_at=NOW - 3 * H, merged_at=NOW - 3 * H)
    later = Comment(
        "lead", "For the follow-up: 20 runs in a row before merge.", NOW - H, human=True
    )
    assert alerts(task(later, status=Status.IN_PROGRESS), closed_pull_requests=(merged,)) == []
    [a] = alerts(task(ASK, status=Status.IN_PROGRESS), closed_pull_requests=(merged,))
    assert "before this lands" not in a.reason  # it has landed


def test_a_merge_is_judged_whoever_closed_the_task() -> None:
    """Trackers move a task to done when its PR merges: the merge landed the work."""
    merged = pr(closed_at=NOW - 3 * H, merged_at=NOW - 3 * H)
    [a] = alerts(task(ASK, status=Status.DONE), closed_pull_requests=(merged,))
    assert a.severity is Severity.CRITICAL


@pytest.mark.parametrize(
    "line",
    ["{r}: met, saw the log, 20/20 confirmed.", "Met: 20/20, confirmed, thanks.",
     "{r}: \u2705 20 runs green.", "{r}: met, checked the log: 20 of 20 passed, thanks."],
)  # fmt: skip
def test_a_persons_verdict_is_no_new_criterion(line: str) -> None:
    report = Comment("dev-1", f"{REF}: met, ran it 20 times in a row, 20/20 passed.", NOW - 2 * H)
    lead = Comment("lead", line.format(r=REF), NOW - H, human=True)
    assert len(criteria(task(ASK, report, lead), PATTERNS)) == 1
    assert unanswered(task(ASK, report, lead), Snapshot(NOW), PATTERNS) == []


def test_a_bare_confirmation_is_a_criterion_of_its_own() -> None:
    """Without a verdict, "Saw the log, 20/20 confirmed" reads like any line with numbers:
    it becomes a criterion (noise, the safe way); "AC-1a2b3c: met" is how to confirm."""
    lead = Comment("lead", "Saw the log, 20/20 confirmed.", NOW - H, human=True)
    assert len(criteria(task(ASK, lead), PATTERNS)) == 2


def test_questions_requests_and_empty_criteria() -> None:
    polite = Comment("lead", "Can you do 20 runs in a row, 20 of 20, before automerge?", NOW - H,
                     human=True)  # fmt: skip
    assert len(criteria(task(polite), PATTERNS)) == 1
    empty = Comment("lead", "Run the whole suite 20 times. Before merging, make sure all pass.",
                    NOW - H, human=True)  # fmt: skip
    assert [c.text for c in criteria(task(empty), PATTERNS)] == ["Run the whole suite 20 times."]


@pytest.mark.parametrize(
    "line",
    ["CI green, merges cleanly. Approved.", "Can we merge? CI green.",
     "Did you do the 20 runs? CI is green.", "Ok, ran it.", "Done: 20 runs please.",
     "Checked: CI green 20 min ago, merges cleanly.", "Approved: 20/20 green CI checks.",
     "CI green: 20/20 jobs passed.", "20 runs: 20/20 passed, 2 retried."],
)  # fmt: skip
def test_a_persons_comment_is_evidence_only_when_it_reports_the_criterion(line: str) -> None:
    """Incident 18 again: a human reviewer's "CI green, merges cleanly" is no 20 of 20."""
    people = (Owner("rev", "reviewer", kind=OwnerKind.HUMAN),)
    review = Comment("rev", line, NOW - H)
    left = unanswered(task(ASK, review), Snapshot(NOW, owners=people), PATTERNS)
    assert ASK.text in [c.text for c in left]  # (a line like "20/20 jobs" is one of its own)
    found = alerts(task(ASK, review), pull_requests=(pr(labels=("automerge",)),), owners=people)
    assert any(a.severity is Severity.CRITICAL and "20 of 20" in a.reason for a in found)


@pytest.mark.parametrize(
    "line",
    ["Ran it 20 times in a row, all green.", "Ran 20 times in a row: all 20 passed.",
     "20 runs in a row, all passed, 0 failures."],
)  # fmt: skip
def test_all_of_a_count_passing_is_n_of_n(line: str) -> None:
    report = Comment("dev-1", f"{REF}: met, {line}", NOW - H)
    assert unanswered(task(ASK, report), Snapshot(NOW), PATTERNS) == []


@pytest.mark.parametrize(
    "line",
    ["Also 20 runs on the iOS simulator before merge.",
     "And 20 of 20 on Android too, before merge.",
     "Before merge also run it 20 times with the cache off."],
)  # fmt: skip
def test_a_follow_up_with_the_same_number_is_a_new_criterion(line: str) -> None:
    more = Comment("lead", line, NOW - 4 * H, human=True)
    assert len(criteria(task(ASK, more), PATTERNS)) == 2


@pytest.mark.parametrize(
    ("criterion", "evidence", "answered"),
    [
        # Not runs, or not all of them, or not done: no answer.
        ("20 runs in a row, 20 of 20", "All 20 checks passed, merges cleanly.", False),
        ("20 runs in a row, 20 of 20", "CI: all 20 checks green", False),
        ("20 runs in a row, 20 of 20", "20/20 workflows passed", False),
        ("20 runs in a row, 20 of 20", "20/20 builds green", False),
        ("20 runs in a row, 20 of 20", "Kicked off 20 runs, all green so far", False),
        (
            "20 runs in a row, 20 of 20",
            "Ran 3 times locally, all 3 passed. 20 runs queued on CI.",
            False,
        ),
        ("20 runs in a row, 20 of 20", "All green. 20 runs pending", False),
        ("20 runs in a row, 20 of 20", "20 runs, all green except one", False),
        ("20 runs in a row, 20 of 20", "20 runs: 18/20 passed, all others green", False),
        ("20 runs in a row, 20 of 20", "20 runs green, except run 14", False),
        ("20 runs in a row, 20 of 20", "20/20 green. Then 2 failed on the next loop.", False),
        ("20 runs in a row, 20 of 20", "20 runs green. Two flaked though.", False),
        ("Run it 20 times before merge.", "Ran it 20 times. Three failed.", False),
        ("Run it 20 times before merge.", "Ran it 20 times.", False),  # and how did they go?
        # Honest reports.
        ("20 runs in a row, 20 of 20", "20/20 green on test device", True),
        ("20 runs in a row, 20 of 20", "20/20 passed in test env", True),
        ("20 runs in a row, 20 of 20", "20/20 runs passed on the test device", True),
        ("20 runs in a row, 20 of 20", "Looped the flaky test 20 times: 20/20 green.", True),
        ("20 runs in a row, 20 of 20", "20/20 green, no issues", True),
        ("20 runs in a row, 20 of 20", "20/20 passed, nothing failed", False),  # "failed" vetoes
        ("20 runs in a row, 20 of 20", "Confirmed: 20 runs green.", True),
        (
            "20 runs in a row, 20 of 20",
            "Ran it 20 times: 20/20 passed. Next: drop the retry?",
            False,  # "retry" anywhere vetoes it
        ),
    ],
)  # fmt: skip
def test_review_cases(criterion: str, evidence: str, answered: bool) -> None:
    c = Criterion(criterion, "lead", NOW)
    assert (c.answered_by(f"{c.ref}: met, {evidence}") is True) is answered


@pytest.mark.parametrize(
    ("line", "new"),
    [
        ("Simulator 20/20 green, now do 20 of 20 on iPad before merge.", True),
        ("20 runs green on the simulator, now the same 20 runs on a real iPad before merge.", True),
        ("Friendly reminder: 20 runs in a row, 20 of 20 before automerge.", False),
        ("Don't forget: 20 runs in a row, 20 of 20 before automerge.", False),
        ("Still waiting on the 20 runs in a row, 20 of 20.", False),
        ("Saw the log: 20/20. Approving.", True),  # no verdict: a line with numbers
        ("LGTM, 20/20 in the log.", True),
    ],
)  # fmt: skip
def test_a_persons_later_line_is_a_new_criterion_only_when_it_asks_more(
    line: str, new: bool
) -> None:
    later = Comment("lead", line, NOW - 4 * H, human=True)
    assert len(criteria(task(ASK, later), PATTERNS)) == (2 if new else 1)


def test_only_evidence_that_addresses_the_criterion_answers_it() -> None:
    """Incident 18: "CI green, merges cleanly" and every other report that does not name or
    quote the criterion answers nothing, however it is worded."""
    c = Criterion(ASK.text, "lead", ASK.at)
    for text in ["Ran it 20 times in a row: 20/20 passed.", "Unit tests: 20/20 passed.",
                 "CI green, merges cleanly. Approved.", "AC-000000: 20/20 passed."]:  # fmt: skip
        assert c.answered_by(text) is None
    quoted = (
        f"> {ASK.text}\n\nMet: 20/20 passed.\n\n> Check the tablet.\n\nMet: works on the tablet."
    )
    assert c.answered_by(quoted) is True  # up to the next quote
    assert c.answered_by(f"{c.ref.lower()}: met, 20/20 passed") is True
    # Addressed, but no result, a partial one, a plan or a failure: no.
    for text in ["Unit tests: 20/20 passed.", "- [ ] 20 runs in a row: 20/20 passed",
                 "Target: 20/20 green.", "20/20 passed locally. On CI: 17/20.",
                 "20 runs: all green, one hung", "20/20 green. Then 2 crashed."]:  # fmt: skip
        assert c.answered_by(f"{c.ref}: met, {text}") is not True


def test_the_latest_addressed_evidence_decides_and_a_persons_failure_takes_it_back() -> None:
    good = Comment("dev-1", f"{REF}: met, 20 runs in a row, 20/20 passed.", NOW - 3 * H)
    bad = Comment("ci-bot", f"{REF}: met, nightly 3 of 20 failed.", NOW - 2 * H)
    again = Comment("dev-1", f"{REF}: met, fixed the race; 20 runs in a row, 20/20 green.", NOW - H)
    assert unanswered(task(ASK, good), Snapshot(NOW), PATTERNS) == []
    assert len(unanswered(task(ASK, good, bad), Snapshot(NOW), PATTERNS)) == 1
    assert unanswered(task(ASK, good, bad, again), Snapshot(NOW), PATTERNS) == []
    lead = Comment("lead", "Saw it fail twice on my machine.", NOW - H / 2, human=True)
    assert len(unanswered(task(ASK, good, lead), Snapshot(NOW), PATTERNS)) == 1


def test_a_persons_reminder_is_no_evidence() -> None:
    for text in ["Make sure all 20 pass before automerge.", "Verify 20/20 green before automerge.",
                 "Ping: 20 runs in a row, 20/20 green, then automerge."]:  # fmt: skip
        later = Comment("lead", text, NOW - H, human=True)
        assert len(unanswered(task(ASK, later), Snapshot(NOW), PATTERNS)) >= 1


def test_each_criterion_is_announced_once_with_its_reference() -> None:
    found = rule().evaluate(Snapshot(NOW, tasks=(task(ASK),)))
    [note] = [a for a in found if a.kind is ActionKind.COMMENT]
    assert note.once and note.key == f"acceptance-ask:T-1:{Criterion(ASK.text, 'lead', ASK.at).key}"
    assert f'starts with "{REF}: met"' in note.reason and ASK.text in note.reason
    answered = Comment("dev-1", f"{REF}: met, 20 runs in a row, 20/20 passed.", NOW - H)
    assert not [a for a in rule().evaluate(Snapshot(NOW, tasks=(task(ASK, answered),)))]
    done = task(ASK, status=Status.DONE)
    assert not [
        a for a in rule().evaluate(Snapshot(NOW, tasks=(done,))) if a.kind is ActionKind.COMMENT
    ]


def test_the_quoted_persons_own_words_are_no_evidence() -> None:
    """Incident 18 again: the rest of the person's line, or quote block, is theirs."""
    words = "Before automerge: 20 runs in a row, 20 of 20. Last time 20/20 passed on main."
    ask = Comment("lead", words, NOW - 5 * H, human=True)
    for text in [
        f"> {ask.text}\n\nOn it, not started yet.",
        f"> {ask.text.split('. ')[0]}.\nLast time 20/20 passed on main.\n\nWill do.",
        f"Re: {ask.text}",
    ]:
        reply = Comment("dev-1", text, NOW - H)
        left = unanswered(task(ask, reply), Snapshot(NOW), PATTERNS)
        assert "Before automerge: 20 runs in a row, 20 of 20." in [c.text for c in left], text


@pytest.mark.parametrize(
    ("text", "verdict"),
    [
        ("TODO. Login fix: 20/20 passed.", False),
        ("will rerun tomorrow. Earlier: 20/20 passed.", False),
        ("\n- [ ] 20 runs on device\n- [x] 20/20 passed in the simulator", False),
        ("pending.\n20/20 passed", False),
        ("ran it.\n\nSeparately: 20/20 passed on the other branch.", None),  # another paragraph
        ("20 runs in a row, 20/20 passed. Log: logs/AC-XXXXXX-20-of-20-passed.txt", True),
        ("20/20 passed (CI run: https://ci.example/AC-XXXXXX/42)", True),
        ("see the log above", None),
    ],
)
def test_what_addresses_it_decides(text: str, verdict: bool | None) -> None:
    c = Criterion(ASK.text, "lead", ASK.at)
    assert c.answered_by(f"{c.ref}: met, {text}".replace("AC-XXXXXX", c.ref)) is verdict


def test_a_persons_later_failure_report_or_reminder_takes_it_back() -> None:
    """Conservative: a person's failure wording on the task is not told apart from an
    unrelated one, and repeating the criterion reopens it."""
    good = Comment("dev-1", f"{REF}: met, 20 runs in a row, 20/20 passed.", NOW - 3 * H)
    reminder = Comment("lead", f"Reminder: {ASK.text}", NOW - H, human=True)
    left = unanswered(task(ASK, good, reminder), Snapshot(NOW), PATTERNS)
    assert ASK.text in [c.text for c in left]  # quoting it again reopens it: the safe way
    for text in ["lint failed on my branch, unrelated", "On my machine it failed twice in 20 runs.",
                 "No, 2 failed.", "Ran it: 18 green, 2 red."]:  # fmt: skip
        later = Comment("lead", text, NOW - H, human=True)
        assert ASK.text in [
            c.text for c in unanswered(task(ASK, good, later), Snapshot(NOW), PATTERNS)
        ]


def test_kelpies_own_announcement_is_neither_criterion_nor_evidence() -> None:
    good = Comment("dev-1", f"{REF}: met, 20 runs in a row, 20/20 passed.", NOW - 3 * H)
    [note] = [a for a in rule().evaluate(Snapshot(NOW, tasks=(task(ASK),)))
              if a.kind is ActionKind.COMMENT]  # fmt: skip
    for human in (False, True):
        echo = Comment("kelpie", note.reason, NOW - H, human=human)
        assert criteria(task(ASK, good, echo), PATTERNS) == criteria(task(ASK), PATTERNS)
        assert unanswered(task(ASK, good, echo), Snapshot(NOW), PATTERNS) == []


def test_criteria_that_would_share_a_reference_get_longer_ones() -> None:
    a = Comment("lead", "Before merge: 2917 runs of case 2917.", NOW - 3 * H, human=True)
    b = Comment("lead", "Before merge: 3753 runs of case 3753.", NOW - 2 * H, human=True)
    found = criteria(task(a, b), PATTERNS)
    assert len(found) == 2 and found[0].ref != found[1].ref
    assert all(len(c.ref) == len("AC-") + 8 for c in found)


def test_announcements_only_for_young_criteria_of_tasks_being_worked_on() -> None:
    def notes(t: Task, **kw: object) -> list:
        found = rule().evaluate(Snapshot(NOW, tasks=(t,), **kw))  # type: ignore[arg-type]
        return [a for a in found if a.kind is ActionKind.COMMENT]

    assert len(notes(task(ASK))) == 1  # in review, not landing
    assert notes(task(ASK, status=Status.BACKLOG)) == []
    assert notes(task(ASK, status=Status.BLOCKED)) == []
    old = Comment("lead", ASK.text, NOW - 100 * H, human=True)
    long = Task("T-1", "Fix flaky test", Status.REVIEW, NOW - 90 * H, comments=(old,))
    assert notes(long) == []  # stated and worked on days ago: not announced at first load
    assert len(notes(task(old))) == 1  # an old criterion, but work on the task began now
    # Landing: the alert alone, so no comment uses up the task's action budget before it.
    landing = notes(task(ASK), pull_requests=(pr(labels=("automerge",)),))
    assert landing == [] and alerts(task(ASK), pull_requests=(pr(labels=("automerge",)),))


def test_a_veto_anywhere_in_the_text_takes_it_back() -> None:
    c = Criterion(ASK.text, "lead", ASK.at)
    for text in [
        f"{c.ref}: met, 20/20 passed in the simulator.\n\nDevice: 3 of 20 crashed.",
        f"{c.ref}: met, 20/20 passed (log logs/{c.ref}.txt); 2 runs hung on device.",
        f"- [ ] {c.ref}: met, 20 runs in a row, 20/20 passed",
        f"Target for {c.ref}: met, 20/20 passed.",
        f"Working on {c.ref} next. Login fix: 20/20 passed.",
    ]:
        assert c.answered_by(text) is False, text


def test_the_person_who_asked_asking_again_is_no_answer() -> None:
    for text in [f"{REF}: met, please post the log showing 20/20 passed.",
                 f"{REF}: met, still waiting; last time 20/20 passed on main."]:  # fmt: skip
        again = Comment("lead", text, NOW - H, human=True)
        assert ASK.text in [c.text for c in unanswered(task(ASK, again), Snapshot(NOW), PATTERNS)]


def test_a_persons_failure_report_about_it_takes_the_answer_back() -> None:
    good = Comment("dev-1", f"{REF}: met, 20 runs in a row, 20/20 passed.", NOW - 3 * H)
    for text in ["It still fails on my machine.", "Flaked again on my machine just now."]:
        later = Comment("lead", text, NOW - H, human=True)
        assert len(unanswered(task(ASK, good, later), Snapshot(NOW), PATTERNS)) == 1, text


def test_echoes_and_references_make_no_new_criterion() -> None:
    note = f'Acceptance criterion {REF} from lead: "{ASK.text}". Before this lands, report it.'
    for text in [f"> {note}\n\nThanks, yes.", f"\n  {note}",
                 f"{REF}: reminder, 20 runs in a row, 20 of 20 before automerge."]:  # fmt: skip
        later = Comment("lead", text, NOW - H, human=True)
        assert [c.text for c in criteria(task(ASK, later), PATTERNS)] == [ASK.text], text


def test_round_eight_review_cases() -> None:
    c = Criterion(ASK.text, "lead", ASK.at)
    q = ASK.text
    vetoed = [
        f'{c.ref} ("{q}"): Simulator: 20/20 passed. Device: 3 of 20 crashed.',
        f"{q}\nDevice: 3 of 20 crashed.\n\n{c.ref}: met, 20/20 passed in the simulator.",
        f"{c.ref}: met, please post the log showing 20/20 passed.",
        f"{c.ref}: met, please confirm 20/20 passed before merging.",
        f"{c.ref}: met, I want to see 20/20 passed.",
        f"Requesting changes. {c.ref}: met, attach proof that 20/20 passed.",
        f"{c.ref}: met, 20/20 passed (run 7 failed once, re-ran it).",
        f"{c.ref}: met, 20/20 passed (2 flaky runs retried).",
        f"{c.ref}: met, 20/20 passed (17/20 on the first attempt).",
        f"{c.ref}: met, 20/20 passed eventually.",
        f"> {q}\n> (lead: {q})\n\n{c.ref}: met, 20/20 passed. Device: 3 of 20 crashed.",
    ]
    for text in vetoed:
        assert c.answered_by(text) is not True, text
    good = Comment("dev-1", f"{REF}: met, 20 runs in a row, 20/20 passed.", NOW - 3 * H)
    for text in ["Tried locally: 17/20.", "It fails on my machine.", "It hangs on my machine.",
                 "Got an error on my laptop.", "Doesn't work for me, red locally."]:  # fmt: skip
        later = Comment("lead", text, NOW - H, human=True)
        assert len(unanswered(task(ASK, good, later), Snapshot(NOW), PATTERNS)) == 1, text
    own = Comment(
        "lead", f"{REF}: met, ran it on my machine too, 20/20 passed.", NOW - H, human=True
    )
    assert unanswered(task(ASK, own), Snapshot(NOW), PATTERNS) == []


@pytest.mark.parametrize(
    "line",
    ["Merge after 20 runs green on the device.",
     "After 20/20 green on the device, merge.", "I'd like 20/20 green on a real device too.",
     "20/20 green on the iPad as well, then automerge.", "Also 20 of 20 green on Android.",
     "Now 20 runs green on the device too."],
)  # fmt: skip
def test_a_requirement_with_result_words_is_still_a_criterion(line: str) -> None:
    later = Comment("lead", line, NOW - 4 * H, human=True)
    assert line in [c.text for c in criteria(task(ASK, later), PATTERNS)]


@pytest.mark.parametrize(
    "text",
    ["{r}: 20/20 green before automerge.", "{r}: merge only after 20/20 green.",
     "{r}: gate automerge on 20/20 green.", "{r}: 20/20 green is the bar.",
     "{r}: met, 20/20 passed (one errored in setup and was re-run).",
     "{r}: met, 20/20 passed (with one rerun).", "{r}: met, 20/20 passed. Run 7 errored in setup.",
     "{r}: met, 20/20 passed; saw 2 timeouts in teardown.",
     "{r}: met, 20/20 passed. Run 4: AssertionError.",
     "{r}: met, 20/20 passed after restarting the emulator twice.",
     "{r}: met, 20/20 passed on the second attempt.",
     "{r}: met, 20/20 passed. No it failed twice.",
     "{r}: met, 20/20 passed. Nothing failed except run 7."],
)  # fmt: skip
def test_requirements_retries_and_errors_answer_nothing(text: str) -> None:
    c = Criterion(ASK.text, "lead", ASK.at)
    assert c.answered_by(text.format(r=c.ref)) is not True


def test_any_failure_a_person_reports_later_takes_it_back() -> None:
    good = Comment("dev-1", f"{REF}: met, 20 runs in a row, 20/20 passed.", NOW - 3 * H)
    for text in ["It fails on the device.", "I see 2 failures.", "Nope, 2 of them failed.",
                 "Tried it, it hangs.", "It errored twice here.",
                 "No it failed on my machine."]:  # fmt: skip
        later = Comment("lead", text, NOW - H, human=True)
        assert ASK.text in [
            c.text for c in unanswered(task(ASK, good, later), Snapshot(NOW), PATTERNS)
        ]


@pytest.mark.parametrize(
    "reply",
    ["{r}: not met.", "{r}: not met, only 18 green in my run.", "{r}: unmet",
     "{r}: ❌ only 18 green", "{r}: met? No.", "Not met: I only see 18 green runs in that log.",
     "Nope, the log stops at run 18 and it failed."],
)  # fmt: skip
def test_a_persons_no_takes_the_answer_back(reply: str) -> None:
    good = Comment("dev-1", f"{REF}: met, 20/20 passed, https://ci.example/run/42", NOW - 3 * H)
    later = Comment("lead", reply.format(r=REF), NOW - H, human=True)
    assert ASK.text in [c.text for c in unanswered(task(ASK, good, later), Snapshot(NOW), PATTERNS)]


@pytest.mark.parametrize(
    "tail",
    ["(so far)", "(except run 7)", "(all but one)", "(not on the device)",
     "(still running the last batch)", "(didn't run on CI)", "(without the fix)",
     "out of 21", "of 21 runs", "in 21 runs", "(21 runs total)", "excluding run 7",
     "ignoring run 7", "with 1 run discarded", "2 runs got stuck", "in a mock environment"],
)  # fmt: skip
def test_asides_and_cherry_picked_results_answer_nothing(tail: str) -> None:
    c = Criterion(ASK.text, "lead", ASK.at)
    assert c.answered_by(f"{c.ref}: met, 20/20 passed {tail}") is not True


@pytest.mark.parametrize(
    "link",
    ["https://ci.example/run/42", "https://github.example/o/r/actions/runs/123456/job/789",
     "https://gitlab.example/o/r/-/pipelines/42", "https://github.example/o/r/actions/runs/1/attempts/1",
     "https://ci.example/fix-flaky-login/42", "https://ci.example/app/1234/5678"],
)  # fmt: skip
def test_the_announced_format_with_real_links_answers(link: str) -> None:
    c = Criterion(ASK.text, "lead", ASK.at)
    assert c.answered_by(f"{c.ref}: met, 20/20 passed, {link}") is True
    assert c.answered_by(f"`{c.ref}`: met, ran the flaky login test 20 times, 20/20 passed") is True


def test_a_new_criterion_next_to_a_verdict_is_kept() -> None:
    for text in [f"{REF}: met, thanks. Next: 10 runs on the tablet before merge.",
                 f"Thanks. {REF}: met. Also 10 runs on the tablet before merge.",
                 "LGTM once it passes 50 runs in a row.", "Approved, pending 50 runs in a row.",
                 "Failed twice for me. 50 runs in a row, 50 of 50."]:  # fmt: skip
        later = Comment("lead", text, NOW - H, human=True)
        assert len(criteria(task(ASK, later), PATTERNS)) == 2, text


@pytest.mark.parametrize(
    "tail",
    ["https://ci.example.com/r/1\n\nNo other changes.",
     "https://ci.example.com/r/1\n\nThe test no longer depends on ordering.",
     "https://ci.example.com/r/1\n\nThe fix adds a lock so teardown can't race.",
     "https://ci.example.com/r/1\n\nI did not touch the other tests.",
     "https://ci.example/r/1. Should be good to go.",
     "https://ci.example/r/1. Ran with -p no:randomly.",
     "workflow run: https://ci.example/r/1",
     "build: <https://ci.example/b/42>", "0 failed", "no red runs"],
)  # fmt: skip
def test_ordinary_words_around_a_clean_answer_are_fine(tail: str) -> None:
    c = Criterion(ASK.text, "lead", ASK.at)
    assert c.answered_by(f"{c.ref}: met, 20/20 passed, {tail}") is True


@pytest.mark.parametrize(
    "reply",
    ["{r}: isn't met", "This isn't met.", "Not yet met.", "Wasn't met.", "Met? No.",
     "I don't think it's met", "No, that's not 20 in a row.", "No, please redo it"],
)  # fmt: skip
def test_a_person_saying_no_in_other_words_takes_it_back(reply: str) -> None:
    good = Comment("dev-1", f"{REF}: met, 20/20 passed, https://ci.example/run/42", NOW - 3 * H)
    later = Comment("lead", reply.format(r=REF), NOW - H, human=True)
    assert ASK.text in [c.text for c in unanswered(task(ASK, good, later), Snapshot(NOW), PATTERNS)]


def test_a_new_ask_in_the_verdict_sentence_is_a_criterion() -> None:
    for text in [f"{REF}: met, but run it 50 times on the iOS simulator before merge too",
                 f"{REF}: ✅ next, 50 runs on the iOS simulator before automerge"]:  # fmt: skip
        later = Comment("lead", text, NOW - H, human=True)
        assert any("50" in c.text for c in criteria(task(ASK, later), PATTERNS)), text


@pytest.mark.parametrize(
    "tail",
    ["run 7 failed/retried/passed", "(run 7: fail/retry/pass)", "(run 7 removed)",
     "(leaving out run 7)", "minus run 7 (infra)", "(top 20 runs)", "runs 3-22"],
)  # fmt: skip
def test_slashed_failures_and_left_out_runs_veto(tail: str) -> None:
    c = Criterion(ASK.text, "lead", ASK.at)
    assert c.answered_by(f"{c.ref}: met, 20/20 passed, {tail}") is not True


def test_long_texts_take_linear_time() -> None:
    import time

    c = Criterion(ASK.text, "lead", ASK.at)
    for text in ["a." * 40000, "<" * 80000, "(" * 80000, f"{c.ref}: met, " + "a/" * 40000]:
        start = time.monotonic()
        c.answered_by(text)
        later = Comment("lead", text, NOW - H, human=True)
        unanswered(task(ASK, later), Snapshot(NOW), PATTERNS)
        assert time.monotonic() - start < 2, text[:20]
