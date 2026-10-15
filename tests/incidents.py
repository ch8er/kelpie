"""Load incident fixtures and match rule output against expectations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import Snapshot
from kelpie.core.serde import parse_datetime, parse_duration, snapshot_from_dict

FIXTURES = Path(__file__).parent / "fixtures" / "incidents"
REQUIRED_KEYS = {"id", "incident", "title", "description", "rule", "snapshot", "expected_actions"}
OPTIONAL_KEYS = {"config", "not_expected_actions"}
MATCH_KEYS = {"kind", "task_id", "severity", "reason_contains", "params"}


@dataclass(frozen=True)
class Incident:
    path: Path
    id: str
    number: int | None
    rule: str
    config: dict[str, Any]
    snapshot: Snapshot
    expected: list[dict[str, Any]]
    not_expected: list[dict[str, Any]]


def expand_repeats(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand fixture-only ``repeat: {count, every}`` span entries.

    Copy ``i`` gets ``span_id`` suffix ``-<i>`` and ``start``/``end`` shifted by
    ``i * every``. Keeps fixtures with hundreds of identical calls readable.
    """
    out: list[dict[str, Any]] = []
    for span in spans:
        repeat = span.get("repeat")
        if repeat is None:
            out.append(span)
            continue
        base = {k: v for k, v in span.items() if k != "repeat"}
        every = parse_duration(repeat["every"])
        for i in range(int(repeat["count"])):
            copy = dict(base, span_id=f"{base['span_id']}-{i}")
            for key in ("start", "end"):
                if key in base:
                    copy[key] = (parse_datetime(base[key]) + i * every).isoformat()
            out.append(copy)
    return out


def load(path: Path) -> Incident:
    raw = yaml.safe_load(path.read_text())
    missing = REQUIRED_KEYS - raw.keys()
    unknown = raw.keys() - REQUIRED_KEYS - OPTIONAL_KEYS
    if missing or unknown:
        raise ValueError(f"{path.name}: missing {sorted(missing)}, unknown {sorted(unknown)}")
    if raw["id"] != path.stem:
        raise ValueError(f"{path.name}: id {raw['id']!r} must equal file stem")
    expected = raw["expected_actions"]
    not_expected = raw.get("not_expected_actions") or []
    for spec in [*expected, *not_expected]:
        if extra := spec.keys() - MATCH_KEYS:
            raise ValueError(f"{path.name}: unknown match keys {sorted(extra)}")
        ActionKind(spec["kind"])
    return Incident(
        path=path,
        id=raw["id"],
        number=raw["incident"],
        rule=raw["rule"],
        config=raw.get("config") or {},
        snapshot=snapshot_from_dict(
            {**raw["snapshot"], "spans": expand_repeats(raw["snapshot"].get("spans", []))}
        ),
        expected=expected,
        not_expected=not_expected,
    )


def all_paths() -> list[Path]:
    return sorted(FIXTURES.glob("*.yaml"))


def matches(action: Action, spec: dict[str, Any]) -> bool:
    if action.kind != ActionKind(spec["kind"]):
        return False
    if "task_id" in spec and action.task_id != spec["task_id"]:
        return False
    if "severity" in spec and action.severity != spec["severity"]:
        return False
    if "reason_contains" in spec and spec["reason_contains"].lower() not in action.reason.lower():
        return False
    params = spec.get("params") or {}
    return all(_param_equal(action.params.get(k), v) for k, v in params.items())


def _param_equal(actual: Any, expected: Any) -> bool:
    """Lists compare order-insensitively; everything else by equality."""
    if isinstance(expected, list) and isinstance(actual, list | tuple):
        return sorted(map(repr, actual)) == sorted(map(repr, expected))
    return actual == expected
