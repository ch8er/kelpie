"""Wire format between ``kelpie agent`` and the hub of ``kelpie serve`` (docs/multi-host.md).

JSON over HTTP. The agent sends reports (one snapshot per adapter) and long-polls for
actions; the hub answers with actions and receives their results. Everything an agent or
the hub reads from the other side goes through a ``decode_*`` function here, which refuses
what the protocol does not allow: unknown fields, other snapshot parts than the board's,
action kinds the agent did not offer, params other than the kind's own, oversized text.

Builds on ``kelpie.core`` only.
"""

from __future__ import annotations

import dataclasses
import math
import re
import types
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum
from functools import partial
from typing import Any, Union, get_args, get_origin, get_type_hints

from kelpie.core import model
from kelpie.core.actions import Action, ActionKind
from kelpie.core.model import ConditionKind, Dependency, Snapshot, Span
from kelpie.core.ports import (
    PlatformAdapter,
    SupportsClose,
    SupportsReassign,
    SupportsStop,
    SupportsWait,
)
from kelpie.core.serde import from_dict, parse_datetime

VERSION = 1
REPORT_PATH = "/v1/agent/report"
ACTIONS_PATH = "/v1/agent/actions"
DEFAULT_PORT = 9465
MIN_TOKEN = 32  # characters of an agent token
MAX_ID = 256  # characters of a task, session, owner, condition or action id
MAX_REASON = 4096  # characters of an action's reason or a result's detail
MAX_RULE = 64
MAX_CONDITIONS = 32  # per wait action
MAX_ACTIONS = 64  # per poll reply
MAX_RESULTS = 256  # per poll request
MAX_WAIT = 25.0  # seconds a poll may be held open
MAX_TTL = 300.0  # seconds an action may wait to be started
MAX_TEXT = 64 * 1024  # characters of a free-text field (title, body, summary)
MAX_DEPTH = 32  # nesting of any JSON body
MAX_PARAMS_DEPTH = 8  # nesting inside free-form maps (condition params, span attributes)
# Per adapter snapshot: more is refused (a board that large needs a shorter lookback).
MAX_COUNTS = {
    "tasks": 20_000,
    "owners": 5_000,
    "dependencies": 20_000,
    "conditions": 20_000,
    "sessions": 50_000,
    "spans": 100_000,
}
# Agent names end up in task ids ("<agent>:<id>") and executor names ("<agent>/<adapter>").
_AGENT_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")
_ADAPTER_NAME = re.compile(r"[a-z0-9_-]{1,64}")
# Control characters and lone UTF-16 surrogates (JSON can carry "\ud800"; nothing can
# encode it as UTF-8 later, so SQLite and every log line would fail on it).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\ud800-\udfff]")  # line breaks, tabs stay
_ID_CONTROL = re.compile(r"[\x00-\x1f\x7f\ud800-\udfff]")
_VERSION = re.compile(r"[\x21-\x7e]{1,64}")
# Times an agent may send, and durations: a board from year 9999 would overflow rules.
_EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)
_LATEST = datetime(2200, 1, 1, tzinfo=UTC)
_LONGEST = timedelta(days=3650)
_BRANCH_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,99}")
_BOOT = re.compile(r"[0-9a-f]{8,64}")

# What an agent's snapshot may hold: the board as its adapters see it. Builds, pull
# requests, providers, resources and budgets come from the server's own connectors.
SNAPSHOT_FIELDS = frozenset(
    {"now", "tasks", "owners", "dependencies", "conditions", "sessions", "spans"}
)
# Kinds every platform adapter carries out (PlatformAdapter), and those that need a
# capability protocol.
BASE_ACTIONS = frozenset(
    {
        ActionKind.REQUEUE,
        ActionKind.WAKE,
        ActionKind.UNBLOCK,
        ActionKind.COMMENT,
        ActionKind.WIP_COMMIT,
    }
)
_CAPABILITIES: tuple[tuple[type, ActionKind], ...] = (
    (SupportsStop, ActionKind.STOP),
    (SupportsWait, ActionKind.WAIT),
    (SupportsReassign, ActionKind.REASSIGN),
    (SupportsClose, ActionKind.CLOSE),
)
PLATFORM_ACTIONS = BASE_ACTIONS | {kind for _, kind in _CAPABILITIES}
# The params each kind may carry, with their type; any other key is dropped.
_PARAMS: dict[ActionKind, dict[str, type]] = {
    ActionKind.STOP: {"session_id": str},
    ActionKind.WIP_COMMIT: {"branch": str, "draft_pr": bool},
    ActionKind.REASSIGN: {"owner_id": str},
    ActionKind.WAIT: {"conditions": list},
}
# Actions whose target is a session, not a task.
_NEEDS_SESSION = frozenset({ActionKind.STOP})


class ProtocolError(ValueError):
    """A message that does not follow the protocol. The text names what is wrong, never
    secrets; it is logged and returned to the sender."""


class Outcome(StrEnum):
    """What the agent made of an action."""

    DONE = "done"  # carried out (``detail``: what happened, for a human)
    NOTHING_TO_DO = "nothing_to_do"  # e.g. the session had already ended
    NOT_EXECUTED = "not_executed"  # the adapter cannot do it (a human is told)
    FAILED = "failed"  # it raised (``detail``: the public reason only)
    EXPIRED = "expired"  # its ttl ran out before it started: not carried out, may be retried


def valid_agent_name(name: object) -> bool:
    return isinstance(name, str) and _AGENT_NAME.fullmatch(name) is not None


def adapter_actions(
    adapter: PlatformAdapter, allowed: Iterable[ActionKind] | None = None
) -> frozenset[ActionKind]:
    """The action kinds ``adapter`` carries out: those it declares in ``actions`` (as action
    connectors do), else the platform's base kinds plus one per capability protocol it
    supports; narrowed to ``allowed`` when given. Never more than the platform kinds."""
    declared = getattr(adapter, "actions", None)
    if isinstance(declared, frozenset | set):
        kinds = {k for k in declared if k in PLATFORM_ACTIONS}
    else:
        kinds = set(BASE_ACTIONS)
        kinds.update(kind for protocol, kind in _CAPABILITIES if isinstance(adapter, protocol))
    if allowed is not None:
        kinds &= set(allowed)
    return frozenset(kinds)


# --- snapshots ----------------------------------------------------------------------------


def to_plain(value: Any) -> Any:
    """A model value as JSON: dataclasses as objects without default-valued fields,
    datetimes in ISO 8601, durations in seconds, enums by value, tuples as lists."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(value):
            v = getattr(value, f.name)
            if f.default is not dataclasses.MISSING and v == f.default:
                continue
            if f.default_factory is not dataclasses.MISSING and v == f.default_factory():
                continue
            out[f.name] = to_plain(v)
        return out
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple | list):
        return [to_plain(v) for v in value]
    if isinstance(value, dict):
        return {str(k): to_plain(v) for k, v in value.items()}
    return value


def encode_snapshot(snapshot: Snapshot) -> dict[str, Any]:
    """The board part of ``snapshot`` (:data:`SNAPSHOT_FIELDS`)."""
    plain = to_plain(snapshot)
    return {k: v for k, v in plain.items() if k in SNAPSHOT_FIELDS}


def depth_ok(data: object, limit: int = MAX_DEPTH) -> bool:
    """Whether ``data`` (parsed JSON) nests at most ``limit`` levels; iterative, so a deep
    body cannot exhaust the stack."""
    stack: list[tuple[object, int]] = [(data, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict | list):
            if depth > limit:
                return False
            values = item.values() if isinstance(item, dict) else item
            stack.extend((v, depth + 1) for v in values)
    return True


def decode_snapshot(data: object) -> Snapshot:
    """A snapshot an agent sent. Unknown fields fail (agent and server must run the same
    release), and so do parts other than the board's. Every field is checked against its
    type (ids: 1 to 256 printable characters; numbers finite; text without control
    characters; free-form maps shallow and of plain values), so nothing an agent sends can
    make a rule, the ledger or an alert fail."""
    if not isinstance(data, dict):
        raise ProtocolError("snapshot must be an object")
    if extra := set(data) - SNAPSHOT_FIELDS:
        raise ProtocolError(f"snapshot: fields not accepted from an agent: {sorted(extra)}")
    if "now" not in data:
        raise ProtocolError("snapshot: now is required")
    for key, limit in MAX_COUNTS.items():
        items = data.get(key, [])
        if not isinstance(items, list) or len(items) > limit:
            raise ProtocolError(f"snapshot: {key} must be a list of at most {limit}")
    if not depth_ok(data):
        raise ProtocolError(f"snapshot: nested deeper than {MAX_DEPTH} levels")
    try:
        _raw_lists(Snapshot, data, "snapshot")
        snapshot = _checked(from_dict(Snapshot, data), "snapshot")
        for name in ("tasks", "owners", "sessions", "conditions"):
            ids = [item.id for item in getattr(snapshot, name)]
            if len(set(ids)) != len(ids):
                raise ProtocolError(f"snapshot: {name} has an id twice")
        return snapshot
    except ProtocolError:
        raise
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError) as exc:
        raise ProtocolError(f"snapshot: {_clip(str(exc), 300)}") from None


def _raw_lists(cls: type, data: Any, where: str) -> None:
    """Fields that hold lists must arrive as JSON lists: serde would split a string into
    its characters."""
    if not isinstance(data, dict):
        raise ProtocolError(f"{where} must be an object")
    hints = _model_hints(cls)
    for key, value in data.items():
        tp = hints.get(key)
        if tp is None or value is None:
            continue
        if get_origin(tp) in (Union, types.UnionType):
            tp = next(a for a in get_args(tp) if a is not type(None))
        if get_origin(tp) is tuple:
            if not isinstance(value, list):
                raise ProtocolError(f"{where}.{key} must be a list")
            item = get_args(tp)[0]
            if dataclasses.is_dataclass(item):
                for v in value:
                    _raw_lists(item, v, f"{where}.{key}")  # type: ignore[arg-type]
        elif dataclasses.is_dataclass(tp):
            _raw_lists(tp, value, f"{where}.{key}")  # type: ignore[arg-type]


def _is_id(name: str) -> bool:
    return name in ("id", "assignee", "subtasks", "depends_on") or name.endswith("_id")


def _checked[T](obj: T, where: str) -> T:
    """``obj`` (a model dataclass) with every field checked, text cleaned."""
    cls = type(obj)
    hints = _model_hints(cls)
    changes: dict[str, Any] = {}
    for f in dataclasses.fields(obj):  # type: ignore[arg-type]
        if not f.init:
            continue
        value = getattr(obj, f.name)
        clean = _value(hints[f.name], value, f"{where}.{f.name}", _is_id(f.name))
        if clean is not value:
            changes[f.name] = clean
    return dataclasses.replace(obj, **changes) if changes else obj  # type: ignore[type-var]


def _value(tp: Any, value: Any, where: str, is_id: bool) -> Any:
    if value is None:
        return None
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        inner = [a for a in get_args(tp) if a is not type(None)]
        return _value(inner[0], value, where, is_id)
    if origin is tuple:
        if not isinstance(value, tuple):
            raise ProtocolError(f"{where} must be a list")
        (item_tp, _) = get_args(tp)
        items = [_value(item_tp, v, where, is_id) for v in value]
        return value if all(a is b for a, b in zip(items, value, strict=True)) else tuple(items)
    if origin is dict or tp is Any:
        return _plain(value, where, 1)
    if origin is Mapping:
        # Snapshot.snoozes, left at its empty default: SNAPSHOT_FIELDS refuses the field
        # from an agent before this check runs.
        return value
    if dataclasses.is_dataclass(tp):
        return _checked(value, where)
    if tp is str:
        if not isinstance(value, str):
            raise ProtocolError(f"{where} must be a string")
        if is_id:
            return _id(value, where)
        clean = _text(value, MAX_TEXT)
        return value if clean == value else clean
    if tp is bool:
        if not isinstance(value, bool):
            raise ProtocolError(f"{where} must be true or false")
        return value
    if tp is int:
        if isinstance(value, bool) or not isinstance(value, int) or abs(value) > 2**53:
            raise ProtocolError(f"{where} must be an integer")
        return value
    if tp is float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ProtocolError(f"{where} must be a number")
        if not math.isfinite(value) or abs(value) > 1e15:
            raise ProtocolError(f"{where} must be a finite number")
        return float(value)
    if tp is datetime:
        if not isinstance(value, datetime):
            raise ProtocolError(f"{where} has the wrong type")
        try:
            utc = value.astimezone(UTC)
        except OverflowError:
            raise ProtocolError(f"{where} is out of range") from None
        if not _EARLIEST <= utc < _LATEST:
            raise ProtocolError(f"{where} must be between 2000 and 2200")
        return utc
    if tp is timedelta:
        if not isinstance(value, timedelta) or not timedelta(0) <= value <= _LONGEST:
            raise ProtocolError(f"{where} must be a duration of 0 to 3650 days")
        return value
    if isinstance(tp, type) and issubclass(tp, Enum):
        if not isinstance(value, tp):
            raise ProtocolError(f"{where} has the wrong type")
        return value
    raise ProtocolError(f"{where} has a type agents may not send")


def _plain(value: Any, where: str, depth: int) -> Any:
    """A free-form value: maps with short string keys, lists, strings, finite numbers,
    booleans and null, at most :data:`MAX_PARAMS_DEPTH` levels deep."""
    if depth > MAX_PARAMS_DEPTH:
        raise ProtocolError(f"{where} nests deeper than {MAX_PARAMS_DEPTH} levels")
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > 2**63:
            raise ProtocolError(f"{where} holds an integer out of range")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProtocolError(f"{where} holds a number that is not finite")
        return value
    if isinstance(value, str):
        return _text(value, MAX_TEXT)
    if isinstance(value, list | tuple):
        return [_plain(v, where, depth + 1) for v in value]
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, v in value.items():
            if not isinstance(key, str) or not key or len(key) > MAX_ID:
                raise ProtocolError(f"{where} keys must be strings of 1 to {MAX_ID} characters")
            out[_ID_CONTROL.sub(" ", key)] = _plain(v, where, depth + 1)
        return out
    raise ProtocolError(f"{where} holds a value of an unsupported type")


_HINTS: dict[type, dict[str, Any]] = {}


def _model_hints(cls: type) -> dict[str, Any]:
    if cls not in _HINTS:
        _HINTS[cls] = get_type_hints(cls, vars(model))
    return _HINTS[cls]


# --- reports ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdapterReport:
    """One adapter's part of a report: its snapshot, or the error it failed with."""

    name: str
    actions: frozenset[ActionKind]
    snapshot: Snapshot | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Report:
    agent_version: str
    sent_at: datetime
    interval: timedelta  # between the agent's reports
    adapters: tuple[AdapterReport, ...]
    # Order of reports, independent of the agent's clock: a random id per agent process and
    # a counter within it.
    boot: str = ""
    seq: int = 0


def encode_report(
    agent_version: str,
    sent_at: datetime,
    interval: timedelta,
    adapters: Iterable[AdapterReport],
    boot: str = "",
    seq: int = 0,
) -> dict[str, Any]:
    parts: list[dict[str, Any]] = []
    for a in adapters:
        part: dict[str, Any] = {"name": a.name, "actions": sorted(a.actions)}
        if a.snapshot is not None:
            part["snapshot"] = encode_snapshot(a.snapshot)
        if a.error is not None:
            part["error"] = _clip(a.error, MAX_REASON)
        parts.append(part)
    return {
        "version": VERSION,
        "agent_version": agent_version,
        "sent_at": sent_at.isoformat(),
        "interval": interval.total_seconds(),
        "boot": boot,
        "seq": seq,
        "adapters": parts,
    }


def decode_report(data: object) -> Report:
    body = _object(
        data,
        "report",
        {"version", "agent_version", "sent_at", "interval", "boot", "seq", "adapters"},
    )
    _check_version(body)
    version = body.get("agent_version")
    if not isinstance(version, str) or _VERSION.fullmatch(version) is None:
        raise ProtocolError("report: agent_version must be 1 to 64 printable characters")
    sent_at = _value(datetime, _datetime(body.get("sent_at"), "report: sent_at"), "sent_at", False)
    interval = body.get("interval")
    if isinstance(interval, bool) or not isinstance(interval, int | float):
        raise ProtocolError("report: interval must be a number of seconds")
    if not 1 <= interval <= 300:
        raise ProtocolError("report: interval must be 1 to 300 seconds")
    boot, seq = body.get("boot"), body.get("seq")
    if not isinstance(boot, str) or _BOOT.fullmatch(boot) is None:
        raise ProtocolError("report: boot must be 8 to 64 hex characters")
    if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq < 2**53:
        raise ProtocolError("report: seq must be a non-negative integer")
    raw = body.get("adapters")
    if not isinstance(raw, list) or not 1 <= len(raw) <= 16:
        raise ProtocolError("report: adapters must be a list of 1 to 16 entries")
    parts: list[AdapterReport] = []
    for item in raw:
        part = _object(item, "adapter", {"name", "actions", "snapshot", "error"})
        name = part.get("name")
        if not isinstance(name, str) or _ADAPTER_NAME.fullmatch(name) is None:
            raise ProtocolError("adapter: name must match [a-z0-9_-]{1,64}")
        if any(p.name == name for p in parts):
            raise ProtocolError(f"adapter {name}: reported twice")
        kinds = part.get("actions")
        if not isinstance(kinds, list) or not all(
            isinstance(k, str) and k in PLATFORM_ACTIONS for k in kinds
        ):
            raise ProtocolError(f"adapter {name}: actions must be platform action kinds")
        error = part.get("error")
        if error is not None and not isinstance(error, str):
            raise ProtocolError(f"adapter {name}: error must be a string")
        has_snapshot = part.get("snapshot") is not None
        if has_snapshot == (error is not None):
            raise ProtocolError(f"adapter {name}: exactly one of snapshot and error")
        snapshot = decode_snapshot(part["snapshot"]) if has_snapshot else None
        parts.append(
            AdapterReport(
                name,
                frozenset(ActionKind(k) for k in kinds),
                snapshot,
                None if error is None else _text(error, MAX_REASON),
            )
        )
    return Report(version, sent_at, timedelta(seconds=interval), tuple(parts), boot, seq)


# --- actions ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WireAction:
    """An action for one adapter of an agent, with ids in the agent's own terms."""

    id: str
    adapter: str
    kind: ActionKind
    rule: str
    reason: str
    task_id: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    ttl: float = MAX_TTL  # seconds left, on receipt, to start it; later it is not run

    def action(self) -> Action:
        """The action the agent's executor carries out."""
        return Action(self.kind, self.rule, self.reason, task_id=self.task_id, params=self.params)


def wire_params(kind: ActionKind, params: Mapping[str, Any]) -> dict[str, Any]:
    """The params of ``kind`` that may cross the wire, checked; others are dropped."""
    allowed = _PARAMS.get(kind, {})
    out: dict[str, Any] = {}
    for key, tp in allowed.items():
        value = params.get(key)
        if value is None:
            continue
        if not isinstance(value, tp) or (tp is not bool and isinstance(value, bool)):
            raise ProtocolError(f"{kind}: {key} must be a {tp.__name__}")
        if key == "conditions":
            out[key] = _conditions(value)
        elif key == "branch":
            if not _branch(value):
                raise ProtocolError(f"{kind}: branch must be a git branch name")
            out[key] = value
        elif isinstance(value, str):
            out[key] = _id(value, f"{kind}: {key}")
        else:
            out[key] = value
    if kind in _NEEDS_SESSION and "session_id" not in out:
        raise ProtocolError(f"{kind}: session_id is required")
    if kind is ActionKind.REASSIGN and "owner_id" not in out:
        raise ProtocolError("reassign: owner_id is required")
    if kind is ActionKind.WAIT and not out.get("conditions"):
        raise ProtocolError("wait: conditions are required")
    return out


def encode_action(action: WireAction) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": action.id,
        "adapter": action.adapter,
        "kind": str(action.kind),
        "rule": action.rule,
        "reason": action.reason,
        "ttl": round(action.ttl, 3),
    }
    if action.task_id is not None:
        out["task_id"] = action.task_id
    if action.params:
        out["params"] = action.params
    return out


def decode_action(data: object, offered: Mapping[str, frozenset[ActionKind]]) -> WireAction:
    """An action the hub sent. ``offered`` maps each of the agent's adapters to the kinds it
    carries out: anything else is refused, whatever the hub asks."""
    keys = {"id", "adapter", "kind", "rule", "reason", "task_id", "params", "ttl"}
    body = _object(data, "action", keys)
    action_id = _id(body.get("id"), "action: id")
    adapter = body.get("adapter")
    if not isinstance(adapter, str) or adapter not in offered:
        raise ProtocolError("action: not an adapter of this agent")
    kind = body.get("kind")
    if (
        not isinstance(kind, str)
        or kind not in PLATFORM_ACTIONS
        or ActionKind(kind) not in offered[adapter]
    ):
        raise ProtocolError(f"action: {adapter} does not carry out {_clip(str(kind), 32)!r}")
    kind = ActionKind(kind)
    rule = body.get("rule")
    if not isinstance(rule, str) or not rule or len(rule) > MAX_RULE:
        raise ProtocolError("action: rule must be a short string")
    reason = body.get("reason")
    if not isinstance(reason, str):
        raise ProtocolError("action: reason must be a string")
    task_id = body.get("task_id")
    if task_id is not None:
        task_id = _id(task_id, "action: task_id")
    elif kind not in _NEEDS_SESSION:
        raise ProtocolError(f"action: {kind} needs a task_id")
    params = body.get("params", {})
    if not isinstance(params, dict):
        raise ProtocolError("action: params must be an object")
    ttl = body.get("ttl")
    if isinstance(ttl, bool) or not isinstance(ttl, int | float) or not 0 < ttl <= MAX_TTL:
        raise ProtocolError(f"action: ttl must be more than 0 and at most {MAX_TTL:g} seconds")
    return WireAction(
        action_id,
        adapter,
        kind,
        _text(rule, MAX_RULE),
        _text(reason, MAX_REASON),
        task_id,
        wire_params(kind, params),
        float(ttl),
    )


def _branch(name: str) -> bool:
    """A branch name ``git check-ref-format --branch`` accepts, narrowed to plain names:
    components of letters, digits, ``._-``, not starting with ``.`` or ``-``, no ``..``,
    none ending in ``.lock`` or ``.``."""
    parts = name.split("/")
    return (
        0 < len(name) <= 200
        and ".." not in name
        and all(
            _BRANCH_PART.fullmatch(p) is not None and not p.endswith((".lock", ".")) for p in parts
        )
    )


def _conditions(raw: list[Any]) -> list[dict[str, Any]]:
    if len(raw) > MAX_CONDITIONS:
        raise ProtocolError(f"wait: at most {MAX_CONDITIONS} conditions")
    out: list[dict[str, Any]] = []
    for item in raw:
        cond = _object(item, "wait: condition", {"id", "kind", "params"})
        kind = cond.get("kind")
        if kind not in ConditionKind.__members__.values():
            raise ProtocolError("wait: condition kind is not known")
        params = cond.get("params", {})
        if not isinstance(params, dict) or len(params) > 16:
            raise ProtocolError("wait: condition params must be an object of at most 16 keys")
        clean: dict[str, Any] = {}
        for key, value in params.items():
            if not isinstance(key, str) or not key or len(key) > 64 or _ID_CONTROL.search(key):
                raise ProtocolError("wait: condition param names must be short strings")
            if key == "task" and kind in (ConditionKind.TASK_STATUS, ConditionKind.HUMAN_TASK):
                value = _id(value, "wait: task_status task")
            elif isinstance(value, str):
                value = _text(value, 1024)
            elif isinstance(value, float) and not math.isfinite(value):
                raise ProtocolError("wait: condition params must be finite numbers")
            elif value is not None and not isinstance(value, bool | int | float):
                raise ProtocolError("wait: condition params must be scalars")
            clean[key] = value
        out.append({"id": _id(cond.get("id"), "wait: condition id"), "kind": kind, "params": clean})
    return out


# --- results ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Result:
    id: str
    outcome: Outcome
    detail: str | None = None


def encode_result(result: Result) -> dict[str, Any]:
    out: dict[str, Any] = {"id": result.id, "outcome": str(result.outcome)}
    if result.detail:
        out["detail"] = _clip(result.detail, MAX_REASON)
    return out


def decode_result(data: object) -> Result:
    body = _object(data, "result", {"id", "outcome", "detail"})
    outcome = body.get("outcome")
    if outcome not in Outcome.__members__.values():
        raise ProtocolError("result: outcome is not known")
    detail = body.get("detail")
    if detail is not None and not isinstance(detail, str):
        raise ProtocolError("result: detail must be a string")
    return Result(
        _id(body.get("id"), "result: id"),
        Outcome(outcome),
        None if detail is None else _text(detail, MAX_REASON),
    )


def encode_poll(results: Iterable[Result], wait: float) -> dict[str, Any]:
    return {"version": VERSION, "wait": wait, "results": [encode_result(r) for r in results]}


def decode_poll(data: object) -> tuple[list[Result], float]:
    """Results the agent sends with a poll, and how long it is willing to wait."""
    body = _object(data, "poll", {"version", "wait", "results"})
    _check_version(body)
    wait = body.get("wait", 0)
    if isinstance(wait, bool) or not isinstance(wait, int | float) or not 0 <= wait <= MAX_WAIT:
        raise ProtocolError(f"poll: wait must be 0 to {MAX_WAIT:g} seconds")
    raw = body.get("results", [])
    if not isinstance(raw, list) or len(raw) > MAX_RESULTS:
        raise ProtocolError(f"poll: results must be a list of at most {MAX_RESULTS}")
    return [decode_result(r) for r in raw], float(wait)


def decode_actions(data: object, offered: Mapping[str, frozenset[ActionKind]]) -> list[object]:
    """The raw actions of a poll reply; each is decoded on its own with :func:`decode_action`
    (one bad action must not hide the others' ids)."""
    body = _object(data, "poll reply", {"actions"})
    raw = body.get("actions", [])
    if not isinstance(raw, list) or len(raw) > MAX_ACTIONS:
        raise ProtocolError(f"poll reply: actions must be a list of at most {MAX_ACTIONS}")
    return raw


# --- helpers ------------------------------------------------------------------------------


def _object(data: object, what: str, keys: set[str]) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ProtocolError(f"{what} must be an object")
    if extra := set(data) - keys:
        raise ProtocolError(f"{what}: unknown fields {sorted(str(k)[:32] for k in extra)}")
    return data


def _check_version(body: Mapping[str, Any]) -> None:
    if body.get("version") != VERSION:
        raise ProtocolError(
            f"protocol version {_clip(str(body.get('version')), 16)} is not {VERSION}: run the "
            "same Kelpie release on the agent and the server"
        )


def _datetime(value: object, what: str) -> datetime:
    if not isinstance(value, str):
        raise ProtocolError(f"{what} must be an ISO 8601 string")
    try:
        return parse_datetime(value)
    except ValueError:
        raise ProtocolError(f"{what} must be a timezone-aware ISO 8601 time") from None


def _id(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_ID or _ID_CONTROL.search(value):
        raise ProtocolError(f"{what} must be a string of 1 to {MAX_ID} printable characters")
    return value


def _text(value: str, limit: int) -> str:
    return _clip(_CONTROL.sub(" ", value), limit)


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --- namespaces ---------------------------------------------------------------------------

# Span attributes that carry an id other spans are joined on (hook and OTLP tool calls).
_SPAN_ID_ATTRIBUTES = ("gen_ai.tool.call.id",)


def namespaced(agent: str, value: str) -> str:
    """``<agent>:<value>``: an id of the agent's board in the merged farm."""
    return f"{agent}:{value}"


def local(agent: str, value: str) -> str | None:
    """The agent's own id behind ``value``, or None when ``value`` is not in its namespace."""
    prefix = f"{agent}:"
    return value[len(prefix) :] if value.startswith(prefix) and len(value) > len(prefix) else None


def condition_id(agent: str, value: str) -> str:
    """A condition id in the merged farm: ``<value>@<agent>``. A suffix, not a prefix, so the
    prefixes rules give the conditions they create (``dispatch_gate:<task>:``) survive."""
    return f"{value}@{agent}"


def local_condition_id(agent: str, value: str) -> str:
    """The id to hand the agent: without this agent's suffix (a condition it reported), or
    as is (one a rule made up)."""
    suffix = f"@{agent}"
    return value[: -len(suffix)] if value.endswith(suffix) and len(value) > len(suffix) else value


def namespace_snapshot(agent: str, adapter: str, part: Snapshot) -> Snapshot:
    """Every id of an agent's board in the agent's namespace (docs/multi-host.md):
    tasks, owners (people too), dependencies, conditions, sessions and spans. ``source``
    becomes the remote executor ``<agent>/<adapter>`` and ``Task.host`` the agent."""
    ns = partial(namespaced, agent)
    source = f"{agent}/{adapter}"

    def opt(value: str | None) -> str | None:
        return None if value is None else ns(value)

    tasks = tuple(
        dataclasses.replace(
            t,
            id=ns(t.id),
            owner_id=opt(t.owner_id),
            assignee=opt(t.assignee),
            subtasks=tuple(ns(s) for s in t.subtasks),
            # Files without a repository still belong to this machine's checkout.
            repo=opt(t.repo) if t.repo is not None or not t.files else f"{agent}:",
            source=source,
            host=agent,
            block=None
            if t.block is None
            else dataclasses.replace(
                t.block,
                condition_id=None
                if t.block.condition_id is None
                else condition_id(agent, t.block.condition_id),
            ),
        )
        for t in part.tasks
    )
    conditions = tuple(
        dataclasses.replace(
            c,
            id=condition_id(agent, c.id),
            task_id=opt(c.task_id),
            params=_condition_params(c.kind, c.params, ns),
        )
        for c in part.conditions
    )
    return Snapshot(
        now=part.now,
        tasks=tasks,
        owners=tuple(dataclasses.replace(o, id=ns(o.id), host=agent) for o in part.owners),
        dependencies=tuple(Dependency(ns(d.task_id), ns(d.depends_on)) for d in part.dependencies),
        conditions=conditions,
        sessions=tuple(
            dataclasses.replace(
                s,
                id=ns(s.id),
                task_id=ns(s.task_id),
                owner_id=ns(s.owner_id),
                source=source,
            )
            for s in part.sessions
        ),
        spans=tuple(namespace_span(agent, s) for s in part.spans),
    )


def namespace_span(agent: str, span: Span) -> Span:
    """A span in the agent's namespace: its trace and span ids too, so links are only
    inherited, and hook and OTLP tool calls only merged, within one host."""
    ns = partial(namespaced, agent)
    attributes = dict(span.attributes)
    for key in _SPAN_ID_ATTRIBUTES:
        if isinstance(attributes.get(key), str) and attributes[key]:
            attributes[key] = ns(attributes[key])
    return dataclasses.replace(
        span,
        trace_id=ns(span.trace_id),
        span_id=ns(span.span_id),
        parent_id=None if span.parent_id is None else ns(span.parent_id),
        task_id=None if span.task_id is None else ns(span.task_id),
        session_id=None if span.session_id is None else ns(span.session_id),
        attributes=attributes,
    )


# Condition kinds whose ``task`` param names a task of the same board.
TASK_REF_KINDS = frozenset({ConditionKind.TASK_STATUS, ConditionKind.HUMAN_TASK})


def _condition_params(
    kind: ConditionKind, params: dict[str, Any], ns: Callable[[str], str]
) -> dict[str, Any]:
    if kind in TASK_REF_KINDS and isinstance(params.get("task"), str):
        return {**params, "task": ns(params["task"])}
    return params
