"""The issues adapter: a farm whose task queue is a tracker's issues (see :mod:`.mapping`).

One adapter per tracker: ``github-issues`` reads GitHub Issues through the GitHub
connector's client (:mod:`.github`). Agents there (Copilot coding agent, Codex cloud, the
Claude Code GitHub Action, OpenHands) run on someone else's machines, so the adapter has
tasks and owners but no sessions or spans.

Actions are not carried out yet: each is refused (``UnsupportedAction``), so a rule in
``act`` mode alerts a human instead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import ClassVar, Protocol

from kelpie.adapters.issues.mapping import Agent, Board, Item, map_items
from kelpie.core.actions import ActionKind
from kelpie.core.alerts import format_age
from kelpie.core.model import Snapshot, Task
from kelpie.core.ports import PlatformEvent, UnsupportedAction

log = logging.getLogger(__name__)

UNSUPPORTED = "the issues adapter does not change issues yet"


class Tracker(Protocol):
    """Reads a tracker's issues as items (GitHub Issues: :class:`.github.GitHubIssues`)."""

    def read(self, now: datetime) -> list[Item]: ...


@dataclass(slots=True)
class IssuesAdapter:
    tracker: Tracker
    boards: Sequence[Board]
    agents: Mapping[str, Agent]
    name: str = "github-issues"
    where: str = ""  # for kelpie status: the tracker's address
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC), repr=False)
    actions: ClassVar[frozenset[ActionKind]] = frozenset()
    _last: tuple[datetime, int, int] | None = field(default=None, init=False, repr=False)
    _problem: str | None = field(default=None, init=False, repr=False)
    # Per issue: how it looked (open, labels, assignees, linked pull requests) and since when.
    _seen: dict[str, tuple[object, datetime]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self.boards = tuple(self.boards)
        self.agents = dict(self.agents)

    @property
    def _boards(self) -> dict[str, Board]:
        return {b.key: b for b in self.boards}

    # --- status --------------------------------------------------------------------------

    def check(self) -> str | None:
        """None if the last read saw everything (or none was made yet), else what it
        missed."""
        return self._problem

    def status_detail(self, now: datetime) -> list[str]:
        boards = ", ".join(b.key for b in self.boards) or "none"
        agents = ", ".join(sorted(self.agents)) or "none"
        lines = [f"boards: {boards}", f"agents: {agents}"]
        if self.where:
            lines.insert(0, f"tracker: {self.where}")
        if self._last is None:
            return [*lines, "issues: not read yet"]
        at, count, active = self._last
        return [*lines, f"issues: {count} tasks, {active} held, read {format_age(now - at)} ago"]

    # --- reading -------------------------------------------------------------------------

    def snapshot(self, now: datetime) -> Snapshot:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        try:
            items = self.tracker.read(now)
        except Exception as exc:
            self._problem = str(getattr(exc, "public_reason", None) or type(exc).__name__)
            raise
        self._problem = getattr(self.tracker, "problem", None)
        items = self._since_seen(items, now)
        mapped = map_items(items, self._boards, self.agents, self.name)
        held = sum(1 for t in mapped.tasks if t.owner_id is not None and not t.status.is_terminal)
        self._last = (now, len(mapped.tasks), held)
        return Snapshot(
            now=now,
            tasks=mapped.tasks,
            owners=mapped.owners,
            dependencies=mapped.dependencies,
        )

    def _since_seen(self, items: list[Item], now: datetime) -> list[Item]:
        """``items`` with ``seen_since``: when this adapter first saw each as it is now."""
        seen: dict[str, tuple[object, datetime]] = {}
        out: list[Item] = []
        for item in items:
            shape = (
                item.open,
                frozenset(label.casefold() for label in item.labels),
                frozenset(login.casefold() for login in item.assignees),
                tuple((p.number, p.draft) for p in item.pulls),
            )
            kept = self._seen.get(item.id)
            # First seen: no later than its last change, so a comment cannot move it on.
            since = kept[1] if kept is not None and kept[0] == shape else min(item.updated_at, now)
            seen[item.id] = (shape, since)
            out.append(replace(item, seen_since=since))
        self._seen = seen
        return out

    # --- PlatformAdapter -----------------------------------------------------------------

    def list_tasks(self) -> Sequence[Task]:
        return self.snapshot(self.clock()).tasks

    def get_task(self, task_id: str) -> Task | None:
        return next((t for t in self.list_tasks() if t.id == task_id), None)

    def events(self, since: datetime) -> Iterable[PlatformEvent]:
        return ()

    def requeue(self, task_id: str, reason: str) -> None:
        raise UnsupportedAction(UNSUPPORTED)

    def unblock(self, task_id: str, reason: str) -> None:
        raise UnsupportedAction(UNSUPPORTED)

    def comment(self, task_id: str, body: str) -> None:
        raise UnsupportedAction(UNSUPPORTED)

    def wip_commit(
        self, task_id: str, message: str, *, branch: str | None = None, draft_pr: bool = False
    ) -> None:
        raise UnsupportedAction(UNSUPPORTED)
