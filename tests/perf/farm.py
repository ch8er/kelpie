"""A large synthetic farm for the tick time budget (issue #248 P1, roadmap "code health").

Deterministic (a seeded ``random.Random``), no I/O: every rule's hot paths get work. Two
thousand tasks spread over every status, roles and owners, dependency chains, blocks with
reasons, review rounds and returns, one session per running task with spans: tool calls
(some repeated, some failing) and model calls with tokens. Names are synthetic.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from kelpie.core.model import (
    Block,
    Dependency,
    Owner,
    OwnerKind,
    Return,
    Session,
    Snapshot,
    Span,
    SpanKind,
    Status,
    Task,
)

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
ROLES = ("coder", "reviewer", "tester", "architect", "ops", "docs")
_REASONS = (
    "waiting for CI on PR #{n}",
    "waiting for merge of PR #{n}",
    "needs a decision from a human",
    "flaky test in suite {n}",
    "blocked by T-{n}",
)
_STATUSES = (
    (Status.BACKLOG, 0.25),
    (Status.READY, 0.15),
    (Status.IN_PROGRESS, 0.15),
    (Status.REVIEW, 0.1),
    (Status.BLOCKED, 0.1),
    (Status.WAITING, 0.05),
    (Status.DONE, 0.15),
    (Status.CANCELLED, 0.05),
)


def large_farm(tasks: int = 2000, spans_per_session: int = 40, seed: int = 7) -> Snapshot:
    rng = random.Random(seed)
    owners = tuple(
        Owner(f"agent-{i}", ROLES[i % len(ROLES)]) for i in range(max(12, tasks // 25))
    ) + tuple(Owner(f"person-{i}", "human", kind=OwnerKind.HUMAN) for i in range(4))
    statuses = [s for s, _ in _STATUSES]
    weights = [w for _, w in _STATUSES]
    out: list[Task] = []
    deps: list[Dependency] = []
    sessions: list[Session] = []
    spans: list[Span] = []
    for i in range(tasks):
        tid = f"T-{i}"
        status = rng.choices(statuses, weights)[0]
        since = NOW - timedelta(minutes=rng.randint(1, 60 * 48))
        owner = rng.choice(owners) if status is not Status.BACKLOG else None
        block = None
        if status is Status.BLOCKED:
            reason = rng.choice(_REASONS).format(n=rng.randint(1, tasks))
            block = Block(reason, since)
        returns = tuple(
            Return(since - timedelta(hours=k + 1), "conflict in shared files")
            for k in range(rng.choice((0, 0, 0, 1, 2)))
        )
        files = tuple(f"src/module_{rng.randint(0, 3000)}.py" for _ in range(rng.randint(0, 4)))
        out.append(
            Task(
                tid,
                f"Synthetic task {i}",
                status,
                since,
                role=owner.role if owner else rng.choice(ROLES),
                owner_id=owner.id if owner else None,
                source="hermes",
                block=block,
                review_rounds=rng.choice((0, 0, 1, 2, 5)) if status is Status.REVIEW else 0,
                files=files,
                returns=returns,
                created_at=since - timedelta(hours=rng.randint(1, 72)),
            )
        )
        if i and rng.random() < 0.4:
            deps.append(Dependency(tid, f"T-{rng.randrange(i)}"))
        if status is Status.IN_PROGRESS and owner is not None:
            sid = f"S-{i}"
            start = NOW - timedelta(minutes=rng.randint(5, 240))
            sessions.append(
                Session(sid, tid, owner.id, start, last_heartbeat=NOW - timedelta(minutes=1))
            )
            spans.extend(_session_spans(rng, sid, tid, start, spans_per_session))
    return Snapshot(
        now=NOW,
        tasks=tuple(out),
        owners=owners,
        dependencies=tuple(deps),
        sessions=tuple(sessions),
        spans=tuple(spans),
    )


def _session_spans(
    rng: random.Random, sid: str, tid: str, start: datetime, count: int
) -> list[Span]:
    out: list[Span] = []
    at = start
    for k in range(count):
        at += timedelta(seconds=rng.randint(5, 120))
        if at >= NOW:
            break
        if k % 3 == 0:
            out.append(
                Span(
                    f"tr-{sid}",
                    f"{sid}-{k}",
                    "chat",
                    SpanKind.LLM,
                    at,
                    at + timedelta(seconds=4),
                    task_id=tid,
                    session_id=sid,
                    model="model-a",
                    input_tokens=rng.randint(1_000, 80_000),
                    output_tokens=rng.randint(10, 2_000),
                    cached_tokens=rng.randint(0, 50_000),
                )
            )
        else:
            tool = rng.choice(("terminal", "read_file", "write_file", "search"))
            out.append(
                Span(
                    f"tr-{sid}",
                    f"{sid}-{k}",
                    "execute_tool",
                    SpanKind.TOOL,
                    at,
                    at + timedelta(seconds=2),
                    task_id=tid,
                    session_id=sid,
                    tool=tool,
                    args_hash=f"h{rng.randint(0, 5)}",
                    outcome="error" if rng.random() < 0.1 else "ok",
                )
            )
    return out
