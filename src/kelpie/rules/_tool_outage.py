"""Rule 10, correlated tool failures: one outage (issue #229, signal 2, incident 20).

On the live farm the Xcode installation broke for ~12 h: every ``ios_sim.sh`` and
``xcodebuild`` run failed on the farm's own Mac, not in GitHub's CI, so the failing-check
outage never fired and each agent blocked its task on its own. The tool calls said it: the
same tool failing with the same exit code for task after task.

**Signs.** Tool spans of the snapshot whose command names one of ``tools`` (config data: the
farm's build and test runners, e.g. ``ios_sim.sh``, ``xcodebuild``; a whole word of the
command as the trace kept it, path and redaction aside) and whose result the adapter knows
(:mod:`kelpie.core.tools`): a foreground call that exited non-zero is a failure of that
tool with its exit code as the error class (``127`` not found, ``124`` timed out, ``1`` a
failed build); one that exited 0 is a success. A call sent to the background (its ``0``
means started) and one the platform refused to run (blocked) are neither, and a span with
no known result does not count.

**Open.** Failures of one tool with one exit code by at least ``min_tasks`` distinct tasks
within ``window`` of each other (a call's task is its span's, else its session's; a session
the snapshot ties to no task counts as one task), none followed by a success of that tool
(in any task) → one ``warning`` outage of kind :data:`~kelpie.core.incidents.TOOL_FAILURES`
per tool and exit code: since its first failure after the tool's last success, scoped to
the tasks that failed (they are the tasks it explains), with the exit code's meaning as its
cause. One task retrying the same failure is no outage. It resolves when the tool runs
successfully again (:func:`ran_again`), or when no failure is left in the snapshot's spans:
the window is the ``[traces] window`` at most, whose spans the snapshot holds.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.incidents import TOOL_FAILURES, Outage, incident_state_id
from kelpie.core.model import Snapshot, Span, SpanKind
from kelpie.core.tools import ERROR, OK, exit_code_of, result_of
from kelpie.rules._alerts import alert_params, count
from kelpie.rules._ci_outage import _listed

COMMAND_ATTRIBUTES = ("command", "kelpie.tool.command_prefix")  # the hook's, then OTLP's
MAX_TASKS = 50  # tasks an outage's params list
# What an exit code usually means, for the cause line (POSIX shells, timeout(1)).
MEANINGS = {
    124: "timed out",
    126: "found but not executable",
    127: "not found",
    130: "interrupted",
    137: "killed (out of memory, or SIGKILL)",
    139: "crashed (segmentation fault)",
}


@dataclass(frozen=True, slots=True)
class _Call:
    at: datetime
    tool: str
    task: str | None  # the call's task, or its session's
    who: str  # what counts as one task: the task, else the session, else the call itself
    ok: bool
    code: int | None  # the exit code of a failure


def tool_outage_state(rule: str, tool: str, code: int) -> str:
    """The state id of ``rule``'s outage of ``tool`` exiting ``code``."""
    return incident_state_id(rule, "tool", tool, str(code))


def tool_outages(
    snapshot: Snapshot,
    rule: str,
    tools: Collection[str],
    min_tasks: int,
    window: timedelta,
    observe: bool = True,
) -> list[Action]:
    """One alert per open outage of a tool (see the module docs); none without ``tools``.
    With ``observe`` (a new check) the alerts are only logged."""
    if not tools or min_tasks <= 0:
        return []
    out = []
    for (tool, code), failures in sorted(_open(_calls(snapshot, tools)).items()):
        since = _since(failures, min_tasks, window)
        if since is None:
            continue
        scope = [f for f in failures if f.at >= since]
        out.append(replace(_alert(rule, tool, code, scope, since), observe_only=observe))
    return out


def ran_again(state_id: str, snapshot: Snapshot, rule: str, tools: Collection[str]) -> str | None:
    """For the "Resolved" notice: the tool's latest success, when the state is one of its
    outages and it ran successfully again; None otherwise (the engine's general notice)."""
    calls = _calls(snapshot, tools)
    for tool in sorted({c.tool for c in calls}):
        codes = {c.code for c in calls if c.tool == tool and c.code is not None}
        if not any(tool_outage_state(rule, tool, code) == state_id for code in codes):
            continue
        ok = [c for c in calls if c.tool == tool and c.ok]
        if ok:
            last = max(ok, key=lambda c: c.at)
            on = f" in {last.task}" if last.task else ""
            return f"{tool} ran successfully again at {last.at:%Y-%m-%d %H:%M} UTC{on}."
    return None


def _calls(snapshot: Snapshot, tools: Collection[str]) -> list[_Call]:
    names = set(tools)
    tasks = {s.id: s.task_id for s in snapshot.sessions}
    out = []
    for i, span in enumerate(snapshot.spans):
        if span.kind is not SpanKind.TOOL:
            continue
        result = result_of(span)
        if result not in (OK, ERROR):
            continue  # background (started), blocked (refused), or not known
        tool = _tool(span, names)
        if tool is None:
            continue
        code = exit_code_of(span) if result == ERROR else None
        if result == ERROR and code is None:
            continue  # a failure without its class cannot be grouped
        task = span.task_id or tasks.get(span.session_id or "")
        who = task or (f"session {span.session_id}" if span.session_id else f"call {i}")
        out.append(_Call(span.end or span.start, tool, task, who, result == OK, code))
    return sorted(out, key=lambda c: c.at)


def _tool(span: Span, names: set[str]) -> str | None:
    """The configured tool a call's command names: the first whole word, path aside."""
    for key in COMMAND_ATTRIBUTES:
        text = span.attributes.get(key)
        if not isinstance(text, str):
            continue
        for word in text.split():
            base = word.rstrip("/").rsplit("/", 1)[-1]
            if base in names:
                return base
    return None


def _open(calls: Sequence[_Call]) -> dict[tuple[str, int], list[_Call]]:
    """Failures per tool and exit code, since the tool's last success."""
    last_ok: dict[str, datetime] = {}
    for c in calls:
        if c.ok:
            last_ok[c.tool] = c.at
    found: dict[tuple[str, int], list[_Call]] = defaultdict(list)
    for c in calls:
        if not c.ok and c.code is not None and (c.tool not in last_ok or c.at > last_ok[c.tool]):
            found[c.tool, c.code].append(c)
    return found


def _since(failures: Sequence[_Call], min_tasks: int, window: timedelta) -> datetime | None:
    """The first failure of the earliest run of ``min_tasks`` distinct tasks within
    ``window`` of each other, or None."""
    for i, first in enumerate(failures):
        tasks = {c.who for c in failures[i:] if c.at - first.at <= window}
        if len(tasks) >= min_tasks:
            return first.at
    return None


def _alert(rule: str, tool: str, code: int, scope: Sequence[_Call], since: datetime) -> Action:
    state = tool_outage_state(rule, tool, code)
    tasks = sorted({c.task for c in scope if c.task})
    meaning = MEANINGS.get(code, "failed")
    distinct = len({c.who for c in scope})
    cause = f"{tool} exits {code} ({meaning}) for {distinct} tasks"
    outage = Outage(
        state=state,
        signature=f"{tool} exit {code}",
        kind=TOOL_FAILURES,
        cause=cause,
        tasks=tuple(tasks[:MAX_TASKS]),
    )
    reason = (
        f"Tool outage since {since:%Y-%m-%d %H:%M} UTC: {cause}, {len(scope)} run(s), and "
        f"nothing has run {tool} successfully since. The cause is the tool or the host it "
        f"runs on, not the tasks: {_listed(tasks)}."
    )
    headline = f"Tool outage: {tool} exits {code} for {count(distinct, 'task')}"
    return Action(
        ActionKind.ALERT,
        rule,
        reason,
        severity=Severity.WARNING,
        key=state,
        state=state,
        since=since,
        params={**outage.to_params(), **alert_params(headline)},
    )
