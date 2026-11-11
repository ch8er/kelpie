"""``quota_reset`` conditions evaluated from the LLM provider probes (``Snapshot.providers``)
or a reset time the condition carries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.conditions import check_quota_reset
from kelpie.core.model import Condition, ConditionKind, ProviderStatus, Snapshot, Status, Task

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
H = timedelta(hours=1)


def board(*conditions: Condition, providers: tuple[ProviderStatus, ...] = ()) -> Snapshot:
    tasks = (Task("T-1", "t", Status.WAITING, NOW - H),)
    return Snapshot(NOW, tasks=tasks, conditions=conditions, providers=providers)


def quota(**params: object) -> Condition:
    return Condition("Q", ConditionKind.QUOTA_RESET, "T-1", params=dict(params))


def probe(remaining: float | None, **kw: object) -> ProviderStatus:
    fields: dict[str, object] = {"name": "Example-LLM", "remaining": remaining, "limit": 100.0,
                                 "checked_at": NOW - timedelta(minutes=2),
                                 "resets_at": NOW + H}  # fmt: skip
    fields.update(kw)
    return ProviderStatus(**fields)  # type: ignore[arg-type]


def checked(c: Condition, *providers: ProviderStatus) -> Condition:
    (out,) = check_quota_reset(board(c, providers=providers)).conditions
    return out


def test_no_quota_conditions_leave_the_snapshot_as_is() -> None:
    snapshot = board(Condition("X", ConditionKind.TASK_STATUS, "T-1"))
    assert check_quota_reset(snapshot) is snapshot


def test_it_holds_once_the_probe_reads_quota_left() -> None:
    c = checked(quota(provider="example-llm"), probe(40.0))
    assert c.satisfied is True and c.satisfied_at == NOW - timedelta(minutes=2)
    assert c.params["observed_remaining"] == 40.0
    again = checked(c, probe(39.0, checked_at=NOW))
    assert again.satisfied_at == c.satisfied_at  # stays the time it first held


def test_it_does_not_hold_while_none_is_left() -> None:
    for p in (probe(0.0), probe(0.0, ok=False, error_kind="rate_limited")):
        c = checked(quota(provider="example-llm"), p)
        assert c.satisfied is False and c.satisfied_at is None
        assert c.params["observed_resets_at"] == (NOW + H).isoformat()


@pytest.mark.parametrize(
    ("p", "error"),
    [
        (probe(None, ok=False, error_kind="timeout"), "probe_failed"),
        (probe(10.0, auth_ok=False), "probe_failed"),
        (probe(None), "no_reading"),
    ],
)
def test_a_probe_without_a_reading_is_unknown(p: ProviderStatus, error: str) -> None:
    c = checked(quota(provider="example-llm"), p)
    assert c.satisfied is None and c.params["error"] == error


def test_without_a_probe_a_reset_time_decides() -> None:
    later = quota(provider="elsewhere", resets_at=(NOW + H).isoformat())
    assert checked(later).satisfied is False
    past = quota(resets_at=(NOW - H).isoformat())
    c = checked(past)
    assert c.satisfied is True and c.satisfied_at == NOW - H
    assert checked(quota(provider="elsewhere")).params["error"] == "not_found"


@pytest.mark.parametrize(
    "params",
    [{}, {"provider": ""}, {"provider": 3}, {"resets_at": "tomorrow"},
     {"resets_at": "2026-03-02T13:00:00"}],
)  # fmt: skip
def test_bad_params_are_unknown(params: dict[str, object]) -> None:
    c = checked(quota(**params), probe(40.0))
    assert c.satisfied is None and c.params["error"] == "invalid_params"


def test_old_results_are_cleared_before_a_check() -> None:
    first = checked(quota(provider="example-llm"), probe(None))
    assert first.params["error"] == "no_reading"
    second = checked(first, probe(5.0))
    assert "error" not in second.params and second.satisfied is True


def test_the_snapshot_checks_quota_conditions_after_the_probes() -> None:
    from kelpie.app import merged_snapshot
    from kelpie.core.model import LimitHit
    from kelpie.metrics import Metrics
    from tests.fakes import FakeAdapter

    class Parked(FakeAdapter):
        def snapshot(self, now: datetime) -> Snapshot:
            return board(quota(provider="example-llm"))

    class Probes:
        def read(self, now: datetime) -> tuple[list[ProviderStatus], list[LimitHit]]:
            return [probe(12.0)], []

    snapshot = merged_snapshot([Parked()], Metrics(), lambda: NOW, providers=Probes())()
    (c,) = snapshot.conditions
    assert c.satisfied is True and c.params["observed_remaining"] == 12.0
