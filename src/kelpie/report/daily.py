"""Daily report (stub). Aggregates actions, idle time and token spend for a day.

An LLM may optionally be used here, and only here, to draft root-cause notes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from kelpie.core.actions import Action


def build_report(day: date, actions: Sequence[Action]) -> str:
    raise NotImplementedError
