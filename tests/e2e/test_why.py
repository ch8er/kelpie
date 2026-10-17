"""``kelpie why`` on a live simulated farm: the conditions ``kelpie serve`` attaches to blocks
from ``[[block_conditions]]`` (#126).

Serve keeps those conditions in memory, so ``why`` rebuilds them. Offline it shows what a
block reason would attach, marked as not checked; with ``--refresh`` it resolves and checks
the PR on the fake GitHub as a tick does. A block no pattern matches shows none.
"""

from __future__ import annotations

import json

from kelpie.sim.github import sha_of
from tests.sim.farm import REPO, Farm
from tests.sim.kelpie import KelpieServe, wait_for

PATTERN = {
    "pattern": r"waiting for (?:ci/)?merge of (?P<pr>\S+)",
    "kind": "pr_merged",
    "repo": REPO,
}
ATTACHED = 'kelpie_block_conditions{outcome="attached"}'


def blocked(farm: Farm, task_id: str, reason: str, title: str) -> None:
    b = farm.board
    b.task(task_id, "todo", farm.ago(hours=4), title=title, assignee="dev")
    b.claim(task_id, "dev", farm.ago(hours=3))
    b.block(task_id, reason, farm.ago(hours=2))


def conditions_section(text: str) -> list[str]:
    """The lines under "Conditions", up to the next section."""
    lines = text.splitlines()
    start = lines.index("Conditions") + 1
    end = next(i for i in range(start, len(lines)) if not lines[i].startswith("  "))
    return lines[start:end]


def start_serve(farm: Farm, *, attached: int) -> KelpieServe:
    kelpie = farm.serve(farm.config(github=True, extra={"block_conditions": [PATTERN]}))
    wait_for(
        f"{attached} block condition(s) attached",
        lambda: kelpie.metrics().get(ATTACHED) == attached,
        timeout=30,
    )
    return kelpie


def test_a_block_condition_serve_attached_shows_in_why(farm: Farm) -> None:
    farm.github.pull(REPO, 57)
    farm.github.pull(REPO, 58, merged_at=farm.ago(minutes=20), state="closed")
    blocked(farm, "t_wait", "waiting for CI/merge of #57", "Review the export")
    blocked(farm, "t_done", "waiting for merge of #58", "Review the import")
    blocked(farm, "t_vague", "stuck, not sure why", "Tidy the config")
    kelpie = start_serve(farm, attached=2)  # the live case: attached 2, and why said "none"
    head = farm.github.repo(REPO).pulls[57].head_sha
    assert head == sha_of(f"{REPO}#57")

    # Offline (the default): the pattern match needs no network, the PR is not looked up.
    offline = kelpie.cli("why", "t_wait")
    assert offline.code == 0, offline.stderr
    section = conditions_section(offline.stdout)
    assert section == [
        f"  would attach pr_merged on {REPO}#57 (from the block reason): "
        "not checked (offline; --refresh checks them)"
    ]
    assert "GitHub conditions were not read (offline)" in offline.stdout
    assert "note: offline: serve treats this block as a wait on example/repo#57" in offline.stdout

    # --refresh: resolved and checked as one tick of serve does.
    refreshed = kelpie.cli("why", "t_wait", "--refresh")
    assert refreshed.code == 0, refreshed.stderr
    assert "note: offline:" not in refreshed.stdout  # the condition is attached: no caveat
    [line] = conditions_section(refreshed.stdout)
    assert line.startswith(
        f"  block:t_wait:pr_merged:{REPO}#57 [pr_merged] {REPO}#57 @ {head[:12]}"
    )
    assert ": unmet (PR open), checked " in line
    merged = conditions_section(kelpie.cli("why", "t_done", "--refresh").stdout)
    assert len(merged) == 1 and f"{REPO}#58" in merged[0] and ": met (PR merged)" in merged[0]

    # --json: additive fields on the same schema version.
    data = json.loads(kelpie.cli("why", "t_wait", "--refresh", "--json").stdout)
    assert data["schema_version"] == 1 and data["refreshed"] is True
    [cond] = data["conditions"]
    assert cond["target"] == {"repo": REPO, "pr": 57, "sha": head, "head_sha": head}
    assert cond["kind"] == "pr_merged" and cond["state"] == "unmet" and cond["checked_at"]
    assert data["expected_conditions"] == []
    off = json.loads(kelpie.cli("why", "t_wait", "--json").stdout)
    assert off["conditions"] == [] and off["refreshed"] is False
    [exp] = off["expected_conditions"]
    assert (exp["status"], exp["kind"], exp["repo"], exp["pr"]) == (
        "not_checked",
        "pr_merged",
        REPO,
        57,
    )

    # Negative case: the reason matches no pattern, so there is nothing to attach.
    for extra in ((), ("--refresh",)):
        vague = kelpie.cli("why", "t_vague", *extra)
        assert vague.code == 0, vague.stderr
        assert conditions_section(vague.stdout) == [
            "  none (the block reason matches none of the 1 [[block_conditions]] patterns)"
        ]
        assert "would attach" not in vague.stdout

    # why is read-only: the board is untouched and Hermes was never called.
    assert farm.board.statuses()["t_wait"] == "blocked"
    assert farm.hermes_calls() == []


def test_a_pattern_hit_that_attaches_nothing_says_why(farm: Farm) -> None:
    farm.github.repo(REPO)  # no PR 99 on it: GitHub says 404
    blocked(farm, "t_gone", "waiting for merge of #99", "Review the cache")
    blocked(farm, "t_elsewhere", "waiting for merge of other/repo#5", "Review the queue")
    kelpie = start_serve(farm, attached=0)

    gone = kelpie.cli("why", "t_gone", "--refresh").stdout
    [line] = conditions_section(gone)
    assert line.startswith(f"  matched pr_merged on {REPO}#99 (from the block reason): GitHub did")

    elsewhere = kelpie.cli("why", "t_elsewhere").stdout
    [line] = conditions_section(elsewhere)
    assert "names other/repo#5, a repository outside [connectors.github] repos" in line
    assert line.endswith("nothing is attached")


# -- #148: a parent whose subtasks' PRs all merged ---------------------------------------------

# Serve knows the lists in full after its second scan. cache_ttl (30s, the shortest) plus
# tick_budget (10s) must stay below poll_interval, or the scans are answered from the cache.
PULLS = {"connectors": {"github": {"pulls": {"poll_interval": "45s"}}}}
OFFLINE_LISTS = (
    "offline: why does not read the PR lists; serve, which does, may judge this parent "
    "differently (ask to close it, name unmerged PRs, or not count it finished)"
)
REFRESH_LISTS = (
    "why reads the PR lists once; serve knows them in full only after two scans in a row "
    "and may say parent-left-open instead"
)


def test_a_parent_with_merged_prs_says_serve_may_know_better(farm: Farm) -> None:
    """The live case of #148: ``why`` says parent-unverified, a note says serve may know
    better, and after its second PR scan serve does ask to close the parent."""
    b = farm.board
    b.task("t_parent", "ready", farm.ago(days=2), title="Store front", assignee="orch")
    b.event("t_parent", "decomposed", farm.ago(days=1), {"child_ids": ["t_1a", "t_1b"]})
    done = farm.ago(hours=4)
    for number, child in ((71, "t_1a"), (72, "t_1b")):
        b.task(
            child, "done", farm.ago(days=1), title=f"Part {child}", assignee="dev", completed=done
        )
        b.event(child, "completed", done)
        farm.github.pull(
            REPO, number, state="closed", merged_at=done, title=f"{child}: part", branch=child
        )  # merged into the default branch ("main")
    kelpie = farm.serve(farm.config(github=True, extra=PULLS))
    wait_for(
        "a tick", lambda: kelpie.metrics().get("kelpie_engine_ticks_total", 0) >= 1, timeout=30
    )

    for extra, want in (((), OFFLINE_LISTS), (("--refresh",), REFRESH_LISTS)):
        out = kelpie.cli("why", "t_parent", *extra)
        assert out.code == 0, out.stderr
        assert "the code host's PRs are not fully known" in out.stdout
        assert f"    note: {want}" in out.stdout.splitlines()
        data = json.loads(kelpie.cli("why", "t_parent", "--json", *extra).stdout)
        [finding] = [f for f in data["findings"] if f["rule"] == "orphan_status"]
        assert finding["note"] == want and data["schema_version"] == 1

    # Serve's second scan completes the lists: it asks to close the parent. That is the
    # premise of the note.
    # The rule is in observe mode, so the alert is an open state, not a message.
    def left_open() -> str | None:
        out = kelpie.cli("why", "t_parent", "--json")
        states = json.loads(out.stdout).get("states") if out.code == 0 else None
        if not states:  # a transient failure (or nothing open yet): poll again
            return None
        return next((s["id"] for s in states if ":parent-left-open:t_parent:" in s["id"]), None)

    wait_for("serve to ask to close t_parent (its second PR scan)", left_open, timeout=90)

    # ...while one scan of why still reads the lists as not known in full, and says so.
    again = kelpie.cli("why", "t_parent", "--refresh")
    assert "the code host's PRs are not fully known" in again.stdout
    assert f"    note: {REFRESH_LISTS}" in again.stdout.splitlines()

    # A finished subtask has no verdict on the lists: no such note on it.
    other = kelpie.cli("why", "t_1a", "--refresh")
    assert other.code == 0, other.stderr
    assert "PR lists" not in other.stdout

    # why is read-only: the board is untouched and Hermes was never called.
    assert farm.board.statuses()["t_parent"] == "ready"
    assert farm.hermes_calls() == []
