"""``lift_hold``: rule 6 lifts a forgotten label hold by taking the label off its pull
requests, through the GitHub connector that reads them.

Off unless ``[connectors.github.pulls] allow_lift_holds`` is set, and then only:

- for a hold whose ``executor`` is ``"github_pulls"`` (a hold names its owner; none by default)
  and whose kind is ``label`` (a flag or a pause lives on the board, not on GitHub);
- on repositories in ``lift_repos`` (an allow-list), and only for the label the action
  names on the pull requests it names (``repo#number``, at most 50);
- with a token of its own (``lift_token_env``: Issues or Pull requests write on those
  repositories), never the read token or another connector's.

It runs only through the engine, so rule 6's mode (``observe`` by default) and the global
dry-run apply: nothing is lifted unless the rule is in ``act``. Each label is removed with
one ``DELETE /repos/{repo}/issues/{number}/labels/{label}``; a label already gone (404) is
nothing to do. GitHub's refusals that retrying will not change (403, an organization
policy, a redirect, 422) refuse, so a person is told; rate limits, back-off, 5xx and network
trouble raise, so the engine records a failure and retries later.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import quote

from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.core.actions import Action, ActionKind, ActionResult

log = logging.getLogger(__name__)

NAME = "github_pulls"  # the executor name a hold gives as its owner
MAX_LIFT_PRS = 50
MAX_LABEL = 100  # GitHub's own limit on a label's length
_REF = re.compile(r"(?P<repo>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(?P<number>[1-9][0-9]{0,9})")
_REFUSED_KINDS = frozenset({"forbidden", "policy", "redirect"})


@dataclass(slots=True)
class HoldLifter:
    """Carries out ``lift_hold`` for label holds on pull requests (module doc)."""

    client: GitHubClient  # with the lift token
    repos: tuple[str, ...]  # lift_repos
    name: str = NAME
    actions: frozenset[ActionKind] = frozenset({ActionKind.LIFT_HOLD})

    def execute(self, action: Action) -> ActionResult:
        if action.kind is not ActionKind.LIFT_HOLD:
            return ActionResult.REFUSED
        label = action.params.get("hold")
        refs = action.params.get("lift_prs")
        if (
            action.params.get("hold_kind", "label") != "label"  # a flag or pause: the board's
            or not isinstance(label, str)
            or not label
            or len(label) > MAX_LABEL
            or not isinstance(refs, list)
        ):
            log.warning("lift_hold: not a label hold on pull requests; refused: %s", action)
            return ActionResult.REFUSED
        allowed = {r.lower() for r in self.repos}
        targets: list[tuple[str, int]] = []
        for ref in refs[:MAX_LIFT_PRS]:
            m = _REF.fullmatch(ref) if isinstance(ref, str) else None
            if m is None or m["repo"].lower() not in allowed:
                log.warning("lift_hold: %r is not a pull request of lift_repos; refused", ref)
                return ActionResult.REFUSED
            targets.append((m["repo"], int(m["number"])))
        if not targets:
            return ActionResult.NOTHING_TO_DO  # held tasks only: their board owns the label
        return self._lift(label, targets)

    def _lift(self, label: str, targets: Sequence[tuple[str, int]]) -> ActionResult:
        removed = 0
        name = quote(label, safe="")
        for repo, number in targets:
            path = f"/repos/{repo}/issues/{number}/labels/{name}"
            try:
                self.client.send("DELETE", path)
                removed += 1
            except GitHubError as exc:
                if exc.status == 404 and exc.kind not in _REFUSED_KINDS:
                    continue  # the label is gone already (or the PR): nothing to lift
                if exc.kind in _REFUSED_KINDS or exc.status in (403, 422):
                    log.warning("lift_hold: GitHub refused %s#%d: %s", repo, number, exc)
                    return ActionResult.REFUSED
                raise  # rate limits, back-off, 5xx, network: the engine retries later
        if not removed:
            return ActionResult.NOTHING_TO_DO
        log.info("lift_hold: took %r off %d pull request(s)", label, removed)
        return ActionResult.DONE
