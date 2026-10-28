"""Rule 10, correlated tool failures: one outage (issue #229, signal 2, incident 20).

On the live farm the Xcode installation broke for ~12 h: every ``ios_sim.sh`` and
``xcodebuild`` run failed on the farm's own Mac, not in GitHub's CI, so the failing-check
outage never fired and each agent blocked its task on its own. The tool calls said it: the
same tool failing with the same exit code for task after task.

**Signs.** Tool spans of the snapshot whose result the adapter knows
(:mod:`kelpie.core.tools`) and whose command runs one of ``tools`` (config data: the farm's
build and test runners, e.g. ``ios_sim.sh``, ``xcodebuild``) where its exit code is the
command's: the tool is the command (path aside, after variable assignments and the wrappers
``env``, ``sudo``, ``nice``, ``rtk``, ``time``, ``exec``, ``nohup`` with their options) of
the command's last segment, after a ``;`` or ``|``, or after ``&&`` only from setup commands
(``cd``, ``export``, ``source``, ``set``, ...): ``cd app && ios_sim.sh test``, not ``lint.sh
&& ios_sim.sh test`` (which of them failed?), ``pkill xcodebuild`` nor ``command -v
xcodebuild`` (it exits 0 wherever the binary is). A command of more lines than the one its
trace keeps is none: its last line's exit code decides. A foreground call that exited
non-zero is a failure of that tool with its exit code as the error class (``127`` not found,
``124`` timed out, ``1`` a failed build). One that exited 0 is a success only when the
adapter says the command is whole (:func:`~kelpie.core.tools.command_whole`: redaction keeps
a prefix, and a dropped ``| xcbeautify`` or second line decides the exit code) and nothing
after the tool was masked: a false success would end an outage. A call sent to the
background (its ``0`` means started) and one the platform refused to run (blocked) are
neither, and a span with no known result does not count. Commands the redaction masked whole
(``bash *** ***``, ``timeout 600 *** ***``) name no tool.

**Open.** Failures of one tool with one exit code by at least ``min_tasks`` distinct tasks
within ``window`` of each other, none followed by a success of that tool → one ``warning``
outage of kind :data:`~kelpie.core.incidents.TOOL_FAILURES` per tool, exit code and host:
since the first failure of the earliest such group after the tool's last success (it moves
later as older spans leave the snapshot), scoped to the tasks that failed (they are the
tasks it explains), with the exit code's meaning as its cause. A call's task is its span's,
else its session's; a session the snapshot ties to no task counts as one task, and a call
with neither as one per trace. One task retrying the same failure is no outage. The host is
the task's (``Task.host``) where the adapter knows it, else the host whose namespace the
call's task or session id is in (a multi-host farm's ``<host>:<id>``, also for a task the
snapshot no longer holds): a success on a healthy host does not hide a broken one. A success
on an unknown host counts for every host only on a farm whose tasks name no host; otherwise
only for calls of unknown hosts. It resolves when the tool runs successfully again
(:func:`ran_again`), or when no failure is left in the snapshot's spans: the window is the
``[traces] window`` at most, whose spans the snapshot holds.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.incidents import TOOL_FAILURES, Outage, incident_state_id, is_outage_state
from kelpie.core.model import Snapshot, Span, SpanKind
from kelpie.core.tools import (
    ERROR,
    OK,
    command_multiline,
    command_whole,
    exit_code_of,
    result_of,
)
from kelpie.rules._alerts import alert_params, count
from kelpie.rules._ci_outage import _listed

COMMAND_ATTRIBUTES = ("command", "kelpie.tool.command_prefix")  # the hook's, then OTLP's
MAX_TASKS = 50  # tasks an outage's params list
MASK = "***"  # what the redaction leaves of a word it hides
# Words that run the command after them (with their options); ``timeout`` takes a
# duration first. ``command`` too, but ``command -v`` only looks the command up.
WRAPPERS = frozenset({"env", "sudo", "nice", "rtk", "time", "exec", "command", "nohup", "timeout"})
# Commands before ``&&`` that do not fail a build's way: the exit code is the build's.
SETUP = frozenset({"cd", "pushd", "export", "source", ".", "set", "unset", "mkdir", "true"})
SEPARATORS = frozenset({"&&", "||", ";", "|", "|&", "&"})
# What an exit code usually means, for the cause line (POSIX shells, timeout(1)).
MEANINGS = {
    124: "timed out",
    126: "found but not executable",
    127: "not found",
    130: "interrupted",
    137: "killed (out of memory, or SIGKILL)",
    139: "crashed (segmentation fault)",
}

_Group = tuple[str, int, str | None]  # tool, exit code, host


@dataclass(frozen=True, slots=True)
class _Call:
    at: datetime
    tool: str
    task: str | None  # the call's task, or its session's
    who: str  # what counts as one task: the task, else the session, else the trace
    host: str | None  # the task's host, if known
    ok: bool
    code: int | None  # the exit code of a failure


def tool_outage_state(rule: str, tool: str, code: int, host: str | None = None) -> str:
    """The state id of ``rule``'s outage of ``tool`` exiting ``code`` (on ``host``)."""
    parts = ("tool", tool, str(code)) if host is None else ("tool", tool, str(code), host)
    return incident_state_id(rule, *parts)


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
    calls = _calls(snapshot, tools)
    for group, failures in sorted(_open(calls, _named_hosts(snapshot)).items(), key=_order):
        since = _since(failures, min_tasks, window)
        if since is None:
            continue
        scope = [f for f in failures if f.at >= since]
        out.append(replace(_alert(rule, group, scope, since), observe_only=observe))
    return out


def ran_again(state_id: str, snapshot: Snapshot, rule: str, tools: Collection[str]) -> str | None:
    """For the "Resolved" notice: the tool's first success after the outage's last failure,
    when the state is one of its outages; None otherwise (the engine's general notice)."""
    if not tools or not state_id.startswith(f"{rule}:") or not is_outage_state(state_id):
        return None
    calls = _calls(snapshot, tools)
    hosts = _named_hosts(snapshot)
    groups = {(c.tool, c.code, c.host) for c in calls if not c.ok and c.code is not None}
    for tool, code, host in groups:
        if code is None or tool_outage_state(rule, tool, code, host) != state_id:
            continue
        last = max(c.at for c in calls if (c.tool, c.code, c.host) == (tool, code, host))
        again = [
            c for c in calls if c.ok and c.tool == tool and _clears(c, host, hosts) and c.at > last
        ]
        if again:
            first = min(again, key=lambda c: c.at)
            on = f" in {first.task}" if first.task else ""
            return f"{tool} ran successfully again at {first.at:%Y-%m-%d %H:%M} UTC{on}."
    return None


def _order(item: tuple[_Group, list[_Call]]) -> tuple[str, int, str]:
    (tool, code, host), _ = item
    return tool, code, host or ""


def _named_hosts(snapshot: Snapshot) -> bool:
    """Whether the farm's tasks name hosts (multi-host): then an unknown host is its own."""
    return any(t.host for t in snapshot.tasks)


def _clears(success: _Call, host: str | None, named: bool) -> bool:
    """Whether a success of the tool ends its failures on ``host``."""
    if success.host == host:
        return True
    return not named and (success.host is None or host is None)


def _calls(snapshot: Snapshot, tools: Collection[str]) -> list[_Call]:
    names = set(tools)
    sessions = {s.id: s.task_id for s in snapshot.sessions}
    hosts = {t.id: t.host for t in snapshot.tasks}
    named = {h for h in hosts.values() if h}
    out = []
    for span in snapshot.spans:
        if span.kind is not SpanKind.TOOL:
            continue
        result = result_of(span)
        if result not in (OK, ERROR) or command_multiline(span):
            continue  # background, blocked, not known; or a later line's exit code
        found = _tool(span, names)
        if found is None:
            continue
        tool, masked = found
        if result == OK and (masked or not command_whole(span)):
            continue  # a masked or dropped pipe may have decided the exit code
        code = exit_code_of(span) if result == ERROR else None
        if result == ERROR and not code:
            continue  # a failure without its class cannot be grouped
        task = span.task_id or sessions.get(span.session_id or "")
        if task:
            who = task
        elif span.session_id:
            who = f"session {span.session_id}"
        else:
            who = f"trace {span.trace_id}"
        host = (hosts.get(task) if task else None) or _namespace(task or span.session_id, named)
        out.append(_Call(span.end or span.start, tool, task, who, host, result == OK, code))
    return sorted(out, key=lambda c: c.at)


def _namespace(ident: str | None, hosts: Collection[str]) -> str | None:
    """The host whose namespace an id is in (a multi-host farm's ``<host>:<id>``): a task
    that left the snapshot (archived) still ran there."""
    if not ident or ":" not in ident:
        return None
    prefix = ident.split(":", 1)[0]
    return prefix if prefix in hosts else None


def _tool(span: Span, names: set[str]) -> tuple[str, bool] | None:
    """The configured tool a call's command runs where the exit code is its own (see the
    module docs), and whether a word after it was masked; None if there is none."""
    for key in COMMAND_ATTRIBUTES:
        text = span.attributes.get(key)
        if isinstance(text, str) and (found := _last_command(text.split(), names)):
            return found
    return None


def _last_command(words: Sequence[str], names: set[str]) -> tuple[str, bool] | None:
    segments: list[list[str]] = [[]]
    joins: list[str] = []
    for word in words:
        if word in SEPARATORS:
            joins.append(word)
            segments.append([])
        else:
            segments[-1].append(word)
    for segment, join in zip(segments[:-1], joins, strict=True):
        if join in ("||", "&") or (join == "&&" and _command(segment) not in SETUP):
            return None  # whose exit code is it?
    last = segments[-1]
    head = _head(last)
    if head is None or head[1] not in names:
        return None
    index, name = head
    return name, any(MASK in w for w in last[index + 1 :])


def _command(segment: Sequence[str]) -> str | None:
    head = _head(segment)
    return None if head is None else head[1]


def _head(segment: Sequence[str]) -> tuple[int, str] | None:
    """The position and base name of the command a segment runs, past assignments and
    wrappers; None when it is masked or there is none."""
    i = 0
    while i < len(segment):
        word = segment[i]
        if "=" in word and not word.startswith("="):
            i += 1  # FOO=1
        elif word in WRAPPERS:
            if word == "command" and any(_lookup(w) for w in _options(segment[i + 1 :])):
                return None  # command -v looks the command up, does not run it
            i += 1
            while i < len(segment) and segment[i].startswith("-"):
                i += 1  # its options, and a masked value of one (sudo -u ***, nice -n ***)
                if i < len(segment) and segment[i] == MASK:
                    i += 1
            if word == "timeout":
                i += 1  # its duration
        else:
            break
    if i >= len(segment) or MASK in segment[i]:
        return None
    return i, segment[i].rstrip("/").rsplit("/", 1)[-1]


def _options(words: Sequence[str]) -> list[str]:
    """The option words a command's arguments start with."""
    out = []
    for word in words:
        if not word.startswith("-") or word == "--":
            break
        out.append(word)
    return out


def _lookup(option: str) -> bool:
    """Whether ``command``'s option asks for a lookup (``-v``, ``-V``, ``-pv``)."""
    return not option.startswith("--") and ("v" in option[1:] or "V" in option[1:])


def _open(calls: Sequence[_Call], named: bool) -> Mapping[_Group, list[_Call]]:
    """Failures per tool, exit code and host, since that tool's last success there."""
    found: dict[_Group, list[_Call]] = defaultdict(list)
    for c in calls:  # oldest first
        if c.ok:
            for tool, code, host in list(found):
                if tool == c.tool and _clears(c, host, named):
                    del found[tool, code, host]
        else:
            assert c.code is not None
            found[c.tool, c.code, c.host].append(c)
    return found


def _since(failures: Sequence[_Call], min_tasks: int, window: timedelta) -> datetime | None:
    """The first failure of the earliest run of ``min_tasks`` distinct tasks within
    ``window`` of each other, or None."""
    for i, first in enumerate(failures):
        tasks = {c.who for c in failures[i:] if c.at - first.at <= window}
        if len(tasks) >= min_tasks:
            return first.at
    return None


def _alert(rule: str, group: _Group, scope: Sequence[_Call], since: datetime) -> Action:
    tool, code, host = group
    state = tool_outage_state(rule, tool, code, host)
    tasks = sorted({c.task for c in scope if c.task})
    distinct = count(len({c.who for c in scope}), "task")
    meaning = MEANINGS.get(code, "failed")
    on = f" on {host}" if host else ""
    cause = f"{tool} exits {code} ({meaning}){on} for {distinct}"
    outage = Outage(
        state=state,
        signature=f"{tool} exit {code}{on}",
        kind=TOOL_FAILURES,
        cause=cause,
        tasks=tuple(tasks[:MAX_TASKS]),
    )
    whose = f"the tasks: {_listed(tasks)}" if tasks else "the runs (no task is known)"
    reason = (
        f"Tool outage since {since:%Y-%m-%d %H:%M} UTC: {cause}, {len(scope)} run(s), and "
        f"nothing has run {tool} successfully since. The cause is the tool or the host it "
        f"runs on, not {whose}."
    )
    headline = f"Tool outage: {tool} exits {code}{on} for {distinct}"
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
