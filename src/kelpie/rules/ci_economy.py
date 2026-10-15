"""Rule 9 — CI economy (incident 5).

CI minutes ran out and tasks waited about 11 hours for checks that would never start. This
rule looks at the CI builds in the snapshot (``Snapshot.builds``, filled by a CI connector)
and at the CI minutes quota (``ci_minutes`` resources). It stays CI-agnostic: builds,
pipelines, branches and change requests are generic; which CI they come from is the
connector's business.

1. **Wasted builds.** Every build started within ``window`` is classified, at most once,
   in this order:

   - ``superseded``: a newer build of the same pipeline and branch started while it was
     still running (``superseded_by``);
   - ``already_verified``: the same pipeline already passed for the same commit before it
     started;
   - ``docs_only``: every changed path matches ``docs_globs``;
   - ``draft``: the change request is a draft;
   - ``wip``: the commit message matches ``wip_patterns`` (a WIP commit pushed without a
     skip marker; a skipped build would not be here).

   Once the wasted minutes in the window reach ``waste_min_minutes``, one ``info`` alert
   summarizes them per category. It is a lasting state: announced when it opens, again
   each time the wasted minutes double, and resolved when the window no longer holds that
   much waste.

2. **Superseded builds still running** → ``cancel_build`` (``cancel_superseded``), routed
   to the connector that reported the build (``executor`` = ``Build.source``). The
   connector re-checks the build before cancelling and may refuse.

3. **Quota.** The burn rate is the minutes of the builds started within ``window`` per
   hour. With minutes used known: exhausted (utilization >= ``quota_critical``) →
   ``critical`` with the tasks waiting on CI (``blocked_tasks``); running out within
   ``forecast_horizon`` and before the quota resets → ``warning``. With only the quota
   known (no usage reading), a burn that would exceed it over ``quota_period`` →
   ``warning``. Readings older than ``max_reading_age`` are ignored.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.conditions import linked_conditions
from kelpie.core.model import (
    Build,
    BuildStatus,
    ConditionKind,
    Resource,
    ResourceKind,
    Snapshot,
    Status,
)
from kelpie.core.serde import parse_duration
from kelpie.rules import register

NAME = "ci_economy"
CATEGORIES = ("superseded", "already_verified", "docs_only", "draft", "wip")
_LABELS = {
    "superseded": "superseded by a newer build",
    "already_verified": "repeated for an already verified commit",
    "docs_only": "docs-only changes",
    "draft": "drafts",
    "wip": "WIP commits",
}
_MAX_LISTED = 20
DEFAULT_DOCS_GLOBS = ("docs/*", "*.md", "*.rst")
DEFAULT_WIP_PATTERNS = (r"^\s*\[?wip\b", r"^\s*(fixup|squash)!")


@dataclass(frozen=True, slots=True)
class CiEconomy:
    window: timedelta = timedelta(hours=24)
    docs_globs: tuple[str, ...] = DEFAULT_DOCS_GLOBS
    wip_patterns: tuple[re.Pattern[str], ...] = tuple(
        re.compile(p, re.IGNORECASE) for p in DEFAULT_WIP_PATTERNS
    )
    waste_min_minutes: float = 30.0
    cancel_superseded: bool = True
    quota_critical: float = 1.0
    forecast_horizon: timedelta = timedelta(days=7)
    quota_period: timedelta = timedelta(days=30)
    max_reading_age: timedelta = timedelta(hours=1)
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        recent = self._recent(snapshot)
        actions: list[Action] = []
        if self.cancel_superseded:
            actions.extend(self._cancels(snapshot))
        if (waste := self._waste(recent, snapshot)) is not None:
            actions.append(waste)
        actions.extend(self._quota(recent, snapshot))
        return actions

    def _recent(self, snapshot: Snapshot) -> list[Build]:
        since = snapshot.now - self.window
        return [
            b
            for b in snapshot.builds
            if (start := b.started_at or b.finished_at) is not None and start >= since
        ]

    # --- 1. wasted builds ---------------------------------------------------------------

    def classify(self, build: Build, snapshot: Snapshot) -> str | None:
        """The waste category of ``build``, or None when it was worth its minutes."""
        if build.status is BuildStatus.SKIPPED:
            return None
        if build.superseded_by is not None:
            newer = snapshot.build(build.superseded_by)
            if build.status.is_active or (
                newer is not None
                and newer.started_at is not None
                and build.finished_at is not None
                and newer.started_at < build.finished_at
            ):
                return "superseded"
        if self._verified_before(build, snapshot):
            return "already_verified"
        if build.changed_paths and all(self._is_doc(p) for p in build.changed_paths):
            return "docs_only"
        if build.draft:
            return "draft"
        message = build.commit_message or ""
        if any(p.search(message) for p in self.wip_patterns):
            return "wip"
        return None

    @staticmethod
    def _verified_before(build: Build, snapshot: Snapshot) -> bool:
        if build.sha is None or build.started_at is None:
            return False
        return any(
            other.id != build.id
            and other.status is BuildStatus.SUCCESS
            and other.sha == build.sha
            and other.repo == build.repo
            and other.workflow == build.workflow
            and other.finished_at is not None
            and other.finished_at <= build.started_at
            for other in snapshot.builds
        )

    def _is_doc(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, g) for g in self.docs_globs)

    def _waste(self, recent: list[Build], snapshot: Snapshot) -> Action | None:
        wasted: dict[str, list[Build]] = {c: [] for c in CATEGORIES}
        for build in recent:
            if (category := self.classify(build, snapshot)) is not None:
                wasted[category].append(build)
        minutes = sum(b.minutes for found in wasted.values() for b in found)
        if minutes <= 0 or minutes < self.waste_min_minutes:
            return None
        total = sum(b.minutes for b in recent)
        count = sum(len(found) for found in wasted.values())
        parts = [
            f"{_LABELS[c]} {len(found)} ({_minutes(sum(b.minutes for b in found))})"
            for c, found in wasted.items()
            if found
        ]
        hours = self.window.total_seconds() / 3600
        reason = (
            f"CI waste in the last {hours:g}h: {count} of {len(recent)} builds burned "
            f"{_minutes(minutes)} of {_minutes(total)} for nothing — {'; '.join(parts)}. "
            "Skip CI for drafts, WIP and docs-only changes, and cancel superseded builds."
        )
        params: dict[str, Any] = {
            "wasted_minutes": round(minutes, 1),
            "total_minutes": round(total, 1),
            "wasted_builds": count,
            "categories": {
                c: {"builds": len(found), "minutes": round(sum(b.minutes for b in found), 1)}
                for c, found in wasted.items()
                if found
            },
        }
        for c, found in wasted.items():
            if found:
                params[f"{c}_builds"] = [b.id for b in found[:_MAX_LISTED]]
        # Announced again each time the waste doubles: the key moves to the next level.
        level = int(math.log2(minutes / max(self.waste_min_minutes, 1.0)))
        starts = [b.started_at or b.finished_at for found in wasted.values() for b in found]
        since = min((s for s in starts if s is not None), default=snapshot.now)
        return Action(
            ActionKind.ALERT,
            NAME,
            reason,
            severity=Severity.INFO,
            key=f"{NAME}:waste:{level}",
            state=f"{NAME}:waste",
            since=since,
            params=params,
        )

    # --- 2. cancel superseded builds ----------------------------------------------------

    def _cancels(self, snapshot: Snapshot) -> list[Action]:
        actions: list[Action] = []
        for build in snapshot.builds:
            if not build.status.is_active or build.superseded_by is None or not build.source:
                continue
            what = " ".join(
                x
                for x in (
                    f"of {build.workflow}" if build.workflow else "",
                    f"on {build.branch}" if build.branch else "",
                    f"in {build.repo}" if build.repo else "",
                )
                if x
            )
            actions.append(
                Action(
                    ActionKind.CANCEL_BUILD,
                    NAME,
                    f"Build {build.id} {what} is superseded by {build.superseded_by}; "
                    f"cancelling it saves CI minutes ({_minutes(build.minutes)} burned so far).",
                    executor=build.source,
                    key=f"{NAME}:cancel:{build.id}",
                    once=True,
                    params={
                        "build_id": build.id,
                        "superseded_by": build.superseded_by,
                        "repo": build.repo,
                        "branch": build.branch,
                        "workflow": build.workflow,
                        "minutes": round(build.minutes, 1),
                    },
                )
            )
        return actions

    # --- 3. quota -------------------------------------------------------------------

    def _quota(self, recent: list[Build], snapshot: Snapshot) -> list[Action]:
        burn = sum(b.minutes for b in recent) / (self.window.total_seconds() / 3600)
        actions: list[Action] = []
        for r in snapshot.resources:
            if r.kind is not ResourceKind.CI_MINUTES or not r.limit:
                continue
            if r.at is not None and snapshot.now - r.at > self.max_reading_age:
                continue  # stale: no data is better than old data
            if r.used is not None:
                action = self._usage_alert(r, r.used, r.limit, burn, snapshot)
            else:
                action = self._projection_alert(r, r.limit, burn, snapshot)
            if action is not None:
                actions.append(action)
        return actions

    def _usage_alert(
        self, r: Resource, used: float, limit: float, burn: float, snapshot: Snapshot
    ) -> Action | None:
        now = snapshot.now
        resets = f" It resets {r.resets_at.isoformat()}." if r.resets_at else ""
        if used / limit >= self.quota_critical:
            blocked = _waiting_on_ci(snapshot)
            waiting = (
                f" {len(blocked)} tasks wait for CI that will not run: {', '.join(blocked)}."
                if blocked
                else ""
            )
            return Action(
                ActionKind.ALERT,
                NAME,
                f"CI minutes quota {r.name} is exhausted ({used:g} of {limit:g} {r.unit}): "
                f"builds will not start; the cause is billing, not the code.{resets}{waiting}",
                severity=Severity.CRITICAL,
                key=f"{NAME}:quota:{r.name}:exhausted",
                state=f"{NAME}:quota:{r.name}",
                params={
                    "resource": r.name,
                    "used": used,
                    "limit": limit,
                    "resets_at": r.resets_at.isoformat() if r.resets_at else None,
                    "blocked_tasks": blocked,
                },
            )
        if burn <= 0:
            return None
        left = timedelta(hours=(limit - used) / burn)
        runs_out_at = now + left
        if left > self.forecast_horizon or (r.resets_at is not None and runs_out_at >= r.resets_at):
            return None
        return Action(
            ActionKind.ALERT,
            NAME,
            f"CI minutes quota {r.name} runs out in about {_hours(left)} at the current burn "
            f"of {burn * 24:.0f} {r.unit or 'minutes'}/day ({used:g} of {limit:g} used).{resets} "
            "Cut wasted builds or raise the quota.",
            severity=Severity.WARNING,
            key=f"{NAME}:quota:{r.name}:forecast",
            state=f"{NAME}:quota:{r.name}",
            params={
                "resource": r.name,
                "used": used,
                "limit": limit,
                "burn_per_hour": round(burn, 2),
                "runs_out_at": runs_out_at.isoformat(),
                "resets_at": r.resets_at.isoformat() if r.resets_at else None,
            },
        )

    def _projection_alert(
        self, r: Resource, limit: float, burn: float, snapshot: Snapshot
    ) -> Action | None:
        projected = burn * self.quota_period.total_seconds() / 3600
        if projected <= limit:
            return None
        days = self.quota_period.total_seconds() / 86400
        return Action(
            ActionKind.ALERT,
            NAME,
            f"At the current burn of {burn * 24:.0f} {r.unit or 'minutes'}/day, CI would use "
            f"about {projected:.0f} in {days:g} days, more than the {limit:g} of quota {r.name}. "
            "Cut wasted builds or raise the quota.",
            severity=Severity.WARNING,
            key=f"{NAME}:quota:{r.name}:projection",
            state=f"{NAME}:quota:{r.name}",
            params={
                "resource": r.name,
                "limit": limit,
                "burn_per_hour": round(burn, 2),
                "projected": round(projected),
                "resets_at": r.resets_at.isoformat() if r.resets_at else None,
            },
        )


def _waiting_on_ci(snapshot: Snapshot) -> list[str]:
    """Live tasks blocked or waiting on a CI condition that has not been met."""
    ids: list[str] = []
    for task in snapshot.tasks:
        if task.status not in {Status.BLOCKED, Status.WAITING}:
            continue
        conditions, _ = linked_conditions(task, snapshot)
        if any(c.kind is ConditionKind.CI_STATUS and c.satisfied is not True for c in conditions):
            ids.append(task.id)
    return sorted(ids)


def _minutes(value: float) -> str:
    return f"{value:.0f} min"


def _hours(d: timedelta) -> str:
    hours = d.total_seconds() / 3600
    return f"{hours / 24:.1f} days" if hours >= 48 else f"{hours:.1f}h"


# --- configuration ----------------------------------------------------------------------


def _strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"{name} must be a list of non-empty strings")
    return tuple(value)


def _duration(value: Any, name: str) -> timedelta:
    try:
        d = parse_duration(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"{name}: invalid duration {value!r}") from exc
    if d <= timedelta(0):
        raise ValueError(f"{name} must be positive")
    return d


def _number(value: Any, name: str, *, top: float | None = None, zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{name} must be a number")
    if value < 0 or (value == 0 and not zero) or (top is not None and value > top):
        raise ValueError(f"{name} is out of range")
    return float(value)


@register(NAME)
def build(config: dict[str, Any]) -> CiEconomy:
    config = dict(config)
    kwargs: dict[str, Any] = {}
    for key in ("window", "forecast_horizon", "quota_period", "max_reading_age"):
        if key in config:
            kwargs[key] = _duration(config.pop(key), key)
    if "docs_globs" in config:
        kwargs["docs_globs"] = _strings(config.pop("docs_globs"), "docs_globs")
    if "wip_patterns" in config:
        patterns = _strings(config.pop("wip_patterns"), "wip_patterns")
        try:
            kwargs["wip_patterns"] = tuple(re.compile(p, re.IGNORECASE) for p in patterns)
        except re.error as exc:
            raise ValueError(f"wip_patterns: {exc}") from None
    if "waste_min_minutes" in config:
        kwargs["waste_min_minutes"] = _number(
            config.pop("waste_min_minutes"), "waste_min_minutes", zero=True
        )
    if "quota_critical" in config:
        kwargs["quota_critical"] = _number(config.pop("quota_critical"), "quota_critical", top=1)
    if "cancel_superseded" in config:
        value = config.pop("cancel_superseded")
        if not isinstance(value, bool):
            raise ValueError("cancel_superseded must be true or false")
        kwargs["cancel_superseded"] = value
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return CiEconomy(**kwargs)
