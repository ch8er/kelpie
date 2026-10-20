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
   - ``already_verified``: the same pipeline already passed for the same commit and the
     same trigger (event) before it started;
   - ``docs_only``: every changed path matches ``docs_globs``;
   - ``draft``: the change request is a draft;
   - ``wip``: the commit message matches ``wip_patterns`` (a WIP commit pushed without a
     skip marker; a skipped build would not be here).

   Once the wasted minutes in the window reach ``waste_min_minutes``, one ``info`` alert
   summarizes them and recommends the CI configuration change that would have saved each
   category's minutes, the most saved first: ``cancel_in_progress`` (superseded; it saves
   only the part of a build after its newer build started, pro rata), ``skip_verified``
   (already verified), ``ignore_docs_paths`` (docs-only), ``skip_drafts`` and ``skip_wip``.
   The changes are named, never written in a CI's syntax: rendering them is the CI
   connector's business. ``params.pipelines`` has them per pipeline (CI ``source``,
   ``repo``, ``workflow``; the most minutes saved first, at most ten; ``pipelines_total``
   counts them all): its ``minutes`` and ``changes``, each ``{change, minutes, builds}``,
   with ``paths`` for ``ignore_docs_paths`` (the ``docs_globs``, fnmatch: ``*`` crosses
   ``/``) and ``patterns`` with ``case_insensitive: true`` for ``skip_wip`` (the
   ``wip_patterns``, matched ignoring case): a connector translates both. It is a lasting
   state keyed by the doubling level of the wasted minutes: announced when it opens and
   again whenever the level changes, which happens when the waste doubles and also when it
   halves as old builds leave the window (a "Changed:" notice); resolved when the window no
   longer holds that much waste. Build minutes are the connector's estimate (see its docs:
   billable job minutes where known, otherwise wall-clock time), so treat the totals as an
   order of magnitude.

   With ``propose_config``, each pipeline whose wasted minutes reach ``propose_min_minutes``
   also gets ``propose_ci_config``, routed to the connector that reported its builds
   (``executor`` = ``Build.source``), with its ``repo``, ``workflow`` and ``changes``: in
   act mode the connector opens a pull request with the changes written for its CI. Once
   per pipeline and set of changes.

2. **Superseded builds still running** → ``cancel_build`` (``cancel_superseded``), only
   for builds the connector marked ``cancellable`` (within its cancel policy), routed to
   that connector (``executor`` = ``Build.source``). The connector re-checks the build
   before cancelling and may refuse.

3. **Quota.** The burn rate is the minutes of the builds started within ``window`` per
   hour. With minutes used known: exhausted (utilization >= ``quota_critical``) →
   ``critical`` with the tasks waiting on CI (``blocked_tasks``); running out within
   ``forecast_horizon`` and before the quota resets → ``warning``. With only the quota
   known (no usage reading), a burn that would exceed it over ``quota_period`` →
   ``warning``. Readings older than ``max_reading_age`` are ignored.

4. **CI outage** (incident 20): some ``outage_window`` holds failures of ``outage_min_prs``
   or more change requests at the same workflow, job and step, each still active (nothing
   passed that job in its repository since), with evidence that the environment broke
   (twice as many without it) → one ``warning`` "CI outage since ..." with its scope
   (repositories, workflows, runners, PRs) and the tasks it explains, open until a build
   passes that job again. See :mod:`kelpie.rules._ci_outage`; the params are the contract
   in :mod:`kelpie.core.incidents`. ``outage_min_prs = 0`` turns it off.
"""

from __future__ import annotations

import fnmatch
import math
import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any, ClassVar

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.alerts import ANNOUNCES
from kelpie.core.conditions import linked_conditions
from kelpie.core.incidents import is_outage_state
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
from kelpie.rules._alerts import alert_params
from kelpie.rules._alerts import count as counted
from kelpie.rules._ci_outage import outages, passed_again

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
# The configuration change that would have saved the minutes of each waste category, and
# what it does, in no CI's syntax.
CHANGES = {
    "superseded": ("cancel_in_progress", "cancel a running build when a newer commit is pushed"),
    "already_verified": (
        "skip_verified",
        "skip a build of a commit that already passed this pipeline for the same trigger",
    ),
    "docs_only": ("ignore_docs_paths", "skip changes that touch only docs"),
    "draft": ("skip_drafts", "skip draft change requests"),
    "wip": ("skip_wip", "skip WIP commits"),
}
_MAX_PIPELINES = 10
CHANGES_BY_NAME = dict(CHANGES.values())


@dataclass(frozen=True, slots=True)
class CiEconomy:
    boards: ClassVar[bool] = False  # builds and quotas only (kelpie.core.rule)
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
    outage_min_prs: int = 3
    outage_window: timedelta = timedelta(hours=2)
    propose_config: bool = False  # opt-in, with the CI connector's allow_propose
    propose_min_minutes: float = 60.0
    name: str = NAME

    def evaluate(self, snapshot: Snapshot) -> Sequence[Action]:
        recent = self._recent(snapshot)
        actions: list[Action] = []
        cancels = self._cancels(snapshot) if self.cancel_superseded else []
        actions.extend(cancels)
        if (waste := self._waste(recent, snapshot)) is not None:
            if cancels:  # the summary says what happened to this tick's cancels
                keys = [c.key for c in cancels if c.key]
                waste = replace(waste, params={**waste.params, ANNOUNCES: keys})
            actions.append(waste)
            if self.propose_config:
                actions.extend(self._proposals(waste.params["pipelines"]))
        actions.extend(self._quota(recent, snapshot))
        actions.extend(outages(snapshot, NAME, self.outage_min_prs, self.outage_window))
        return actions

    def resolved(self, state_id: str, snapshot: Snapshot) -> str | None:
        """An outage resolves when its job passes again: say where. None for the others."""
        if not is_outage_state(state_id):
            return None
        return passed_again(state_id, snapshot, self.outage_min_prs, self.outage_window)

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
        return self._classify(build, _Index(snapshot.builds))

    def _classify(self, build: Build, index: _Index) -> str | None:
        if build.status is BuildStatus.SKIPPED:
            return None
        if build.superseded_by is not None:
            newer = index.by_id.get(build.superseded_by)
            if build.status.is_active or (
                newer is not None
                and newer.started_at is not None
                and build.finished_at is not None
                and newer.started_at < build.finished_at
            ):
                return "superseded"
        if index.verified_before(build):
            return "already_verified"
        if build.changed_paths and all(self._is_doc(p) for p in build.changed_paths):
            return "docs_only"
        if build.draft:
            return "draft"
        message = build.commit_message or ""
        if any(p.search(message) for p in self.wip_patterns):
            return "wip"
        return None

    def _is_doc(self, path: str) -> bool:
        return any(fnmatch.fnmatchcase(path, g) for g in self.docs_globs)

    def _waste(self, recent: list[Build], snapshot: Snapshot) -> Action | None:
        index = _Index(snapshot.builds)
        wasted: dict[str, list[Build]] = {c: [] for c in CATEGORIES}
        for build in recent:
            if (category := self._classify(build, index)) is not None:
                wasted[category].append(build)
        minutes = sum(b.minutes for found in wasted.values() for b in found)
        if minutes <= 0 or minutes < self.waste_min_minutes:
            return None
        total = sum(b.minutes for b in recent)
        count = sum(len(found) for found in wasted.values())
        hours = self.window.total_seconds() / 3600
        saved = {c: self._saved(c, found, index, snapshot.now) for c, found in wasted.items()}
        advice = [
            f"{CHANGES[c][1]} ({_LABELS[c]} {len(found)}, "
            + (
                f"{_minutes(saved[c])} of {_minutes(sum(b.minutes for b in found))})"
                if round(saved[c]) != round(sum(b.minutes for b in found))
                else f"{_minutes(saved[c])})"
            )
            for c, found in _most_first(wasted, saved)
        ]
        reason = (
            f"CI waste in the last {hours:g}h: {count} of {len(recent)} builds burned "
            f"{_minutes(minutes)} of {_minutes(total)} for nothing. A CI configuration would "
            f"have saved {_minutes(sum(saved.values()))}: {'; '.join(advice)}."
            + (
                " Rule 9 cancels superseded builds in act mode."
                if wasted["superseded"] and self.cancel_superseded
                else ""
            )
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
        params["pipelines"], params["pipelines_total"] = self._pipelines(
            wasted, index, snapshot.now
        )
        params.update(
            alert_params(
                f"CI waste: {count} of {counted(len(recent), 'build')} burned {_minutes(minutes)} "
                "for nothing"
            )
        )
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

    def _saved(self, category: str, builds: Sequence[Build], index: _Index, now: datetime) -> float:
        """The minutes ``category``'s configuration change would have saved on ``builds``:
        all of them, except for a superseded build, where cancelling in progress saves only
        the part after its newer build started (pro rata over its run)."""
        if category != "superseded":
            return float(sum(b.minutes for b in builds))
        total = 0.0
        for b in builds:
            newer = index.by_id.get(b.superseded_by or "")
            end = b.finished_at or now
            if newer is None or newer.started_at is None or b.started_at is None:
                total += b.minutes  # its successor unknown: at most all of it
            elif end > b.started_at:
                share = (end - newer.started_at) / (end - b.started_at)
                total += b.minutes * min(max(share, 0.0), 1.0)
        return total

    def _pipelines(
        self, wasted: dict[str, list[Build]], index: _Index, now: datetime
    ) -> tuple[list[dict[str, Any]], int]:
        """Per pipeline (CI source, repository and workflow), the configuration changes that
        would have saved its minutes, each with those minutes and builds; the pipeline with
        the most to save first, at most ``_MAX_PIPELINES``; and how many pipelines there
        were."""
        by_pipeline: dict[tuple[str, str, str], dict[str, list[Build]]] = {}
        for category, found in wasted.items():
            for b in found:
                key = (b.source or "", b.repo or "", b.workflow or "")
                by_pipeline.setdefault(key, {}).setdefault(category, []).append(b)
        out: list[dict[str, Any]] = []
        for (source, repo, workflow), categories in by_pipeline.items():
            saved = {c: self._saved(c, found, index, now) for c, found in categories.items()}
            changes: list[dict[str, Any]] = []
            for category, found in _most_first(categories, saved):
                change: dict[str, Any] = {
                    "change": CHANGES[category][0],
                    "minutes": round(saved[category], 1),
                    "builds": len(found),
                }
                if category == "docs_only":
                    change["paths"] = list(self.docs_globs)  # fnmatch: * crosses /
                elif category == "wip":
                    change["patterns"] = [p.pattern for p in self.wip_patterns]
                    change["case_insensitive"] = True
                changes.append(change)
            out.append(
                {
                    "repo": repo or None,
                    "workflow": workflow or None,
                    "source": source or None,
                    "minutes": round(sum(saved.values()), 1),
                    "changes": changes,
                }
            )
        out.sort(
            key=lambda p: (-p["minutes"], str(p["repo"]), str(p["workflow"]), str(p["source"]))
        )
        return out[:_MAX_PIPELINES], len(out)

    def _proposals(self, pipelines: list[dict[str, Any]]) -> list[Action]:
        hours = self.window.total_seconds() / 3600
        actions = []
        for p in pipelines:
            source, repo, workflow = p["source"], p["repo"], p["workflow"]
            if not source or not repo or not workflow or p["minutes"] < self.propose_min_minutes:
                continue
            names = [c["change"] for c in p["changes"]]
            what = "; ".join(
                f"{CHANGES_BY_NAME[c['change']]} ({_minutes(c['minutes'])})" for c in p["changes"]
            )
            actions.append(
                Action(
                    ActionKind.PROPOSE_CI_CONFIG,
                    NAME,
                    f"A better CI configuration for {workflow} in {repo} would have saved "
                    f"{_minutes(p['minutes'])} in the last {hours:g}h: {what}.",
                    executor=source,
                    key=f"{NAME}:propose:{repo}:{workflow}:{'+'.join(sorted(names))}",
                    once=True,
                    params={
                        "repo": repo,
                        "workflow": workflow,
                        "minutes": p["minutes"],
                        "changes": p["changes"],
                    },
                )
            )
        return actions

    # --- 2. cancel superseded builds ----------------------------------------------------

    def _cancels(self, snapshot: Snapshot) -> list[Action]:
        actions: list[Action] = []
        for build in snapshot.builds:
            if (
                not build.cancellable
                or not build.status.is_active
                or build.superseded_by is None
                or not build.source
            ):
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
                    f"Build {build.id} {what} was superseded by {build.superseded_by} after "
                    f"burning {_minutes(build.minutes)} of CI.",
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
                    **alert_params(
                        f"CI minutes quota {r.name} is exhausted: builds will not start"
                    ),
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
                **alert_params(f"CI minutes quota {r.name} runs out in about {_hours(left)}"),
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
                **alert_params(f"CI would use more than quota {r.name} within {days:g} days"),
            },
        )


class _Index:
    """Builds indexed once per evaluation, so classifying is linear in the builds."""

    def __init__(self, builds: Sequence[Build]) -> None:
        self.by_id = {b.id: b for b in builds}
        # The two earliest successes per pipeline, commit and trigger: enough to tell
        # whether a build other than the one asked about passed before it started.
        self._passed: dict[tuple[Any, ...], list[tuple[datetime, str]]] = {}
        for b in builds:
            if b.status is BuildStatus.SUCCESS and b.sha and b.finished_at is not None:
                key = (b.repo, b.workflow, b.sha, b.event)
                found = self._passed.setdefault(key, [])
                found.append((b.finished_at, b.id))
                found.sort()
                del found[2:]

    def verified_before(self, build: Build) -> bool:
        """The same pipeline passed for the same commit and trigger before it started."""
        if build.sha is None or build.started_at is None:
            return False
        passed = self._passed.get((build.repo, build.workflow, build.sha, build.event), [])
        started = build.started_at
        return any(at <= started and bid != build.id for at, bid in passed)


def _most_first(
    wasted: dict[str, list[Build]], saved: dict[str, float]
) -> list[tuple[str, list[Build]]]:
    """The categories with builds, the most minutes saved first (ties by name)."""
    return sorted(
        ((c, found) for c, found in wasted.items() if found),
        key=lambda item: (-saved[item[0]], item[0]),
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
    for key in ("window", "forecast_horizon", "quota_period", "max_reading_age", "outage_window"):
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
    if "outage_min_prs" in config:
        value = config.pop("outage_min_prs")
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1000:
            raise ValueError("outage_min_prs must be a whole number from 0 (off) to 1000")
        if value == 1:
            raise ValueError("outage_min_prs must be at least 2: one red PR is not an outage")
        kwargs["outage_min_prs"] = value
    if "quota_critical" in config:
        kwargs["quota_critical"] = _number(config.pop("quota_critical"), "quota_critical", top=1)
    if "propose_min_minutes" in config:
        kwargs["propose_min_minutes"] = _number(
            config.pop("propose_min_minutes"), "propose_min_minutes", zero=True
        )
    if "propose_config" in config:
        value = config.pop("propose_config")
        if not isinstance(value, bool):
            raise ValueError("propose_config must be true or false")
        kwargs["propose_config"] = value
    if "cancel_superseded" in config:
        value = config.pop("cancel_superseded")
        if not isinstance(value, bool):
            raise ValueError("cancel_superseded must be true or false")
        kwargs["cancel_superseded"] = value
    if config:
        raise ValueError(f"unknown parameters: {sorted(config)}")
    return CiEconomy(**kwargs)
