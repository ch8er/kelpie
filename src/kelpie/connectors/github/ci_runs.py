"""CI state of a commit from GitHub Actions runs.

The fallback for a token that may not read check runs or commit statuses (a fine-grained
personal access token has no Checks permission; statuses need ``Commit statuses: read``):
``GET /repos/{repo}/actions/runs?head_sha=<sha>`` needs only ``Actions: read``.

Rules, all of them deterministic and pure (no I/O):

- ``workflows`` lists required workflows, each a workflow name or its file path
  (``.github/workflows/ci.yml``), matched exactly, case included. They count in full, whatever
  the event, and **each must have a result**: one without a run, or with nothing but skipped
  runs, is ``pending``, never green (it may not have started yet, or it never runs for this
  commit).
- What counts *in full* (can make the commit green, and satisfies a listed workflow) is
  decided by the listed workflows and, with an empty list, by the events ``push``,
  ``pull_request`` and ``merge_group``; and a ``pull_request`` run counts in full only if it
  names pull request ``pr`` in its ``pull_requests`` (the same head commit in two PRs does not
  share a green, and a run whose PR is unknown, a fork's, is not trusted for one).
- **Every run that does not count in full counts one-sided**, whatever the reason (an
  unlisted workflow, another event, another or an unknown PR): a concluded red makes the
  commit ``failure``, but it can never make it ``success``; and it makes it ``pending`` only
  while a re-run (``run_attempt`` above 1) is in progress, because the runs list shows only
  the latest attempt and the red it replaced would otherwise be forgotten. The same
  latest-run rule applies, per PR for pull request runs, so a re-run that went green clears
  an earlier red. So a list and the PR filter add requirements and remove nothing: the
  check-run path shows such a red too.
- One run counts per workflow and event: the one that started last (``run_started_at``,
  which a re-run moves to the new attempt), then the highest ``run_attempt``,
  ``run_number`` and id. A later run that was skipped or neutral does not replace an
  earlier real result: the latest run that was not skipped counts, and only a group that
  was never anything but skipped counts as skipped. A failed first attempt that a re-run
  fixed is green. A workflow is told apart by ``workflow_id``, else its ``path``, else its
  name; a run with none of them counts on its own.
- ``failure`` if a counted run concluded ``failure``, ``cancelled``, ``timed_out``,
  ``action_required``, ``startup_failure`` or ``stale``; this wins over a run still going.
- ``pending`` if a counted run is not completed yet (queued, waiting, in progress) or
  completed with a conclusion this module does not know, or a listed workflow has no result.
- ``skipped`` and ``neutral`` do not block, but they are no evidence either: when every
  counted run is skipped or neutral, or none counts, the state is ``none``, never green.
  (The check-run path counts them as success; there GitHub itself has reported the checks
  that were required to run.)
- ``incomplete`` if the commit has more runs than were read and no failure is visible:
  what is hidden may be red or still running.
- ``success`` only when every counted run concluded ``success`` (or was skipped or neutral)
  and at least one did conclude ``success``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kelpie.core.serde import parse_datetime

RED = frozenset(
    {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
)
NEUTRAL = frozenset({"skipped", "neutral"})
COUNTED_EVENTS = frozenset({"push", "pull_request", "merge_group"})  # without being listed
_EPOCH = datetime.min.replace(tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RunsVerdict:
    state: str  # success | failure | pending | none | incomplete
    completed_at: datetime | None  # latest update of a successful counted run
    missing: tuple[str, ...] = ()  # listed workflows without a run


def judge(
    runs: Sequence[Mapping[str, Any]],
    workflows: Collection[str] = (),
    *,
    more: bool = False,
    pr: int | None = None,
) -> RunsVerdict:
    """The CI state of one commit from its workflow runs. ``more``: the list was cut off."""
    full = [
        r
        for r in runs
        if _for_pr(r, pr) and (_listed(r, workflows) if workflows else _counted_event(r))
    ]
    latest = latest_runs(full)
    verdicts = [_run_state(run) for run in latest]
    # Every run that does not count in full: red only (see the module docstring).
    rest = latest_runs([r for r in runs if not any(r is f for f in full)], by_pr=True)
    rest_red = any(_run_state(r) == "failure" for r in rest)
    rest_rerun = any(
        r.get("status") != "completed" and _number(r.get("run_attempt")) > 1 for r in rest
    )
    # A listed workflow with no run, or none but skipped ones, has not produced a result.
    missing = tuple(
        w
        for w in workflows
        if not any(
            _listed(r, (w,)) and v != "skipped" for r, v in zip(latest, verdicts, strict=True)
        )
    )
    if "failure" in verdicts or rest_red:
        state = "failure"
    elif more:
        state = "incomplete"
    elif "pending" in verdicts or missing or rest_rerun:
        state = "pending"
    elif "success" in verdicts:
        state = "success"
    else:
        state = "none"  # nothing counted, or everything was skipped
    times = [
        t
        for run, v in zip(latest, verdicts, strict=True)
        if v == "success" and (t := parse_time(run.get("updated_at"))) is not None
    ]
    done = max(times, default=None) if state == "success" else None
    return RunsVerdict(state, done, missing)


def latest_runs(runs: Sequence[Mapping[str, Any]], by_pr: bool = False) -> list[Mapping[str, Any]]:
    """One run per workflow and event (and per set of pull requests with ``by_pr``), in the
    order of the input: the latest one, or, if that was skipped, the latest that was not (see
    the module docstring)."""
    groups: dict[Any, list[Mapping[str, Any]]] = {}
    for position, run in enumerate(runs):
        event = run.get("event")
        prs = _pull_requests(run) if by_pr and event == "pull_request" else ()
        key = (_workflow(run, position), event if isinstance(event, str) else None, prs)
        groups.setdefault(key, []).append(run)
    picked = []
    for group in groups.values():
        group.sort(key=_recency, reverse=True)
        picked.append(next((r for r in group if _run_state(r) != "skipped"), group[0]))
    return picked


def _pull_requests(run: Mapping[str, Any]) -> tuple[int, ...]:
    entries = run.get("pull_requests")
    numbers = (
        [e.get("number") for e in entries if isinstance(e, Mapping)]
        if isinstance(entries, list)
        else []
    )
    return tuple(sorted(n for n in numbers if isinstance(n, int) and not isinstance(n, bool)))


def _counted_event(run: Mapping[str, Any]) -> bool:
    event = run.get("event")
    return isinstance(event, str) and event in COUNTED_EVENTS


def _listed(run: Mapping[str, Any], workflows: Collection[str]) -> bool:
    """Whether a run is of one of the workflows, by name or by exact file path."""
    return run.get("name") in workflows or run.get("path") in workflows


def _for_pr(run: Mapping[str, Any], pr: int | None) -> bool:
    if pr is None or run.get("event") != "pull_request":
        return True
    entries = run.get("pull_requests")
    if not isinstance(entries, list):
        return False
    return any(isinstance(e, Mapping) and e.get("number") == pr for e in entries)


def _workflow(run: Mapping[str, Any], position: int) -> tuple[str, Any]:
    """What tells workflows apart; a run that names none of them counts on its own."""
    workflow_id = run.get("workflow_id")
    if isinstance(workflow_id, int) and not isinstance(workflow_id, bool):
        return "workflow_id", workflow_id
    for field in ("path", "name"):
        value = run.get(field)
        if isinstance(value, str) and value:
            return field, value
    return "position", position


def _recency(run: Mapping[str, Any]) -> tuple[datetime, int, int, int]:
    started = parse_time(run.get("run_started_at")) or parse_time(run.get("created_at")) or _EPOCH
    return (
        started,
        _number(run.get("run_attempt")),
        _number(run.get("run_number")),
        _number(run.get("id")),
    )


def _run_state(run: Mapping[str, Any]) -> str:
    if run.get("status") != "completed":
        return "pending"
    conclusion = run.get("conclusion")
    if conclusion == "success":
        return "success"
    if conclusion in NEUTRAL:
        return "skipped"
    return "failure" if conclusion in RED else "pending"


def _number(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_datetime(value.replace("Z", "+00:00"))
    except ValueError:
        return None
