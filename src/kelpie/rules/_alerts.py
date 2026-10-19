"""What an alert shows besides its reason (#169): ``params`` for the alert layout.

A rule's ``reason`` stays its full account: the ledger and the daily report read it, and the
alert shows it as "Why". These params give the message its other parts:

- ``headline``: a short, specific line that says what happened, not the whole reason. The
  rule writes it whole; the renderer cuts it to its limit (``MAX_HEADLINE`` characters,
  never inside an open quote);
- ``needed``: what a person should do, for an alert about one task (the layout shows it on
  that task's line), only when there is a concrete ask. The engine leaves it out when the
  alert announces an effect that Kelpie carried out;
- ``blocking``: how many tasks are stuck behind that task, only when the rule knows it.

They are the rule's own English text: the ``[alerts] language`` setting translates Kelpie's
fixed words only, as it passes agents' text through as is.
"""

from __future__ import annotations

from typing import Any

MAX_HEADLINE = 100  # what the renderer shows of a rule's headline (kelpie.alerts.render)

_SINGULAR_VERBS = {"are": "is", "were": "was", "have": "has"}


def alert_params(
    headline: str, needed: str | None = None, blocking: int | None = None
) -> dict[str, Any]:
    """The params of an alert's layout: always a headline, ``needed`` and ``blocking`` only
    when given."""
    params: dict[str, Any] = {"headline": " ".join(headline.split())}
    if needed:
        params["needed"] = needed
    if blocking is not None:
        params["blocking"] = blocking
    return params


def count(n: int, noun: str, verb: str = "") -> str:
    """``1 task``, ``3 tasks``; with a (plural) ``verb``, in agreement: ``1 task waits``,
    ``3 tasks wait``, ``1 host is``, ``2 hosts are``. For a headline (the reason keeps its
    ``task(s)``)."""
    words = f"{n} {noun}" if n == 1 else f"{n} {noun}s"
    if not verb:
        return words
    return f"{words} {_SINGULAR_VERBS.get(verb, verb + 's') if n == 1 else verb}"
