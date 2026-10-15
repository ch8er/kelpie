"""Rule 7, provider part: LLM quotas, limit hits and credentials."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.core.actions import Action, Severity
from kelpie.core.model import (
    LimitHit,
    Owner,
    OwnerKind,
    ProviderStatus,
    Resource,
    ResourceKind,
    Snapshot,
    Status,
    Task,
)
from kelpie.rules.resources import Resources, build

NOW = datetime(2026, 3, 2, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=3)


def provider(name: str = "main", **kw: Any) -> ProviderStatus:
    kw.setdefault("ok", True)
    kw.setdefault("auth_ok", True)
    kw.setdefault("checked_at", NOW - timedelta(minutes=1))
    return ProviderStatus(name, **kw)


def task(id_: str, status: Status = Status.IN_PROGRESS, **kw: Any) -> Task:
    return Task(id_, id_, status, NOW - timedelta(hours=1), **kw)


TASKS = (
    task("T-1", role="coder", owner_id="agent-1"),
    task("T-2", Status.READY, role="writer"),
    task("T-3", role="coder", owner_id="human-1"),
    task("T-4", Status.DONE, role="coder"),
    task("T-5", Status.BLOCKED, role="coder"),
    task("T-6", owner_id="agent-2"),
)
OWNERS = (
    Owner("agent-1", "coder"),
    Owner("agent-2", "reviewer"),
    Owner("human-1", "coder", OwnerKind.HUMAN),
)


def run(rule: Resources | None = None, **snapshot: Any) -> list[Action]:
    snapshot.setdefault("tasks", TASKS)
    snapshot.setdefault("owners", OWNERS)
    return list((rule or build({})).evaluate(Snapshot(now=NOW, **snapshot)))


def one(actions: list[Action]) -> Action:
    assert len(actions) == 1, actions
    return actions[0]


def test_defaults() -> None:
    rule = build({})
    assert rule.quota_warning == 0.1
    assert rule.rate_limit_events_warning == 20
    assert rule.auth_expiry_warning == timedelta(hours=72)
    assert rule.provider_roles == {}


# --- quotas -----------------------------------------------------------------------------


@pytest.mark.parametrize(("remaining", "alert"), [(11, False), (10, True), (1, True)])
def test_low_quota_is_a_warning(remaining: float, alert: bool) -> None:
    actions = run(providers=(provider(remaining=remaining, limit=100, resets_at=LATER),))
    if not alert:
        assert actions == []
        return
    a = one(actions)
    assert a.severity is Severity.WARNING
    assert f"has {remaining}% of its quota left" in a.reason
    assert a.state == a.key == "resources:llm_quota:main"
    assert a.params["exhausted"] is False and a.params["blocked_tasks"] == []


def test_the_key_does_not_change_with_the_reading() -> None:
    a = one(run(providers=(provider(remaining=5, limit=100),)))
    b = one(run(providers=(provider(remaining=0.5, limit=100),)))
    assert (a.key, a.state) == (b.key, b.state)
    assert "(0.50 of 100)" in b.reason


def test_exhausted_single_provider_blocks_every_active_agent_task() -> None:
    a = one(run(providers=(provider(remaining=0, limit=100, resets_at=LATER),)))
    assert a.severity is Severity.CRITICAL
    assert a.params["blocked_tasks"] == ["T-1", "T-2", "T-6"]  # not human, done or blocked
    assert "no quota left until 2026-03-02T15:00:00+00:00 (in 3h)" in a.reason
    assert "3 agent task(s)" in a.reason


def test_exhausted_without_active_tasks_is_a_warning() -> None:
    a = one(run(providers=(provider(remaining=0),), tasks=()))
    assert a.severity is Severity.WARNING and a.params["exhausted"] is True


def test_with_several_providers_only_mapped_tasks_are_blocked() -> None:
    providers = (provider("main", remaining=0), provider("other", remaining=50, limit=100))
    a = one(run(providers=providers))
    assert a.severity is Severity.WARNING and a.params["blocked_tasks"] == []
    rule = build({"provider_roles": {"main": "writer"}, "provider_owners": {"main": ["agent-2"]}})
    a = one(run(rule, providers=providers))
    assert a.severity is Severity.CRITICAL
    assert a.params["blocked_tasks"] == ["T-2", "T-6"]


def test_mapping_by_owner_role() -> None:
    rule = build({"provider_roles": {"main": ["reviewer"]}})
    a = one(run(rule, providers=(provider(remaining=0),)))
    assert a.params["blocked_tasks"] == ["T-6"]


def test_mapping_of_another_provider_turns_off_the_single_provider_default() -> None:
    rule = build({"provider_roles": {"other": ["coder"]}})
    a = one(run(rule, providers=(provider(remaining=0),)))
    assert a.params["blocked_tasks"] == []


@pytest.mark.parametrize(
    "resets_at",
    [NOW + timedelta(minutes=10), NOW + timedelta(seconds=30), NOW - timedelta(minutes=1)],
)
def test_short_or_past_windows_are_not_reported(resets_at: datetime) -> None:
    assert run(providers=(provider(remaining=0, limit=100, resets_at=resets_at),)) == []


@pytest.mark.parametrize(
    "status",
    [
        provider(ok=False, auth_ok=None, error_kind="timeout"),
        provider(remaining=0, checked_at=NOW - timedelta(minutes=11)),  # stale
        provider(remaining=None, limit=100),
        provider(remaining=5, limit=0),
        provider(remaining=5, limit=None),
    ],
)
def test_unknown_or_stale_quotas_raise_nothing(status: ProviderStatus) -> None:
    assert run(providers=(status,)) == []


def test_quota_resources_from_other_sources() -> None:
    quota = Resource("llm:x", ResourceKind.LLM_QUOTA, used=95, limit=100, unit="tokens", at=NOW)
    older = Resource("llm:x", ResourceKind.LLM_QUOTA, used=10, limit=100, at=NOW - timedelta(1))
    stale = Resource("llm:y", ResourceKind.LLM_QUOTA, used=100, limit=100, at=NOW - timedelta(1))
    a = one(run(resources=(older, quota, stale)))
    assert "llm:x has 5% of its quota left (5 of 100 tokens)" in a.reason


def test_a_probe_newer_than_a_resource_wins() -> None:
    quota = Resource("main", ResourceKind.LLM_QUOTA, used=99, limit=100, events=3, at=NOW)
    old = provider(remaining=50, limit=100, checked_at=NOW - timedelta(minutes=2))
    new = provider(remaining=50, limit=100, checked_at=NOW + timedelta(seconds=1))
    assert one(run(resources=(quota,), providers=(old,))).params["remaining"] == 1
    assert run(resources=(quota,), providers=(new,)) == []


def test_quota_warning_can_be_switched_off() -> None:
    rule = build({"quota_warning": False})
    assert run(rule, providers=(provider(remaining=1, limit=100),)) == []


# --- limit hits -------------------------------------------------------------------------


def hits(n: int, name: str = "main", start: datetime = NOW) -> tuple[LimitHit, ...]:
    return tuple(LimitHit(name, start - timedelta(minutes=10 * i)) for i in range(n))


def test_repeated_limit_hits_are_one_warning_with_a_count() -> None:
    rule = build({"rate_limit_events_warning": 3})
    a = one(run(rule, limit_hits=hits(3), providers=(provider(remaining=80, limit=100),)))
    assert a.severity is Severity.WARNING
    assert "main hit its rate limit 3 times in the last 24h" in a.reason
    assert a.params["limit_hits"] == 3
    assert run(rule, limit_hits=hits(2)) == []


def test_hits_outside_the_window_or_in_the_future_do_not_count() -> None:
    rule = build({"rate_limit_events_warning": 2, "rate_limit_window": "1h"})
    old = (LimitHit("main", NOW - timedelta(hours=2)), LimitHit("main", NOW + timedelta(1)))
    assert run(rule, limit_hits=(*hits(1), *old)) == []
    a = one(run(rule, limit_hits=hits(2)))
    assert "in the last 1h" in a.reason


def test_hits_add_to_resource_events_and_can_be_switched_off() -> None:
    quota = Resource("main", ResourceKind.LLM_QUOTA, used=1, limit=100, events=19, at=NOW)
    a = one(run(resources=(quota,), limit_hits=hits(1)))
    assert a.params["limit_hits"] == 20
    assert run(build({"rate_limit_events_warning": False}), resources=(quota,)) == []


# --- credentials ------------------------------------------------------------------------


def test_a_rejected_key_is_critical_with_the_affected_tasks() -> None:
    status = provider(ok=False, auth_ok=False, error_kind="auth")
    a = one(run(providers=(status,)))
    assert a.severity is Severity.CRITICAL
    assert "rejected its key" in a.reason
    assert a.state == a.key == "resources:auth:main"
    assert a.params["blocked_tasks"] == ["T-1", "T-2", "T-6"]
    assert "Affected: T-1, T-2, T-6." in a.reason
    assert run(providers=(provider(),)) == []  # accepted again: the state ends


def test_a_stale_rejection_is_ignored() -> None:
    status = provider(ok=False, auth_ok=False, checked_at=NOW - timedelta(hours=1))
    assert run(providers=(status,)) == []


@pytest.mark.parametrize(
    ("expires_at", "severity", "text"),
    [
        (NOW - timedelta(minutes=5), Severity.CRITICAL, "expired 5m ago"),
        (NOW, Severity.CRITICAL, "expired 0s ago"),
        (NOW + timedelta(hours=5, minutes=30), Severity.WARNING, "expires in 5h30m"),
        (NOW + timedelta(hours=72), Severity.WARNING, "expires in 3d"),
        (NOW + timedelta(hours=72, seconds=1), None, ""),
    ],
)
def test_credential_expiry(expires_at: datetime, severity: Severity | None, text: str) -> None:
    status = provider(expires_at=expires_at)
    auth = Resource("auth:cli", ResourceKind.AUTH, host="host-a", expires_at=expires_at)
    for snapshot in ({"providers": (status,)}, {"resources": (auth,)}):
        actions = run(**snapshot)
        if severity is None:
            assert actions == []
            continue
        a = one(actions)
        assert a.severity is severity and text in a.reason


def test_an_expired_credential_lists_tasks_only_when_mapped() -> None:
    auth = Resource("auth:cli", ResourceKind.AUTH, host="host-a", expires_at=NOW)
    assert one(run(resources=(auth,))).params["blocked_tasks"] == []
    rule = build({"provider_roles": {"auth:cli": ["coder"]}})
    a = one(run(rule, resources=(auth,)))
    assert a.params["blocked_tasks"] == ["T-1"]
    assert a.params["host"] == "host-a"
    assert "on host-a" in a.reason


def test_an_expiring_credential_lists_no_tasks() -> None:
    status = provider(expires_at=NOW + timedelta(hours=1))
    assert one(run(providers=(status,))).params["blocked_tasks"] == []


def test_short_durations_read_well() -> None:
    a = one(run(providers=(provider(expires_at=NOW + timedelta(minutes=59, seconds=59)),)))
    assert "expires in 59m" in a.reason
    a = one(run(providers=(provider(expires_at=NOW + timedelta(hours=2)),)))
    assert "expires in 2h " in a.reason


# --- configuration ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"quota_warning": 1.5}, "between 0 and 1"),
        ({"quota_warning": 0}, "positive"),
        ({"rate_limit_events_warning": 0}, "positive integer"),
        ({"rate_limit_events_warning": True}, "positive integer"),
        ({"rate_limit_window": "0s"}, "positive"),
        ({"auth_expiry_warning": "soon"}, "auth_expiry_warning"),
        ({"ignore_resets_within": []}, "invalid duration"),
        ({"provider_roles": ["coder"]}, "map provider names"),
        ({"provider_roles": {"main": 3}}, "list of strings"),
        ({"provider_owners": {"": ["a"]}}, "map provider names"),
    ],
)
def test_invalid_provider_config(config: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        build(config)


def test_provider_config_is_read() -> None:
    rule = build(
        {
            "quota_warning": 0.25,
            "rate_limit_events_warning": 5,
            "rate_limit_window": "6h",
            "auth_expiry_warning": "24h",
            "ignore_resets_within": "1m",
            "provider_roles": {"main": "coder"},
            "provider_owners": {"main": ["agent-1"]},
        }
    )
    assert rule.quota_warning == 0.25
    assert rule.rate_limit_events_warning == 5
    assert rule.rate_limit_window == timedelta(hours=6)
    assert rule.ignore_resets_within == timedelta(minutes=1)
    assert rule.provider_roles == {"main": ("coder",)}
    assert rule.provider_owners == {"main": ("agent-1",)}
