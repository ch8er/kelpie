"""Several Kanban boards of one Hermes home watched as one adapter (boards config).

A Hermes home can hold named boards (``~/.hermes/kanban/boards/<slug>/kanban.db``). Profiles,
the hooks spool, the ``hermes`` CLI and the host's tool processes belong to the home, the
tasks and runs to a board. :class:`HermesBoards` reads each board with its own
:class:`~kelpie.adapters.hermes.adapter.HermesAdapter` and merges them into one snapshot:

- **Ids.** A board's tasks, dependencies, sessions and spans are namespaced by its name
  (``qr:t_1a``, ``qr:hermes-run-42``; ``Task.board`` = ``qr``); run ids restart on every
  board, so ``hermes-run-42`` alone would name two runs. Profiles are not namespaced: a
  profile of the home is the same agent on every board. Condition ids are left as rules made
  them (they already carry the namespaced task id).
- **Spool.** The shared spool is read once per snapshot, and each line goes to the board
  that has its task (and its run, under that task): a session is never on two boards. Lines
  that name no Kanban task belong to no board.
- **Host.** Tool runs and tool slots (``[adapters.hermes.tools]``) are read once, over the
  tasks of all boards, so a simulator held by one board's agent never counts as CI on
  another, and each slot pool is reported once.
- **Actions.** ``stop``, ``wait``, wake (``requeue``) and ``comment`` go to the board in the
  id's prefix, with the board's own ids (and its own signing key); an id without a known
  board prefix is refused before any side effect.
- **A board that cannot be read** is logged, listed in ``Snapshot.unobserved`` by its name
  (the prefix of its ids, as a silent host is listed) and left out of that tick; the others
  still count. If none can be read, the snapshot fails like a one-board adapter's.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from kelpie.adapters.hermes.adapter import HermesAdapter, HermesError
from kelpie.core.actions import ActionKind, ActionResult
from kelpie.core.model import (
    Condition,
    ConditionKind,
    Dependency,
    Owner,
    Snapshot,
    Span,
    Task,
)
from kelpie.core.ports import PlatformEvent, UnsupportedAction

log = logging.getLogger(__name__)

# Condition kinds whose ``task`` param names a task of the same board.
TASK_REF_KINDS = frozenset({ConditionKind.TASK_STATUS, ConditionKind.HUMAN_TASK})


@dataclass(slots=True)
class HermesBoards:
    """The boards of one Hermes home; ``boards`` read their own databases and nothing of
    the home (no spool, no tools): those are read here, once."""

    boards: Sequence[HermesAdapter]
    spool: Path | None = None
    # The host's tool runs and slots, read over every board's tasks (a board's own
    # ``tools`` stays None); its ``_host_seen`` keeps the last good read.
    host: HermesAdapter | None = None
    name: str = "hermes"
    actions: ClassVar[frozenset[ActionKind]] = HermesAdapter.actions

    def __post_init__(self) -> None:
        self.boards = tuple(self.boards)
        names = [b.board for b in self.boards]
        if not names or None in names or len(set(names)) < len(names):
            raise ValueError("boards need distinct names")
        if self.spool is not None:
            self.spool = Path(self.spool).expanduser()

    @property
    def board_names(self) -> tuple[str, ...]:
        return tuple(str(b.board) for b in self.boards)

    @property
    def clock(self) -> Callable[[], datetime]:
        return self.boards[0].clock

    @clock.setter
    def clock(self, clock: Callable[[], datetime]) -> None:
        for b in self.boards:
            b.clock = clock

    # --- reading -------------------------------------------------------------------------

    def check(self) -> str | None:
        """``None`` if every board can be read, else which cannot and why."""
        problems = [f"{b.board}: {p}" for b in self.boards if (p := b.check()) is not None]
        return "; ".join(problems) or None

    def status_detail(self, now: datetime) -> list[str]:
        lines = [f"board {b.board}: {b.db_path}" for b in self.boards]
        return lines + self._spool_reader().status_detail(now)

    def last_spool_at(self) -> datetime | None:
        return self._spool_reader().last_spool_at()

    def _spool_reader(self) -> HermesAdapter:
        return replace(self.boards[0], spool=self.spool)

    def snapshot(self, now: datetime) -> Snapshot:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        reader = self._spool_reader()
        spans = reader._spans(now - reader.lookback)
        parts: list[Snapshot] = []
        rows: dict[str, Any] = {}
        unobserved: list[str] = []
        errors: list[str] = []
        for b in self.boards:
            board = str(b.board)
            try:
                part, found = b.read_board(now, spans, own_spans=True)
            except Exception as exc:
                log.error("hermes board %s failed to snapshot: %s", board, exc)
                unobserved.append(board)  # the prefix of its ids, as for a silent host
                errors.append(f"{board}: {exc}")
                continue
            parts.append(namespace_board(board, part, self.name))
            rows.update((f"{board}:{tid}", row) for tid, row in found.items())
        if not parts:
            raise HermesError("no Hermes board can be read: " + "; ".join(errors))
        owners: dict[str, Owner] = {}
        for p in parts:  # one profile, one owner: the boards share the home's settings
            owners.update((o.id, o) for o in p.owners)
        merged = Snapshot(
            now=now,
            tasks=tuple(t for p in parts for t in p.tasks),
            owners=tuple(owners.values()),
            dependencies=tuple(d for p in parts for d in p.dependencies),
            conditions=tuple(c for p in parts for c in p.conditions),
            sessions=tuple(s for p in parts for s in p.sessions),
            spans=_claimed_once(parts),
            unobserved=tuple(unobserved),
        )
        if self.host is None or self.host.tools is None:
            return merged
        # A workspace pattern matches a task's id on its board: the namespaced id it stands
        # for, none when several boards have it (as for a shared workspace path).
        aliases: dict[str, str | None] = {}
        for t in merged.tasks:
            if (local := t.local_id) is not None:
                aliases[local] = None if local in aliases else t.id
        sessions, slots = self.host._with_tools(
            self.host.tools, merged.sessions, merged.tasks, rows, now, aliases
        )
        return replace(merged, sessions=sessions, resources=slots)

    # --- PlatformAdapter -----------------------------------------------------------------

    def list_tasks(self) -> Sequence[Task]:
        return self.snapshot(datetime.now(UTC)).tasks

    def get_task(self, task_id: str) -> Task | None:
        return next((t for t in self.list_tasks() if t.id == task_id), None)

    def events(self, since: datetime) -> Iterable[PlatformEvent]:
        return ()

    def _route(self, value: str) -> tuple[HermesAdapter, str]:
        """The board in ``value``'s prefix and the id on it; refused when there is none."""
        for b in self.boards:
            prefix = f"{b.board}:"
            if value.startswith(prefix) and len(value) > len(prefix):
                return b, value[len(prefix) :]
        raise UnsupportedAction(f"{value!r} is not on a watched Hermes board")

    def stop(self, session_id: str, reason: str) -> str | ActionResult:
        board, local = self._route(session_id)
        return board.stop(local, reason)

    def wait(self, task_id: str, conditions: Sequence[Condition], reason: str) -> None:
        board, local = self._route(task_id)
        return board.wait(local, [_local_condition(board, c) for c in conditions], reason)

    def requeue(self, task_id: str, reason: str) -> None:
        board, local = self._route(task_id)
        board.requeue(local, reason)

    def unblock(self, task_id: str, reason: str) -> None:
        board, local = self._route(task_id)
        board.unblock(local, reason)

    def comment(self, task_id: str, body: str) -> None:
        board, local = self._route(task_id)
        board.comment(local, body)

    def wip_commit(
        self, task_id: str, message: str, *, branch: str | None = None, draft_pr: bool = False
    ) -> None:
        board, local = self._route(task_id)
        board.wip_commit(local, message, branch=branch, draft_pr=draft_pr)


def _claimed_once(parts: Sequence[Snapshot]) -> tuple[Span, ...]:
    """The spool's spans of every board, without any two boards claimed (the same task id
    and run on both: a session is never on two boards)."""
    claims: dict[tuple[str, str], int] = {}
    for p in parts:
        for s in p.spans:
            claims[(s.trace_id, s.span_id)] = claims.get((s.trace_id, s.span_id), 0) + 1
    if twice := sorted(k for k, n in claims.items() if n > 1):
        log.warning("hermes: %d spool spans match tasks on several boards; dropped", len(twice))
    return tuple(s for p in parts for s in p.spans if claims[(s.trace_id, s.span_id)] == 1)


def namespace_board(board: str, part: Snapshot, source: str) -> Snapshot:
    """``part`` (one board's snapshot) with its ids in the board's namespace; ``source``
    names the adapter that carries out actions on it."""

    def ns(value: str) -> str:
        return f"{board}:{value}"

    def opt(value: str | None) -> str | None:
        return None if value is None else ns(value)

    return replace(
        part,
        tasks=tuple(
            replace(
                t,
                id=ns(t.id),
                subtasks=tuple(ns(s) for s in t.subtasks),
                source=source,
                board=board,
            )
            for t in part.tasks
        ),
        dependencies=tuple(Dependency(ns(d.task_id), ns(d.depends_on)) for d in part.dependencies),
        conditions=tuple(
            replace(c, task_id=opt(c.task_id), params=_params(c, ns)) for c in part.conditions
        ),
        sessions=tuple(
            replace(s, id=ns(s.id), task_id=ns(s.task_id), source=source) for s in part.sessions
        ),
        spans=tuple(_span(s, ns) for s in part.spans),
    )


def _params(c: Condition, ns: Callable[[str], str]) -> dict[str, Any]:
    if c.kind in TASK_REF_KINDS and isinstance(c.params.get("task"), str):
        return {**c.params, "task": ns(c.params["task"])}
    return dict(c.params)


def _span(span: Span, ns: Callable[[str], str]) -> Span:
    # A spool line belongs to one board, so its trace and span ids stay unique as they are.
    return replace(
        span,
        task_id=None if span.task_id is None else ns(span.task_id),
        session_id=None if span.session_id is None else ns(span.session_id),
    )


def _local_condition(board: HermesAdapter, c: Condition) -> Condition:
    """A condition to park on ``board``, in the board's own task ids (as its park marker
    records them); refused when it names a task of another board."""
    prefix = f"{board.board}:"

    def local(value: str) -> str:
        if not value.startswith(prefix):
            raise UnsupportedAction(f"condition {c.id} names {value!r}, not a task of this board")
        return value[len(prefix) :]

    params = dict(c.params)
    if c.kind in TASK_REF_KINDS and isinstance(params.get("task"), str):
        params["task"] = local(params["task"])
    return replace(c, task_id=None if c.task_id is None else local(c.task_id), params=params)
