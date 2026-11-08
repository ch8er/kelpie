"""The farm view: a read-only summary of the board and of what Kelpie did, for people.

``kelpie status`` prints it and the read-only page of ``kelpie serve`` ([view]) serves it;
both build it here, from the same inputs, so they never disagree:

- tasks per status, per board (``Task.board``; ``None`` on a farm with one board);
- waiting conditions: every condition linked to a task that is not met yet, with its task's
  board and status;
- open alert states (``kelpie.core.states``), newest first;
- the latest ledger entries per rule (``kelpie.core.ledger``), newest first;
- the open tasks ordered by attention (:class:`Attention`): what needs a person, what is
  stuck or blocked, what has run long, then the rest; oldest first within each;
- today's cost of chaos and spend so far (:class:`Today`), when the caller has them (the
  server computes them from the report, off the request path).

Pure: no I/O, no clock reads (``now`` is passed in). The view holds facts only; text from
agents (titles, reasons) is kept as it is and cleaned where it is rendered.
:func:`to_dict` and :func:`from_dict` are the wire form (``schema_version`` 1), served as
JSON by ``kelpie serve`` and read back by ``kelpie status --server``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from kelpie.core.ledger import LedgerEntry
from kelpie.core.model import OwnerKind, Snapshot, Status, Task
from kelpie.core.states import OpenState
from kelpie.core.transitions import StatusChange

SCHEMA_VERSION = 1
PER_RULE = 3  # ledger entries kept per rule
MAX_TEXT = 500  # characters kept of any text from the board or the ledger
MAX_ITEMS = 200  # waiting conditions, alerts and attention items kept; the rest are counted
LONG_AFTER = timedelta(hours=2)  # in progress this long: "running long"
NEEDS_YOU_RULE = "needs_you"  # rule 13: its open states are a person's to-do


class Attention(StrEnum):
    """Why a task is shown where it is, most urgent first."""

    NEEDS_YOU = "needs_you"  # a person holds it, or rule 13 says it waits for one
    STUCK = "stuck"  # blocked, or an open alert about it
    RUNNING_LONG = "running_long"  # in progress past ``long_after``
    REST = "rest"


_RANK = {a: i for i, a in enumerate(Attention)}


@dataclass(frozen=True, slots=True)
class AttentionItem:
    task_id: str
    title: str
    status: str
    since: datetime
    attention: str
    board: str | None = None
    owner: str | None = None
    why: str | None = None  # the block reason or the alert summary that put it there


@dataclass(frozen=True, slots=True)
class Today:
    """The report's figures for the day so far (``kelpie.report``), at ``at``."""

    at: datetime
    day: str  # YYYY-MM-DD in [report] timezone
    chaos_hours: float
    chaos_usd: float  # tokens burned plus, with [report] hour_usd, the stuck hours
    spend_usd: float  # API providers' tokens priced
    tokens: int
    plan_tokens: int = 0  # tokens of providers on a subscription (never dollars)


@dataclass(frozen=True, slots=True)
class StatusCount:
    status: str
    count: int
    board: str | None = None


@dataclass(frozen=True, slots=True)
class WaitingItem:
    """A condition a task waits on that is not met (``satisfied`` False or unknown)."""

    task_id: str
    condition_id: str
    kind: str
    satisfied: bool | None
    target: str | None = None  # ``repo#pr``, a task id, or None
    task_status: str | None = None
    board: str | None = None
    registered_at: datetime | None = None
    checked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class AlertItem:
    id: str
    rule: str
    severity: str
    since: datetime
    opened_at: datetime
    summary: str
    mode: str
    task_id: str | None = None
    board: str | None = None


@dataclass(frozen=True, slots=True)
class ActionItem:
    at: datetime
    kind: str
    mode: str
    outcome: str
    reason: str
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class RuleActions:
    rule: str
    actions: tuple[ActionItem, ...]


@dataclass(frozen=True, slots=True)
class FarmView:
    now: datetime
    # Where the tasks came from: "board" (read now), "server" (kelpie serve's last tick),
    # "stored" (the last status kelpie serve observed per task) or "none".
    source: str
    statuses: tuple[StatusCount, ...] = ()
    waiting: tuple[WaitingItem, ...] = ()
    alerts: tuple[AlertItem, ...] = ()
    rules: tuple[RuleActions, ...] = ()
    boards: tuple[str, ...] = ()  # named boards; empty on a farm with one board
    tasks: int = 0
    waiting_total: int = 0
    alerts_total: int = 0
    snapshot_at: datetime | None = None  # when the tasks were read, if not ``now``
    problems: tuple[str, ...] = ()  # what could not be read
    items: tuple[AttentionItem, ...] = ()  # open tasks by attention (needs a board read)
    items_total: int = 0
    today: Today | None = None


def _cut(text: str | None, limit: int = MAX_TEXT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _target(params: Mapping[str, Any]) -> str | None:
    repo, pr = params.get("repo"), params.get("pr")
    if repo and pr is not None:
        return _cut(f"{repo}#{pr}", 200)
    for key in ("task_id", "task", "depends_on"):
        value = params.get(key)
        if isinstance(value, str) and value:
            return _cut(value, 200)
    return None


def _counts(pairs: Iterable[tuple[str | None, str]]) -> tuple[StatusCount, ...]:
    counted = Counter(pairs)
    order = {str(s): i for i, s in enumerate(Status)}
    return tuple(
        StatusCount(status, n, board)
        for (board, status), n in sorted(
            counted.items(), key=lambda kv: (kv[0][0] or "", order.get(kv[0][1], 99), kv[0][1])
        )
    )


def latest_per_rule(
    entries: Iterable[LedgerEntry], per_rule: int = PER_RULE
) -> dict[str, list[LedgerEntry]]:
    """The newest ``per_rule`` entries of each rule, newest first."""
    by_rule: dict[str, list[LedgerEntry]] = {}
    for e in sorted(entries, key=lambda e: e.at, reverse=True):
        kept = by_rule.setdefault(e.rule, [])
        if len(kept) < per_rule:
            kept.append(e)
    return by_rule


def _attention(
    t: Task, snapshot: Snapshot, alerted: Mapping[str, OpenState], long_after: timedelta
) -> tuple[Attention, str | None]:
    state = alerted.get(t.id)
    owner = snapshot.owner(t.owner_id) if t.owner_id else None
    if (owner is not None and owner.kind is OwnerKind.HUMAN) or (
        state is not None and state.rule == NEEDS_YOU_RULE
    ):
        return Attention.NEEDS_YOU, state.summary if state is not None else None
    if t.status is Status.BLOCKED:
        return Attention.STUCK, t.block.reason if t.block is not None else None
    if state is not None:
        return Attention.STUCK, state.summary
    if t.status is Status.IN_PROGRESS and snapshot.now - t.status_since >= long_after:
        return Attention.RUNNING_LONG, None
    return Attention.REST, None


def attention_items(
    snapshot: Snapshot, states: Sequence[OpenState] = (), long_after: timedelta = LONG_AFTER
) -> list[AttentionItem]:
    """The snapshot's open tasks (not done, failed or cancelled) by attention, then board,
    then oldest status first. A task with several open states is ranked by the most urgent
    (rule 13's first, else the newest)."""
    alerted: dict[str, OpenState] = {}
    for st in sorted(states, key=lambda st: (st.rule == NEEDS_YOU_RULE, st.opened_at)):
        if st.task_id is not None:
            alerted[st.task_id] = st  # rule 13's states, then the newest, win
    items = []
    for t in snapshot.tasks:
        if t.status.is_terminal:
            continue
        attention, why = _attention(t, snapshot, alerted, long_after)
        items.append(
            AttentionItem(
                task_id=t.id,
                title=_cut(t.title, 200),
                status=str(t.status),
                since=t.status_since,
                attention=str(attention),
                board=t.board,
                owner=t.owner_id,
                why=_cut(why, 300) if why else None,
            )
        )
    items.sort(key=lambda i: (_RANK[Attention(i.attention)], i.board or "", i.since, i.task_id))
    return items


def build_view(
    now: datetime,
    snapshot: Snapshot | None,
    states: Sequence[OpenState] = (),
    entries: Iterable[LedgerEntry] = (),
    *,
    source: str = "board",
    stored: Mapping[str, StatusChange] | None = None,
    per_rule: int = PER_RULE,
    problems: Sequence[str] = (),
    long_after: timedelta = LONG_AFTER,
    today: Today | None = None,
) -> FarmView:
    """The view at ``now``. Tasks come from ``snapshot`` when there is one, else from
    ``stored`` (the last status observed per task), else there are none."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    boards_of: dict[str, str | None] = {}
    status_of: dict[str, str] = {}
    waiting: list[WaitingItem] = []
    snapshot_at = None
    if snapshot is not None:
        snapshot_at = snapshot.now if snapshot.now != now else None
        for t in snapshot.tasks:
            boards_of[t.id] = t.board
            status_of[t.id] = str(t.status)
        for c in snapshot.conditions:
            if c.task_id is None or c.satisfied is True:
                continue
            waiting.append(
                WaitingItem(
                    task_id=c.task_id,
                    condition_id=c.id,
                    kind=str(c.kind),
                    satisfied=c.satisfied,
                    target=_target(c.params),
                    task_status=status_of.get(c.task_id),
                    board=boards_of.get(c.task_id),
                    registered_at=c.registered_at,
                    checked_at=c.checked_at,
                )
            )
    elif stored:
        source = "stored"
        for task_id, change in stored.items():
            boards_of[task_id] = change.board
            status_of[task_id] = str(change.status)
    else:
        source = "none"
    waiting.sort(key=lambda w: (w.board or "", w.task_id, w.condition_id))
    alerts = sorted(
        (
            AlertItem(
                id=s.id,
                rule=s.rule,
                severity=str(s.severity),
                since=s.since,
                opened_at=s.opened_at,
                summary=_cut(s.summary),
                mode=str(s.mode),
                task_id=s.task_id,
                board=boards_of.get(s.task_id) if s.task_id else None,
            )
            for s in states
        ),
        key=lambda a: (a.opened_at, a.id),
        reverse=True,
    )
    rules = tuple(
        RuleActions(
            rule,
            tuple(
                ActionItem(
                    at=e.at,
                    kind=str(e.kind),
                    mode=str(e.mode),
                    outcome=str(e.outcome),
                    reason=_cut(e.reason),
                    task_id=e.task_id,
                )
                for e in kept
            ),
        )
        for rule, kept in sorted(latest_per_rule(entries, per_rule).items())
    )
    boards = sorted({b for b in boards_of.values() if b})
    items = attention_items(snapshot, states, long_after) if snapshot is not None else []
    return FarmView(
        now=now,
        source=source,
        statuses=_counts((boards_of[i], s) for i, s in status_of.items()),
        waiting=tuple(waiting[:MAX_ITEMS]),
        alerts=tuple(alerts[:MAX_ITEMS]),
        rules=rules,
        boards=tuple(boards),
        tasks=len(status_of),
        waiting_total=len(waiting),
        alerts_total=len(alerts),
        snapshot_at=snapshot_at,
        problems=tuple(problems),
        items=tuple(items[:MAX_ITEMS]),
        items_total=len(items),
        today=today,
    )


# --- wire form -----------------------------------------------------------------------------


def _ts(at: datetime | None) -> str | None:
    return None if at is None else at.isoformat()


def _at(value: Any) -> datetime | None:
    if value is None:
        return None
    at = datetime.fromisoformat(str(value))
    if at.tzinfo is None:
        raise ValueError(f"time without an offset: {value!r}")
    return at


def _req(value: Any) -> datetime:
    at = _at(value)
    if at is None:
        raise ValueError("a required time is missing")
    return at


def _opt(value: Any) -> str | None:
    return None if value is None else str(value)


def to_dict(view: FarmView) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "now": _ts(view.now),
        "source": view.source,
        "snapshot_at": _ts(view.snapshot_at),
        "boards": list(view.boards),
        "tasks": view.tasks,
        "statuses": [
            {"status": c.status, "count": c.count, "board": c.board} for c in view.statuses
        ],
        "waiting_total": view.waiting_total,
        "waiting": [
            {
                "task_id": w.task_id,
                "condition_id": w.condition_id,
                "kind": w.kind,
                "satisfied": w.satisfied,
                "target": w.target,
                "task_status": w.task_status,
                "board": w.board,
                "registered_at": _ts(w.registered_at),
                "checked_at": _ts(w.checked_at),
            }
            for w in view.waiting
        ],
        "alerts_total": view.alerts_total,
        "alerts": [
            {
                "id": a.id,
                "rule": a.rule,
                "severity": a.severity,
                "since": _ts(a.since),
                "opened_at": _ts(a.opened_at),
                "summary": a.summary,
                "mode": a.mode,
                "task_id": a.task_id,
                "board": a.board,
            }
            for a in view.alerts
        ],
        "rules": [
            {
                "rule": r.rule,
                "actions": [
                    {
                        "at": _ts(e.at),
                        "kind": e.kind,
                        "mode": e.mode,
                        "outcome": e.outcome,
                        "reason": e.reason,
                        "task_id": e.task_id,
                    }
                    for e in r.actions
                ],
            }
            for r in view.rules
        ],
        "problems": list(view.problems),
        "items_total": view.items_total,
        "items": [
            {
                "task_id": i.task_id,
                "title": i.title,
                "status": i.status,
                "since": _ts(i.since),
                "attention": i.attention,
                "board": i.board,
                "owner": i.owner,
                "why": i.why,
            }
            for i in view.items
        ],
        "today": None
        if view.today is None
        else {
            "at": _ts(view.today.at),
            "day": view.today.day,
            "chaos_hours": view.today.chaos_hours,
            "chaos_usd": view.today.chaos_usd,
            "spend_usd": view.today.spend_usd,
            "tokens": view.today.tokens,
            "plan_tokens": view.today.plan_tokens,
        },
    }


def _today(data: Any) -> Today | None:
    if data is None:
        return None
    return Today(
        at=_req(data["at"]),
        day=str(data["day"]),
        chaos_hours=float(data["chaos_hours"]),
        chaos_usd=float(data["chaos_usd"]),
        spend_usd=float(data["spend_usd"]),
        tokens=int(data["tokens"]),
        plan_tokens=int(data.get("plan_tokens", 0)),
    )


def from_dict(data: Mapping[str, Any]) -> FarmView:
    """The view :func:`to_dict` wrote; ValueError on another schema or a malformed field."""
    if not isinstance(data, Mapping) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"not a farm view of schema {SCHEMA_VERSION}")
    try:
        return FarmView(
            now=_req(data["now"]),
            source=str(data["source"]),
            snapshot_at=_at(data.get("snapshot_at")),
            boards=tuple(str(b) for b in data.get("boards", ())),
            tasks=int(data.get("tasks", 0)),
            statuses=tuple(
                StatusCount(str(c["status"]), int(c["count"]), _opt(c.get("board")))
                for c in data.get("statuses", ())
            ),
            waiting_total=int(data.get("waiting_total", 0)),
            waiting=tuple(
                WaitingItem(
                    task_id=str(w["task_id"]),
                    condition_id=str(w["condition_id"]),
                    kind=str(w["kind"]),
                    satisfied=None if w.get("satisfied") is None else bool(w["satisfied"]),
                    target=_opt(w.get("target")),
                    task_status=_opt(w.get("task_status")),
                    board=_opt(w.get("board")),
                    registered_at=_at(w.get("registered_at")),
                    checked_at=_at(w.get("checked_at")),
                )
                for w in data.get("waiting", ())
            ),
            alerts_total=int(data.get("alerts_total", 0)),
            alerts=tuple(
                AlertItem(
                    id=str(a["id"]),
                    rule=str(a["rule"]),
                    severity=str(a["severity"]),
                    since=_req(a["since"]),
                    opened_at=_req(a["opened_at"]),
                    summary=str(a.get("summary", "")),
                    mode=str(a["mode"]),
                    task_id=_opt(a.get("task_id")),
                    board=_opt(a.get("board")),
                )
                for a in data.get("alerts", ())
            ),
            rules=tuple(
                RuleActions(
                    str(r["rule"]),
                    tuple(
                        ActionItem(
                            at=_req(e["at"]),
                            kind=str(e["kind"]),
                            mode=str(e["mode"]),
                            outcome=str(e["outcome"]),
                            reason=str(e.get("reason", "")),
                            task_id=_opt(e.get("task_id")),
                        )
                        for e in r.get("actions", ())
                    ),
                )
                for r in data.get("rules", ())
            ),
            problems=tuple(str(p) for p in data.get("problems", ())),
            items_total=int(data.get("items_total", 0)),
            items=tuple(
                AttentionItem(
                    task_id=str(i["task_id"]),
                    title=str(i.get("title", "")),
                    status=str(i["status"]),
                    since=_req(i["since"]),
                    attention=str(i["attention"]),
                    board=_opt(i.get("board")),
                    owner=_opt(i.get("owner")),
                    why=_opt(i.get("why")),
                )
                for i in data.get("items", ())
            ),
            today=_today(data.get("today")),
        )
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"malformed farm view: {exc!r}") from None
