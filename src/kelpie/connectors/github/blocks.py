"""Turn "waiting expressed as a block" into a condition, during snapshot assembly.

Agents often block their own task with a reason such as "waiting for CI/merge of #123"
instead of registering a condition and exiting (incident 10 in another form). Rule 4 then
sees a block with nothing it can check and alerts. ``[[block_conditions]]`` patterns
(configuration, never code) recognize such reasons; when the PR a reason names resolves on
GitHub, the block gets a condition of the configured kind, linked through
``Block.condition_id`` exactly like a condition the platform registered. Rule 4 then treats
the block as waiting: it clears it when the condition holds and alerts when it stays unmet
past ``max_block``.

Only blocks with no linked condition are touched. A reason that matches but names a PR
that cannot be resolved (unparsable, a repository not in ``[connectors.github] repos``, or
GitHub says 404) leaves the block as it is and is counted. A condition is attached only
once GitHub has shown the PR to exist (remembered across ticks); after that, a PR that
cannot be checked right now (network, rate limit, tick budget) keeps its condition with
the last known result, and rules judge its age.

The attached condition is a declared wait: rule 1 (``exit_contract``) no longer requeues
such a block, as for any block tied to a registered condition, and rule 4 owns it.

Each tick is bounded: at most ``max_requests`` GitHub requests and ``tick_budget`` of
wall-clock time; beyond them cached answers are served, or the result is unknown.

The same step evaluates conditions the platform already registered (``pr_merged`` and
``ci_status`` with ``repo``/``pr`` params for a configured repository).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from typing import Any, Protocol

from kelpie.connectors.github.connector import GitHubConnector, PrRef, parse_pr_ref, valid_repo
from kelpie.core.conditions import linked_conditions
from kelpie.core.model import Condition, ConditionKind, Snapshot, Status, Task

log = logging.getLogger(__name__)

KINDS = {"pr_merged": ConditionKind.PR_MERGED, "ci_green": ConditionKind.CI_STATUS}
MAX_REASON = 2000  # characters of a block reason the patterns look at
DEFAULT_MAX_REQUESTS = 50
DEFAULT_TICK_BUDGET = timedelta(seconds=10)
# Blocks whose reason matched a pattern: got a condition; the PR could not be resolved
# (bad ref, repo not allowed, 404); GitHub could not confirm it yet; the step failed on it.
OUTCOMES = ("attached", "unresolved", "unconfirmed", "error")
_KEYS = {"pattern", "kind", "repo"}


@dataclass(frozen=True, slots=True)
class BlockConditionPattern:
    pattern: re.Pattern[str]  # case-insensitive, with a named group ``pr``
    kind: str  # pr_merged | ci_green
    repo: str | None = None  # owner/name for bare refs such as "#123"

    @property
    def condition_kind(self) -> ConditionKind:
        return KINDS[self.kind]


def load_block_conditions(
    raw: Any, repos: Collection[str] | None
) -> tuple[BlockConditionPattern, ...]:
    """Parse ``[[block_conditions]]``. ``repos`` is the GitHub allow-list, None when the
    connector is not configured (then patterns are refused: nothing could resolve them)."""
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(t, dict) for t in raw):
        raise ValueError("block_conditions must be an array of tables ([[block_conditions]])")
    if raw and repos is None:
        raise ValueError("[[block_conditions]] needs [connectors.github] to resolve PRs")
    allowed = {r.lower() for r in repos or ()}
    patterns: list[BlockConditionPattern] = []
    for i, table in enumerate(raw):
        where = f"[[block_conditions]] #{i + 1}"
        if extra := set(table) - _KEYS:
            raise ValueError(f"{where}: unknown keys {sorted(extra)}")
        source = table.get("pattern")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"{where}: pattern must be a non-empty string")
        try:
            pattern = re.compile(source, re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"{where}: pattern does not compile: {exc}") from None
        if "pr" not in pattern.groupindex:
            raise ValueError(f"{where}: pattern needs a named group (?P<pr>...)")
        kind = table.get("kind")
        if kind not in KINDS:
            raise ValueError(f"{where}: kind must be one of {sorted(KINDS)}, got {kind!r}")
        repo = table.get("repo")
        if repo is not None:
            if not valid_repo(repo):
                raise ValueError(f'{where}: repo must be "owner/name"')
            if repo.lower() not in allowed:
                raise ValueError(f"{where}: repo {repo} is not in [connectors.github] repos")
            repo = repo.lower()
        patterns.append(BlockConditionPattern(pattern, kind, repo))
    return tuple(patterns)


class BlockConditionObserver(Protocol):
    def block_conditions(self, outcomes: Mapping[str, int]) -> None: ...


@dataclass(slots=True)
class ConditionSync:
    """A snapshot step: evaluate GitHub conditions and attach block conditions."""

    connector: GitHubConnector
    patterns: Sequence[BlockConditionPattern] = ()
    observer: BlockConditionObserver | None = None
    max_requests: int = DEFAULT_MAX_REQUESTS  # per tick
    tick_budget: timedelta = DEFAULT_TICK_BUDGET  # wall-clock time per tick

    def __call__(self, snapshot: Snapshot) -> Snapshot:
        kinds = self.connector.kinds
        blocked = sum(1 for t in snapshot.tasks if t.status is Status.BLOCKED)
        # Up to three lookups per PR (pull, status, check runs); none evicted within a tick.
        self.connector.client.begin_tick(
            self.max_requests,
            self.tick_budget,
            min_cache_entries=3 * (blocked + len(snapshot.conditions)),
        )
        conditions = tuple(self._check(c) if c.kind in kinds else c for c in snapshot.conditions)
        evaluated = replace(snapshot, conditions=conditions)
        tasks: list[Task] = []
        added: list[Condition] = []
        outcomes = dict.fromkeys(OUTCOMES, 0)
        for task in snapshot.tasks:
            condition = None
            if self.patterns and _bare_block(task, evaluated):
                try:
                    condition, outcome = self._resolve(task)
                except Exception as exc:  # one odd block reason must not sink the step
                    log.warning("block condition for task %s failed: %s", task.id, exc)
                    condition, outcome = None, "error"
                if outcome is not None:
                    outcomes[outcome] += 1
            if condition is None:
                tasks.append(task)
                continue
            assert task.block is not None
            tasks.append(replace(task, block=replace(task.block, condition_id=condition.id)))
            added.append(condition)
        if self.observer is not None:
            self.observer.block_conditions(outcomes)
        if not added:
            return evaluated
        return replace(evaluated, tasks=tuple(tasks), conditions=conditions + tuple(added))

    def _check(self, condition: Condition) -> Condition:
        try:
            return self.connector.check(condition)
        except Exception as exc:  # check() never raises by contract; belt and braces
            log.warning("condition %s could not be checked: %s", condition.id, exc)
            return condition

    def _resolve(self, task: Task) -> tuple[Condition | None, str | None]:
        """The condition for the task's block reason, and the outcome (None: no match).

        A condition is attached only for a PR GitHub has shown to exist, now or on an
        earlier tick. While that is not known (back-off, network, 401), the block stays as
        it is, so it does not flap between "waiting" and "no condition"."""
        block = task.block
        assert block is not None
        reason = block.reason[:MAX_REASON]
        outcome: str | None = None
        for p in self.patterns:
            m = p.pattern.search(reason)
            if m is None or not m["pr"]:
                continue
            outcome = outcome or "unresolved"
            ref = parse_pr_ref(m["pr"], p.repo, self.connector.host)
            if ref is None or not self.connector.allows(ref.repo):
                continue
            checked = self.connector.check(_condition(task, p, ref))
            if self.connector.confirmed(ref):
                return checked, "attached"
            if checked.params.get("error") not in {"not_found", "not_allowed"}:
                outcome = "unconfirmed"
        return None, outcome


def _bare_block(task: Task, snapshot: Snapshot) -> bool:
    """A blocked task whose block names no condition and has none linked."""
    if task.status is not Status.BLOCKED or task.block is None or task.block.condition_id:
        return False
    conditions, _ = linked_conditions(task, snapshot)
    return not conditions


def _condition(task: Task, pattern: BlockConditionPattern, ref: PrRef) -> Condition:
    assert task.block is not None
    params: dict[str, Any] = {"repo": ref.repo, "pr": ref.number, "source": "block_reason"}
    if pattern.kind == "ci_green":
        params["expect"] = "success"
    return Condition(
        id=f"block:{task.id}:{pattern.kind}:{ref}",
        kind=pattern.condition_kind,
        task_id=task.id,
        params=params,
        registered_at=task.block.since,
    )
