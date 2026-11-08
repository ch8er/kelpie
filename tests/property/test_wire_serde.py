"""The multi-host wire format: a ``kelpie agent`` sends snapshots and the hub sends actions.
Each side decodes what the other sent, and ``decode_snapshot`` promises that nothing an
agent sends can make a rule fail. Snapshots of the incident fixtures are the starting
points; their encoded JSON is then changed a little."""

from __future__ import annotations

import contextlib
import copy
import json
from functools import cache
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kelpie.core.actions import ActionKind
from kelpie.core.model import Snapshot
from kelpie.remote.protocol import (
    SNAPSHOT_FIELDS,
    ProtocolError,
    decode_action,
    decode_report,
    decode_result,
    decode_snapshot,
    encode_snapshot,
)
from kelpie.rules import REGISTRY
from tests.incidents import Incident, all_paths, load
from tests.property.strategies import JSON

INCIDENTS = [load(p) for p in all_paths()]


def _wire(snapshot: Snapshot) -> Any:
    return json.loads(json.dumps(encode_snapshot(snapshot)))


def _paths(data: Any, prefix: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    out = [prefix] if prefix else []
    if isinstance(data, dict):
        for key, value in data.items():
            out += _paths(value, (*prefix, key))
    elif isinstance(data, list):
        for i, value in enumerate(data):
            out += _paths(value, (*prefix, i))
    return out


@cache
def _rules(name: str, config: str) -> Any:
    return REGISTRY[name](json.loads(config))


def _evaluate_all(snapshot: Snapshot, incident: Incident) -> None:
    for name in sorted(REGISTRY):
        config = incident.config if name == incident.rule else {}
        _rules(name, json.dumps(config, sort_keys=True)).evaluate(snapshot)


@pytest.mark.parametrize("incident", INCIDENTS, ids=lambda i: i.path.stem)
def test_a_fixture_snapshot_survives_the_wire(incident: Incident) -> None:
    """Encoded and decoded, a snapshot is the same snapshot, or the hub says why not."""
    try:
        decoded = decode_snapshot(_wire(incident.snapshot))
    except ProtocolError:
        return  # a fixture may hold what an agent may not send (a control character, ...)
    for name in SNAPSHOT_FIELDS:  # only the board goes over the wire
        assert getattr(decoded, name) == getattr(incident.snapshot, name), name


@given(st.sampled_from(INCIDENTS), st.data())
def test_a_changed_snapshot_is_decoded_or_refused(incident: Incident, data: st.DataObject) -> None:
    wire = copy.deepcopy(_wire(incident.snapshot))
    for _ in range(data.draw(st.integers(min_value=1, max_value=3))):
        paths = _paths(wire)
        if not paths:
            break
        *parent, last = data.draw(st.sampled_from(paths))
        target = wire
        for key in parent:
            target = target[key]
        target[last] = data.draw(JSON)
    try:
        snapshot = decode_snapshot(wire)
    except ProtocolError:
        return
    _evaluate_all(snapshot, incident)  # nothing an agent sends makes a rule fail


@given(JSON)
def test_any_value_is_a_snapshot_or_refused(value: Any) -> None:
    for body in (value, {"now": "2026-03-02T12:00:00+00:00", "tasks": value}):
        with contextlib.suppress(ProtocolError):
            decode_snapshot(body)


OFFERED = {"hermes": frozenset(ActionKind), "claude-code": frozenset({ActionKind.STOP})}
ACTION_KEYS = {
    "id": st.text(max_size=12),
    "adapter": st.sampled_from(sorted(OFFERED)),
    "kind": st.sampled_from([k.value for k in ActionKind]),
    "rule": st.text(max_size=12),
    "reason": st.text(max_size=40),
    "task_id": st.text(max_size=12) | st.none(),
    "params": st.dictionaries(st.text(max_size=8), JSON, max_size=4),
    "ttl": st.floats() | st.integers(),
}


@given(st.fixed_dictionaries({}, optional={k: v | JSON for k, v in ACTION_KEYS.items()}))
def test_any_action_is_decoded_or_refused(body: dict[str, Any]) -> None:
    try:
        action = decode_action(body, OFFERED)
    except ProtocolError:
        return
    assert action.kind in OFFERED[action.adapter]


@given(JSON)
def test_any_report_or_result_is_decoded_or_refused(value: Any) -> None:
    for decode in (decode_report, decode_result):
        with contextlib.suppress(ProtocolError):
            decode(value)
