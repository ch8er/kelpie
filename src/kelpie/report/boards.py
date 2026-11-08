"""The daily report per board (#283): one Kelpie over several boards (``[[adapters.hermes.
boards]]``, #198) shows cost of chaos, throughput and the biggest wait of each, beside the
farm total. Pure and deterministic, like the rest of the report.

**What a board is.** A task on one of several boards has ``Task.board`` set and its id
starts with the board's prefix: ``qr:t_1a``, or ``<host>:qr:t_1a`` on a multi-host farm.
The prefix (``qr``, ``h1:qr``) names the board here. Its sessions carry the same prefix
(``qr:hermes-run-42``). The boards are those of the tasks on the board now; with fewer than
two there is nothing to split and the report says nothing new.

**How it is split.** Each board's day is the farm's day with only what is on that board: its
tasks, dependencies, conditions, snoozes and sessions, the ledger entries and alert states
about its tasks or sessions (an entry by its task, else its ``session_id`` param), and the
spans of its sessions. Its cost of chaos and completed tasks come from the report built over
that (:func:`kelpie.report.daily.build_report`, without the extra sections), so they are
counted as the farm's are. Its biggest wait is classified with the farm's wait causes: a
person's inbox, a CI outage or a snooze names no board, yet says what a board's task waited
for. Owners are shared: a Hermes home's profiles work on every board. What names no board
(a forgotten hold's idle agents, a person's inbox, an outage's tasks: its state and
alerts name no task of their own, a state without a task) counts only in
the farm total, and the difference is shown as "not on one board".
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from kelpie.core.model import Task

if TYPE_CHECKING:
    from kelpie.core.ledger import LedgerEntry
    from kelpie.report.daily import DailyReport, ReportInputs
    from kelpie.report.waits import BiggestWait


@dataclass(frozen=True, slots=True)
class BoardLine:
    """One board's day: its cost of chaos, the tasks it completed and its biggest wait."""

    board: str  # the ids' prefix: "qr", or "h1:qr" on a multi-host farm
    chaos_hours: float
    chaos_tokens: int  # billed tokens burned on chaos
    chaos_plan_tokens: int  # subscription tokens burned on chaos
    chaos_usd: float  # tokens burned plus, with ``hour_usd``, the stuck hours
    completed: int
    biggest_wait: BiggestWait | None


def board_prefix(task: Task) -> str | None:
    """The prefix that names ``task``'s board in its id, or None when it is on no board of
    several."""
    if not task.board:
        return None
    local = task.local_id
    if local is None or not task.id.endswith(":" + local):
        return None
    return task.id[: -len(local) - 1]


def board_prefixes(tasks: Iterable[Task]) -> tuple[str, ...]:
    """The boards of ``tasks`` (their ids' prefixes), sorted."""
    found = {p for t in tasks if (p := board_prefix(t)) is not None}
    return tuple(sorted(found))


def _owner(prefixes: tuple[str, ...]) -> Callable[[str | None], str | None]:
    ordered = sorted(prefixes, key=len, reverse=True)

    def of(value: str | None) -> str | None:
        if not value:
            return None
        for p in ordered:
            if value.startswith(p + ":"):
                return p
        return None

    return of


def split_inputs(inputs: ReportInputs) -> dict[str, ReportInputs]:
    """Each board's part of ``inputs`` (see the module docstring); {} with fewer than two
    boards."""
    from kelpie.report.daily import session_of

    prefixes = board_prefixes(inputs.tasks)
    if len(prefixes) < 2:
        return {}
    of = _owner(prefixes)
    session_board = {s.id: of(s.id) or of(s.task_id) for s in inputs.sessions}

    def by_session(session: str | None, task: str | None) -> str | None:
        found = session_board.get(session) if session else None
        return found or of(session) or of(task)

    parts: dict[str, ReportInputs] = {}
    for p in prefixes:

        def mine(value: str | None, p: str = p) -> bool:
            return of(value) == p

        parts[p] = replace(
            inputs,
            tasks=tuple(t for t in inputs.tasks if board_prefix(t) == p),
            sessions=tuple(s for s in inputs.sessions if session_board.get(s.id) == p),
            dependencies=tuple(d for d in inputs.dependencies if mine(d.task_id)),
            conditions=tuple(c for c in inputs.conditions if any(mine(w) for w in c.waiters)),
            snoozes=tuple(s for s in inputs.snoozes if mine(s.task_id)),
            actions=tuple(e for e in inputs.actions if _entry_board(e, by_session) == p),
            states=tuple(
                r for r in inputs.states if by_session(session_of(r.state), r.state.task_id) == p
            ),
            spans=tuple(s for s in inputs.spans if by_session(s.session_id, s.task_id) == p),
            fixes=(),  # verified for the farm, not per board
            reconciliations=(),  # providers bill the farm
        )
    return parts


def _entry_board(
    entry: LedgerEntry, by_session: Callable[[str | None, str | None], str | None]
) -> str | None:
    """A ledger entry's board: its task's, else its session's (``params.session_id``, as the
    report reads a restart's session). Never its subject (``task:<id>``, ``session:<id>``)."""
    session = entry.params.get("session_id")
    return by_session(session if isinstance(session, str) else None, entry.task_id)


def board_lines(
    inputs: ReportInputs,
    build: Callable[[ReportInputs], DailyReport],
    wait: Callable[[ReportInputs], BiggestWait | None],
) -> tuple[BoardLine, ...]:
    """Each board's line: chaos and completed tasks from ``build`` over its part of the
    inputs, the biggest wait from ``wait``; () for one board."""
    lines = []
    for board, part in split_inputs(inputs).items():
        r = build(part)
        spend = r.chaos_spend
        lines.append(
            BoardLine(
                board=board,
                chaos_hours=r.chaos_hours,
                chaos_tokens=spend.billed_tokens,
                chaos_plan_tokens=spend.plan_tokens,
                chaos_usd=r.chaos_usd,
                completed=len(r.completed),
                biggest_wait=wait(part),
            )
        )
    return tuple(lines)


def rest_line(report: DailyReport, lines: Iterable[BoardLine]) -> BoardLine | None:
    """What the farm total holds beyond the boards' lines (things that name no board), or
    None when that is nothing worth a line."""
    shown = tuple(lines)
    spend = report.chaos_spend
    # Never below zero: a board's part holds a subset of the farm's, counted the same way.
    rest = BoardLine(
        board="",
        chaos_hours=max(report.chaos_hours - sum(b.chaos_hours for b in shown), 0.0),
        chaos_tokens=max(spend.billed_tokens - sum(b.chaos_tokens for b in shown), 0),
        chaos_plan_tokens=max(spend.plan_tokens - sum(b.chaos_plan_tokens for b in shown), 0),
        chaos_usd=max(report.chaos_usd - sum(b.chaos_usd for b in shown), 0.0),
        completed=max(len(report.completed) - sum(b.completed for b in shown), 0),
        biggest_wait=None,
    )
    if (
        rest.chaos_hours < 0.05
        and rest.chaos_usd < 0.005
        and rest.chaos_tokens <= 0
        and rest.chaos_plan_tokens <= 0
        and rest.completed <= 0
    ):
        return None
    return rest
